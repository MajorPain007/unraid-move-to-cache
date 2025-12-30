#!/usr/bin/python3
import requests, os, shutil, subprocess, time, sys, re, fcntl, signal
from pathlib import Path

CONFIG_FILE = "/boot/config/plugins/plex_to_cache/settings.cfg"
LOCK_FILE_PATH = "/tmp/media_cache_cleaner.lock"
PERMS_ROOT = "/mnt/user0"

CONFIG = {
    "ENABLE_PLEX": "False", "PLEX_URL": "http://localhost:32400", "PLEX_TOKEN": "",
    "ENABLE_EMBY": "False", "EMBY_URL": "http://localhost:8096", "EMBY_API_KEY": "",
    "ENABLE_JELLYFIN": "False", "JELLYFIN_URL": "http://localhost:8096", "JELLYFIN_API_KEY": "",
    "CHECK_INTERVAL": "10", "CACHE_MAX_USAGE": "80", "COPY_DELAY": "30",
    "ENABLE_SMART_CLEANUP": "False", "MOVIE_DELETE_DELAY": "1800", "EPISODE_KEEP_PREVIOUS": "2",
    "EXCLUDE_DIRS": "", "MEDIA_FILETYPES": ".mkv .mp4 .avi", "ARRAY_ROOT": "/mnt/user",
    "CACHE_ROOT": "/mnt/cache", "DOCKER_MAPPINGS": ""
}

movie_deletion_queue, stream_start_times, metadata_path_cache, parsed_docker_mappings = {}, {}, {}, {}

def log_info(msg): print(msg, flush=True)

def parse_docker_mappings(mapping_str):
    m = {}
    if not mapping_str: return m
    for p in mapping_str.split(';'):
        if ':' in p:
            k, v = p.split(':', 1)
            m[k.strip()] = v.strip()
    return m

def load_config():
    global CONFIG, parsed_docker_mappings
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                for l in f:
                    l = l.strip()
                    if not l or l.startswith("#") or "=" not in l: continue
                    k, v = l.split("=", 1)
                    CONFIG[k.strip()] = v.strip().strip('"').strip("'")
        except: pass
    parsed_docker_mappings = parse_docker_mappings(CONFIG.get("DOCKER_MAPPINGS", ""))

def get_config_bool(k): return CONFIG.get(k, "False").lower() in ["true", "1"]
def get_config_int(k):
    try: return int(CONFIG.get(k, 0))
    except: return 0

