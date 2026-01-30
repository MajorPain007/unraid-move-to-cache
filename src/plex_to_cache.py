#!/usr/bin/python3
import requests
import os
import shutil
import subprocess
import time
import sys
import re
import fcntl
import signal
import urllib3
from pathlib import Path

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# --- CONFIGURATION DEFAULTS ---
# ==========================================

CONFIG_FILE = "/boot/config/plugins/plex_to_cache/settings.cfg"
TRACKED_FILES = "/boot/config/plugins/plex_to_cache/cached_files.list"
LOCK_FILE_PATH = "/tmp/media_cache_cleaner.lock"

# Hardcoded Perms Root (Physical Disks ONLY - No Cache)
# We use /mnt/user0 to verify if a file is really on the array.
PERMS_ROOT = "/mnt/user0"

CONFIG = {
    "ENABLE_PLEX": "False",
    "PLEX_URL": "http://localhost:32400",
    "PLEX_TOKEN": "",

    "ENABLE_EMBY": "False",
    "EMBY_URL": "http://localhost:8096",
    "EMBY_API_KEY": "",

    "ENABLE_JELLYFIN": "False",
    "JELLYFIN_URL": "http://localhost:8096",
    "JELLYFIN_API_KEY": "",

    "CHECK_INTERVAL": "10",
    "CACHE_MAX_USAGE": "80",
    "COPY_DELAY": "30",
    "CLEANUP_MODE": "none",  # "none", "smart", or "days"
    "MOVIE_DELETE_DELAY": "1800",
    "EPISODE_KEEP_PREVIOUS": "2",
    "CACHE_MAX_DAYS": "7",  # Days before files are moved back to array
    "EXCLUDE_DIRS": "",
    "MEDIA_FILETYPES": ".mkv .mp4 .avi",
    "ARRAY_ROOT": "/mnt/user",
    "CACHE_ROOT": "/mnt/cache",
    "DOCKER_MAPPINGS": ""
}

# --- INTERNAL VARIABLES ---
movie_deletion_queue = {}
stream_start_times = {}
metadata_path_cache = {}
parsed_docker_mappings = {}

# ---------------------

def log_info(msg):
    print(msg, flush=True)

def log_error(msg):
    print(f"[Error] {msg}", flush=True)

# --- TRACKING FUNCTIONS ---

