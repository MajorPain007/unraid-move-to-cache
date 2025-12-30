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
from pathlib import Path

# ==========================================
# --- CONFIGURATION DEFAULTS ---
# ==========================================

CONFIG_FILE = "/boot/config/plugins/plex_to_cache/settings.cfg"
LOCK_FILE_PATH = "/tmp/media_cache_cleaner.lock"

# Hardcoded Perms Root (Physical Disks)
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
    "ENABLE_SMART_CLEANUP": "False",
    "MOVIE_DELETE_DELAY": "1800",
    "EPISODE_KEEP_PREVIOUS": "2",
    "EXCLUDE_DIRS": "",
    "MEDIA_FILETYPES": ".mkv .mp4 .avi",
    "ARRAY_ROOT": "/mnt/user",
    "CACHE_ROOT": "/mnt/cache",
    "DOCKER_MAPPINGS": "" 
}

# --- INTERNE VARIABLES ---
movie_deletion_queue = {}
stream_start_times = {} 
metadata_path_cache = {} 
parsed_docker_mappings = {}

# ---------------------

def log_info(msg):
    print(msg, flush=True)

def parse_docker_mappings(mapping_str):
    mappings = {}
    if not mapping_str:
        return mappings
    # Format: docker:host;docker:host
    pairs = mapping_str.split(';')
    for pair in pairs:
        if ':' in pair:
            k, v = pair.split(':', 1)
            mappings[k.strip()] = v.strip()
    return mappings

def load_config():
    global CONFIG, parsed_docker_mappings
    log_info(f"Lade Konfiguration von {CONFIG_FILE}...")
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
            log_info(f"[Error] Konnte Config nicht laden: {e}")
    
    parsed_docker_mappings = parse_docker_mappings(CONFIG.get("DOCKER_MAPPINGS", ""))
    log_info("Konfiguration geladen.")

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
        print("[Error] Skript läuft bereits! Beende diese Instanz.", flush=True)
        sys.exit(1)

# --- RECHTE LOGIK (USER0) ---

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
        log_info(f"[Perms Error] Fehler bei {dst_path}: {e}")

# --- RESTLICHE FUNKTIONEN ---

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
    PLEX_URL = CONFIG["PLEX_URL"]
    PLEX_TOKEN = CONFIG["PLEX_TOKEN"]
    headers = {'X-Plex-Token': PLEX_TOKEN, 'Accept': 'application/json'}
    try:
        r = requests.get(f"{PLEX_URL}{endpoint}", headers=headers, timeout=5, verify=False)
        r.raise_for_status()
        return r.json()
    except Exception: return None

def emby_api_get(endpoint):
    EMBY_URL = CONFIG["EMBY_URL"]
    EMBY_API_KEY = CONFIG["EMBY_API_KEY"]
    headers = {'X-Emby-Token': EMBY_API_KEY, 'Accept': 'application/json'}
    try:
        r = requests.get(f"{EMBY_URL}{endpoint}", headers=headers, timeout=5, verify=False)
        r.raise_for_status()
        return r.json()
    except Exception: return None

def jellyfin_api_get(endpoint):
    # Jellyfin uses same API structure as Emby usually, but lets keep distinct config
    JELLYFIN_URL = CONFIG["JELLYFIN_URL"]
    JELLYFIN_API_KEY = CONFIG["JELLYFIN_API_KEY"]
    headers = {'X-Emby-Token': JELLYFIN_API_KEY, 'Accept': 'application/json'} # Jellyfin often accepts X-Emby-Token or X-MediaBrowser-Token
    try:
        r = requests.get(f"{JELLYFIN_URL}{endpoint}", headers=headers, timeout=5, verify=False)
        r.raise_for_status()
        return r.json()
    except Exception: return None

def check_connection():
    ok = True
    if get_config_bool("ENABLE_PLEX"):
        log_info(f"[Init] Teste Plex ({CONFIG['PLEX_URL']})...")
        if plex_api_get("/identity"): log_info("-> Plex OK")
        else: 
            log_info("[Error] Plex Fehler!")
            ok = False
    
    if get_config_bool("ENABLE_EMBY"):
        log_info(f"[Init] Teste Emby ({CONFIG['EMBY_URL']})...") 
        if emby_api_get("/System/Info"): log_info("-> Emby OK")
        else: 
            log_info("[Error] Emby Fehler!")
            ok = False

    if get_config_bool("ENABLE_JELLYFIN"):
        log_info(f"[Init] Teste Jellyfin ({CONFIG['JELLYFIN_URL']})...") 
        if jellyfin_api_get("/System/Info"): log_info("-> Jellyfin OK")
        else: 
            log_info("[Error] Jellyfin Fehler!")
            ok = False
    return ok

