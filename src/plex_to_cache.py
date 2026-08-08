#!/usr/bin/python3
"""
Plex to Cache - Automatic media caching daemon for Unraid
Moves actively streamed media from array to cache for faster playback.
Supports Plex, Emby, and Jellyfin.

No external Python dependencies — uses only the standard library
(urllib/ssl/json/threading). This keeps the plugin offline-bootable:
Unraid's root filesystem is a tmpfs, so anything in /usr/local/lib/pythonX/
site-packages gets wiped on reboot. Relying on `requests` means the
daemon would need internet on every boot to reinstall it.

Architecture:
  - Main thread: polls media server APIs, decides what should be cached,
    runs cleanup. Never blocks on file transfers.
  - Copy worker thread: processes the copy queue one file at a time, so a
    multi-gigabyte rsync never stalls stream detection.
"""

import os
import sys
import re
import time
import json
import ssl
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import shutil
import signal
import queue
import threading
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG_FILE   = "/boot/config/plugins/plex_to_cache/settings.cfg"
TRACKED_FILES = "/boot/config/plugins/plex_to_cache/cached_files.list"
LOCK_FILE     = "/tmp/media_cache_cleaner.lock"
LOG_FILE      = "/var/log/plex_to_cache.log"
LOG_MAX_BYTES = 5 * 1024 * 1024   # rotate when the log reaches 5 MB
ARRAY_ROOT    = "/mnt/user0"      # physical array path (no cache) — used for permission cloning

RSYNC_RETRIES        = 3          # attempts per file before giving up
RSYNC_RETRY_DELAY    = 5          # seconds between attempts
COPY_FAIL_COOLDOWN   = 300        # seconds before re-trying a file that failed all attempts
METADATA_CACHE_LIMIT = 500        # max entries kept in the Plex ratingKey→path cache

STATUS_FILE          = "/var/run/plex_to_cache.status.json"  # snapshot for the web UI
FLUSH_REQUEST        = "/var/run/plex_to_cache.flush"        # web UI asks for a full flush
STATUS_INTERVAL      = 30         # seconds between status snapshots
EVICT_MAX_FILES      = 25         # max files moved back per eviction pass
MOVE_MIN_AGE         = 30 * 60    # a file written this recently is left where it is
WATCHED_MIN_PROGRESS = 0.90       # session progress at which media counts as watched

DEFAULT_CONFIG = {
    "ENABLE_PLEX": "False", "PLEX_URL": "http://localhost:32400", "PLEX_TOKEN": "",
    "ENABLE_EMBY": "False", "EMBY_URL": "http://localhost:8096", "EMBY_API_KEY": "",
    "ENABLE_JELLYFIN": "False", "JELLYFIN_URL": "http://localhost:8096", "JELLYFIN_API_KEY": "",
    "CHECK_INTERVAL": "10", "CACHE_MAX_USAGE": "80", "COPY_DELAY": "30",
    "CLEANUP_MODE": "none", "MOVIE_DELETE_DELAY": "1800", "EPISODE_KEEP_PREVIOUS": "2",
    "CACHE_MAX_DAYS": "7", "EXCLUDE_DIRS": "", "MEDIA_FILETYPES": ".mkv .mp4 .avi",
    "ARRAY_ROOT": "/mnt/user", "CACHE_ROOT": "/mnt/cache", "DOCKER_MAPPINGS": "",
    # Season batching: for very long seasons, cache episodes in batches
    # instead of the whole season at once.
    "ENABLE_EPISODE_BATCHING": "False",
    "EPISODE_BATCH_SIZE": "30",        # episodes per batch
    "EPISODE_BATCH_TOLERANCE": "10",   # if the leftover after a batch is <= this, merge it in
    "EPISODE_BATCH_PREFETCH": "4",     # start next batch when this many episodes remain in the current one
    # When the cache is full, move the oldest plugin-cached files back to
    # the array to make room for the currently streamed media.
    "ENABLE_CACHE_EVICTION": "True",
    # Near the end of a season, pre-cache the beginning of the next season.
    "ENABLE_NEXT_SEASON_PREFETCH": "False",
}

# Runtime state
config          = dict(DEFAULT_CONFIG)
docker_mappings = {}
metadata_cache  = {}
stream_timers   = {}
deletion_queue  = {}
failed_copies   = {}               # cache_path -> timestamp of last failed attempt
active_cache_paths = set()         # cache paths of currently streamed files (never evicted)

# Copy worker state
copy_queue      = queue.Queue()
_pending_copies = set()            # array paths queued or currently copying
_pending_lock   = threading.Lock()
_current_copy   = None             # basename of the file being copied right now
_current_rsync  = None             # running rsync Popen (for clean shutdown)
_rsync_lock     = threading.Lock()
_shutting_down  = threading.Event()

# Manual flush state, shown in the web UI while it runs
_flush_lock  = threading.Lock()
_flush_state = {"active": False, "total": 0, "done": 0, "bytes": 0,
                "skipped": 0, "conflicts": 0, "failed": 0, "finished": 0}

# SSL context: Plex / Emby / Jellyfin typically use self-signed certs on the
# local network, so we intentionally skip verification. This is equivalent
# to the old `verify=False` on the `requests` calls.
_SSL_CTX = ssl._create_unverified_context()

# =============================================================================
# UTILITIES
# =============================================================================

_logger = logging.getLogger("plex_to_cache")

def setup_logging():
    """Log to LOG_FILE with size-based rotation that also works while the
    daemon is running (the old approach only rotated at start, and the
    shell's stdout redirect kept writing to the renamed inode anyway).
    Falls back to stderr if the log file is not writable. Thread-safe."""
    try:
        handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=1)
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                           datefmt="%Y-%m-%d %H:%M:%S"))
    _logger.setLevel(logging.INFO)
    _logger.addHandler(handler)

def log(msg, error=False, warn=False):
    prefix = "[Error] " if error else ("[Warn] " if warn else "")
    _logger.info(f"{prefix}{msg}")

def load_config():
    global config, docker_mappings
    config = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            for line in Path(CONFIG_FILE).read_text().splitlines():
                if '=' in line and not line.strip().startswith('#'):
                    k, v = line.split('=', 1)
                    config[k.strip()] = v.strip().strip('"\'')
        except OSError as e:
            log(f"Config load failed: {e}", error=True)

    # Parse docker mappings (stored as docker_path:host_path;...)
    docker_mappings = {}
    for pair in config.get("DOCKER_MAPPINGS", "").split(';'):
        if ':' in pair:
            k, v = pair.split(':', 1)
            docker_mappings[k.strip()] = v.strip()

