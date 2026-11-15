#!/bin/bash
. "$(dirname "$0")/common.sh"

M=$T/mnt
B=$T/bin/move-to-cache
export MTC_MNT=$M MTC_CONFIG_DIR=$T/cfg MTC_TMP_DIR=$T/tmp MTC_LOG_FILE=$T/log MTC_NOTIFY=/bin/true MTC_BIN=$B

cleanup() {
    pkill -f "$T/bin/move-to-cache" 2>/dev/null
    pkill -f "$T/plex" 2>/dev/null
    pkill -f "bwlimit=[0-9]" 2>/dev/null
    zpool destroy -f mtctestpool 2>/dev/null
    for d in cache2 cache user0 zcache; do umount "$M/$d" 2>/dev/null; done
    rm -rf "$T"
}

settings() {
    mkdir -p "$T/cfg"
    {
        echo "CACHE_ROOT=\"${1:-$M/cache}\""
        echo "ARRAY_ROOT=\"$M/user\""
        echo "DOCKER_MAPPINGS=\"/media:$M/user/Media\""
        echo "CHECK_INTERVAL=\"2\""
        echo "COPY_DELAY=\"0\""
        echo "ENABLE_PLEX=\"${2:-no}\""
        echo "PLEX_URL=\"http://127.0.0.1:18432\""
        echo "PLEX_TOKEN=\"t\""
    } > "$T/cfg/settings.cfg"
}

job() { python3 -c "import json,sys; print(json.load(open('$T/tmp/move.json')).get('$1'))" 2>/dev/null; }
api() { printf '%s' "$2" | "$B" api "$1"; }
pick() { api uncache "{\"paths\":[\"$1\"]}" >/dev/null; }
slow() { printf '#!/bin/sh\nexec rsync --bwlimit=%s "$@"\n' "$1" > "$T/bin/rsync-slow"; chmod +x "$T/bin/rsync-slow"; export MTC_RSYNC=$T/bin/rsync-slow; }
fast() { unset MTC_RSYNC; }
mover_done() { wait_for '! "$B" service status | grep -q "move to the array is running"' "${1:-300}"; }

cleanup
mkdir -p "$M/cache" "$M/user0" "$T/tmp"
mount -t tmpfs mtccache "$M/cache"
mount -t tmpfs mtcarray "$M/user0"
ln -s user0 "$M/user"
mkdir -p "$M/user0/Media" "$M/cache/Media"
build
settings

echo "Moves with real rsync"
fill "$M/cache/Media/Only/a.mkv" 100000
chown -R nobody:users "$M/cache/Media/Only" 2>/dev/null || chown -R nobody "$M/cache/Media/Only"
chmod 0775 "$M/cache/Media/Only"
pick "$M/cache/Media/Only"
mover_done
ok "a file only on the cache is moved" '[ -f "$M/user0/Media/Only/a.mkv" ] && [ ! -e "$M/cache/Media/Only/a.mkv" ]'
ok "the new array folder copies owner and mode from the cache" '[ "$(stat -c %U:%a "$M/user0/Media/Only")" = "$(id -un nobody):775" ]'
ok "the array root is not created anew" '[ -d "$M/user0" ]'

fill "$M/user0/Media/N/b.mkv" 1000
fill "$M/cache/Media/N/b.mkv" 5000
touch "$M/cache/Media/N/b.mkv"
printf '{"%s":1}\n' "$M/cache/Media/N/b.mkv" > "$T/cfg/tracked.json"
pick "$M/cache/Media/N/b.mkv"
mover_done
ok "a newer cache file replaces the array copy" '[ "$(stat -c %s "$M/user0/Media/N/b.mkv")" = 5000 ] && [ ! -e "$M/cache/Media/N/b.mkv" ]'

echo "Stop move"
slow 2000
fill "$M/cache/Media/Big/big.mkv" 20000000
printf '{"%s":1}\n' "$M/cache/Media/Big/big.mkv" > "$T/cfg/tracked.json"
pick "$M/cache/Media/Big"
wait_for '[ "$(job current)" = big.mkv ]' 50
fill "$M/cache/Media/Big2/c.mkv" 1000
pick "$M/cache/Media/Big2"
wait_for '[ "$(job total)" = 2 ]' 30
ok "a second pick joins the running move at once" '[ "$(job total)" = 2 ]'
"$B" service stop_move >/dev/null
ok "Stop move ends the mover within 10 s" 'mover_done 100'
ok "the file stays on the cache, still tracked" '[ -f "$M/cache/Media/Big/big.mkv" ] && grep -q big.mkv "$T/cfg/tracked.json"'
ok "no rsync temp file is left on the array" '[ -z "$(find "$M/user0" -name ".big.mkv.*")" ]'
ok "rsync is gone" '! pgrep -f "bwlimit=2000" >/dev/null'

