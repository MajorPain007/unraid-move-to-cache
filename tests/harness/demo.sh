#!/bin/bash
set -u
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
T=${MTC_DEMO_DIR:-$(cd /tmp && pwd -P)/mtcdemo}
PORT=${MTC_DEMO_PORT:-8094}
PLEX=$((PORT + 1))
M=$T/mnt
export MTC_MNT=$M MTC_CONFIG_DIR=$T/cfg MTC_TMP_DIR=$T/tmp MTC_LOG_FILE=$T/log MTC_NOTIFY=/bin/true
export MTC_MOUNTS=$T/mounts MTC_BIN=$T/bin/move-to-cache MTC_RSYNC=$T/bin/rsync

stop() {
    [ -f "$T/bin/move-to-cache" ] && "$MTC_BIN" service array_stopping >/dev/null 2>&1
    for f in "$T/php.pid" "$T/plex.pid"; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; done
    rm -rf "$T"
}

stop
[ "${1:-}" = stop ] && exit 0

mkdir -p "$T/bin" "$T/cfg" "$T/tmp" "$T/plex/status" "$M/cache/Media" "$M/user0/Media"
ln -s user0 "$M/user"
GO=$(command -v go || echo /usr/local/go/bin/go)
( cd "$ROOT" && "$GO" build -o "$MTC_BIN" ./cmd/move-to-cache ) || exit 1
( cd "$ROOT/web" && npm run --silent build >/dev/null ) || exit 1
printf '#!/bin/sh\nrm=0\nfor a in "$@"; do [ "$a" = --remove-source-files ] && rm=1; done\neval src=\\${$(($#-1))}\neval dst=\\${$#}\ntmp="$(dirname "$dst")/.$(basename "$dst").AbCdEf"\ntrap '"'"'rm -f "$tmp"; exit 20'"'"' TERM\nsize=$(wc -c < "$src")\ni=0\n: > "$tmp"\nwhile [ $i -lt 30 ]; do head -c $((size / 30)) /dev/zero >> "$tmp"; /bin/sleep 1; i=$((i+1)); done\ncp -p "$src" "$dst"; rm -f "$tmp"\n[ $rm = 1 ] && rm -f "$src"\nexit 0\n' > "$MTC_RSYNC"
chmod +x "$MTC_RSYNC"
printf 'democache %s/cache btrfs rw 0 0\nshfs %s/user fuse.shfs rw 0 0\nshfs %s/user0 fuse.shfs rw 0 0\n' "$M" "$M" "$M" > "$MTC_MOUNTS"

mk() { mkdir -p "$(dirname "$1")"; head -c "$2" /dev/urandom > "$1"; touch -t 202601010000 "$1"; }
mk "$M/user0/Media/Movies/Arrival (2016)/Arrival (2016).mkv" 40000000
mk "$M/user0/Media/Movies/<img src=x onerror=alert(1)>/<img src=x onerror=alert(1)>.mkv" 1000
for e in 01 02 03 04 05 06; do mk "$M/user0/Media/TV/Ein sehr langer Serienname mit Umlauten äöü/Season 01/Ein sehr langer Serienname mit Umlauten äöü - S01E$e - Die Episode mit dem langen Titel.mkv" 2000000; done
for e in 01 02 03; do
    f="Media/TV/Ein sehr langer Serienname mit Umlauten äöü/Season 01/Ein sehr langer Serienname mit Umlauten äöü - S01E$e - Die Episode mit dem langen Titel.mkv"
    mkdir -p "$(dirname "$M/cache/$f")"
    cp -p "$M/user0/$f" "$M/cache/$f"
done
mk "$M/cache/Media/Movies/Download only on cache/Fresh.mkv" 5000000
mk "$M/cache/Media/Movies/<img src=x onerror=alert(1)>/<img src=x onerror=alert(1)>.mkv" 1000
cp -p "$M/user0/Media/Movies/<img src=x onerror=alert(1)>/<img src=x onerror=alert(1)>.mkv" "$M/cache/Media/Movies/<img src=x onerror=alert(1)>/"

printf '{"MediaContainer":{"friendlyName":"Demo Plex","Metadata":[{"ratingKey":"7","title":"Arrival","viewOffset":3000,"duration":10000,"User":{"title":"vincent"},"Media":[{"Part":[{"file":"/data/Movies/Arrival (2016)/Arrival (2016).mkv"}]}]},{"ratingKey":"8","title":"Episode","grandparentTitle":"Unmapped show","viewOffset":10,"duration":100,"Media":[{"Part":[{"file":"/tv/Show/S01E01.mkv"}]}]}]}}' > "$T/plex/status/sessions"
cp "$T/plex/status/sessions" "$T/plex/index.html"
( cd "$T/plex" && exec python3 -m http.server "$PLEX" --bind 127.0.0.1 >/dev/null 2>&1 ) &
echo $! > "$T/plex.pid"

cat > "$T/cfg/settings.cfg" <<CFG
ENABLE_PLEX="yes"
PLEX_URL="http://127.0.0.1:$PLEX"
PLEX_TOKEN="demo-token"
CHECK_INTERVAL="3"
COPY_DELAY="0"
CACHE_MAX_USAGE="99"
CLEANUP_MODE="smart"
ARRAY_ROOT="$M/user"
CACHE_ROOT="$M/cache"
DOCKER_MAPPINGS="/data:$M/user/Media"
CFG
"$MTC_BIN" service start >/dev/null
cd "$ROOT" || exit 1
php -S "127.0.0.1:$PORT" tests/harness/router.php > "$T/php.log" 2>&1 &
echo $! > "$T/php.pid"
/bin/sleep 0.5
echo "demo on http://127.0.0.1:$PORT  (stop: $0 stop)"
