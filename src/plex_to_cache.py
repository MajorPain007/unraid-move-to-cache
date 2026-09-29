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
  - Main thread: polls media server APIs, decides what should be cached and
    what Auto Cleanup sends back. Never blocks on file transfers.
  - Copy worker thread: processes the copy queue one file at a time, so a
    multi-gigabyte rsync never stalls stream detection.
  - Mover: this script started with --move, as a process of its own. It does
    every move back to the array - the ones picked in the browser and the ones
    Auto Cleanup queues. Being separate is what lets Stop end the copying to
    the cache while a move to the array carries on; the mover has its own
    button to stop it.
"""

import os
import sys
import re
import time
import json
import ssl
import stat
import glob
import fcntl
import logging
from logging.handlers import RotatingFileHandler, WatchedFileHandler
import shutil
import signal
import queue
import collections
import threading
import contextlib
import subprocess
import http.client
import urllib.request
import urllib.error
from pathlib import Path

# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG_FILE   = "/boot/config/plugins/plex_to_cache/settings.cfg"
TRACKED_FILES = "/boot/config/plugins/plex_to_cache/cached_files.list"
TRACKED_LOCK  = "/var/run/plex_to_cache.tracked.lock"   # the service and the mover both change the list
LOCK_FILE     = "/var/run/plex_to_cache.lock"            # one service at a time
LOG_FILE      = "/var/log/plex_to_cache.log"
LOG_MAX_BYTES = 5 * 1024 * 1024   # rotate when the log reaches 5 MB
USER0_ROOT    = "/mnt/user0"      # the user shares without the cache pool - where a move back goes
PROC_MOUNTS   = "/proc/self/mounts"

RSYNC_RETRIES        = 3          # attempts per file before giving up
RSYNC_RETRY_DELAY    = 5          # seconds between attempts
RSYNC_POLL           = 1          # seconds between looks at the move queue while rsync runs
COPY_FAIL_COOLDOWN   = 300        # seconds before re-trying a file that failed all attempts
METADATA_CACHE_LIMIT = 500        # max entries kept in the Plex ratingKey→path cache

STATUS_FILE          = "/var/run/plex_to_cache.status.json"  # service snapshot for the web UI and the mover
STATUS_INTERVAL      = 30         # seconds between status snapshots
STATUS_FRESH         = 90         # the mover does not trust a snapshot older than this
MOVE_QUEUE           = "/var/run/plex_to_cache.move.queue"   # "<kind>\t<path>" per line
MOVE_LOCK            = "/var/run/plex_to_cache.move.lock"    # held by the running mover
MOVE_PID             = "/var/run/plex_to_cache.move.pid"     # read by the Stop move button
MOVE_STATUS          = "/var/run/plex_to_cache.move.json"    # mover progress for the web UI
MOVE_MIN_AGE         = 30 * 60    # a file written this recently is left where it is
MOVE_RETRY           = 600        # Auto Cleanup asks again for a file still on cache after this
EVICT_MAX_FILES      = 25         # max files moved back per eviction pass
WATCHED_MIN_PROGRESS = 0.90       # session progress at which media counts as watched
LEGACY_PARTIAL_DIR   = ".plex_to_cache-partial"  # left by interrupted copies before 2026.09.29.01

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
deletion_queue  = {}               # cache path -> when it was queued; main loop and copy worker
failed_copies   = {}               # cache_path -> timestamp of last failed attempt
active_cache_paths = set()         # cache paths of currently streamed files (never evicted or moved)
_requested      = {}               # cache path -> when Auto Cleanup handed it to the mover
_movers         = []               # mover processes this process started, reaped with poll()
_api_state      = {}               # media server -> whether it answered the last poll
_warned         = set()            # keys of warnings already logged by log_once

# Transfer state
copy_queue      = queue.Queue()
_pending_copies = set()            # array paths queued or currently copying
_pending_lock   = threading.Lock()
_current_copy   = None             # basename of the file being copied right now
_current_rsync  = None             # running rsync Popen (for clean shutdown)
_while_waiting  = None             # the mover's: called every RSYNC_POLL seconds while rsync runs
# Re-entrant: the stop handler takes it, and in the mover it runs on the same
# thread that may be holding it at that moment.
_rsync_lock     = threading.RLock()
_shutting_down  = threading.Event()   # asked to stop: end the current transfer, start no new one

# Mover state (the --move process only), mirrored to MOVE_STATUS for the web UI
_job         = {}
_stream_poll = {"at": 0.0, "paths": frozenset()}

# SSL context: Plex / Emby / Jellyfin typically use self-signed certs on the
# local network, so we intentionally skip verification. This is equivalent
# to the old `verify=False` on the `requests` calls.
_SSL_CTX = ssl._create_unverified_context()

# =============================================================================
# UTILITIES
# =============================================================================

_logger = logging.getLogger("plex_to_cache")

def setup_logging(rotate=True):
    """Log to LOG_FILE, or to stderr if it is not writable. Thread-safe.

    The service rotates the file by size, also while it runs. The mover writes
    to the same file but never rotates it: two processes rotating one file keep
    renaming it out from under each other. It reopens the file when the service
    has rotated it instead, so its lines land in the current log."""
    try:
        if rotate:
            handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=1)
        else:
            handler = WatchedFileHandler(LOG_FILE)
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                           datefmt="%Y-%m-%d %H:%M:%S"))
    _logger.setLevel(logging.INFO)
    _logger.addHandler(handler)

def log(msg, error=False, warn=False):
    prefix = "[Error] " if error else ("[Warn] " if warn else "")
    _logger.info(f"{prefix}{msg}")

def log_once(key, msg, **kw):
    """log(), but only the first time for a given key in this process."""
    if key not in _warned:
        _warned.add(key)
        log(msg, **kw)

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

    # Free-text fields, and every prefix comparison assumes no trailing slash.
    for key in ("ARRAY_ROOT", "CACHE_ROOT"):
        config[key] = config[key].rstrip('/') or '/'

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

def _size(n):
    """Bytes the way the web UI shows them."""
    if n >= 1073741824:
        return f"{n / 1073741824:.1f} GB"
    if n >= 1048576:
        return f"{n / 1048576:.0f} MB"
    return f"{n / 1024:.0f} KB"

def _read_json(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None

def _write_json(path, data):
    """Atomic replace, so a reader never gets half a file."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        pass

# =============================================================================
# FILE TRACKING
# =============================================================================

