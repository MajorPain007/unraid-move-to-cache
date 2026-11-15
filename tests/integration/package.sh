#!/bin/bash
. "$(dirname "$0")/common.sh"

PKG=${1:?usage: package.sh <package.txz> <manifest.plg>}
PLG=${2:?usage: package.sh <package.txz> <manifest.plg>}
N=plex_to_cache
PD=$T/root/usr/local/emhttp/plugins/$N
CD=$T/boot/config/plugins/$N
M=$T/mnt
export MTC_CONFIG_DIR=$CD MTC_TMP_DIR=$T/tmp/$N MTC_LOG_FILE=$T/log/$N.log MTC_MNT=$M MTC_NOTIFY=/bin/true

cleanup() {
    pkill -f "$T/root" 2>/dev/null
    rm -rf "$T"
}

run_plg() {
    python3 -I - "$PLG" "$1" "$T" "$PKG" <<'PY'
import sys, re, shutil, subprocess, xml.etree.ElementTree as ET
plg, method, t, pkg = sys.argv[1:]
root = ET.parse(plg).getroot()
subst = [("/usr/local/emhttp/plugins/", t + "/root/usr/local/emhttp/plugins/"), ("/boot/config/", t + "/boot/config/"),
         ("/tmp/", t + "/tmp/"), ("/var/run/", t + "/run/"), ("/etc/rc.d/", t + "/etc/rc.d/"), ("/var/log/", t + "/log/")]
def fix(s):
    for a, b in subst:
        s = re.sub(r'(?<![\w/.-])' + re.escape(a), b, s)
    return s
env = {"PATH": t + "/stub:/usr/bin:/bin:/usr/sbin:/sbin"}
import os
env.update({k: v for k, v in os.environ.items() if k.startswith("MTC_")})
for f in root.findall("FILE"):
    if (f.get("Method") or "install") != method:
        continue
    if f.get("Name", "").endswith(".txz"):
        dst = fix(f.get("Name"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(pkg, dst)
        subprocess.run(f.get("Run").split() + [dst], env=env, check=True, stdin=subprocess.DEVNULL)
        continue
    inline = f.find("INLINE")
    if inline is not None and f.get("Run") == "/bin/bash":
        script = t + "/inline.sh"
        open(script, "w").write(fix(inline.text))
        r = subprocess.run(["/bin/bash", script], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        sys.stdout.write(r.stdout + r.stderr)
PY
}

cleanup
mkdir -p "$T/stub" "$T/run" "$T/etc/rc.d" "$T/log" "$PD/scripts" "$CD" "$M/cache/Media" "$M/user0/Media"
ln -s user0 "$M/user"
printf '#!/bin/sh\nmkdir -p "%s/root" && tar -C "%s/root" -xJf "$2"\n' "$T" "$T" > "$T/stub/upgradepkg"
printf '#!/bin/sh\nrm -rf "%s"\n' "$PD" > "$T/stub/removepkg"
chmod +x "$T/stub/"*

printf '#!/bin/sh\ntrap "exit 0" TERM\nwhile :; do sleep 1; done\n' > "$PD/scripts/$N.py"
chmod +x "$PD/scripts/$N.py"
"$PD/scripts/$N.py" &
echo $! > "$T/run/$N.pid"
"$PD/scripts/$N.py" --move &
echo old > "$PD/$N.php"
echo old > "$T/etc/rc.d/rc.$N"
echo 1 > "$CD/flush_last.txt"
cat > "$CD/settings.cfg" <<CFG
ENABLE_PLEX="True"
PLEX_URL="https://192.168.20.3:32400"
PLEX_TOKEN="secret"
CHECK_INTERVAL="10"
CLEANUP_MODE="smart"
ARRAY_ROOT="$M/user"
CACHE_ROOT="$M/cache"
DOCKER_MAPPINGS="/media:$M/user/Media"
ENABLE_CACHE_EVICTION="True"
CFG
fill "$M/cache/Media/a.mkv" 100
fill "$M/cache/Media/b|c.mkv" 100
printf '%s|1700000000.0\n%s|1700000001.0\n' "$M/cache/Media/a.mkv" "$M/cache/Media/b|c.mkv" > "$CD/cached_files.list"

echo "Update from the Python version"
out=$(run_plg install)
echo "$out" | sed 's/^/    /'
ok "the old service and mover are stopped" '! pgrep -f "$PD/scripts/$N.py" >/dev/null'
ok "the old files are gone" '[ ! -e "$PD/scripts" ] && [ ! -e "$PD/$N.php" ] && [ ! -e "$T/etc/rc.d/rc.$N" ] && [ ! -e "$CD/flush_last.txt" ] && [ ! -e "$T/run/$N.pid" ]'
ok "settings are taken over, certificate check on" 'grep -q "^ENABLE_PLEX=\"yes\"" "$CD/settings.cfg" && grep -q "^PLEX_TOKEN=\"secret\"" "$CD/settings.cfg" && grep -q "^PLEX_VERIFY_TLS=\"yes\"" "$CD/settings.cfg" && grep -q "^CLEANUP_MODE=\"smart\"" "$CD/settings.cfg"'
ok "the cached-file list is taken over" '[ ! -e "$CD/cached_files.list" ] && grep -q "b|c.mkv" "$CD/tracked.json" && grep -q "a.mkv" "$CD/tracked.json"'
ok "the new service runs" 'wait_for "\"$PD/bin/move-to-cache\" service status >/dev/null" 50'
ok "the page is installed" '[ -f "$PD/$N.page" ] && [ -f "$PD/assets/app.js" ] && [ -x "$PD/event/stopping" ]'
ok "settings.cfg is readable by root only" '[ "$(stat -c %a "$CD/settings.cfg")" = 600 ]'

echo "Update of the Go version"
old=$(cat "$T/tmp/$N/daemon.pid")
run_plg install >/dev/null
ok "the service is restarted" 'wait_for "\"$PD/bin/move-to-cache\" service status >/dev/null" 50 && [ "$(cat "$T/tmp/$N/daemon.pid")" != "$old" ]'

echo "Uninstall"
out=$(run_plg remove)
echo "$out" | sed 's/^/    /'
ok "nothing of the plugin runs" '! pgrep -f "$PD/bin/move-to-cache" >/dev/null'
ok "program, settings and RAM data are gone" '[ ! -e "$PD" ] && [ ! -e "$CD" ] && [ ! -e "$T/tmp/$N" ]'
ok "media files are untouched" '[ -f "$M/cache/Media/a.mkv" ]'

cleanup
finish