def cfg(key, as_int=False, as_bool=False):
    val = config.get(key, DEFAULT_CONFIG.get(key, ""))
    if as_bool:
        return str(val).lower() in ("true", "1", "yes")
    if as_int:
        try:
            return int(str(val).strip())
        except (ValueError, TypeError):
            # Fall back to the built-in default instead of a silent 0,
            # so a typo in settings.cfg can't create a busy-loop or
            # zero-delay behaviour.
            try:
                return int(DEFAULT_CONFIG.get(key, "0"))
            except (ValueError, TypeError):
                return 0
    return val

# =============================================================================
# FILE TRACKING
# =============================================================================

class TrackedFiles:
    """Manages the list of plugin-cached files with timestamps.
    Thread-safe: accessed by both the main loop (cleanup) and the
    copy worker (adding freshly cached files)."""

    _lock = threading.RLock()

    @staticmethod
    def load():
        """Load tracked files. Returns dict: {path: timestamp}"""
        with TrackedFiles._lock:
            tracked = {}
            if os.path.exists(TRACKED_FILES):
                try:
                    for line in Path(TRACKED_FILES).read_text().splitlines():
                        if '|' in line:
                            path, ts = line.rsplit('|', 1)
                            try:
                                tracked[path] = float(ts)
                            except ValueError:
                                continue
                        elif line.strip():
                            tracked[line.strip()] = time.time()
                except OSError as e:
                    log(f"Tracking load failed: {e}", error=True)
            return tracked

    @staticmethod
    def save(tracked):
        """Save tracked files dict to disk (atomic replace)."""
        with TrackedFiles._lock:
            try:
                content = '\n'.join(f"{p}|{t}" for p, t in sorted(tracked.items()))
                tmp = TRACKED_FILES + ".tmp"
                Path(tmp).write_text(content + '\n' if content else '')
                os.replace(tmp, TRACKED_FILES)
            except OSError as e:
                log(f"Tracking save failed: {e}", error=True)

    @staticmethod
    def add(path):
        with TrackedFiles._lock:
            tracked = TrackedFiles.load()
            if path not in tracked:
                tracked[path] = time.time()
                TrackedFiles.save(tracked)

    @staticmethod
    def remove(path):
        with TrackedFiles._lock:
            tracked = TrackedFiles.load()
            if path in tracked:
                del tracked[path]
                TrackedFiles.save(tracked)

    @staticmethod
    def clear():
        TrackedFiles.save({})

def reconcile_tracked_files():
    """Startup consistency check for the tracked-files list.

    1. Drops entries whose cache file no longer exists (e.g. removed
       manually or lost in a crash).
    2. Adopts orphaned plugin copies: media files inside the docker-mapped
       cache directories that also exist on the array (i.e. duplicates the
       plugin created but lost track of). Only mapped media directories are
       scanned — never the whole cache pool, so appdata/system shares and
       legitimately cache-only files (e.g. fresh downloads awaiting the
       mover) are left alone.
    """
    tracked = TrackedFiles.load()
    removed = 0
    for path in list(tracked):
        if not os.path.exists(path):
            del tracked[path]
            removed += 1

    adopted    = 0
    cache_root = cfg("CACHE_ROOT")
    for cache_dir in sorted(_protected_cache_dirs()):
        if cache_dir == cache_root or not os.path.isdir(cache_dir):
            continue
        for root, _dirs, files in os.walk(cache_dir):
            for f in files:
                cache_path = os.path.join(root, f)
                if cache_path in tracked or not is_media_file(f):
                    continue
                # A duplicate of an array file is one of ours
                if os.path.exists(cache_to_array(cache_path)):
                    try:
                        tracked[cache_path] = os.path.getmtime(cache_path)
                    except OSError:
                        tracked[cache_path] = time.time()
                    adopted += 1

    if removed or adopted:
        TrackedFiles.save(tracked)
        log(f"[Reconcile] Tracked list: {removed} stale entries removed, "
            f"{adopted} orphaned cache copies adopted")

# =============================================================================
# PATH UTILITIES
# =============================================================================

def _under(path, prefix):
    """True if path is prefix itself or lies below it.

    A plain startswith() also matches a sibling whose name merely begins the
    same way: /database would count as being under /data.
    """
    prefix = prefix.rstrip('/')
    return path == prefix or path.startswith(prefix + '/')

def _relative_to(path, prefix):
    """The part of path below prefix, or None if it is not below it."""
    prefix = prefix.rstrip('/')
    if path == prefix:
        return ''
    if path.startswith(prefix + '/'):
        return path[len(prefix) + 1:]
    return None

def physical_array_root():
    """The array-only view of the configured array root.

    ARRAY_ROOT is normally the user share /mnt/user, which includes the cache
    pool - writing a file back there could land it straight on cache again.
    /mnt/user0 is the same share with the cache excluded, so that is what a
    move back has to target. A root that is not a user share (an unassigned
    device, a second pool) has no such twin and is used unchanged.
    """
    root = cfg("ARRAY_ROOT").rstrip('/')
    if root == '/mnt/user':
        return ARRAY_ROOT
    if root.startswith('/mnt/user/'):
        return ARRAY_ROOT + root[len('/mnt/user'):]
    return root

def cache_to_array(cache_path):
    """Cache path -> the physical location of the original on the array."""
    rel = _relative_to(cache_path, cfg("CACHE_ROOT"))
    if rel is None:
        return cache_path
    return os.path.join(physical_array_root(), rel)

def array_to_cache(array_path):
    """Array path -> where its cached copy lives."""
    rel = _relative_to(array_path, cfg("ARRAY_ROOT"))
    if rel is None:
        return array_path
    return os.path.join(cfg("CACHE_ROOT").rstrip('/'), rel)

def array_share_to_cache(host_path):
    """Translate a user-share host path to its cache equivalent.
    Same as array_to_cache but keeps the result when already on cache."""
    if _under(host_path, cfg("CACHE_ROOT")):
        return host_path
    return array_to_cache(host_path)

def translate_docker_path(docker_path):
    """Translate docker container path to host path.

    Mappings are tried longest prefix first, so a specific /media/movies wins
    over a general /media no matter which order they were configured in.
    """
    path = docker_path.replace('\\', '/')
    for docker_prefix in sorted(docker_mappings, key=len, reverse=True):
        rel = _relative_to(path, docker_prefix)
        if rel is None:
            continue
        host_prefix = docker_mappings[docker_prefix]
        base = host_prefix if host_prefix.startswith('/') \
               else os.path.join(cfg("ARRAY_ROOT"), host_prefix)
        return os.path.join(base, rel)
    return path