class TrackedFiles:
    """The files this plugin put on the cache, each with the time it did.

    The service and the mover both change the list, so every change is a
    read-modify-write under a lock both processes take. A plain read needs no
    lock: the file is replaced atomically, so a reader sees one version or the
    other and never half of one."""

    _lock  = threading.RLock()   # threads of this process
    _depth = 0                   # how deep the holder of _lock is nested in locked()
    _fh    = None                # the flock shared with the other process

    @classmethod
    @contextlib.contextmanager
    def locked(cls):
        with cls._lock:
            if cls._depth == 0:
                # An flock belongs to the open file, not to the process: a
                # second open() and flock() in a nested call would wait for
                # itself. So one handle, taken by the outermost call only.
                fh = None
                try:
                    fh = open(TRACKED_LOCK, "a")
                    fcntl.flock(fh, fcntl.LOCK_EX)
                except OSError:
                    if fh is not None:
                        fh.close()
                    fh = None
                cls._fh = fh
            cls._depth += 1
            try:
                yield
            finally:
                cls._depth -= 1
                if cls._depth == 0 and cls._fh is not None:
                    cls._fh.close()
                    cls._fh = None

    @staticmethod
    def load():
        """Load tracked files. Returns dict: {path: timestamp}"""
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
        with TrackedFiles.locked():
            try:
                content = '\n'.join(f"{p}|{t}" for p, t in sorted(tracked.items()))
                tmp = TRACKED_FILES + ".tmp"
                Path(tmp).write_text(content + '\n' if content else '')
                os.replace(tmp, TRACKED_FILES)
            except OSError as e:
                log(f"Tracking save failed: {e}", error=True)

    @staticmethod
    def add(path):
        with TrackedFiles.locked():
            tracked = TrackedFiles.load()
            if path not in tracked:
                tracked[path] = time.time()
                TrackedFiles.save(tracked)

    @staticmethod
    def update(add=None, drop=()):
        """Add and drop entries with a single write. The list lives on the USB
        flash drive, so a move of fifty files must not rewrite it fifty times."""
        with TrackedFiles.locked():
            tracked = TrackedFiles.load()
            changed = False
            for path in drop:
                if tracked.pop(path, None) is not None:
                    changed = True
            for path, ts in (add or {}).items():
                if path not in tracked:
                    tracked[path] = ts
                    changed = True
            if changed:
                TrackedFiles.save(tracked)

    @staticmethod
    def remove_many(paths):
        if paths:
            TrackedFiles.update(drop=paths)

def _storage_ready():
    """True once the cache pool and the array are mounted.

    On boot the plugin is installed, and the service started, before the array
    is: CACHE_ROOT is missing or an empty mount point, and every tracked file
    looks deleted. Anything that judges the tracked list by what exists has to
    wait for this - reconciling at that moment would drop every entry."""
    try:
        if not os.listdir(cfg("CACHE_ROOT")):
            return False
    except OSError:
        return False
    return os.path.isdir(physical_array_root())

def reconcile_tracked_files():
    """Bring the tracked-files list in line with what is on the cache.

    1. Drops entries whose cache file no longer exists (e.g. removed
       manually or lost in a crash).
    2. Adopts orphaned plugin copies: media files inside the mapped cache
       folders with an identical twin on the array - same size and
       modification time, which is what rsync -a leaves behind. A same-named
       file that differs is not a copy of anything and is left alone, and so
       is everything outside the mapped folders. The time it is adopted with
       is its ctime, the closest thing on disk to when it was copied.
    3. Removes LEGACY_PARTIAL_DIR folders. Older versions kept interrupted
       copies in them; they are incomplete by definition and their originals
       are on the array.

    Only run once _storage_ready() says so.
    """
    tracked = TrackedFiles.load()
    stale = [p for p in tracked if not os.path.exists(p)]

    adopt   = {}
    cleared = 0
    for cache_dir in sorted(_mapped_cache_dirs()):
        if not os.path.isdir(cache_dir):
            continue
        for root, dirs, files in os.walk(cache_dir):
            if LEGACY_PARTIAL_DIR in dirs:
                shutil.rmtree(os.path.join(root, LEGACY_PARTIAL_DIR), ignore_errors=True)
                cleared += 1
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for f in files:
                cache_path = os.path.join(root, f)
                if f.startswith('.') or cache_path in tracked or not is_media_file(f):
                    continue
                try:
                    if _twin(cache_path) == "same":
                        adopt[cache_path] = os.stat(cache_path).st_ctime
                except OSError:
                    continue

    if stale or adopt:
        TrackedFiles.update(add=adopt, drop=stale)
    if stale or adopt or cleared:
        log(f"[Reconcile] Tracked list: {len(stale)} stale entries removed, "
            f"{len(adopt)} orphaned cache copies adopted"
            + (f", {cleared} folder(s) of interrupted copies removed" if cleared else ""))

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
        return USER0_ROOT
    if root.startswith('/mnt/user/'):
        return USER0_ROOT + root[len('/mnt/user'):]
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

def _mapped_cache_dirs():
    """The cache-side twin of every mapped host folder - the only part of the
    pool this plugin works in. The web UI derives its list the same way."""
    cache_root = cfg("CACHE_ROOT")
    array_root = cfg("ARRAY_ROOT")
    mapped = set()
    for host_path in docker_mappings.values():
        host_path = host_path.rstrip('/')
        if not host_path:
            continue
        rel = _relative_to(host_path, array_root)
        if rel is not None:
            mapped.add(os.path.join(cache_root, rel) if rel else cache_root)
        elif _under(host_path, cache_root):
            mapped.add(host_path)
        else:
            # host_path given as a relative share name — prepend cache root
            mapped.add(os.path.join(cache_root, host_path.lstrip('/')))
    # The pool itself is never one of them: a mapping of the whole user share
    # would otherwise hand appdata and system to the mover.
    mapped.discard(cache_root)
    return mapped

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
    src = cache_to_array(dest_path) if _under(dest_path, cfg("CACHE_ROOT")) else None
    if not src or not os.path.exists(src):
        return
    try:
        st = os.stat(src)
        os.chown(dest_path, st.st_uid, st.st_gid)
        os.chmod(dest_path, st.st_mode)
    except OSError as e:
        log(f"Permission clone failed: {e}", error=True)

def _mirror_parents(cache_path):
    """Create the directories above the array side of cache_path that do not
    exist yet, each owned like its counterpart on the cache.

    os.makedirs would create them as root with mode 0755. For a folder that
    only existed on the cache - a series downloaded last night - that shuts out
    Sonarr and friends, which run as nobody: the next episode could not be
    written into its own season folder. The array root itself is never made:
    if it is missing the array is not mounted, and a directory made there would
    be on the RAM disk.

    Returns the directories it made, top first, so a move that does not happen
    after all can take them away again."""
    cache_root = cfg("CACHE_ROOT")
    rel = _relative_to(os.path.dirname(cache_path), cache_root)
    if rel is None:
        raise OSError(f"{cache_path} is not under {cache_root}")
    dst, src = physical_array_root(), cache_root
    if not os.path.isdir(dst):
        raise OSError(f"{dst} does not exist - is the array started?")
    created = []
    for part in (p for p in rel.split('/') if p):
        dst, src = os.path.join(dst, part), os.path.join(src, part)
        if os.path.isdir(dst):
            continue
        os.mkdir(dst)
        created.append(dst)
        try:
            st = os.stat(src)
            os.chown(dst, st.st_uid, st.st_gid)
            os.chmod(dst, stat.S_IMODE(st.st_mode))
        except OSError as e:
            log(f"Could not give {dst} the owner of {src}: {e}", warn=True)
    return created