echo "Mover killed hard"
pick "$M/cache/Media/Big"
wait_for '[ "$(job current)" = big.mkv ] && pgrep -f "bwlimit=2000" >/dev/null' 50
kill -9 "$(cat "$T/tmp/move.pid")"
ok "rsync dies with the mover" 'wait_for "! pgrep -f bwlimit=2000 >/dev/null" 50'
ok "the cache file is untouched" '[ "$(stat -c %s "$M/cache/Media/Big/big.mkv")" = 20000000 ]'
find "$M/user0" -name '.big.mkv.*' -delete

echo "Service"
"$B" service start >/dev/null
ok "the service starts" '"$B" service status >/dev/null'
pick "$M/cache/Media/Big"
wait_for '[ "$(job current)" = big.mkv ]' 50
"$B" service stop >/dev/null
ok "stopping the service leaves the move running" '"$B" service status | grep -q "move to the array is running"'
ok "stop sets the autostart flag" '[ -f "$T/cfg/service_disabled" ]'
"$B" service boot >/dev/null
ok "boot respects a manual stop" '! "$B" service status >/dev/null'
"$B" service start >/dev/null
ok "start clears the flag and runs" '[ ! -f "$T/cfg/service_disabled" ] && "$B" service status >/dev/null'
"$B" service array_stopping >/dev/null
ok "array_stopping ends service and mover" '! "$B" service status >/dev/null && mover_done 50'
ok "array_stopping leaves the flag alone" '[ ! -f "$T/cfg/service_disabled" ]'
fast

echo "Service stopped while it copies"
mkdir -p "$T/plex/status" "$T/plex/library"
printf '{"MediaContainer":{"Metadata":[{"ratingKey":"1","viewOffset":10,"duration":100,"Media":[{"Part":[{"file":"/media/Film/film.mkv"}]}]}]}}' > "$T/plex/status/sessions"
fill "$M/user0/Media/Film/film.mkv" 30000000
( cd "$T/plex" && exec python3 -m http.server 18432 --bind 127.0.0.1 >/dev/null 2>&1 ) &
wait_for 'curl -s -o /dev/null http://127.0.0.1:18432/status/sessions' 50
settings "$M/cache" yes
slow 3000
"$B" service start >/dev/null
wait_for 'pgrep -f "bwlimit=3000" >/dev/null' 100
ok "the service copies what is playing" 'pgrep -f "bwlimit=3000" >/dev/null'
kill -TERM "$(cat "$T/tmp/daemon.pid")"
ok "SIGTERM ends the service" 'wait_for "! \"$B\" service status >/dev/null" 100'
ok "rsync ends with it" 'wait_for "! pgrep -f bwlimit=3000 >/dev/null" 50'
ok "no half file under the real name, no temp file" '[ ! -e "$M/cache/Media/Film/film.mkv" ] && [ -z "$(find "$M/cache" -name ".film.mkv.*")" ]'
fast
pkill -f "http.server 18432"
settings

echo "One filesystem seen twice"
mkdir -p "$M/cache2"
mount --bind "$M/user0" "$M/cache2"
settings "$M/cache2"
fill "$M/user0/Media/Same/s.mkv" 1000
pick "$M/cache2/Media/Same/s.mkv"
mover_done
ok "a bind mount of the array is never taken for a second copy" '[ -f "$M/user0/Media/Same/s.mkv" ]'
ok "the log says why" 'grep -q "not on two separate filesystems" "$T/log"'
umount "$M/cache2"
settings

if command -v zpool >/dev/null 2>&1; then
    echo "ZFS pool usage"
    truncate -s 1G "$T/pool.img"
    zpool create -f -m "$M/zcache" mtctestpool "$T/pool.img"
    zfs create mtctestpool/Media
    head -c 60000000 /dev/urandom > "$M/zcache/Media/x.mkv"
    zpool sync mtctestpool
    settings "$M/zcache"
    used=$(api overview '{}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["pool"]["used"])')
    ok "the whole pool counts, not the top dataset ($used bytes)" '[ "$used" -gt 50000000 ]'
fi

cleanup
finish