def is_excluded(path):
    """Check if path contains any of the configured excluded folder names."""
    excludes = [x.strip() for x in cfg("EXCLUDE_DIRS").split(',') if x.strip()]
    return any(exc in path.split(os.sep) for exc in excludes)

def is_media_file(filename):
    """Check if file is a media file according to the configured extensions."""
    extensions = cfg("MEDIA_FILETYPES").split()
    return not extensions or any(filename.lower().endswith(ext.lower()) for ext in extensions)

# Matches "S01E05" style and "1x05" style episode numbering.
# The lookarounds on the 1x05 pattern prevent matching inside
# resolutions like "1920x1080".
_EP_PATTERNS = (
    re.compile(r"[sS]\d{1,4}[eE](\d{1,4})"),
    re.compile(r"(?<!\d)\d{1,2}x(\d{2,3})(?!\d)"),
)

def parse_episode(filename):
    """Extract episode number from filename. Returns None if not an episode."""
    for pattern in _EP_PATTERNS:
        match = pattern.search(filename)
        if match:
            return int(match.group(1))
    return None

# =============================================================================
# PERMISSIONS
# =============================================================================

def clone_permissions(dest_path):
    """Clone permissions from the array original (via /mnt/user0) to dest_path."""
    src = cache_to_array(dest_path) if dest_path.startswith(cfg("CACHE_ROOT")) else None
    if not src or not os.path.exists(src):
        return
    try:
        st = os.stat(src)
        os.chown(dest_path, st.st_uid, st.st_gid)
        os.chmod(dest_path, st.st_mode)
    except OSError as e:
        log(f"Permission clone failed: {e}", error=True)

# =============================================================================
# FILE OPERATIONS
# =============================================================================

def _rsync_timeout_for(src):
    """Generous size-based timeout so a hung rsync can't block the worker
    forever: 10 minutes base + 1 second per 10 MB (i.e. assumes a floor
    of ~10 MB/s throughput on top of the base)."""
    try:
        size = os.path.getsize(src)
    except OSError:
        size = 0
    return 600 + size // (10 * 1024 * 1024)

def _run_rsync(cmd, timeout):
    """Run rsync, tracking the process so shutdown can terminate it.
    Returns (returncode, stderr_text)."""
    global _current_rsync
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    with _rsync_lock:
        _current_rsync = proc
    try:
        _, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, (stderr or "")
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return -1, f"rsync timed out after {timeout}s"
    finally:
        with _rsync_lock:
            _current_rsync = None

def _sizes_match(src, dst):
    """True if dst exists and has the same size as src (or src is gone
    but dst exists — the source vanished after a completed transfer)."""
    try:
        if not os.path.exists(dst):
            return False
        if not os.path.exists(src):
            return True
        return os.path.getsize(src) == os.path.getsize(dst)
    except OSError:
        return False

def rsync_transfer(src, dst, remove_source=True):
    """Move (or copy) a file using rsync. Robust against transient errors:

    - Retries up to RSYNC_RETRIES times (with --partial --inplace a retry
      resumes instead of restarting).
    - rsync stderr is captured and logged so failures are diagnosable
      (previously it was thrown away).
    - Exit codes 23 (partial transfer / attribute errors) and 24 (source
      file vanished) are tolerated when the destination is verifiably
      complete (same size as source). Exit 23 is frequently caused by
      chown/chmod/utime problems on FUSE shares even though the file
      content transferred fine — the caller re-applies permissions via
      clone_permissions anyway.

    Raises subprocess.CalledProcessError if the transfer really failed.
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    # --partial-dir instead of --inplace: a retry still resumes, but the
    # partial data sits in a side directory. With --inplace the destination
    # carries its final name while still incomplete, and Unraid serves new
    # opens from cache - a stream starting mid-copy could read a truncated file.
    cmd = ["rsync", "-a", "--partial", "--partial-dir=.plex_to_cache-partial"]
    if remove_source:
        cmd.append("--remove-source-files")
    cmd.extend([src, dst])

    timeout  = _rsync_timeout_for(src)
    last_err = ""

    for attempt in range(1, RSYNC_RETRIES + 1):
        rc, stderr = _run_rsync(cmd, timeout)
        if rc == 0:
            return

        # Keep the last few stderr lines for the log
        err_lines = [l for l in stderr.strip().splitlines() if l.strip()]
        last_err  = "; ".join(err_lines[-3:]) if err_lines else f"exit status {rc}"

        # Partial transfer (23) / vanished source (24): accept if the data
        # actually arrived completely.
        if rc in (23, 24) and _sizes_match(src, dst):
            log(f"rsync finished with warnings (exit {rc}) but file is complete: "
                f"{os.path.basename(dst)} — {last_err}", warn=True)
            if remove_source and os.path.exists(src):
                try:
                    os.remove(src)
                except OSError as e:
                    log(f"Could not remove source after transfer: {e}", warn=True)
            return

        if _shutting_down.is_set():
            break

        if attempt < RSYNC_RETRIES:
            log(f"rsync attempt {attempt}/{RSYNC_RETRIES} failed (exit {rc}) for "
                f"{os.path.basename(src)}: {last_err} — retrying in {RSYNC_RETRY_DELAY}s",
                warn=True)
            time.sleep(RSYNC_RETRY_DELAY)

    raise subprocess.CalledProcessError(rc if rc != 0 else 1, "rsync", stderr=last_err)

# Backwards-compatible alias (old name used elsewhere / in docs)
rsync_move = rsync_transfer

def _protected_cache_dirs():
    """Dirs inside CACHE_ROOT that must never be rmdir'd by cleanup_empty_dirs.
    Derived from the docker mappings: every mapped host path, translated to
    its cache-side equivalent."""
    cache_root = cfg("CACHE_ROOT")
    array_root = cfg("ARRAY_ROOT")
    protected  = {cache_root}
    for host_path in docker_mappings.values():
        if host_path.startswith(array_root):
            protected.add(host_path.replace(array_root, cache_root, 1))
        elif host_path.startswith(cache_root):
            protected.add(host_path)
        else:
            # host_path given as a relative share name — prepend cache root
            protected.add(os.path.join(cache_root, host_path.lstrip('/')))
    return protected

def cleanup_empty_dirs(start_path):
    """Remove empty parent directories up to CACHE_ROOT, but stop at any
    directory that's protected by a docker mapping."""
    cache_root = cfg("CACHE_ROOT")
    protected  = _protected_cache_dirs()

    parent = os.path.dirname(start_path)
    while parent.startswith(cache_root) and len(parent) > len(cache_root):
        if parent in protected:
            break
        try:
            os.rmdir(parent)
            parent = os.path.dirname(parent)
        except OSError:
            break