def _remove_empty_dirs(dirs):
    """Take away directories made for a move that did not happen, deepest
    first, as far as they are still empty."""
    for d in reversed(dirs):
        try:
            os.rmdir(d)
        except OSError:
            break

# =============================================================================
# CACHE SPACE
# =============================================================================

_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")

def _mount_of(path):
    """(mount point, source, type) of the filesystem path is on, or None."""
    best = None
    try:
        with open(PROC_MOUNTS) as fh:
            for line in fh:
                fields = line.split()
                if len(fields) < 3:
                    continue
                source, mnt, fstype = (_MOUNT_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), f)
                                       for f in fields[:3])
                if _under(path, mnt) and (best is None or len(mnt) > len(best[0])):
                    best = (mnt, source, fstype)
    except OSError:
        return None
    return best

def _pool_space():
    """(used, total, free) in bytes for the pool CACHE_ROOT is on, or None.

    statvfs is right for XFS and Btrfs and wrong for ZFS, which answers it per
    dataset: "used" is what that one dataset references, not its children.
    Unraid makes each share on a ZFS pool a dataset of its own, so the media
    folder is a child of /mnt/cache and none of it would be counted - the
    usage limit would not be reached until the pool was full. For ZFS the
    numbers come from zfs itself."""
    root = cfg("CACHE_ROOT")
    try:
        du = shutil.disk_usage(root)
    except OSError:
        return None
    mount = _mount_of(root)
    if mount and mount[2] == "zfs":
        pool = mount[1].split('/')[0]
        try:
            out = subprocess.run(["zfs", "list", "-Hp", "-o", "used,avail", pool],
                                 capture_output=True, text=True, timeout=15)
            used, avail = (int(x) for x in out.stdout.split())
            return used, used + avail, avail
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            log_once(("zfs", pool), f"Cannot read the size of ZFS pool {pool} ({e}) - "
                     f"the usage limit is judged from {root} alone", warn=True)
    return du.used, du.total, du.free

def _existing_dir(path):
    """The nearest directory above path that exists."""
    d = os.path.dirname(path)
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return d or '/'

def cache_has_room_for(file_size, dest=None, freed=0):
    """Check both the configured max-usage percentage and the free bytes this
    file needs (plus a small safety margin).

    dest is where the file will go: a quota on that dataset can leave less room
    than the pool has. freed is what the caller has just moved off the cache -
    ZFS gives deleted space back a few seconds after the fact, and without it
    eviction would keep finding the pool full and move twenty-five files to
    make room for one."""
    space = _pool_space()
    if space is None:
        log(f"Cannot stat cache filesystem {cfg('CACHE_ROOT')}", error=True)
        return False
    used, total, free = space
    used = max(0, used - freed)
    free += freed
    if total and used / total * 100 >= cfg("CACHE_MAX_USAGE", as_int=True):
        return False
    if dest:
        try:
            free = min(free, shutil.disk_usage(_existing_dir(dest)).free + freed)
        except OSError:
            pass
    margin = 512 * 1024 * 1024  # keep at least 512 MB headroom
    return free > file_size + margin

# =============================================================================
# FILE OPERATIONS
# =============================================================================

class TransferStopped(Exception):
    """The transfer was cut short because this process was asked to stop."""

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
    Returns (returncode, stderr_text).

    If _while_waiting is set, it is called every RSYNC_POLL seconds until rsync
    is done - the mover takes new requests there, rather than after a file that
    can take minutes. It must not raise: rsync would go on without anyone
    waiting for it."""
    global _current_rsync
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    with _rsync_lock:
        _current_rsync = proc
    # A stop that came in just before the line above found nothing to end.
    if _shutting_down.is_set():
        proc.terminate()
    deadline = time.monotonic() + timeout
    try:
        while True:
            step = deadline - time.monotonic()
            if _while_waiting is not None:
                step = min(step, RSYNC_POLL)
            try:
                # Asking again after a timeout loses none of the output.
                _, stderr = proc.communicate(timeout=max(step, 0))
                return proc.returncode, (stderr or "")
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise
            if _while_waiting is not None:
                _while_waiting()
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

def _remove_rsync_leftovers(dst):
    """rsync cleans up its temporary file when it is told to stop, but not
    when it is killed - which is what the timeout does."""
    pattern = os.path.join(os.path.dirname(dst),
                           "." + glob.escape(os.path.basename(dst)) + ".??????")
    for leftover in glob.glob(pattern):
        try:
            os.remove(leftover)
        except OSError:
            pass

def rsync_transfer(src, dst, remove_source=True):
    """Move (or copy) a file using rsync. Robust against transient errors:

    - Neither --inplace nor --partial. rsync writes into a hidden temporary
      file next to the destination and renames it into place once complete,
      so a stream that opens the file mid-copy never sees a truncated one;
      Unraid serves new opens from cache. When a transfer is stopped, rsync
      deletes that temporary file itself. (--partial kept it for a resume,
      but between two local paths rsync copies whole files and never resumes
      - all it kept was the debris, up to a whole film of it.)
    - Retries up to RSYNC_RETRIES times.
    - rsync stderr is captured and logged so failures are diagnosable.
    - Exit codes 23 (partial transfer / attribute errors) and 24 (source
      file vanished) are tolerated when the destination is verifiably
      complete (same size as source). Exit 23 is frequently caused by
      chown/chmod/utime problems on FUSE shares even though the file
      content transferred fine — the caller re-applies permissions via
      clone_permissions anyway.

    The destination directory must exist: only the caller knows whose
    directory it should be.

    Raises TransferStopped if this process was asked to stop, and
    subprocess.CalledProcessError if the transfer really failed.
    """
    cmd = ["rsync", "-a"]
    if remove_source:
        cmd.append("--remove-source-files")
    cmd.extend([src, dst])

    timeout  = _rsync_timeout_for(src)
    last_err = ""
    rc       = 1

    for attempt in range(1, RSYNC_RETRIES + 1):
        if _shutting_down.is_set():
            raise TransferStopped()
        rc, stderr = _run_rsync(cmd, timeout)
        if rc == 0:
            return
        if _shutting_down.is_set():
            raise TransferStopped()
        if rc == -1:
            _remove_rsync_leftovers(dst)

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

        if attempt < RSYNC_RETRIES:
            log(f"rsync attempt {attempt}/{RSYNC_RETRIES} failed (exit {rc}) for "
                f"{os.path.basename(src)}: {last_err} — retrying in {RSYNC_RETRY_DELAY}s",
                warn=True)
            if _shutting_down.wait(RSYNC_RETRY_DELAY):
                raise TransferStopped()

    raise subprocess.CalledProcessError(rc if rc != 0 else 1, "rsync", stderr=last_err)

def cleanup_empty_dirs(start_path):
    """Remove empty parent directories up to CACHE_ROOT, but stop at any
    directory that's protected by a docker mapping."""
    cache_root = cfg("CACHE_ROOT")
    protected  = _mapped_cache_dirs() | {cache_root}

    parent = os.path.dirname(start_path)
    while _under(parent, cache_root) and parent != cache_root:
        if parent in protected:
            break
        try:
            os.rmdir(parent)
            parent = os.path.dirname(parent)
        except OSError:
            break

