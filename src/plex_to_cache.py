#!/usr/bin/python3
"""
Plex to Cache - Automatic media caching daemon for Unraid
Moves actively streamed media from array to cache for faster playback.
Supports Plex, Emby, and Jellyfin.

No external Python dependencies — uses only the standard library
(urllib/ssl/json). This keeps the plugin offline-bootable: Unraid's
root filesystem is a tmpfs, so anything in /usr/local/lib/pythonX/
site-packages gets wiped on reboot. Relying on `requests` means the
daemon would need internet on every boot to reinstall it.
"""

import os
import sys
import re
import time
import json
import ssl
import fcntl
import shutil
import signal
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

DEFAULT_CONFIG = {
    "ENABLE_PLEX": "False", "PLEX_URL": "http://localhost:32400", "PLEX_TOKEN": "",
    "ENABLE_EMBY": "False", "EMBY_URL": "http://localhost:8096", "EMBY_API_KEY": "",
    "ENABLE_JELLYFIN": "False", "JELLYFIN_URL": "http://localhost:8096", "JELLYFIN_API_KEY": "",
    "CHECK_INTERVAL": "10", "CACHE_MAX_USAGE": "80", "COPY_DELAY": "30",
    "CLEANUP_MODE": "none", "MOVIE_DELETE_DELAY": "1800", "EPISODE_KEEP_PREVIOUS": "2",
    "CACHE_MAX_DAYS": "7", "EXCLUDE_DIRS": "", "MEDIA_FILETYPES": ".mkv .mp4 .avi",
    "ARRAY_ROOT": "/mnt/user", "CACHE_ROOT": "/mnt/cache", "DOCKER_MAPPINGS": ""
}

# Runtime state
config          = dict(DEFAULT_CONFIG)
docker_mappings = {}
metadata_cache  = {}
stream_timers   = {}
deletion_queue  = {}

# SSL context: Plex / Emby / Jellyfin typically use self-signed certs on the
# local network, so we intentionally skip verification. This is equivalent
# to the old `verify=False` on the `requests` calls.
_SSL_CTX = ssl._create_unverified_context()

# =============================================================================
# UTILITIES
# =============================================================================

def log(msg, error=False):
    prefix = "[Error] " if error else ""
    print(f"{prefix}{msg}", flush=True)

def rotate_log_if_needed():
    """Rotate /var/log/plex_to_cache.log if it grew past LOG_MAX_BYTES.
    Keeps one previous copy as .log.1. Called once at daemon start."""
    try:
        if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            rotated = LOG_FILE + ".1"
            if os.path.exists(rotated):
                os.remove(rotated)
            os.rename(LOG_FILE, rotated)
    except OSError:
        pass

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
        return val.lower() in ("true", "1")
    if as_int:
        try:    return int(val)
        except (ValueError, TypeError): return 0
    return val

# =============================================================================
# FILE TRACKING
# =============================================================================

class TrackedFiles:
    """Manages the list of plugin-cached files with timestamps."""

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
        """Save tracked files dict to disk."""
        try:
            content = '\n'.join(f"{p}|{t}" for p, t in sorted(tracked.items()))
            Path(TRACKED_FILES).write_text(content + '\n' if content else '')
        except OSError as e:
            log(f"Tracking save failed: {e}", error=True)

    @staticmethod
    def add(path):
        tracked = TrackedFiles.load()
        if path not in tracked:
            tracked[path] = time.time()
            TrackedFiles.save(tracked)

    @staticmethod
    def remove(path):
        tracked = TrackedFiles.load()
        if path in tracked:
            del tracked[path]
            TrackedFiles.save(tracked)

    @staticmethod
    def clear():
        TrackedFiles.save({})

# =============================================================================
# PATH UTILITIES
# =============================================================================

def cache_to_array(cache_path):
    """Convert cache path (/mnt/cache/x) to physical array path (/mnt/user0/x)."""
    return cache_path.replace(cfg("CACHE_ROOT"), ARRAY_ROOT, 1)

def array_to_cache(array_path):
    """Convert user share path (/mnt/user/x) to cache path (/mnt/cache/x)."""
    return array_path.replace(cfg("ARRAY_ROOT"), cfg("CACHE_ROOT"), 1)

def array_share_to_cache(host_path):
    """Translate a user-share host path to its cache equivalent.
    Same as array_to_cache but keeps the result when already on cache."""
    if host_path.startswith(cfg("CACHE_ROOT")):
        return host_path
    return array_to_cache(host_path)