def move_file_to_array(cache_path, track=True):
    """Move a single file from cache to array. Returns (success, was_deleted, size)."""
    if not os.path.exists(cache_path):
        if track:
            TrackedFiles.remove(cache_path)
        return True, False, 0

    try:
        size = os.path.getsize(cache_path)
        array_path = cache_to_array(cache_path)

        # If already on array, just delete cache copy
        if os.path.exists(array_path):
            os.remove(cache_path)
            cleanup_empty_dirs(cache_path)
            if track:
                TrackedFiles.remove(cache_path)
            return True, True, size

        # Move to array
        rsync_transfer(cache_path, array_path, remove_source=True)
        clone_permissions(array_path)
        cleanup_empty_dirs(cache_path)
        if track:
            TrackedFiles.remove(cache_path)
        return True, False, size

    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, 'stderr', '') or ''
        log(f"Move failed for {os.path.basename(cache_path)}: {e} {detail}".strip(), error=True)
        return False, False, 0

def cache_has_room_for(file_size):
    """Check both the configured max-usage percentage and the actual free
    bytes needed for this specific file (plus a small safety margin)."""
    try:
        usage = shutil.disk_usage(cfg("CACHE_ROOT"))
    except OSError as e:
        log(f"Cannot stat cache filesystem: {e}", error=True)
        return False
    if (usage.used / usage.total) * 100 >= cfg("CACHE_MAX_USAGE", as_int=True):
        return False
    margin = 512 * 1024 * 1024  # keep at least 512 MB headroom
    return usage.free > file_size + margin

def evict_oldest_cached(needed_size):
    """LRU eviction: when the cache is full, move the oldest plugin-cached
    files back to the array until needed_size fits (bounded by
    EVICT_MAX_FILES per pass). Files that belong to an active stream or
    are queued for copying are never evicted.

    Returns True if there is room for needed_size afterwards."""
    if not cfg("ENABLE_CACHE_EVICTION", as_bool=True):
        return False

    with _pending_lock:
        pending = {array_to_cache(p) for p in _pending_copies}
    protected = set(active_cache_paths) | pending

    tracked = TrackedFiles.load()
    evicted = 0
    for cache_path, _ts in sorted(tracked.items(), key=lambda kv: kv[1]):
        if cache_has_room_for(needed_size):
            return True
        if evicted >= EVICT_MAX_FILES:
            break
        if cache_path in protected:
            continue
        if not os.path.exists(cache_path):
            TrackedFiles.remove(cache_path)
            continue
        log(f"[Evict] {os.path.basename(cache_path)} (making room on cache)")
        ok, _, _ = move_file_to_array(cache_path)
        if ok:
            evicted += 1

    return cache_has_room_for(needed_size)

def _cache_media_files(min_age_seconds):
    """Every media file sitting in the mapped cache folders, whether this
    plugin put it there or not.

    Used by the "all" flush scope. Only the folders from the docker mappings
    are walked, so a media file that landed there without this plugin is
    included, while a separate downloads share, appdata, system and every
    other share on the same pool are left alone.

    Files modified within min_age_seconds are left alone: something still
    being written into the media folder - an import in progress, say - would
    otherwise be moved out from under the process writing it.
    """
    found = {}
    cache_root = cfg("CACHE_ROOT")
    now = time.time()

    for cache_dir in sorted(_protected_cache_dirs()):
        if cache_dir == cache_root or not os.path.isdir(cache_dir):
            continue
        for root, _dirs, files in os.walk(cache_dir):
            for name in files:
                path = os.path.join(root, name)
                if not is_media_file(name) or is_excluded(path):
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if now - st.st_mtime < min_age_seconds:
                    continue
                found[path] = st.st_mtime
    return found

def flush_cache_to_array(only=None, label="Flush"):
    """Move files this plugin cached back to the array.

    Same operation as eviction, but unconditional and not bounded by
    EVICT_MAX_FILES. With `only` set to a collection of cache paths, just those
    are moved - that is the per-file button in the UI. The restriction is
    applied by filtering the tracked list, so a path that is not tracked simply
    never matches and an untrusted request cannot reach an arbitrary file.

    Two kinds of file are deliberately left on the cache: those belonging to an
    active stream, and those queued for copying - moving either would pull the
    file out from under a running playback. They are counted as skipped so the
    UI can say so rather than silently doing less than "all".

    Only tracked files are touched. Anything else on the cache pool - a fresh
    download waiting for the mover, appdata, files the user put there on
    purpose - is none of this plugin's business.
    """
    with _flush_lock:
        if _flush_state["active"]:
            log(f"[{label}] Another flush is already running", warn=True)
            return
        _flush_state.update(active=True, total=0, done=0, bytes=0,
                            skipped=0, conflicts=0, failed=0, finished=0)

    try:
        with _pending_lock:
            pending = {array_to_cache(p) for p in _pending_copies}
        protected = set(active_cache_paths) | pending

        tracked_map = TrackedFiles.load()
        candidates = dict(tracked_map)
        # An explicit selection means "move this", so it covers media the plugin
        # never copied. Without one - the bare --flush - only what this plugin
        # put on the cache is touched.
        if only is not None:
            extra = _cache_media_files(MOVE_MIN_AGE)
            for path, ts in extra.items():
                candidates.setdefault(path, ts)

        entries = sorted(candidates.items(), key=lambda kv: kv[1])
        if only is not None:
            # An entry in `only` is either a file or a folder - the browser
            # offers a button on a whole season or series. The trailing
            # separator keeps /Media/Show2 from matching /Media/Show.
            wanted = set(only)
            prefixes = tuple(p.rstrip(os.sep) + os.sep for p in only)
            entries = [item for item in entries
                       if item[0] in wanted or item[0].startswith(prefixes)]
        with _flush_lock:
            _flush_state["total"] = len(entries)

        log(f"[{label}] Moving {len(entries)} cached file(s) back to the array")
        last_write = 0.0

        for cache_path, _ts in entries:
            if _shutting_down.is_set():
                log(f"[{label}] Aborted: service is shutting down", warn=True)
                break

            if cache_path in protected:
                with _flush_lock:
                    _flush_state["skipped"] += 1
                log(f"[{label}] Skipping {os.path.basename(cache_path)} (in use)")
                continue

            # A tracked file is a copy of the array original by construction, so
            # move_file_to_array may drop the cache side when both exist. An
            # untracked file is not: a same-named file on the array is a
            # different file, and deleting the cache copy would lose it.
            if cache_path not in tracked_map and os.path.exists(cache_to_array(cache_path)):
                with _flush_lock:
                    _flush_state["conflicts"] += 1
                log(f"[{label}] Skipping {os.path.basename(cache_path)}: a different file "
                    f"of that name is already on the array", warn=True)
                continue

            ok, _deleted, size = move_file_to_array(cache_path)
            with _flush_lock:
                if ok:
                    _flush_state["done"] += 1
                    _flush_state["bytes"] += size
                else:
                    _flush_state["failed"] += 1

            # The move loop can run for a long time; refresh the snapshot the
            # UI polls rather than leaving it stale until the next interval.
            if time.time() - last_write >= 5:
                write_status()
                last_write = time.time()

        with _flush_lock:
            done, moved     = _flush_state["done"], _flush_state["bytes"]
            skipped, failed = _flush_state["skipped"], _flush_state["failed"]
            conflicts       = _flush_state["conflicts"]
        summary = f"[{label}] Done: {done} file(s), {moved / 1073741824:.1f} GB moved"
        if skipped:
            summary += f", {skipped} skipped (in use)"
        if conflicts:
            summary += f", {conflicts} skipped (name clash on the array)"
        if failed:
            summary += f", {failed} failed"
        log(summary, error=bool(failed))

    except Exception as e:
        log(f"[{label}] Error: {e}", error=True)
    finally:
        with _flush_lock:
            _flush_state["active"] = False
            _flush_state["finished"] = int(time.time())
        write_status()