# --- SESSION HANDLING ---

def get_active_sessions():
    active_items = {}
    
    # PLEX
    if get_config_bool("ENABLE_PLEX"):
        data = plex_api_get("/status/sessions")
        if data and 'MediaContainer' in data and 'Metadata' in data['MediaContainer']:
            for item in data['MediaContainer']['Metadata']:
                rating_key = item.get('ratingKey')
                found_file = None
                if rating_key in metadata_path_cache:
                    found_file = metadata_path_cache[rating_key]
                if not found_file and 'Media' in item:
                    for media in item['Media']:
                        if 'Part' in media:
                            for part in media['Part']:
                                if part.get('file'): found_file = part.get('file'); break
                if not found_file and rating_key:
                    meta = plex_api_get(f"/library/metadata/{rating_key}")
                    if meta and 'MediaContainer' in meta and 'Metadata' in meta['MediaContainer']:
                        m = meta['MediaContainer']['Metadata'][0]
                        if 'Media' in m:
                            for media in m['Media']:
                                if 'Part' in media:
                                    for part in media['Part']:
                                        if part.get('file'): found_file = part.get('file'); break
                if found_file:
                    metadata_path_cache[rating_key] = found_file
                    active_items[found_file] = {'service': 'plex', 'id': rating_key}
    
    # EMBY
    if get_config_bool("ENABLE_EMBY"):
        data = emby_api_get("/Sessions")
        if data:
            for s in data:
                if 'NowPlayingItem' in s:
                    item = s['NowPlayingItem']
                    path = item.get('Path')
                    if path:
                        active_items[path] = {'service': 'emby', 'id': item.get('Id'), 'user': s.get('UserId')}

    # JELLYFIN
    if get_config_bool("ENABLE_JELLYFIN"):
        data = jellyfin_api_get("/Sessions")
        if data:
            for s in data:
                if 'NowPlayingItem' in s:
                    item = s['NowPlayingItem']
                    path = item.get('Path')
                    if path:
                        active_items[path] = {'service': 'jellyfin', 'id': item.get('Id'), 'user': s.get('UserId')}
    
    return active_items

def check_is_watched(session_data):
    if not session_data: return False
    service = session_data.get('service')
    
    if service == 'plex':
        r_key = session_data.get('id')
        data = plex_api_get(f"/library/metadata/{r_key}")
        if data and 'MediaContainer' in data and 'Metadata' in data['MediaContainer']:
            meta = data['MediaContainer']['Metadata'][0]
            if 'viewCount' in meta and meta['viewCount'] > 0: return True
            
    elif service == 'emby':
        item_id = session_data.get('id')
        user_id = session_data.get('user')
        if item_id and user_id:
            data = emby_api_get(f"/Users/{user_id}/Items/{item_id}")
            if data and 'UserData' in data: return data['UserData'].get('Played', False)

    elif service == 'jellyfin':
        item_id = session_data.get('id')
        user_id = session_data.get('user')
        if item_id and user_id:
            data = jellyfin_api_get(f"/Users/{user_id}/Items/{item_id}")
            if data and 'UserData' in data: return data['UserData'].get('Played', False)
            
    return False

# --- PFAD TOOLS ---

def translate_path(docker_path):
    clean_path = docker_path.replace('\\', '/')
    ARRAY_ROOT = CONFIG["ARRAY_ROOT"]
    
    for d_prefix, real_prefix in parsed_docker_mappings.items():
        if clean_path.startswith(d_prefix):
            rel_path = clean_path[len(d_prefix):]
            if not rel_path.startswith('/'): rel_path = '/' + rel_path
            final_path = f"{ARRAY_ROOT}{real_prefix}{rel_path}"
            return final_path.replace('//', '/')
    if clean_path.startswith("/mnt/"): return clean_path
    return docker_path

def get_cache_path(array_path):
    ARRAY_ROOT = CONFIG["ARRAY_ROOT"]
    CACHE_ROOT = CONFIG["CACHE_ROOT"]
    if array_path.startswith(ARRAY_ROOT): return array_path.replace(ARRAY_ROOT, CACHE_ROOT, 1)
    return None

def parse_episode_number(filename):
    match = re.search(r"[sS]\d+[eE](\d+)", filename)
    if match: return int(match.group(1))
    return None

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
    except: return False