def _twin(cache_path):
    """How a cache file relates to the file of the same name on the array.

    None when there is none. "same" for equal size and modification time,
    which is what rsync -a leaves: the cache file is a copy. Otherwise which of
    the two was changed later - "cache_newer" or "array_newer" - or "unclear"
    when they are the same age but not the same size.

    The cache side can legitimately be the newer one. A file on both is served
    from the cache, so anything that writes to it through the user share - a
    tag editor, or Sonarr replacing an episode with a better release under the
    same name - changes the cache copy and leaves the one on the array alone.

    Raises OSError if either file cannot be read, or if cache_path is not on
    the cache at all - its "twin" would be the file itself."""
    array_path = cache_to_array(cache_path)
    if array_path == cache_path:
        raise OSError(f"{cache_path} is not under {cfg('CACHE_ROOT')}")
    try:
        a = os.stat(array_path)
    except FileNotFoundError:
        return None
    c = os.stat(cache_path)
    dt = c.st_mtime - a.st_mtime
    if abs(dt) < 2:
        return "same" if c.st_size == a.st_size else "unclear"
    return "cache_newer" if dt > 0 else "array_newer"

def move_file_to_array(cache_path):
    """Move a single file from cache to array. Returns (result, size).

    result is one of:
      "moved"   - it is on the array now and gone from the cache
      "dropped" - the array already had it, so only the cache copy was removed
      "gone"    - there was nothing on the cache to move
      "kept"    - left alone: the array holds a different file of that name
                  and there is no telling which of the two is the right one
      "stopped" - this process was asked to stop; the cache file is untouched
      "failed"  - see the log
    The tracked list is the caller's business, so a batch can update it once.
    """
    name = os.path.basename(cache_path)
    # A tracked entry from before CACHE_ROOT was changed. Its "place on the
    # array" would be the file itself, and a file compared with itself looks
    # like a duplicate that can go.
    if _relative_to(cache_path, cfg("CACHE_ROOT")) is None:
        log(f"{cache_path} is not under {cfg('CACHE_ROOT')} - left where it is", warn=True)
        return "kept", 0
    try:
        size = os.path.getsize(cache_path)
    except FileNotFoundError:
        return "gone", 0
    except OSError as e:
        log(f"Move failed for {name}: {e}", error=True)
        return "failed", 0

    array_path = cache_to_array(cache_path)
    created = []
    try:
        twin = _twin(cache_path)
        if twin in ("same", "array_newer"):
            if twin == "array_newer":
                log(f"{name}: the copy on the array is newer - removing the one on the cache",
                    warn=True)
            os.remove(cache_path)
            cleanup_empty_dirs(cache_path)
            return "dropped", size
        if twin == "unclear":
            log(f"{name} is also on the array, just as old but a different size. Left on "
                f"the cache: compare the two and delete the one that is wrong.", warn=True)
            return "kept", 0
        if twin == "cache_newer":
            log(f"{name} was changed on the cache after it was cached - "
                f"it replaces the older copy on the array")
        else:
            created = _mirror_parents(cache_path)
        rsync_transfer(cache_path, array_path, remove_source=True)
        cleanup_empty_dirs(cache_path)
        return "moved", size

    except TransferStopped:
        _remove_empty_dirs(created)
        return "stopped", 0
    except (OSError, subprocess.CalledProcessError) as e:
        _remove_empty_dirs(created)
        detail = getattr(e, 'stderr', '') or ''
        log(f"Move failed for {name}: {e} {detail}".strip(), error=True)
        return "failed", 0

def evict_oldest_cached(needed_size, dest=None):
    """LRU eviction: when the cache is full, move the oldest plugin-cached
    files back to the array until needed_size fits (bounded by
    EVICT_MAX_FILES per pass). Files that belong to an active stream, are
    queued for copying or were already handed to the mover are never evicted.

    This runs in the copy worker, as part of copying to the cache, so Stop
    ends it along with the copy it was making room for. Nothing is lost when
    it does: the file it was moving stays on the cache, and stays tracked.

    Returns True if there is room for needed_size afterwards."""
    if not cfg("ENABLE_CACHE_EVICTION", as_bool=True):
        return False

    with _pending_lock:
        pending = {array_to_cache(p) for p in _pending_copies}
    protected = set(active_cache_paths) | pending | set(_requested)

    tracked = TrackedFiles.load()
    freed   = 0
    evicted = 0
    untrack = []
    try:
        for cache_path, _ts in sorted(tracked.items(), key=lambda kv: kv[1]):
            if cache_has_room_for(needed_size, dest, freed):
                return True
            if evicted >= EVICT_MAX_FILES:
                break
            if cache_path in protected:
                continue
            if not os.path.exists(cache_path):
                untrack.append(cache_path)
                continue
            log(f"[Evict] {os.path.basename(cache_path)} (making room on cache)")
            result, size = move_file_to_array(cache_path)
            if result == "stopped":
                return False
            if result != "failed":
                untrack.append(cache_path)
            if result in ("moved", "dropped"):
                freed += size
                evicted += 1
    finally:
        TrackedFiles.remove_many(untrack)

    return cache_has_room_for(needed_size, dest, freed)

def _cache_media_files(min_age_seconds, roots=None, recent=None):
    """Media files sitting in the mapped cache folders, whether this plugin put
    them there or not.

    `roots` limits the search - moving one episode should not walk the whole
    library. Every root is still required to sit inside a mapped folder, so a
    request naming a path outside them finds nothing: the containment is
    enforced here rather than trusted from whoever wrote the request.

    Files modified within min_age_seconds are left alone: something still being
    written into the media folder - an import in progress, say - would otherwise
    be moved out from under the process writing it. Their paths go into
    `recent`, if given, so the caller can say it left them. Hidden files and
    folders are skipped, as the browser does: .Recycle.Bin is not media, and
    neither are the ._ files a Mac leaves on a share.
    """
    mapped = sorted(_mapped_cache_dirs())

    if roots is None:
        targets = mapped
    else:
        targets = [r.rstrip('/') for r in roots
                   if any(_under(r.rstrip('/'), m) for m in mapped)]

    found = {}
    now = time.time()

    def consider(path, name):
        if name.startswith('.') or not is_media_file(name) or is_excluded(path):
            return
        try:
            st = os.stat(path)
        except OSError:
            return
        if now - st.st_mtime < min_age_seconds:
            if recent is not None:
                recent.append(path)
            return
        found[path] = st.st_mtime

    for target in targets:
        if os.path.isfile(target):
            consider(target, os.path.basename(target))
            continue
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for name in files:
                consider(os.path.join(root, name), name)
    return found