def start_flush(only=None, label="Flush"):
    """Run a flush in the background so the main loop keeps tracking streams -
    it is what keeps the in-use protection current while the flush runs."""
    with _flush_lock:
        if _flush_state["active"]:
            return False
    threading.Thread(target=flush_cache_to_array, name="flush", daemon=True,
                     kwargs={"only": only, "label": label}).start()
    return True

def copy_file_to_cache(array_path):
    """Copy file from array to cache. Runs inside the copy worker thread."""
    if is_excluded(array_path) or not is_media_file(os.path.basename(array_path)):
        return

    cache_path = array_to_cache(array_path)

    # Already cached and same size?
    if os.path.exists(cache_path):
        try:
            if os.path.getsize(array_path) == os.path.getsize(cache_path):
                deletion_queue.pop(cache_path, None)
                TrackedFiles.add(cache_path)
                return
        except OSError:
            pass

    # Recently failed? Don't hammer the disks / spam the log every poll.
    last_fail = failed_copies.get(cache_path, 0)
    if time.time() - last_fail < COPY_FAIL_COOLDOWN:
        return

    if not os.path.exists(array_path):
        return

    try:
        file_size = os.path.getsize(array_path)
    except OSError:
        return

    if not cache_has_room_for(file_size) and not evict_oldest_cached(file_size):
        return

    log(f"[Copy] -> {os.path.basename(array_path)}")
    try:
        # Create directory structure with proper permissions
        cache_dir  = os.path.dirname(cache_path)
        cache_root = cfg("CACHE_ROOT")
        cur = cache_root
        for part in os.path.relpath(cache_dir, cache_root).split(os.sep):
            if not part or part == '.':
                continue
            cur = os.path.join(cur, part)
            if not os.path.exists(cur):
                os.mkdir(cur)
                clone_permissions(cur)

        rsync_transfer(array_path, cache_path, remove_source=False)
        clone_permissions(cache_path)
        TrackedFiles.add(cache_path)
        failed_copies.pop(cache_path, None)
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, 'stderr', '') or ''
        log(f"Copy failed for {os.path.basename(array_path)}: {e} {detail}".strip(), error=True)
        failed_copies[cache_path] = time.time()

# =============================================================================
# COPY WORKER — transfers happen off the main loop
# =============================================================================

def enqueue_copy(array_path):
    """Queue a file for copying to cache. De-duplicates: a path that is
    already queued (or being copied right now) is not queued again."""
    with _pending_lock:
        if array_path in _pending_copies:
            return
        _pending_copies.add(array_path)
    copy_queue.put(array_path)

def copy_worker():
    """Single worker thread: copies queued files one at a time, in order.
    Keeping this off the main thread means a long-running transfer never
    blocks stream polling or cleanup."""
    global _current_copy
    while not _shutting_down.is_set():
        try:
            array_path = copy_queue.get(timeout=1)
        except queue.Empty:
            continue
        _current_copy = os.path.basename(array_path)
        try:
            copy_file_to_cache(array_path)
        except Exception as e:
            log(f"Copy worker error for {array_path}: {e}", error=True)
        finally:
            _current_copy = None
            with _pending_lock:
                _pending_copies.discard(array_path)
            copy_queue.task_done()

# =============================================================================
# API CLIENTS — urllib-based, no external deps
# =============================================================================

def api_get(url, headers, timeout=5):
    """Make an API GET request and return parsed JSON, or None on failure.

    Uses urllib from the stdlib so this plugin doesn't depend on the
    `requests` package (which needs to be pip-installed on every boot
    because Unraid's root FS is tmpfs). SSL verification is disabled
    to support the self-signed certs that Plex/Emby/Jellyfin typically
    use on LAN."""
    try:
        req = urllib.request.Request(url, headers=headers, method='GET')
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            if resp.status != 200:
                return None
            body = resp.read()
            if not body:
                return None
            try:
                return json.loads(body.decode('utf-8', errors='replace'))
            except ValueError:
                return None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None

def _progress(position, duration):
    """Playback progress as 0.0–1.0, or None if unknown."""
    try:
        position, duration = float(position), float(duration)
        if duration > 0:
            return max(0.0, min(1.0, position / duration))
    except (TypeError, ValueError):
        pass
    return None