# --- MOVER / DELETE LOGIC (UPDATED) ---

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
    # PERMS_ROOT is constant now
    
    rel_path = cache_path.replace(CACHE_ROOT, "").lstrip("/")
    dest_path = os.path.join(PERMS_ROOT, rel_path)
    
    log_info(f"[Mover] Verschiebe nach Array: {os.path.basename(cache_path)}")
    
    try:
        dest_dir = os.path.dirname(dest_path)
        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir, exist_ok=True)
            clone_rights_from_disk(os.path.dirname(cache_path).replace(CACHE_ROOT, CACHE_ROOT)) 

        cmd = ["rsync", "-a", "--remove-source-files", cache_path, dest_path]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
        
        cleanup_empty_parent_dirs(cache_path)
        log_info(f"[Mover] Erfolgreich verschoben.")
        
    except Exception as e:
        log_info(f"[Error] Mover fehlgeschlagen: {e}")

def smart_manage_cache_file(cache_path, reason="Cleanup"):
    if not os.path.exists(cache_path): return
    
    CACHE_ROOT = CONFIG["CACHE_ROOT"]
    # PERMS_ROOT is constant

    rel_path = cache_path.replace(CACHE_ROOT, "").lstrip("/")
    array_path = os.path.join(PERMS_ROOT, rel_path)
    
    if os.path.exists(array_path):
        try:
            if os.path.getsize(cache_path) == os.path.getsize(array_path):
                os.remove(cache_path)
                log_info(f"[{reason}] Gelöscht (Duplikat): {os.path.basename(cache_path)}")
                cleanup_empty_parent_dirs(cache_path)
        except OSError: pass
    else:
        move_to_array(cache_path)

def ensure_directory_structure_mirror(full_cache_file_path):
    CACHE_ROOT = CONFIG["CACHE_ROOT"]
    target_dir = os.path.dirname(full_cache_file_path)
    try: rel_path = os.path.relpath(target_dir, CACHE_ROOT)
    except ValueError: return 
    current_cache_path = CACHE_ROOT
    path_parts = rel_path.split(os.sep)
    for part in path_parts:
        if part == "." or not part: continue
        current_cache_path = os.path.join(current_cache_path, part)
        if not os.path.exists(current_cache_path):
            try: os.mkdir(current_cache_path)
            except OSError: pass
        clone_rights_from_disk(current_cache_path)

def cache_file_if_needed(source_path):
    ARRAY_ROOT = CONFIG["ARRAY_ROOT"]
    CACHE_ROOT = CONFIG["CACHE_ROOT"]
    
    rel_path = source_path.replace(ARRAY_ROOT, "")
    if rel_path.startswith("/"): rel_path = rel_path[1:]
    if is_excluded(rel_path): return

    try:
        usage = shutil.disk_usage(CACHE_ROOT)
        if (usage.used / usage.total) * 100 >= get_config_int("CACHE_MAX_USAGE"): return
    except: return

    dest_path = os.path.join(CACHE_ROOT, rel_path)
    
    if os.path.exists(dest_path):
        try:
            if os.path.getsize(source_path) == os.path.getsize(dest_path):
                if dest_path in movie_deletion_queue: del movie_deletion_queue[dest_path]
                return
        except: pass

    log_info(f"[Copy] Starte: {os.path.basename(source_path)}")
    try:
        ensure_directory_structure_mirror(dest_path)
        cmd = ["rsync", "-a", source_path, dest_path]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
        clone_rights_from_disk(dest_path)
        log_info(f"[Copy] Fertig: {os.path.basename(source_path)}")
    except Exception as e:
        log_info(f"[Error] Copy failed: {e}")

# --- HANDLER ---

def handle_series_logic_smart(playing_real_path):
    filename = os.path.basename(playing_real_path)
    current_ep = parse_episode_number(filename)
    if current_ep is None:
        handle_movie_logic(playing_real_path)
        return

    season_dir_array = os.path.dirname(playing_real_path)
    season_dir_cache = get_cache_path(season_dir_array)

    if get_config_bool("ENABLE_SMART_CLEANUP") and season_dir_cache and os.path.exists(season_dir_cache):
        threshold_ep = current_ep - get_config_int("EPISODE_KEEP_PREVIOUS")
        for f in os.listdir(season_dir_cache):
            full_path = os.path.join(season_dir_cache, f)
            if os.path.isfile(full_path):
                ep_num = parse_episode_number(f)
                if ep_num is not None and ep_num < threshold_ep:
                    smart_manage_cache_file(full_path, reason="Smart Cleanup")

    candidates = []
    try:
        if os.path.exists(season_dir_array):
            for f in os.listdir(season_dir_array):
                full_path = os.path.join(season_dir_array, f)
                if not os.path.isfile(full_path): continue
                ep_num = parse_episode_number(f)
                if ep_num is not None and ep_num >= current_ep:
                    candidates.append((ep_num, full_path))
            for _, path in sorted(candidates, key=lambda x: x[0]):
                cache_file_if_needed(path)
    except OSError: pass

