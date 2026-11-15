#!/bin/bash
set -u
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
T=${MTC_TEST_DIR:-/var/tmp/mtctest}
GO=$(command -v go || echo /usr/local/go/bin/go)
pass=0
failed=0

ok() {
    if eval "$2"; then
        echo "  PASS $1"
        pass=$((pass + 1))
    else
        echo "  FAIL $1"
        failed=$((failed + 1))
    fi
}

finish() {
    echo
    echo "$pass passed, $failed failed"
    [ "$failed" = 0 ]
}

wait_for() {
    local i
    for i in $(seq "${2:-100}"); do
        eval "$1" && return 0
        /bin/sleep 0.1
    done
    return 1
}

build() {
    mkdir -p "$T/bin"
    ( cd "$ROOT" && "$GO" build -o "$T/bin/move-to-cache" ./cmd/move-to-cache ) || exit 1
}

fill() {
    mkdir -p "$(dirname "$1")"
    head -c "$2" /dev/urandom > "$1"
    touch -d '-2 hours' "$1"
}