def load_tracked_files():
    """Load tracked files with timestamps. Format: path|timestamp"""
    tracked = {}
    if os.path.exists(TRACKED_FILES):
        try:
            with open(TRACKED_FILES, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if '|' in line:
                        path, ts = line.rsplit('|', 1)
                        tracked[path] = float(ts)
                    else:
                        # Legacy format without timestamp
                        tracked[line] = time.time()
        except Exception as e:
            log_error(f"Failed to load tracked files: {e}")
    return tracked

def save_tracked_files(tracked):
    """Save tracked files with timestamps."""
    try:
        with open(TRACKED_FILES, 'w') as f:
            for path, ts in sorted(tracked.items()):
                f.write(f"{path}|{ts}\n")
    except Exception as e:
        log_error(f"Failed to save tracked files: {e}")

def track_cached_file(cache_path):
    """Add a file to the tracking list with current timestamp."""
    tracked = load_tracked_files()
    if cache_path not in tracked:
        tracked[cache_path] = time.time()
        save_tracked_files(tracked)

def untrack_cached_file(cache_path):
    """Remove a file from the tracking list."""
    tracked = load_tracked_files()
    if cache_path in tracked:
        del tracked[cache_path]
        save_tracked_files(tracked)

def get_tracked_files_set():
    """Get just the file paths as a set (for compatibility)."""
    return set(load_tracked_files().keys())

def parse_docker_mappings(mapping_str):
    mappings = {}
    if not mapping_str:
        return mappings
    pairs = mapping_str.split(';')
    for pair in pairs:
        if ':' in pair:
            k, v = pair.split(':', 1)
            mappings[k.strip()] = v.strip()
    return mappings

def load_config():
    global CONFIG, parsed_docker_mappings
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"): continue
                    if "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        CONFIG[key] = value
        except Exception as e:
            log_error(f"Failed to load config: {e}")

    parsed_docker_mappings = parse_docker_mappings(CONFIG.get("DOCKER_MAPPINGS", ""))

def get_config_bool(key):
    val = CONFIG.get(key, "False")
    return val.lower() == "true" or val == "1"

def get_config_int(key):
    try: return int(CONFIG.get(key, 0))
    except: return 0

def acquire_lock():
    global lock_file
    lock_file = open(LOCK_FILE_PATH, 'w')
    try:
        fcntl.lockf(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        log_error("Another instance is already running")
        sys.exit(1)

# --- PERMISSIONS LOGIC ---

def get_perms_reference_path(cache_path):
    CACHE_ROOT = CONFIG["CACHE_ROOT"]
    if cache_path.startswith(CACHE_ROOT):
        return cache_path.replace(CACHE_ROOT, PERMS_ROOT, 1)
    return None

def clone_rights_from_disk(dst_path):
    src_path = get_perms_reference_path(dst_path)
    if not src_path or not os.path.exists(src_path):
        return
    try:
        st = os.stat(src_path)
        os.chown(dst_path, st.st_uid, st.st_gid)
        os.chmod(dst_path, st.st_mode)
    except Exception as e:
        log_error(f"Failed to clone permissions: {e}")

# --- HELPERS ---

def is_excluded(path):
    EXCLUDE_DIRS = CONFIG["EXCLUDE_DIRS"]
    if not EXCLUDE_DIRS: return False
    path_parts = path.split(os.sep)
    excludes = [x.strip() for x in EXCLUDE_DIRS.split(',')]
    for exc in excludes:
        if exc and exc in path_parts: return True
    return False

def is_trigger_filetype(filename):
    MEDIA_FILETYPES = CONFIG["MEDIA_FILETYPES"]
    if not MEDIA_FILETYPES: return True
    valid_exts = [x.strip().lower() for x in MEDIA_FILETYPES.split()]
    lower_name = filename.lower()
    for ext in valid_exts:
        if lower_name.endswith(ext): return True
    return False

# --- API CLIENTS ---

def plex_api_get(endpoint):
    headers = {'X-Plex-Token': CONFIG["PLEX_TOKEN"], 'Accept': 'application/json'}
    try:
        r = requests.get(f"{CONFIG['PLEX_URL']}{endpoint}", headers=headers, timeout=5, verify=False)
        return r.json()
    except Exception as e:
        log_error(f"Plex API request failed: {e}")
        return None

def emby_api_get(endpoint, key, url):
    headers = {'X-Emby-Token': key, 'Accept': 'application/json'}
    try:
        r = requests.get(f"{url}{endpoint}", headers=headers, timeout=5, verify=False)
        return r.json()
    except Exception as e:
        log_error(f"Emby/Jellyfin API request failed: {e}")
        return None

def get_active_sessions():
    active_items = {}
    if get_config_bool("ENABLE_PLEX"):
        data = plex_api_get("/status/sessions")
        if data and 'MediaContainer' in data and 'Metadata' in data['MediaContainer']:
            for item in data['MediaContainer']['Metadata']:
                rk = item.get('ratingKey')
                found = metadata_path_cache.get(rk)
                if not found and 'Media' in item:
                    for media in item['Media']:
                        for part in media.get('Part', []):
                            if part.get('file'): found = part['file']; break
                if not found and rk:
                    meta = plex_api_get(f"/library/metadata/{rk}")
                    if meta and 'MediaContainer' in meta and 'Metadata' in meta['MediaContainer']:
                        for m in meta['MediaContainer']['Metadata']:
                            for med in m.get('Media', []):
                                for p in med.get('Part', []):
                                    if p.get('file'): found = p['file']; break
                if found:
                    metadata_path_cache[rk] = found
                    active_items[found] = {'service': 'plex', 'id': rk}

    for svc in [("ENABLE_EMBY", "EMBY_API_KEY", "EMBY_URL", "emby"), ("ENABLE_JELLYFIN", "JELLYFIN_API_KEY", "JELLYFIN_URL", "jellyfin")]:
        if get_config_bool(svc[0]):
            data = emby_api_get("/Sessions", CONFIG[svc[1]], CONFIG[svc[2]])
            if data:
                for s in data:
                    p = s.get('NowPlayingItem', {}).get('Path')
                    if p: active_items[p] = {'service': svc[3], 'id': s['NowPlayingItem'].get('Id'), 'user': s.get('UserId')}
    return active_items

def check_is_watched(session_data):
    if not session_data: return False
    service = session_data.get('service')
    if service == 'plex':
        rk = session_data.get('id')
        data = plex_api_get(f"/library/metadata/{rk}")
        if data and 'MediaContainer' in data and 'Metadata' in data['MediaContainer']:
            meta = data['MediaContainer']['Metadata'][0]
            if 'viewCount' in meta and meta['viewCount'] > 0: return True
    elif service in ['emby', 'jellyfin']:
        u = CONFIG["EMBY_URL"] if service == 'emby' else CONFIG["JELLYFIN_URL"]
        k = CONFIG["EMBY_API_KEY"] if service == 'emby' else CONFIG["JELLYFIN_API_KEY"]
        d = emby_api_get(f"/Users/{session_data.get('user')}/Items/{session_data.get('id')}", k, u)
        if d and 'UserData' in d: return d['UserData'].get('Played', False)
    return False

# --- PATH TOOLS ---

def translate_path(docker_path):
    clean_path = docker_path.replace('\\', '/')
    ARRAY_ROOT = CONFIG["ARRAY_ROOT"]
    for d_prefix, real_prefix in parsed_docker_mappings.items():
        if clean_path.startswith(d_prefix):
            rel_path = clean_path[len(d_prefix):].lstrip('/')
            return os.path.join(real_prefix if real_prefix.startswith(ARRAY_ROOT) else os.path.join(ARRAY_ROOT, real_prefix.lstrip('/')), rel_path).replace('//', '/')
    return clean_path

def get_cache_path(array_path):
    ARRAY_ROOT = CONFIG["ARRAY_ROOT"]
    CACHE_ROOT = CONFIG["CACHE_ROOT"]
    if array_path.startswith(ARRAY_ROOT): return array_path.replace(ARRAY_ROOT, CACHE_ROOT, 1)
    return None

def parse_episode_number(filename):
    match = re.search(r"[sS]\d+[eE](\d+)", filename)
    return int(match.group(1)) if match else None

def is_last_episode_on_array(file_path):
    try:
        ep_num = parse_episode_number(os.path.basename(file_path))
        if ep_num is None: return False
        folder = os.path.dirname(file_path)
        max_ep = 0
        if os.path.exists(folder):
            for f in os.listdir(folder):
                e = parse_episode_number(f)
                if e and e > max_ep: max_ep = e
        return ep_num >= max_ep
    except Exception as e:
        log_error(f"Failed to check last episode: {e}")
        return False

# --- MOVER / DELETE LOGIC ---

def cleanup_empty_parent_dirs(path):
    parent_dir = os.path.dirname(path)
    CACHE_ROOT = CONFIG["CACHE_ROOT"]
    protected = [os.path.join(CACHE_ROOT, m.strip("/")) for m in parsed_docker_mappings.values()]
    while parent_dir.startswith(CACHE_ROOT) and len(parent_dir) > len(CACHE_ROOT):
        if parent_dir in protected: break
        try:
            os.rmdir(parent_dir)
            parent_dir = os.path.dirname(parent_dir)
        except OSError: break

def move_to_array(cache_path):
    CACHE_ROOT = CONFIG["CACHE_ROOT"]
    rel_path = cache_path.replace(CACHE_ROOT, "").lstrip("/")
    dest_path = os.path.join(PERMS_ROOT, rel_path)

    log_info(f"[Mover] -> Array: {os.path.basename(cache_path)}")
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        subprocess.run(["rsync", "-a", "--inplace", "--remove-source-files", cache_path, dest_path], check=True, stdout=subprocess.DEVNULL)
        clone_rights_from_disk(dest_path)
        cleanup_empty_parent_dirs(cache_path)
        untrack_cached_file(cache_path)
    except Exception as e:
        log_error(f"Move to array failed: {e}")

def smart_manage_cache_file(cache_path, reason="Cleanup"):
    if not os.path.exists(cache_path): return
    rel_path = cache_path.replace(CONFIG["CACHE_ROOT"], "").lstrip("/")
    array_path = os.path.join(PERMS_ROOT, rel_path)

    # We check against PERMS_ROOT (/mnt/user0) to be absolutely sure the file is on the array
    if os.path.exists(array_path):
        if os.path.getsize(cache_path) == os.path.getsize(array_path):
            os.remove(cache_path)
            log_info(f"[{reason}] Deleted: {os.path.basename(cache_path)}")
            cleanup_empty_parent_dirs(cache_path)
            untrack_cached_file(cache_path)
    else:
        # Not on array yet, so move it instead of just deleting
        move_to_array(cache_path)

def ensure_structure(full_cache_file_path):
    CACHE_ROOT = CONFIG["CACHE_ROOT"]
    try: rel_path = os.path.relpath(os.path.dirname(full_cache_file_path), CACHE_ROOT)
    except: return
    curr = CACHE_ROOT
    for part in rel_path.split(os.sep):
        if not part or part == ".": continue
        curr = os.path.join(curr, part)
        if not os.path.exists(curr):
            try:
                os.mkdir(curr)
                clone_rights_from_disk(curr)
            except Exception as e:
                log_error(f"Failed to create directory {curr}: {e}")

def cache_file_if_needed(source_path):
    rel_path = source_path.replace(CONFIG["ARRAY_ROOT"], "").lstrip("/")
    if not rel_path or is_excluded(rel_path): return
    dest_path = os.path.join(CONFIG["CACHE_ROOT"], rel_path)

    if os.path.exists(dest_path) and os.path.getsize(source_path) == os.path.getsize(dest_path):
        if dest_path in movie_deletion_queue: del movie_deletion_queue[dest_path]
        # Make sure it's tracked even if already exists
        track_cached_file(dest_path)
        return

    try:
        usage = shutil.disk_usage(CONFIG["CACHE_ROOT"])
        if (usage.used / usage.total) * 100 >= get_config_int("CACHE_MAX_USAGE"): return
    except Exception as e:
        log_error(f"Failed to check disk usage: {e}")
        return

    log_info(f"[Copy] -> {os.path.basename(source_path)}")
    try:
        ensure_structure(dest_path)
        subprocess.run(["rsync", "-a", "--inplace", source_path, dest_path], check=True, stdout=subprocess.DEVNULL)
        clone_rights_from_disk(dest_path)
        track_cached_file(dest_path)
    except Exception as e:
        log_error(f"Copy failed: {e}")

# --- HANDLER ---

def handle_series_smart(rp):
    curr_ep = parse_episode_number(os.path.basename(rp))
    if curr_ep is None: return handle_movie_logic(rp)
    sd_array = os.path.dirname(rp)
    sd_cache = get_cache_path(sd_array)
    cleanup_mode = CONFIG.get("CLEANUP_MODE", "none").lower()
    if cleanup_mode == "smart" and sd_cache and os.path.exists(sd_cache):
        th = curr_ep - get_config_int("EPISODE_KEEP_PREVIOUS")
        for f in os.listdir(sd_cache):
            num = parse_episode_number(f)
            if num is not None and num < th:
                smart_manage_cache_file(os.path.join(sd_cache, f), "Smart Cleanup")
    try:
        if os.path.exists(sd_array):
            for f in sorted(os.listdir(sd_array)):
                num = parse_episode_number(f)
                if num is not None and num >= curr_ep: cache_file_if_needed(os.path.join(sd_array, f))
    except Exception as e:
        log_error(f"Series handling failed: {e}")

def handle_movie_logic(rp):
    cache_file_if_needed(rp)
    try:
        folder, stem = os.path.dirname(rp), os.path.splitext(os.path.basename(rp))[0]
        if os.path.exists(folder):
            for f in os.listdir(folder):
                if f.startswith(stem): cache_file_if_needed(os.path.join(folder, f))
    except Exception as e:
        log_error(f"Movie handling failed: {e}")

def cleanup_by_days():
    """Move files back to array if they've been cached longer than CACHE_MAX_DAYS."""
    max_days = get_config_int("CACHE_MAX_DAYS")
    if max_days <= 0:
        return

    max_age_seconds = max_days * 24 * 60 * 60
    tracked = load_tracked_files()
    now = time.time()

    for cache_path, cached_time in list(tracked.items()):
        age = now - cached_time
        if age > max_age_seconds:
            if os.path.exists(cache_path):
                log_info(f"[Days Cleanup] {os.path.basename(cache_path)} cached for {int(age/86400)} days")
                smart_manage_cache_file(cache_path, "Days Cleanup")

if __name__ == "__main__":
    load_config(); acquire_lock()
    signal.signal(signal.SIGHUP, lambda s, f: load_config())
    log_info("Service started. Waiting for streams...")
    last_loop_sessions = {}
    last_days_check = 0
    while True:
        try:
            curr_sessions = get_active_sessions()
            active_paths = []
            for dp, s_data in curr_sessions.items():
                rp = translate_path(dp)
                if not rp.startswith(CONFIG["ARRAY_ROOT"]) or is_excluded(rp) or not is_trigger_filetype(os.path.basename(rp)): continue
                active_paths.append(rp)
                if rp not in stream_start_times:
                    log_info(f"[Stream] Active: {os.path.basename(rp)}")
                    stream_start_times[rp] = time.time(); continue
                if time.time() - stream_start_times[rp] >= get_config_int("COPY_DELAY"):
                    if parse_episode_number(os.path.basename(rp)) is not None: handle_series_smart(rp)
                    else: handle_movie_logic(rp)
            for p in list(stream_start_times.keys()):
                if p not in set(active_paths): del stream_start_times[p]

            cleanup_mode = CONFIG.get("CLEANUP_MODE", "none").lower()

            # Smart cleanup: triggered by watching behavior
            if cleanup_mode == "smart":
                stopped = set(last_loop_sessions.keys()) - set(curr_sessions.keys())
                for d_p in stopped:
                    s_d, r_p = last_loop_sessions[d_p], translate_path(d_p)
                    if check_is_watched(s_d):
                        cp = get_cache_path(r_p)
                        if cp and os.path.exists(cp):
                            if parse_episode_number(os.path.basename(r_p)) is None: movie_deletion_queue[cp] = time.time()
                            elif is_last_episode_on_array(r_p):
                                for f in os.listdir(os.path.dirname(cp)):
                                    movie_deletion_queue[os.path.join(os.path.dirname(cp), f)] = time.time()
                for cp, ts in list(movie_deletion_queue.items()):
                    if time.time() - ts > get_config_int("MOVIE_DELETE_DELAY"):
                        smart_manage_cache_file(cp, "Deletion Timer"); del movie_deletion_queue[cp]

            # Days-based cleanup: check once per hour
            elif cleanup_mode == "days":
                if time.time() - last_days_check > 3600:
                    cleanup_by_days()
                    last_days_check = time.time()

            last_loop_sessions = curr_sessions
        except Exception as e:
            log_error(f"Main loop error: {e}")
        time.sleep(get_config_int("CHECK_INTERVAL"))