def handle_movie_logic(movie_real_path):
    cache_file_if_needed(movie_real_path)
    try:
        folder = os.path.dirname(movie_real_path)
        basename = os.path.basename(movie_real_path)
        stem = os.path.splitext(basename)[0] 
        if os.path.exists(folder):
            for f in os.listdir(folder):
                if f == basename: continue 
                if f.startswith(stem):
                    full_path = os.path.join(folder, f)
                    if os.path.isfile(full_path):
                        cache_file_if_needed(full_path)
    except: pass

def handle_movie_cleanup_smart(active_paths):
    if not get_config_bool("ENABLE_SMART_CLEANUP"): return
    current_time = time.time()
    for movie in active_paths:
        cache_path = get_cache_path(movie)
        if cache_path in movie_deletion_queue:
            del movie_deletion_queue[cache_path]
    for cache_path, timestamp in list(movie_deletion_queue.items()):
        if current_time - timestamp > get_config_int("MOVIE_DELETE_DELAY"):
            smart_manage_cache_file(cache_path, reason="Deletion Timer")
            del movie_deletion_queue[cache_path]

# --- MAIN LOOP ---
if __name__ == "__main__":
    load_config()
    acquire_lock()
    
    signal.signal(signal.SIGHUP, lambda signum, frame: load_config())

    try: requests.packages.urllib3.disable_warnings() 
    except: pass
    
    stream_start_times = {} 
    last_loop_sessions = {}

    log_info(f"Starte Media Cache Manager (Plex & Emby & Jellyfin)...")
    if not check_connection(): log_info("[Warning] Nicht alle Dienste erreichbar.")

    try:
        while True:
            current_sessions = get_active_sessions()
            active_real_paths = []
            
            for docker_path, session_data in current_sessions.items():
                real_path = translate_path(docker_path)
                if not real_path.startswith(CONFIG["ARRAY_ROOT"]): continue
                if is_excluded(real_path): continue
                if not is_trigger_filetype(os.path.basename(real_path)): continue

                active_real_paths.append(real_path)
                
                COPY_DELAY = get_config_int("COPY_DELAY")
                if real_path not in stream_start_times:
                    log_info(f"[Delay] Neuer Stream: {os.path.basename(real_path)} (Warte {COPY_DELAY}s)")
                    stream_start_times[real_path] = time.time()
                    continue 
                if time.time() - stream_start_times[real_path] < COPY_DELAY: continue
                
                ep_check = parse_episode_number(os.path.basename(real_path))
                if ep_check is not None: handle_series_logic_smart(real_path)
                else: handle_movie_logic(real_path) 

            for path in list(stream_start_times.keys()):
                if path not in set(active_real_paths): del stream_start_times[path]

            if get_config_bool("ENABLE_SMART_CLEANUP"):
                stopped_docker_paths = set(last_loop_sessions.keys()) - set(current_sessions.keys())
                for d_path in stopped_docker_paths:
                    session_data = last_loop_sessions.get(d_path)
                    real_path = translate_path(d_path)
                    if is_excluded(real_path): continue
                    
                    if parse_episode_number(os.path.basename(real_path)) is None:
                        if check_is_watched(session_data):
                            log_info(f"[Gesehen] {os.path.basename(real_path)} -> Timer Start")
                            c_path = get_cache_path(real_path)
                            if c_path and os.path.exists(c_path): movie_deletion_queue[c_path] = time.time()
                    else:
                        if check_is_watched(session_data):
                             if is_last_episode_on_array(real_path):
                                log_info(f"[Gesehen] Staffel-Finale: {os.path.basename(real_path)} -> Timer für Rest")
                                season_cache_dir = get_cache_path(os.path.dirname(real_path))
                                if season_cache_dir and os.path.exists(season_cache_dir):
                                    for f in os.listdir(season_cache_dir):
                                        full_p = os.path.join(season_cache_dir, f)
                                        if os.path.isfile(full_p): movie_deletion_queue[full_p] = time.time()
                handle_movie_cleanup_smart(active_real_paths)

            last_loop_sessions = current_sessions
            time.sleep(get_config_int("CHECK_INTERVAL"))
            
    except KeyboardInterrupt:
        print("Beende Skript...", flush=True)
        sys.exit(0)