# =============================================================================
# MOVER — everything that goes back to the array, in a process of its own
# =============================================================================
#
# Requests sit in MOVE_QUEUE, one "<kind>\t<path>" per line:
#   pick  a file or folder chosen in the browser. It covers media the plugin
#         never cached too - choosing it by hand says what is meant.
#   auto  a single file Auto Cleanup chose. Only ever one this plugin cached.
# The web UI and the service append; the mover takes the lot and empties it.

def _queue_append(lines):
    """flock, not lockf: the web UI appends with PHP's LOCK_EX, which is flock."""
    with open(MOVE_QUEUE, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write("".join(line + "\n" for line in lines))

def _queue_take():
    """Everything queued so far as (kind, path), leaving the queue empty.

    Emptied by truncating under the lock rather than by deleting the file: a
    writer that had opened the file just before would otherwise append to one
    nobody reads any more."""
    try:
        fh = open(MOVE_QUEUE, "r+")
    except FileNotFoundError:
        return []
    with fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        data = fh.read()
        fh.seek(0)
        fh.truncate()
    requests = []
    for line in data.splitlines():
        kind, sep, path = line.partition("\t")
        if sep and path and kind in ("pick", "auto"):
            requests.append((kind, path))
    return requests

def _queue_pending():
    try:
        return os.path.getsize(MOVE_QUEUE) > 0
    except OSError:
        return False

def _try_lock(fh):
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False

def mover_running():
    """True while a mover holds MOVE_LOCK."""
    try:
        fh = open(MOVE_LOCK, "a")
    except OSError:
        return False
    with fh:
        if not _try_lock(fh):
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False

def _start_mover():
    """Start a mover unless one is running. It gets a session of its own, so
    that stopping the service does not take it along."""
    global _movers
    _movers = [p for p in _movers if p.poll() is None]   # reap the finished ones
    if mover_running():
        return
    try:
        err = open(LOG_FILE, "a")
    except OSError:
        err = subprocess.DEVNULL
    try:
        _movers.append(subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--move"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
            start_new_session=True, close_fds=True))
    except OSError as e:
        log(f"Cannot start the mover: {e}", error=True)
    finally:
        if err is not subprocess.DEVNULL:
            err.close()

def queue_moves(kind, paths):
    """Hand paths to the mover and make sure one is running.

    Queued first, then the lock looked at: a mover that is just finishing
    looks at the queue again after letting go of its lock, so a request
    cannot fall into the gap between the two (see run_mover)."""
    if not paths:
        return
    _queue_append([f"{kind}\t{p}" for p in paths])
    _start_mover()

def _stop_transfers():
    """End the transfer in progress and start no new one. rsync removes its
    own unfinished file when terminated, so nothing is left half-copied."""
    _shutting_down.set()
    with _rsync_lock:
        proc = _current_rsync
    if proc is not None:
        try:
            proc.terminate()
        except OSError:
            pass

def _request_stop(signum=None, frame=None):
    """The mover's SIGTERM: the Stop move button, or a shutdown."""
    _job["stopped"] = True
    _stop_transfers()

def _protected_now():
    """Cache paths the mover must not touch right now: what is being played,
    and - while the service runs - what it is copying or about to copy.

    Asked again per file rather than once at the start. A move can run for
    hours, and a stream that begins halfway through needs protecting as much
    as one that was already running. The media servers are asked directly, so
    this also holds while the service is stopped."""
    now = time.time()
    if now - _stream_poll["at"] >= max(5, cfg("CHECK_INTERVAL", as_int=True)):
        try:
            paths = set()
            for docker_path in get_active_streams():
                array_path = _stream_array_path(docker_path)
                if array_path:
                    paths.add(array_to_cache(array_path))
            _stream_poll["paths"] = frozenset(paths)
        except Exception as e:
            log(f"[Move] Cannot ask the media servers what is playing: {e}", warn=True)
        _stream_poll["at"] = now
    protected = set(_stream_poll["paths"])
    status = _read_json(STATUS_FILE)
    if status and now - status.get("updated", 0) < STATUS_FRESH:
        protected.update(status.get("protected") or [])
    return protected

def _write_job():
    _write_json(MOVE_STATUS, _job)

def _write_pid():
    try:
        Path(MOVE_PID).write_text(f"{os.getpid()}\n")
    except OSError:
        pass

def _remove_pid():
    try:
        if Path(MOVE_PID).read_text().strip() == str(os.getpid()):
            os.remove(MOVE_PID)
    except OSError:
        pass

def _job_start():
    _job.clear()
    _job.update(active=True, pid=os.getpid(), started=int(time.time()),
                total=0, done=0, bytes=0, skipped=0, conflicts=0, recent=0,
                kept=0, failed=0, dropped=0, current=None, finished=0,
                stopped=_shutting_down.is_set())
    _write_pid()
    _write_job()

def _job_finish():
    j = _job
    parts = [f"{j['done']} of {j['total']} file(s) moved ({_size(j['bytes'])})"]
    if j["skipped"]:
        parts.append(f"{j['skipped']} in use")
    if j["conflicts"]:
        parts.append(f"{j['conflicts']} with a different file of that name on the array")
    if j["recent"]:
        parts.append(f"{j['recent']} written in the last {MOVE_MIN_AGE // 60} minutes")
    if j["kept"]:
        parts.append(f"{j['kept']} not matching the copy on the array")
    if j["failed"]:
        parts.append(f"{j['failed']} failed")
    if j["dropped"]:
        parts.append(f"{j['dropped']} queued request(s) dropped")
    if j["stopped"]:
        log("[Move] Stopped: " + ", ".join(parts) + ". The rest stays on the cache.")
    else:
        log("[Move] Done: " + ", ".join(parts), error=bool(j["failed"]))
    j.update(active=False, current=None, finished=int(time.time()))
    _write_job()
    _remove_pid()