def get_active_streams():
    """Get currently playing files from all enabled services.
    Each session carries the current playback progress so that watched
    detection works per-session (and therefore also on rewatches)."""
    streams = {}

    # --- Plex ---
    if cfg("ENABLE_PLEX", as_bool=True):
        headers = {'X-Plex-Token': cfg("PLEX_TOKEN"), 'Accept': 'application/json'}
        data = api_get(f"{cfg('PLEX_URL')}/status/sessions", headers)
        if data and 'MediaContainer' in data:
            for item in data['MediaContainer'].get('Metadata', []):
                rk = item.get('ratingKey')
                path = metadata_cache.get(rk)

                if not path:
                    for media in item.get('Media', []):
                        for part in media.get('Part', []):
                            if part.get('file'):
                                path = part['file']
                                break
                        if path:
                            break

                if not path and rk:
                    meta = api_get(f"{cfg('PLEX_URL')}/library/metadata/{rk}", headers)
                    if meta and 'MediaContainer' in meta:
                        for m in meta['MediaContainer'].get('Metadata', []):
                            for med in m.get('Media', []):
                                for p in med.get('Part', []):
                                    if p.get('file'):
                                        path = p['file']
                                        break
                                if path:
                                    break
                            if path:
                                break

                if path:
                    if len(metadata_cache) >= METADATA_CACHE_LIMIT:
                        metadata_cache.clear()
                    metadata_cache[rk] = path
                    streams[path] = {
                        'service':  'plex',
                        'id':       rk,
                        'progress': _progress(item.get('viewOffset'), item.get('duration')),
                    }

    # --- Emby / Jellyfin ---
    for enabled_key, api_key, url_key, name in [
        ("ENABLE_EMBY",     "EMBY_API_KEY",     "EMBY_URL",     "emby"),
        ("ENABLE_JELLYFIN", "JELLYFIN_API_KEY", "JELLYFIN_URL", "jellyfin"),
    ]:
        if cfg(enabled_key, as_bool=True):
            headers = {'X-Emby-Token': cfg(api_key), 'Accept': 'application/json'}
            data = api_get(f"{cfg(url_key)}/Sessions", headers)
            if isinstance(data, list):
                for s in data:
                    item = s.get('NowPlayingItem', {}) or {}
                    if item.get('Path'):
                        play_state = s.get('PlayState', {}) or {}
                        streams[item['Path']] = {
                            'service':  name,
                            'id':       item.get('Id'),
                            'user':     s.get('UserId'),
                            'progress': _progress(play_state.get('PositionTicks'),
                                                  item.get('RunTimeTicks')),
                        }

    return streams

def is_watched(session):
    """Check if the media item was watched in this session.

    Primary signal: the playback progress recorded at the last poll before
    the session ended (>= WATCHED_MIN_PROGRESS counts as watched). Unlike
    the server-side "played" flags (Plex viewCount, Emby/Jellyfin Played),
    this also behaves correctly on rewatches — those flags stay true
    forever once something was watched a single time. The server flags
    remain as a fallback when no progress information was available."""
    if not session:
        return False

    progress = session.get('progress')
    if progress is not None:
        return progress >= WATCHED_MIN_PROGRESS

    service = session.get('service')

    if service == 'plex':
        headers = {'X-Plex-Token': cfg("PLEX_TOKEN"), 'Accept': 'application/json'}
        data = api_get(f"{cfg('PLEX_URL')}/library/metadata/{session.get('id')}", headers)
        if data and 'MediaContainer' in data:
            meta = (data['MediaContainer'].get('Metadata') or [{}])[0]
            return meta.get('viewCount', 0) > 0

    elif service in ('emby', 'jellyfin'):
        url_key = "EMBY_URL"     if service == 'emby' else "JELLYFIN_URL"
        api_key = "EMBY_API_KEY" if service == 'emby' else "JELLYFIN_API_KEY"
        headers = {'X-Emby-Token': cfg(api_key), 'Accept': 'application/json'}
        data = api_get(f"{cfg(url_key)}/Users/{session.get('user')}/Items/{session.get('id')}", headers)
        if data:
            return data.get('UserData', {}).get('Played', False)

    return False

# =============================================================================
# SEASON BATCHING
# =============================================================================

def select_batch_episodes(all_eps, current_ep):
    """Decide which episodes (>= current_ep) should be on cache when
    season batching is enabled.

    all_eps:    sorted list of unique episode numbers present in the season
    current_ep: the episode currently being played

    Rules:
    - If the whole season fits within BATCH_SIZE + TOLERANCE episodes,
      cache everything (e.g. 36 episodes with size 30 / tolerance 10).
    - Otherwise split the season into fixed batches of BATCH_SIZE. If the
      final batch would be <= TOLERANCE episodes, it is merged into the
      previous one (avoids a tiny trailing batch).
    - The batch containing the current episode is always cached (from the
      current episode onward). When only PREFETCH episodes remain in the
      current batch, the next batch is cached too — so e.g. at episode
      26/30 the next 30 episodes start copying.
    """
    batch_size = max(1, cfg("EPISODE_BATCH_SIZE", as_int=True))
    tolerance  = max(0, cfg("EPISODE_BATCH_TOLERANCE", as_int=True))
    prefetch   = max(0, cfg("EPISODE_BATCH_PREFETCH", as_int=True))

    upcoming = [e for e in all_eps if e >= current_ep]
    if len(all_eps) <= batch_size + tolerance:
        return set(upcoming)

    # Fixed batch boundaries across the full season, so they stay stable
    # regardless of which episode is currently playing.
    batches = [all_eps[i:i + batch_size] for i in range(0, len(all_eps), batch_size)]
    if len(batches) >= 2 and len(batches[-1]) <= tolerance:
        batches[-2].extend(batches.pop())

    # Locate the batch containing (or nearest after) the current episode
    idx = len(batches) - 1
    for i, b in enumerate(batches):
        if current_ep <= b[-1]:
            idx = i
            break

    selected = [e for e in batches[idx] if e >= current_ep]
    remaining_in_batch = sum(1 for e in batches[idx] if e > current_ep)
    if remaining_in_batch <= prefetch and idx + 1 < len(batches):
        selected.extend(batches[idx + 1])

    return set(selected)

# =============================================================================
# STREAM HANDLERS
# =============================================================================

def _season_episode_files(season_dir):
    """Map episode number -> list of filenames for a season directory."""
    try:
        files = sorted(os.listdir(season_dir))
    except OSError:
        return {}
    ep_files = {}
    for f in files:
        ep = parse_episode(f)
        if ep is not None:
            ep_files.setdefault(ep, []).append(f)
    return ep_files