def translate_docker_path(docker_path):
    """Translate docker container path to host path."""
    path = docker_path.replace('\\', '/')
    for docker_prefix, host_prefix in docker_mappings.items():
        if path.startswith(docker_prefix):
            rel = path[len(docker_prefix):].lstrip('/')
            base = host_prefix if host_prefix.startswith(cfg("ARRAY_ROOT")) \
                   else os.path.join(cfg("ARRAY_ROOT"), host_prefix.lstrip('/'))
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

def parse_episode(filename):
    """Extract episode number from filename. Returns None if not an episode."""
    match = re.search(r"[sS]\d+[eE](\d+)", filename)
    return int(match.group(1)) if match else None

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

def rsync_move(src, dst, remove_source=True):
    """Move (or copy) a file using rsync --inplace."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    cmd = ["rsync", "-a", "--inplace"]
    if remove_source:
        cmd.append("--remove-source-files")
    cmd.extend([src, dst])
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
        rsync_move(cache_path, array_path)
        clone_permissions(array_path)
        cleanup_empty_dirs(cache_path)
        if track:
            TrackedFiles.remove(cache_path)
        return True, False, size

    except (OSError, subprocess.CalledProcessError) as e:
        log(f"Move failed: {e}", error=True)
        return False, False, 0

def copy_file_to_cache(array_path):
    """Copy file from array to cache."""
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

    # Check disk space
    try:
        usage = shutil.disk_usage(cfg("CACHE_ROOT"))
        if (usage.used / usage.total) * 100 >= cfg("CACHE_MAX_USAGE", as_int=True):
            return
    except OSError:
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

        rsync_move(array_path, cache_path, remove_source=False)
        clone_permissions(cache_path)
        TrackedFiles.add(cache_path)
    except (OSError, subprocess.CalledProcessError) as e:
        log(f"Copy failed: {e}", error=True)

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

def get_active_streams():
    """Get currently playing files from all enabled services."""
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
                    metadata_cache[rk] = path
                    streams[path] = {'service': 'plex', 'id': rk}

    # --- Emby / Jellyfin ---
    for enabled_key, api_key, url_key, name in [
        ("ENABLE_EMBY",     "EMBY_API_KEY",     "EMBY_URL",     "emby"),
        ("ENABLE_JELLYFIN", "JELLYFIN_API_KEY", "JELLYFIN_URL", "jellyfin"),
    ]:
        if cfg(enabled_key, as_bool=True):
            headers = {'X-Emby-Token': cfg(api_key), 'Accept': 'application/json'}
            data = api_get(f"{cfg(url_key)}/Sessions", headers)
            if data:
                for s in data:
                    item = s.get('NowPlayingItem', {}) or {}
                    if item.get('Path'):
                        streams[item['Path']] = {
                            'service': name,
                            'id':      item.get('Id'),
                            'user':    s.get('UserId'),
                        }

    return streams

def is_watched(session):
    """Check if media item has been watched."""
    if not session:
        return False

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
# STREAM HANDLERS
# =============================================================================

def handle_movie(array_path):
    """Handle movie caching - cache main file and related side-car files."""
    copy_file_to_cache(array_path)

    folder = os.path.dirname(array_path)
    stem   = os.path.splitext(os.path.basename(array_path))[0]

    if os.path.exists(folder):
        try:
            for f in os.listdir(folder):
                if f.startswith(stem):
                    copy_file_to_cache(os.path.join(folder, f))
        except OSError:
            pass

def handle_series(array_path):
    """Handle series caching - cache current and upcoming episodes."""
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
    if os.path.exists(season_dir):
        try:
            for f in sorted(os.listdir(season_dir)):
                ep = parse_episode(f)
                if ep is not None and ep >= episode:
                    copy_file_to_cache(os.path.join(season_dir, f))
        except OSError:
            pass

# =============================================================================
# DAEMON
# =============================================================================

def run_daemon():
    """Main daemon loop."""
    rotate_log_if_needed()
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
    log("Service started. Waiting for streams...")

    last_streams    = {}
    last_days_check = 0

    while True:
        try:
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
                                if os.path.exists(folder):
                                    try:
                                        max_ep = max(
                                            (parse_episode(f) or 0)
                                            for f in os.listdir(folder)
                                        )
                                    except (OSError, ValueError):
                                        max_ep = ep
                                    if ep >= max_ep:
                                        cache_dir = os.path.dirname(cache_path)
                                        try:
                                            for f in os.listdir(cache_dir):
                                                deletion_queue[os.path.join(cache_dir, f)] = time.time()
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

        except Exception as e:
            log(f"Loop error: {e}", error=True)

        time.sleep(cfg("CHECK_INTERVAL", as_int=True))

def _shutdown_cleanup():
    """Called on SIGTERM so systemd/rc.d stop reports cleanly."""
    try:
        log("Service stopped.")
    except Exception:
        pass

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    run_daemon()
