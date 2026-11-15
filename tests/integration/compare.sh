#!/bin/bash
. "$(dirname "$0")/common.sh"

PORT=18433
PY=$T/old.py
B=$T/bin/move-to-cache

cleanup() {
    pkill -f "$T/[r]un-old.py" 2>/dev/null
    pkill -f "$T/bin/move-to-cach[e]" 2>/dev/null
    pkill -f "http.server $PORT" 2>/dev/null
    for d in old new; do umount "$T/$d/mnt/cache" "$T/$d/mnt/user" 2>/dev/null; done
    rm -rf "$T"
}

library() {
    local m=$1/mnt
    mkdir -p "$m/cache" "$m/user" "$1/cfg" "$1/tmp"
    mount -t tmpfs "mtc$(basename "$1")c" "$m/cache"
    mount -t tmpfs "mtc$(basename "$1")a" "$m/user"
    mkdir -p "$m/cache/Media" "$m/user/Media"
    ln -s user "$m/user0"
    echo keep > "$m/cache/.keep"
    fill "$m/user/Media/Movies/Film/Film.mkv" 3000
    fill "$m/user/Media/Movies/Other/Other.mkv" 3000
    local e
    for e in $(seq -w 1 50); do fill "$m/user/Media/TV/Show/Season 01/Show S01E$e.mkv" 1000; done
    for e in 20 21 22 23 24 25 26; do
        mkdir -p "$m/cache/Media/TV/Show/Season 01"
        cp -p "$m/user/Media/TV/Show/Season 01/Show S01E$e.mkv" "$m/cache/Media/TV/Show/Season 01/"
    done
    cat > "$1/cfg/settings.cfg" <<CFG
ENABLE_PLEX="True"
PLEX_URL="http://127.0.0.1:$PORT"
PLEX_TOKEN="t"
CHECK_INTERVAL="2"
COPY_DELAY="0"
CACHE_MAX_USAGE="99"
CLEANUP_MODE="smart"
MOVIE_DELETE_DELAY="0"
EPISODE_KEEP_PREVIOUS="2"
MEDIA_FILETYPES=".mkv .mp4 .avi"
ARRAY_ROOT="$m/user"
CACHE_ROOT="$m/cache"
DOCKER_MAPPINGS="/media:$m/user/Media"
ENABLE_EPISODE_BATCHING="True"
EPISODE_BATCH_SIZE="30"
EPISODE_BATCH_TOLERANCE="10"
EPISODE_BATCH_PREFETCH="4"
ENABLE_CACHE_EVICTION="True"
ENABLE_NEXT_SEASON_PREFETCH="False"
CFG
}

sessions() {
    local items="" s path prog
    for s in "$@"; do
        path=${s%@*}
        prog=${s#*@}
        items="$items${items:+,}{\"ratingKey\":\"${#items}\",\"viewOffset\":$prog,\"duration\":100,\"Media\":[{\"Part\":[{\"file\":\"$path\"}]}]}"
    done
    printf '{"MediaContainer":{"Metadata":[%s]}}' "$items" > "$T/plex/status/sessions"
}

scenario() {
    sessions "/media/Movies/Film/Film.mkv@50" "/media/TV/Show/Season 01/Show S01E27.mkv@10"
    /bin/sleep 12
    sessions "/media/Movies/Film/Film.mkv@95" "/media/TV/Show/Season 01/Show S01E27.mkv@20"
    /bin/sleep 5
    sessions
    /bin/sleep 6
}

result() {
    ( cd "$1/mnt/cache" && find . -name '*.mkv' | sort )
    echo "--- move requests"
    sed "s|$1/mnt/cache/||" "$1/tmp/move.queue" 2>/dev/null | sort -u
}

cleanup
mkdir -p "$T/plex/status"
sessions
( cd "$T/plex" && exec python3 -m http.server $PORT --bind 127.0.0.1 >/dev/null 2>&1 ) &
wait_for "curl -s -o /dev/null http://127.0.0.1:$PORT/status/sessions" 50
build

git -c safe.directory="*" -C "$ROOT" show python:src/plex_to_cache.py > "$PY" || { echo "branch python is missing"; exit 1; }
O=$T/old
library "$O"
cat > "$T/run-old.py" <<PYW
import sys, importlib.util
spec = importlib.util.spec_from_file_location("ptc", "$PY")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.CONFIG_FILE = "$O/cfg/settings.cfg"
m.TRACKED_FILES = "$O/cfg/cached_files.list"
m.TRACKED_LOCK = "$O/tmp/tracked.lock"
m.LOCK_FILE = "$O/tmp/daemon.lock"
m.LOG_FILE = "$O/log"
m.STATUS_FILE = "$O/tmp/status.json"
m.MOVE_QUEUE = "$O/tmp/move.queue"
m.MOVE_LOCK = "$O/tmp/move.lock"
m.MOVE_PID = "$O/tmp/move.pid"
m.MOVE_STATUS = "$O/tmp/move.json"
m._start_mover = lambda: None
m.run_daemon()
PYW
for e in 20 21 22 23 24 25 26; do echo "$O/mnt/cache/Media/TV/Show/Season 01/Show S01E$e.mkv|1700000000"; done > "$O/cfg/cached_files.list"
python3 "$T/run-old.py" > "$O/out" 2>&1 &
pid=$!
scenario
kill -TERM "$pid"
wait "$pid" 2>/dev/null

N=$T/new
library "$N"
for e in 20 21 22 23 24 25 26; do echo "$N/mnt/cache/Media/TV/Show/Season 01/Show S01E$e.mkv|1700000000"; done > "$N/cfg/cached_files.list"
MTC_MNT=$N/mnt MTC_CONFIG_DIR=$N/cfg MTC_TMP_DIR=$N/tmp MTC_LOG_FILE=$N/log MTC_NOTIFY=/bin/true MTC_BIN=/bin/true "$B" daemon > "$N/out" 2>&1 &
pid=$!
scenario
kill -TERM "$pid"
wait "$pid" 2>/dev/null

result "$O" > "$T/old.txt"
result "$N" > "$T/new.txt"
echo "Old and new service against the same fake Plex"
ok "the same files are copied and the same moves are requested" 'diff -u "$T/old.txt" "$T/new.txt"'
ok "the scenario did something" 'grep -q "S01E50" "$T/new.txt" && grep -q "Film.mkv" "$T/new.txt" && grep -q "auto" "$T/new.txt"'
sed 's/^/    /' "$T/new.txt" | head -60
cleanup
finish