def _enqueue_episodes(season_dir, ep_files, wanted):
    for ep in sorted(wanted):
        for f in ep_files[ep]:
            enqueue_copy(os.path.join(season_dir, f))

_SEASON_NUM_RE = re.compile(r"(\d+)")

def find_next_season_dir(season_dir):
    """Locate the sibling directory of the following season
    (e.g. 'Season 11' -> 'Season 12'). Returns None if there is none."""
    match = _SEASON_NUM_RE.search(os.path.basename(season_dir))
    if not match:
        return None
    next_num = int(match.group(1)) + 1

    show_dir = os.path.dirname(season_dir)
    try:
        entries = sorted(os.listdir(show_dir))
    except OSError:
        return None

    for name in entries:
        full = os.path.join(show_dir, name)
        if not os.path.isdir(full):
            continue
        m = _SEASON_NUM_RE.search(name)
        if m and int(m.group(1)) == next_num:
            return full
    return None

_prefetched_seasons = set()   # log each next-season prefetch only once per run

def prefetch_next_season(season_dir):
    """Queue the beginning of the next season (first batch if batching is
    enabled, otherwise the whole season)."""
    next_dir = find_next_season_dir(season_dir)
    if not next_dir or is_excluded(next_dir):
        return

    ep_files = _season_episode_files(next_dir)
    if not ep_files:
        return

    all_eps = sorted(ep_files)
    if cfg("ENABLE_EPISODE_BATCHING", as_bool=True):
        wanted = select_batch_episodes(all_eps, all_eps[0])
    else:
        wanted = set(all_eps)

    if next_dir not in _prefetched_seasons:
        _prefetched_seasons.add(next_dir)
        log(f"[Prefetch] Next season: {os.path.basename(os.path.dirname(next_dir))}"
            f"/{os.path.basename(next_dir)} ({len(wanted)} episodes)")

    _enqueue_episodes(next_dir, ep_files, wanted)

def handle_movie(array_path):
    """Handle movie caching - cache main file and related side-car files."""
    enqueue_copy(array_path)

    folder = os.path.dirname(array_path)
    stem   = os.path.splitext(os.path.basename(array_path))[0]

    if os.path.isdir(folder):
        try:
            for f in sorted(os.listdir(folder)):
                if f.startswith(stem):
                    enqueue_copy(os.path.join(folder, f))
        except OSError:
            pass

def handle_series(array_path):
    """Handle series caching - cache current and upcoming episodes.
    With season batching enabled, long seasons are cached in batches
    (see select_batch_episodes)."""
    episode = parse_episode(os.path.basename(array_path))
    if episode is None:
        return handle_movie(array_path)

    season_dir       = os.path.dirname(array_path)
    cache_season_dir = array_to_cache(season_dir)

    # Smart cleanup: remove old episodes
    if cfg("CLEANUP_MODE").lower() == "smart" and os.path.exists(cache_season_dir):
        threshold = episode - cfg("EPISODE_KEEP_PREVIOUS", as_int=True)
        try:
            for f in os.listdir(cache_season_dir):
                ep = parse_episode(f)
                if ep is not None and ep < threshold:
                    cache_path = os.path.join(cache_season_dir, f)
                    if os.path.exists(cache_path):
                        move_file_to_array(cache_path)
                        log(f"[Smart Cleanup] {f}")
        except OSError:
            pass

    # Cache current and upcoming episodes
    if not os.path.isdir(season_dir):
        return
    ep_files = _season_episode_files(season_dir)
    if not ep_files:
        return

    all_eps = sorted(ep_files)
    if cfg("ENABLE_EPISODE_BATCHING", as_bool=True):
        wanted = select_batch_episodes(all_eps, episode)
    else:
        wanted = {e for e in all_eps if e >= episode}

    _enqueue_episodes(season_dir, ep_files, wanted)

    # Near the end of the season: pre-cache the start of the next one,
    # using the same threshold as the batch prefetch.
    if cfg("ENABLE_NEXT_SEASON_PREFETCH", as_bool=True):
        remaining_in_season = sum(1 for e in all_eps if e > episode)
        if remaining_in_season <= max(0, cfg("EPISODE_BATCH_PREFETCH", as_int=True)):
            prefetch_next_season(season_dir)

# =============================================================================
# STATUS SNAPSHOT (read by the web UI)
# =============================================================================

def write_status():
    """Write a small JSON snapshot to STATUS_FILE (atomic replace).
    The web UI polls this to show cache/queue state."""
    cached_files = 0
    cached_bytes = 0
    for path in TrackedFiles.load():
        try:
            cached_bytes += os.path.getsize(path)
            cached_files += 1
        except OSError:
            continue

    try:
        usage = shutil.disk_usage(cfg("CACHE_ROOT"))
        usage_pct = round(usage.used / usage.total * 100, 1)
    except OSError:
        usage_pct = None

    with _pending_lock:
        queue_length = len(_pending_copies)

    with _flush_lock:
        flush = dict(_flush_state)

    status = {
        "updated":         int(time.time()),
        "cached_files":    cached_files,
        "cached_bytes":    cached_bytes,
        "cache_usage_pct": usage_pct,
        "queue_length":    queue_length,
        "copying":         _current_copy,
        "active_streams":  sorted(os.path.basename(p) for p in stream_timers),
        "flush":           flush,
    }

    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f)
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass

# =============================================================================
# DAEMON
# =============================================================================