def _plan_moves(requests, seen):
    """The files a batch of requests names, as (cache path, tracked) pairs in
    the order they are to be moved. What `seen` holds - everything this run has
    dealt with - is left out, so a path asked for twice is handled once.

    They are counted into the job here, before the first of them moves, so the
    progress bar has the total from the start. Files written in the last
    MOVE_MIN_AGE seconds are counted and left out.
    """
    tracked = TrackedFiles.load()
    picks   = [path for kind, path in requests if kind == "pick"]

    # Auto Cleanup names single files, and only ever ones this plugin cached;
    # one that is not tracked any more by now is not ours to move.
    candidates = {path: tracked[path] for kind, path in requests
                  if kind == "auto" and path in tracked}
    recent = []
    if picks:
        # The trailing separator keeps /Media/Show2 from matching /Media/Show.
        wanted   = set(picks)
        prefixes = tuple(p.rstrip('/') + '/' for p in picks)
        for path, ts in tracked.items():
            if path in wanted or path.startswith(prefixes):
                candidates[path] = ts
        for path, ts in _cache_media_files(MOVE_MIN_AGE, roots=picks, recent=recent).items():
            candidates.setdefault(path, ts)

    # Oldest first, and by name among equals - a season picked in the browser
    # then goes episode by episode.
    entries = sorted(((p, ts) for p, ts in candidates.items() if p not in seen),
                     key=lambda kv: (kv[1], kv[0]))
    recent = [p for p in recent if p not in tracked and p not in seen]
    seen.update(p for p, _ts in entries)
    seen.update(recent)

    for path in recent:
        log(f"[Move] Leaving {os.path.basename(path)}: written in the last "
            f"{MOVE_MIN_AGE // 60} minutes")
    if entries:
        more = "more " if _job["total"] else ""
        log(f"[Move] {len(entries)} {more}file(s) to move back to the array")
    _job["total"]  += len(entries)
    _job["recent"] += len(recent)
    _write_job()
    return [(p, p in tracked) for p, _ts in entries]

def _move_batch(requests, seen):
    """Move what a batch of requests names, and what is asked for while it
    runs. `seen` is passed on to _plan_moves.

    A request that comes in meanwhile - a second series picked while the first
    is on its way - is taken within RSYNC_POLL seconds, also halfway through a
    file. Its files join the total at once and go to the back of the line. They
    used to wait for the whole batch, and the progress bar left them out until
    the first selection had gone.

    Left where they are: a file being played or about to be, one written in
    the last MOVE_MIN_AGE seconds, and one whose name the array already has
    for a different file. Each is counted, so the result can say it did less
    than everything rather than implying otherwise.
    """
    global _while_waiting
    work = collections.deque(_plan_moves(requests, seen))

    def take_new():
        # Nothing new after a stop: run_mover deals with what is queued then.
        if _shutting_down.is_set() or not _queue_pending():
            return
        try:
            work.extend(_plan_moves(_queue_take(), seen))
        except Exception as e:
            log(f"[Move] Error: {e}", error=True)

    untrack = []
    _while_waiting = take_new
    try:
        while work:
            take_new()
            if _shutting_down.is_set():
                break
            cache_path, is_tracked = work.popleft()
            name = os.path.basename(cache_path)

            if cache_path in _protected_now():
                _job["skipped"] += 1
                log(f"[Move] Skipping {name}: being played, or about to be")
                continue

            # A tracked file is a copy of the array original by construction.
            # An untracked one is not: a different file of the same name on the
            # array makes two versions of something, and only a person can say
            # which to keep. An identical one is a copy this plugin lost track
            # of, and moving it only drops the duplicate.
            if not is_tracked:
                try:
                    twin = _twin(cache_path)
                except OSError:
                    twin = "unclear"
                if twin not in (None, "same"):
                    _job["conflicts"] += 1
                    log(f"[Move] Skipping {name}: a different file of that name "
                        f"is already on the array", warn=True)
                    continue

            _job["current"] = name
            _write_job()
            result, size = move_file_to_array(cache_path)
            if result == "stopped":
                break
            if result in ("moved", "dropped", "gone"):
                _job["done"]  += 1
                _job["bytes"] += size
                if result == "moved":
                    log(f"[Move] {name} ({_size(size)})")
                elif result == "dropped":
                    log(f"[Move] {name} (the array already had it)")
            elif result == "kept":
                _job["kept"] += 1
            else:
                _job["failed"] += 1
            if result != "failed":
                untrack.append(cache_path)
    finally:
        _while_waiting = None
        _job["current"] = None
        TrackedFiles.remove_many(untrack)
        _write_job()

def run_mover():
    """The --move process: work through the queue until it is empty, then exit.

    Exiting is where a request could get lost. One queued after the last look
    at the queue, while the lock is still held, starts no mover - its sender
    sees this one running. So the lock is let go first and the queue looked at
    once more: whatever is there by then is either picked up here, or its
    sender found the lock free and started a mover of its own.
    """
    setup_logging(rotate=False)
    load_config()
    try:
        lock = open(MOVE_LOCK, "a")
    except OSError as e:
        log(f"[Move] Cannot open {MOVE_LOCK}: {e}", error=True)
        return 1
    with lock:
        if not _try_lock(lock):
            return 0          # a mover is running, and takes the queue from here

        # SIGTERM is the Stop move button, and also what a shutdown sends.
        # Either way the file being moved stays on the cache, and so does the
        # rest.
        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)

        _job_start()
        seen = set()
        while True:
            requests = _queue_take()
            if requests and not _shutting_down.is_set():
                try:
                    _move_batch(requests, seen)
                except Exception as e:
                    log(f"[Move] Error: {e}", error=True)
                continue
            _job["dropped"] += len(requests)
            _job_finish()
            fcntl.flock(lock, fcntl.LOCK_UN)
            if not _queue_pending():
                return 0
            if _shutting_down.is_set():
                # Queued after the stop: a new request, not part of what was
                # stopped. It gets a mover of its own.
                _start_mover()
                return 0
            if not _try_lock(lock):
                return 0
            _job.update(active=True, finished=0)
            _write_pid()
            _write_job()

# =============================================================================
# COPY TO CACHE
# =============================================================================

def copy_file_to_cache(array_path):
    """Copy file from array to cache. Runs inside the copy worker thread."""
    if is_excluded(array_path) or not is_media_file(os.path.basename(array_path)):
        return

    cache_path = array_to_cache(array_path)

    # Already on the cache. Only a copy of the file on the array is ours:
    # array_path is on the user share, which shows the cache copy whenever there
    # is one, so comparing against it compares the file with itself - that is
    # how a fresh download, on the cache and nowhere else, used to end up on
    # the tracked list and later get moved to the array by Auto Cleanup.
    # Anything else of that name is left exactly as it is.
    if os.path.exists(cache_path):
        try:
            twin = _twin(cache_path)
        except OSError:
            return
        if twin == "same":
            deletion_queue.pop(cache_path, None)
            TrackedFiles.add(cache_path)
        return

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

    if not cache_has_room_for(file_size, cache_path) \
            and not evict_oldest_cached(file_size, cache_path):
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
    except TransferStopped:
        log(f"[Copy] Stopped: {os.path.basename(array_path)}")
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, 'stderr', '') or ''
        log(f"Copy failed for {os.path.basename(array_path)}: {e} {detail}".strip(), error=True)
        failed_copies[cache_path] = time.time()

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