def acquire_lock():
    global lock_file
    lock_file = open(LOCK_FILE_PATH, 'w')
    try: fcntl.lockf(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except: sys.exit(1)

def clone_rights_from_disk(dst):
    CACHE_ROOT = CONFIG["CACHE_ROOT"]
    if dst.startswith(CACHE_ROOT):
        src = dst.replace(CACHE_ROOT, PERMS_ROOT, 1)
        if os.path.exists(src):
            try:
                st = os.stat(src)
                os.chown(dst, st.st_uid, st.st_gid)
                os.chmod(dst, st.st_mode)
            except: pass

def is_excluded(p):
    E = CONFIG["EXCLUDE_DIRS"]
    if not E: return False
    parts = p.split(os.sep)
    for exc in [x.strip() for x in E.split(',')]:
        if exc and exc in parts: return True
    return False

def is_trigger_filetype(f):
    T = CONFIG["MEDIA_FILETYPES"]
    if not T: return True
    valid = [x.strip().lower() for x in T.split()]
    for ext in valid:
        if f.lower().endswith(ext): return True
    return False

def plex_api_get(e):
    h = {'X-Plex-Token': CONFIG["PLEX_TOKEN"], 'Accept': 'application/json'}
    try: return requests.get(f"{CONFIG['PLEX_URL']}{e}", headers=h, timeout=5, verify=False).json()
    except: return None

def emby_api_get(e, k, u):
    h = {'X-Emby-Token': k, 'Accept': 'application/json'}
    try: return requests.get(f"{u}{e}", headers=h, timeout=5, verify=False).json()
    except: return None

def get_active_sessions():
    active = {}
    if get_config_bool("ENABLE_PLEX"):
        data = plex_api_get("/status/sessions")
        if data and 'MediaContainer' in data and 'Metadata' in data['MediaContainer']:
            for item in data['MediaContainer']['Metadata']:
                rk = item.get('ratingKey')
                found = metadata_path_cache.get(rk)
                if not found and 'Media' in item:
                    for m in item['Media']:
                        for p in m.get('Part', []):
                            if p.get('file'): found = p['file']; break
                if not found and rk:
                    meta = plex_api_get(f"/library/metadata/{rk}")
                    if meta and 'MediaContainer' in meta and 'Metadata' in meta['MediaContainer']:
                        for med in meta['MediaContainer']['Metadata'][0].get('Media', []):
                            for pt in med.get('Part', []):
                                if pt.get('file'): found = pt['file']; break
                if found: metadata_path_cache[rk] = found; active[found] = {'service': 'plex', 'id': rk}
    
    for svc in [("ENABLE_EMBY", "EMBY_API_KEY", "EMBY_URL", "emby"), ("ENABLE_JELLYFIN", "JELLYFIN_API_KEY", "JELLYFIN_URL", "jellyfin")]:
        if get_config_bool(svc[0]):
            data = emby_api_get("/Sessions", CONFIG[svc[1]], CONFIG[svc[2]])
            if data:
                for s in data:
                    p = s.get('NowPlayingItem', {}).get('Path')
                    if p: active[p] = {'service': svc[3], 'id': s['NowPlayingItem'].get('Id'), 'user': s.get('UserId')}
    return active

def check_is_watched(s):
    if not s: return False
    svc = s.get('service')
    if svc == 'plex':
        d = plex_api_get(f"/library/metadata/{s.get('id')}")
        if d and 'MediaContainer' in d and 'Metadata' in d['MediaContainer']:
            if d['MediaContainer']['Metadata'][0].get('viewCount', 0) > 0: return True
    elif svc in ['emby', 'jellyfin']:
        u = CONFIG["EMBY_URL"] if svc == 'emby' else CONFIG["JELLYFIN_URL"]
        k = CONFIG["EMBY_API_KEY"] if svc == 'emby' else CONFIG["JELLYFIN_API_KEY"]
        d = emby_api_get(f"/Users/{s.get('user')}/Items/{s.get('id')}", k, u)
        if d and 'UserData' in d: return d['UserData'].get('Played', False)
    return False

def translate_path(p):
    c = p.replace('\\', '/')
    A = CONFIG["ARRAY_ROOT"]
    for dp, hp in parsed_docker_mappings.items():
        if c.startswith(dp):
            rel = c[len(dp):].lstrip('/')
            return os.path.join(hp if hp.startswith(A) else os.path.join(A, hp.lstrip('/')), rel).replace('//', '/')
    return c

def parse_episode_number(f):
    m = re.search(r"[sS]\d+[eE](\d+)", f)
    return int(m.group(1)) if m else None

def is_last_episode(p):
    try:
        e = parse_episode_number(os.path.basename(p))
        if e is None: return False
        folder, max_e = os.path.dirname(p), 0
        if os.path.exists(folder):
            for f in os.listdir(folder):
                num = parse_episode_number(f)
                if num and num > max_e: max_e = num
        return e >= max_e
    except: return False

def cleanup_empty_parent_dirs(p):
    P, R = os.path.dirname(p), CONFIG["CACHE_ROOT"]
    prot = [os.path.join(R, m.strip("/")) for m in parsed_docker_mappings.values()]
    while P.startswith(R) and len(P) > len(R):
        if P in prot: break
        try: os.rmdir(P); P = os.path.dirname(P)
        except: break

def move_to_array(cp):
    R = CONFIG["CACHE_ROOT"]
    dest = os.path.join(PERMS_ROOT, cp.replace(R, "", 1).lstrip("/"))
    log_info(f"[Mover] -> Array: {os.path.basename(cp)}")
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        subprocess.run(["rsync", "-a", "--remove-source-files", cp, dest], check=True, stdout=subprocess.DEVNULL)
        cleanup_empty_parent_dirs(cp)
    except: pass

def smart_manage_cache_file(cp, reason="Cleanup"):
    if not os.path.exists(cp): return
    ap = os.path.join(PERMS_ROOT, cp.replace(CONFIG["CACHE_ROOT"], "", 1).lstrip("/"))
    if os.path.exists(ap) and os.path.getsize(cp) == os.path.getsize(ap):
        os.remove(cp); log_info(f"[{reason}] Gelöscht: {os.path.basename(cp)}")
        cleanup_empty_parent_dirs(cp)
    else: move_to_array(cp)

def ensure_structure(fcp):
    R = CONFIG["CACHE_ROOT"]
    try: rel = os.path.relpath(os.path.dirname(fcp), R)
    except: return
    curr = R
    for part in rel.split(os.sep):
        if not part or part == ".": continue
        curr = os.path.join(curr, part)
        if not os.path.exists(curr):
            try: os.mkdir(curr); clone_rights_from_disk(curr)
            except: pass

def cache_file_if_needed(sp):
    rel = sp.replace(CONFIG["ARRAY_ROOT"], "").lstrip("/")
    if not rel or is_excluded(rel): return
    dp = os.path.join(CONFIG["CACHE_ROOT"], rel)
    if os.path.exists(dp) and os.path.getsize(sp) == os.path.getsize(dp):
        if dp in movie_deletion_queue: del movie_deletion_queue[dp]
        return
    try:
        u = shutil.disk_usage(CONFIG["CACHE_ROOT"])
        if (u.used / u.total) * 100 >= get_config_int("CACHE_MAX_USAGE"): return
    except: return
    log_info(f"[Copy] -> {os.path.basename(sp)}")
    try:
        ensure_structure(dp)
        subprocess.run(["rsync", "-a", sp, dp], check=True, stdout=subprocess.DEVNULL)
        clone_rights_from_disk(dp)
    except: pass

def handle_series_smart(rp):
    curr = parse_episode_number(os.path.basename(rp))
    if curr is None: return handle_movie_logic(rp)
    sd_array = os.path.dirname(rp)
    sd_cache = os.path.join(CONFIG["CACHE_ROOT"], sd_array.replace(CONFIG["ARRAY_ROOT"], "").lstrip("/"))
    if get_config_bool("ENABLE_SMART_CLEANUP") and os.path.exists(sd_cache):
        th = curr - get_config_int("EPISODE_KEEP_PREVIOUS")
        for f in os.listdir(sd_cache):
            num = parse_episode_number(f)
            if num is not None and num < th: smart_manage_cache_file(os.path.join(sd_cache, f), "Smart Cleanup")
    try:
        if os.path.exists(sd_array):
            for f in sorted(os.listdir(sd_array)):
                num = parse_episode_number(f)
                if num is not None and num >= curr: cache_file_if_needed(os.path.join(sd_array, f))
    except: pass

def handle_movie_logic(rp):
    cache_file_if_needed(rp)
    try:
        folder, stem = os.path.dirname(rp), os.path.splitext(os.path.basename(rp))[0]
        if os.path.exists(folder):
            for f in os.listdir(folder):
                if f.startswith(stem): cache_file_if_needed(os.path.join(folder, f))
    except: pass

if __name__ == "__main__":
    load_config(); acquire_lock()
    signal.signal(signal.SIGHUP, lambda s, f: load_config())
    log_info("Dienst gestartet. Warte auf Streams...")
    last_loop = {}
    while True:
        try:
            curr_sessions = get_active_sessions()
            active_paths = []
            for dp, s_data in curr_sessions.items():
                rp = translate_path(dp)
                if not rp.startswith(CONFIG["ARRAY_ROOT"]) or is_excluded(rp) or not is_trigger_filetype(os.path.basename(rp)): continue
                active_paths.append(rp)
                if rp not in stream_start_times:
                    log_info(f"[Stream] Aktiv: {os.path.basename(rp)}"); stream_start_times[rp] = time.time()
                    continue
                if time.time() - stream_start_times[rp] >= get_config_int("COPY_DELAY"):
                    if parse_episode_number(os.path.basename(rp)) is not None: handle_series_smart(rp)
                    else: handle_movie_logic(rp)
            for p in list(stream_start_times.keys()):
                if p not in set(active_paths): del stream_start_times[p]
            if get_config_bool("ENABLE_SMART_CLEANUP"):
                stopped = set(last_loop.keys()) - set(curr_sessions.keys())
                for d_p in stopped:
                    s_d, r_p = last_loop[d_p], translate_path(d_p)
                    if check_is_watched(s_d):
                        cp = os.path.join(CONFIG["CACHE_ROOT"], r_p.replace(CONFIG["ARRAY_ROOT"], "").lstrip("/"))
                        if cp and os.path.exists(cp):
                            if parse_episode_number(os.path.basename(r_p)) is None: movie_deletion_queue[cp] = time.time()
                            elif is_last_episode(r_p):
                                for f in os.listdir(os.path.dirname(cp)):
                                    movie_deletion_queue[os.path.join(os.path.dirname(cp), f)] = time.time()
                for c_p, ts in list(movie_deletion_queue.items()):
                    if time.time() - ts > get_config_int("MOVIE_DELETE_DELAY"):
                        smart_manage_cache_file(c_p, "Deletion Timer"); del movie_deletion_queue[c_p]
            last_loop = curr_sessions
        except: pass
        time.sleep(get_config_int("CHECK_INTERVAL"))