def run_daemon():
    """Main daemon loop."""
    setup_logging()
    load_config()

    # Acquire lock
    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.lockf(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        log("Another instance is already running", error=True)
        sys.exit(1)

    signal.signal(signal.SIGHUP,  lambda s, f: load_config())
    signal.signal(signal.SIGTERM, lambda s, f: (_shutdown_cleanup(), sys.exit(0)))
    signal.signal(signal.SIGINT,  lambda s, f: (_shutdown_cleanup(), sys.exit(0)))

    worker = threading.Thread(target=copy_worker, name="copy-worker", daemon=True)
    worker.start()

    log("Service started. Waiting for streams...")
    if cfg("ENABLE_EPISODE_BATCHING", as_bool=True):
        log(f"Season batching enabled: batch={cfg('EPISODE_BATCH_SIZE', as_int=True)}, "
            f"tolerance={cfg('EPISODE_BATCH_TOLERANCE', as_int=True)}, "
            f"prefetch={cfg('EPISODE_BATCH_PREFETCH', as_int=True)}")

    reconcile_tracked_files()
    write_status()

    global active_cache_paths
    last_streams      = {}
    last_days_check   = 0
    last_status_write = time.time()

    while True:
        try:
            # The web UI cannot talk to this process directly, so it drops a
            # marker file: an op line, optionally followed by one path per line
            # for the per-file buttons. Read it, then remove it before acting -
            # if the flush fails, the user gets an error rather than a request
            # that retriggers on every pass.
            if os.path.exists(FLUSH_REQUEST):
                try:
                    request = [ln.strip() for ln in Path(FLUSH_REQUEST).read_text().splitlines()]
                except OSError:
                    request = []
                try:
                    os.remove(FLUSH_REQUEST)
                except OSError:
                    pass
                paths = ([ln for ln in request[1:] if ln]
                         if request and request[0].lower() == "move" else None)
                start_flush(only=paths, label="Move" if paths else "Flush")

            streams = get_active_streams()
            active_paths = set()

            for docker_path, session in streams.items():
                array_path = translate_docker_path(docker_path)

                if not array_path.startswith(cfg("ARRAY_ROOT")):
                    continue
                if is_excluded(array_path):
                    continue
                if not is_media_file(os.path.basename(array_path)):
                    continue

                active_paths.add(array_path)

                # New stream?
                if array_path not in stream_timers:
                    log(f"[Stream] Active: {os.path.basename(array_path)}")
                    stream_timers[array_path] = time.time()
                    continue

                # Copy delay passed?
                if time.time() - stream_timers[array_path] >= cfg("COPY_DELAY", as_int=True):
                    if parse_episode(os.path.basename(array_path)) is not None:
                        handle_series(array_path)
                    else:
                        handle_movie(array_path)

            # Remove inactive streams from the timer map
            for path in list(stream_timers.keys()):
                if path not in active_paths:
                    del stream_timers[path]

            # Cache paths of active streams must never be evicted
            active_cache_paths = {array_to_cache(p) for p in active_paths}

            cleanup_mode = cfg("CLEANUP_MODE").lower()

            # Smart cleanup (fires when a session has stopped)
            if cleanup_mode == "smart":
                stopped = set(last_streams.keys()) - set(streams.keys())
                for docker_path in stopped:
                    session    = last_streams[docker_path]
                    array_path = translate_docker_path(docker_path)

                    if is_watched(session):
                        cache_path = array_to_cache(array_path)
                        if os.path.exists(cache_path):
                            ep = parse_episode(os.path.basename(array_path))
                            if ep is None:
                                deletion_queue[cache_path] = time.time()
                            else:
                                # If this was the last episode in the season folder,
                                # queue the whole season for deletion.
                                folder = os.path.dirname(array_path)
                                max_ep = None
                                if os.path.exists(folder):
                                    try:
                                        eps = [parse_episode(f) for f in os.listdir(folder)]
                                        eps = [e for e in eps if e is not None]
                                        max_ep = max(eps) if eps else None
                                    except OSError as e:
                                        # Cannot tell whether this was the finale.
                                        # Assuming it was would evict the whole
                                        # season on a transient listing error.
                                        log(f"Cannot read {folder}: {e} - not treating "
                                            f"episode {ep} as the season finale", warn=True)
                                        max_ep = None
                                if max_ep is not None and ep >= max_ep:
                                    cache_dir = os.path.dirname(cache_path)
                                    # Only ever queue files this plugin cached.
                                    # The season folder can also hold a fresh
                                    # download waiting for the mover, or a file
                                    # the user keeps on cache deliberately -
                                    # moving those to the array is not ours to do.
                                    ours = TrackedFiles.load()
                                    try:
                                        for f in os.listdir(cache_dir):
                                            candidate = os.path.join(cache_dir, f)
                                            if candidate in ours:
                                                deletion_queue[candidate] = time.time()
                                    except OSError:
                                        pass

                # Process deletion queue
                delay = cfg("MOVIE_DELETE_DELAY", as_int=True)
                for cache_path, queued_time in list(deletion_queue.items()):
                    if time.time() - queued_time > delay:
                        if os.path.exists(cache_path):
                            move_file_to_array(cache_path)
                            log(f"[Cleanup] {os.path.basename(cache_path)}")
                        del deletion_queue[cache_path]

            # Days-based cleanup (runs at most once per hour)
            elif cleanup_mode == "days":
                if time.time() - last_days_check > 3600:
                    max_age = cfg("CACHE_MAX_DAYS", as_int=True) * 86400
                    tracked = TrackedFiles.load()
                    now = time.time()

                    for cache_path, cached_time in list(tracked.items()):
                        if now - cached_time > max_age and os.path.exists(cache_path):
                            log(f"[Days Cleanup] {os.path.basename(cache_path)}")
                            move_file_to_array(cache_path)

                    last_days_check = time.time()

            last_streams = streams

            # Status snapshot for the web UI
            if time.time() - last_status_write >= STATUS_INTERVAL:
                write_status()
                last_status_write = time.time()

        except Exception as e:
            log(f"Loop error: {e}", error=True)

        time.sleep(max(1, cfg("CHECK_INTERVAL", as_int=True)))

def _shutdown_cleanup():
    """Called on SIGTERM/SIGINT so systemd/rc.d stop reports cleanly and
    a running rsync doesn't linger as an orphan."""
    _shutting_down.set()
    try:
        with _rsync_lock:
            proc = _current_rsync
        if proc and proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
    try:
        log("Service stopped.")
    except Exception:
        pass

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    if "--flush" in sys.argv:
        # One-shot flush for when the service is stopped. The daemon holds
        # LOCK_FILE for its whole life, so this refuses to run next to it -
        # the web UI routes the request through FLUSH_REQUEST in that case.
        setup_logging()
        load_config()
        _flush_lock_fd = open(LOCK_FILE, 'w')
        try:
            fcntl.lockf(_flush_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            log("Service is running - ask it to flush instead of running --flush", error=True)
            sys.exit(1)

        # Nothing is streaming as far as this process knows, so ask the media
        # servers directly rather than moving a file somebody is watching.
        try:
            active_cache_paths = {
                array_to_cache(translate_docker_path(p)) for p in get_active_streams()
            }
        except Exception as e:
            log(f"Cannot determine active streams: {e} - flushing everything", warn=True)

        paths = [a for a in sys.argv[1:] if a != "--flush"]
        flush_cache_to_array(only=paths or None, label="Move" if paths else "Flush")
    else:
        run_daemon()