def api_get(url, headers, timeout=5, errors=None):
    """Make an API GET request and return parsed JSON, or None on failure.
    `errors`, if given, receives a short reason for a failure.

    Uses urllib from the stdlib so this plugin doesn't depend on the
    `requests` package (which needs to be pip-installed on every boot
    because Unraid's root FS is tmpfs). SSL verification is disabled
    to support the self-signed certs that Plex/Emby/Jellyfin typically
    use on LAN."""
    def fail(reason):
        if errors is not None:
            errors.append(reason)
        return None

    try:
        req = urllib.request.Request(url, headers=headers, method='GET')
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            if resp.status != 200:
                return fail(f"HTTP {resp.status}")
            body = resp.read()
    except urllib.error.HTTPError as e:
        return fail(f"HTTP {e.code} {e.reason}")
    except urllib.error.URLError as e:
        return fail(str(e.reason))
    # ValueError: a server address without http:// - the field is free text.
    # It used to escape from here and abort the whole pass of the main loop, so
    # one mistyped address kept the other servers' streams from being cached.
    except (http.client.HTTPException, OSError, ValueError) as e:
        return fail(str(e) or type(e).__name__)
    if not body:
        return fail("empty response")
    try:
        return json.loads(body.decode('utf-8', errors='replace'))
    except ValueError:
        return fail("the response is not JSON")

def _note_api(service, ok, errors):
    """Say when a media server stops or starts answering - once per change,
    not on every poll. A server that never answers looks exactly like one
    where nobody is watching, and nothing else would tell the two apart."""
    was = _api_state.get(service)
    _api_state[service] = ok
    if ok and was is False:
        log(f"{service} is answering again")
    elif not ok and was is not False:
        log(f"{service} does not answer ({errors[-1] if errors else 'no reason given'}) - "
            f"nothing played on it is cached until it does", warn=True)

def _progress(position, duration):
    """Playback progress as 0.0–1.0, or None if unknown."""
    try:
        position, duration = float(position), float(duration)
        if duration > 0:
            return max(0.0, min(1.0, position / duration))
    except (TypeError, ValueError):
        pass
    return None

def _first_part_file(media):
    """The file of the first part of a Plex Media list, or None."""
    for med in media or []:
        for part in med.get('Part', []) or []:
            if part.get('file'):
                return part['file']
    return None

def get_active_streams():
    """Get currently playing files from all enabled services.
    Each session carries the current playback progress so that watched
    detection works per-session (and therefore also on rewatches)."""
    streams = {}

    # --- Plex ---
    if cfg("ENABLE_PLEX", as_bool=True):
        headers = {'X-Plex-Token': cfg("PLEX_TOKEN"), 'Accept': 'application/json'}
        errors = []
        data = api_get(f"{cfg('PLEX_URL')}/status/sessions", headers, errors=errors)
        ok = isinstance(data, dict) and 'MediaContainer' in data
        if data is not None and not ok:
            errors.append("unexpected response")
        _note_api("Plex", ok, errors)
        if ok:
            for item in data['MediaContainer'].get('Metadata', []):
                rk = item.get('ratingKey')
                # The session names the file it plays. The ratingKey cache is a
                # fallback for sessions that do not: an item upgraded to a
                # better release keeps its ratingKey, and the cached path
                # would point at the file that has since been replaced.
                path = _first_part_file(item.get('Media')) or metadata_cache.get(rk)

                if not path and rk:
                    meta = api_get(f"{cfg('PLEX_URL')}/library/metadata/{rk}", headers)
                    if meta and 'MediaContainer' in meta:
                        for m in meta['MediaContainer'].get('Metadata', []):
                            path = _first_part_file(m.get('Media'))
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
    for enabled_key, api_key, url_key, name, label in [
        ("ENABLE_EMBY",     "EMBY_API_KEY",     "EMBY_URL",     "emby",     "Emby"),
        ("ENABLE_JELLYFIN", "JELLYFIN_API_KEY", "JELLYFIN_URL", "jellyfin", "Jellyfin"),
    ]:
        if cfg(enabled_key, as_bool=True):
            headers = {'X-Emby-Token': cfg(api_key), 'Accept': 'application/json'}
            errors = []
            data = api_get(f"{cfg(url_key)}/Sessions", headers, errors=errors)
            ok = isinstance(data, list)
            if data is not None and not ok:
                errors.append("unexpected response")
            _note_api(label, ok, errors)
            if ok:
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
        if f.startswith('.'):
            continue
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

    # Smart cleanup: send older episodes back - the ones this plugin cached.
    # The season folder can also hold a download waiting for Unraid's mover.
    if cfg("CLEANUP_MODE").lower() == "smart" and os.path.isdir(cache_season_dir):
        threshold = episode - cfg("EPISODE_KEEP_PREVIOUS", as_int=True)
        tracked = TrackedFiles.load()
        old = []
        try:
            for f in sorted(os.listdir(cache_season_dir)):
                ep = parse_episode(f)
                path = os.path.join(cache_season_dir, f)
                if ep is not None and ep < threshold and path in tracked:
                    old.append(path)
        except OSError:
            pass
        request_moves(old, "Smart Cleanup")

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

def _stream_array_path(docker_path):
    """The array path a stream plays, or None if it is nothing to cache."""
    array_path = translate_docker_path(docker_path)
    if not _under(array_path, cfg("ARRAY_ROOT")):
        return None
    if is_excluded(array_path) or not is_media_file(os.path.basename(array_path)):
        return None
    return array_path

def request_moves(paths, label):
    """Hand files Auto Cleanup picked to the mover.

    What is being played or copied right now is left out already here - the
    mover checks again, but asking it for what it would only skip fills the log
    for nothing. A file asked for recently is not asked for again: the main
    loop comes round every few seconds, and the move may not have happened
    yet."""
    if not paths:
        return
    now = time.time()
    for path, when in list(_requested.items()):
        if now - when >= MOVE_RETRY:
            del _requested[path]
    with _pending_lock:
        pending = {array_to_cache(p) for p in _pending_copies}

    fresh = []
    for path in dict.fromkeys(paths):
        if path in _requested or path in active_cache_paths or path in pending:
            continue
        _requested[path] = now
        fresh.append(path)
        log(f"[{label}] {os.path.basename(path)}")
    queue_moves("auto", fresh)

def _smart_cleanup(last_streams, streams):
    """Smart Cleanup for streams that ended: a watched movie goes back to the
    array after MOVIE_DELETE_DELAY, and so does a season once its finale was
    watched. Only files this plugin cached: a fresh download waiting for the
    mover, or a file somebody keeps on the cache on purpose, is not ours."""
    tracked = None
    for docker_path in set(last_streams) - set(streams):
        session    = last_streams[docker_path]
        array_path = translate_docker_path(docker_path)
        if not is_watched(session):
            continue
        cache_path = array_to_cache(array_path)
        if not os.path.exists(cache_path):
            continue
        if tracked is None:
            tracked = TrackedFiles.load()

        ep = parse_episode(os.path.basename(array_path))
        if ep is None:
            if cache_path in tracked:
                deletion_queue[cache_path] = time.time()
            continue

        # If this was the last episode in the season folder, queue the whole
        # season for deletion.
        folder = os.path.dirname(array_path)
        max_ep = None
        if os.path.exists(folder):
            try:
                eps = [parse_episode(f) for f in os.listdir(folder)]
                eps = [e for e in eps if e is not None]
                max_ep = max(eps) if eps else None
            except OSError as e:
                # Cannot tell whether this was the finale. Assuming it was
                # would evict the whole season on a transient listing error.
                log(f"Cannot read {folder}: {e} - not treating "
                    f"episode {ep} as the season finale", warn=True)
                max_ep = None
        if max_ep is not None and ep >= max_ep:
            cache_dir = os.path.dirname(cache_path)
            try:
                for f in os.listdir(cache_dir):
                    candidate = os.path.join(cache_dir, f)
                    if candidate in tracked:
                        deletion_queue[candidate] = time.time()
            except OSError:
                pass

    # pop() rather than del: the copy worker takes entries out of the queue too
    # when a file is played again, and a del racing it raised KeyError.
    delay = cfg("MOVIE_DELETE_DELAY", as_int=True)
    now = time.time()
    due = [p for p, queued in list(deletion_queue.items()) if now - queued > delay]
    for path in due:
        deletion_queue.pop(path, None)
    request_moves([p for p in due if os.path.exists(p)], "Cleanup")

# =============================================================================
# STATUS SNAPSHOT (read by the web UI and the mover)
# =============================================================================

def write_status(protected=frozenset()):
    """Write a small JSON snapshot to STATUS_FILE (atomic replace).
    The web UI polls this to show cache/queue state; the mover reads
    `protected` so it leaves alone what is playing or being copied."""
    cached_files = 0
    cached_bytes = 0
    for path in TrackedFiles.load():
        try:
            cached_bytes += os.path.getsize(path)
            cached_files += 1
        except OSError:
            continue

    space = _pool_space()
    usage_pct = round(space[0] / space[1] * 100, 1) if space and space[1] else None

    with _pending_lock:
        queue_length = len(_pending_copies)

    _write_json(STATUS_FILE, {
        "updated":         int(time.time()),
        "cached_files":    cached_files,
        "cached_bytes":    cached_bytes,
        "cache_usage_pct": usage_pct,
        "queue_length":    queue_length,
        "copying":         _current_copy,
        "active_streams":  sorted(os.path.basename(p) for p in stream_timers),
        "stream_paths":    sorted(active_cache_paths),
        "protected":       sorted(protected),
    })

# =============================================================================
# DAEMON
# =============================================================================

def _daemon_pass(state):
    """One round of the main loop. Returns the cache paths the mover must not
    touch right now: what is being played and what is being copied."""
    global active_cache_paths

    ready = _storage_ready()
    if ready != state["storage"]:
        state["storage"] = ready
        if ready:
            reconcile_tracked_files()
        else:
            log("Waiting for the cache pool and the array to be mounted")
    if not ready:
        return frozenset()

    streams = get_active_streams()
    active_paths = {p for p in map(_stream_array_path, streams) if p}
    # Protect first, act second: the handlers below decide what goes back to
    # the array, and they have to see this pass's streams, not the last one's.
    active_cache_paths = {array_to_cache(p) for p in active_paths}

    now = time.time()
    for array_path in sorted(active_paths):
        if array_path not in stream_timers:
            log(f"[Stream] Active: {os.path.basename(array_path)}")
            stream_timers[array_path] = now
        elif now - stream_timers[array_path] >= cfg("COPY_DELAY", as_int=True):
            if parse_episode(os.path.basename(array_path)) is not None:
                handle_series(array_path)
            else:
                handle_movie(array_path)

    # Remove inactive streams from the timer map
    for path in list(stream_timers):
        if path not in active_paths:
            del stream_timers[path]

    cleanup_mode = cfg("CLEANUP_MODE").lower()
    if cleanup_mode == "smart":
        _smart_cleanup(state["last_streams"], streams)
    elif cleanup_mode == "days" and now - state["last_days_check"] > 3600:
        # At most once an hour
        max_age = cfg("CACHE_MAX_DAYS", as_int=True) * 86400
        request_moves([p for p, cached in TrackedFiles.load().items()
                       if now - cached > max_age and os.path.exists(p)], "Days Cleanup")
        state["last_days_check"] = now
    state["last_streams"] = streams

    with _pending_lock:
        pending = {array_to_cache(p) for p in _pending_copies}
    return frozenset(active_cache_paths | pending)

def run_daemon():
    """Main daemon loop."""
    setup_logging()
    load_config()

    # Acquire lock
    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.lockf(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Another instance is already running", error=True)
        sys.exit(1)

    signal.signal(signal.SIGHUP,  lambda s, f: load_config())
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    worker = threading.Thread(target=copy_worker, name="copy-worker", daemon=True)
    worker.start()

    log("Service started. Waiting for streams...")
    if cfg("ENABLE_EPISODE_BATCHING", as_bool=True):
        log(f"Season batching enabled: batch={cfg('EPISODE_BATCH_SIZE', as_int=True)}, "
            f"tolerance={cfg('EPISODE_BATCH_TOLERANCE', as_int=True)}, "
            f"prefetch={cfg('EPISODE_BATCH_PREFETCH', as_int=True)}")

    state = {"storage": None, "last_streams": {}, "last_days_check": 0}
    last_protected    = None
    last_status_write = 0.0

    while True:
        try:
            protected = _daemon_pass(state)
            # The mover reads `protected` from the snapshot, so it is written
            # the moment that changes, not only on the interval.
            if protected != last_protected or time.time() - last_status_write >= STATUS_INTERVAL:
                write_status(protected)
                last_protected    = protected
                last_status_write = time.time()
        except Exception as e:
            log(f"Loop error: {e}", error=True)

        time.sleep(max(1, cfg("CHECK_INTERVAL", as_int=True)))

def _shutdown(signum=None, frame=None):
    """SIGTERM/SIGINT: stop copying and exit, so rc.d stop reports cleanly and
    a running rsync doesn't linger as an orphan. The mover is a process of its
    own and carries on."""
    _stop_transfers()
    try:
        log("Service stopped.")
    except Exception:
        pass
    sys.exit(0)

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--move"]:
        sys.exit(run_mover())
    if args:
        sys.exit(f"usage: {sys.argv[0]} [--move]")
    run_daemon()
