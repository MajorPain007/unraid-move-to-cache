#!/bin/bash
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
fail=0
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }
check() {
    if eval "$2"; then echo "  PASS $1"; else echo "  FAIL $1"; fail=1; fi
}

section "Go"
GO=$(command -v go || echo /usr/local/go/bin/go)
unformatted=$(gofmt -l cmd internal)
check "gofmt" '[ -z "$unformatted" ]'
GOOS=linux "$GO" vet ./... || fail=1
"$GO" vet ./... || fail=1
"$GO" test ./internal/mtc/ -count=1 2>&1 | grep -v 'TLS handshake error' || fail=1
[ "${PIPESTATUS[0]}" = 0 ] || fail=1

section "Web interface"
( cd web && { [ -d node_modules ] || npm ci --silent; } && npm run --silent check ) || fail=1

section "PHP and shell"
if command -v php >/dev/null 2>&1; then
    for f in src/include/*.php src/*.page; do
        out=$(php -l "$f" 2>&1) || { echo "  $out"; fail=1; }
    done
    echo "  checked PHP syntax"
    check "the manifest is well-formed XML" 'php -r "exit(simplexml_load_file(\$argv[1]) === false ? 1 : 0);" plex_to_cache.plg 2>/dev/null'
    found=$(for f in src/include/*.php src/*.page tests/harness/*.php; do
        [ -f "$f" ] && php -r 'foreach (token_get_all(file_get_contents($argv[1])) as $t) { if (is_array($t) && ($t[0] === T_COMMENT || $t[0] === T_DOC_COMMENT || ($t[0] === T_INLINE_HTML && preg_match("~/\*|<!--~", $t[1])))) echo $argv[1], ":", $t[2], "\n"; }' "$f"
    done)
    check "no comments in PHP" '[ -z "$found" ] || { echo "$found"; false; }'
else
    echo "  SKIP php not installed"
fi
shfiles=$(find scripts tests src/event -type f \( -name '*.sh' -o -path 'src/event/*' \) | sort)
found=$(for f in $shfiles; do awk -v f="$f" 'NR > 1 && /^[[:space:]]*#/ { print f ":" NR }' "$f"; done)
check "no comments in shell" '[ -z "$found" ] || { echo "$found"; false; }'
for f in $shfiles; do bash -n "$f" || fail=1; done
if command -v shellcheck >/dev/null 2>&1; then
    check "shellcheck" 'shellcheck -S warning -e SC2034 $shfiles'
fi
check "every page sets Markdown=\"false\"" '! grep -L "^Markdown=\"false\"" src/*.page | grep -q .'

section "Manifest"
ver=$(sed -n 's/^<!ENTITY version *"\([^"]*\)">.*/\1/p' plex_to_cache.plg)
md5=$(sed -n 's/^<!ENTITY md5 *"\([^"]*\)">.*/\1/p' plex_to_cache.plg)
pkg="archive/plex_to_cache-${ver}-x86_64-1.txz"
check "the manifest's md5 matches $pkg" '[ -f "$pkg" ] && [ "$( (md5sum "$pkg" 2>/dev/null || md5 -r "$pkg") | cut -d" " -f1)" = "$md5" ]'
check "the install URL is unchanged" 'grep -q "<!ENTITY pluginURL \"https://raw.githubusercontent.com/&github;/main/plex_to_cache.plg\">" plex_to_cache.plg'
check "the uninstall block stops the service and the mover" 'sed -n "/Method=\"remove\"/,/<\/FILE>/p" plex_to_cache.plg | grep -q "service array_stopping"'

section "Test build"
out=$(mktemp -d)
before=$(git status --porcelain plex_to_cache.plg archive)
if MTC_PLUGIN_NAME=plex_to_cache.test MTC_OUT_DIR="$out" ./scripts/build.sh 2026.01.01.90 >/dev/null 2>&1; then
    plg="$out/plex_to_cache.test.plg"
    check "test manifest has its own name and file URL" 'grep -q "<!ENTITY name      \"plex_to_cache.test\">" "$plg" && grep -q "file:///boot/config/plugins/plex_to_cache.test.plg" "$plg"'
    mkdir "$out/x" && tar -C "$out/x" -xJf "$out/plex_to_cache.test-2026.01.01.90-x86_64-1.txz"
    p="$out/x/usr/local/emhttp/plugins/plex_to_cache.test"
    check "test package uses its own paths" '[ -x "$p/bin/move-to-cache" ] && grep -q "plugins/plex_to_cache.test/" "$p/plex_to_cache_test.page" "$p/include/page.php" "$p/event/stopping" && ! grep -rq "plugins/plex_to_cache/" "$p" --include=*.php --include=*.page --include=stopping --include=disks_mounted'
    check "test page is titled Test" 'grep -q "^Title=\"Move to Cache Test\"" "$p/plex_to_cache_test.page"'
    check "the test binary knows its name" 'strings "$p/bin/move-to-cache" | grep -q "plex_to_cache.test"'
else
    echo "  FAIL test build"
    fail=1
fi
check "a test build changes nothing tracked" '[ "$(git status --porcelain plex_to_cache.plg archive)" = "$before" ]'
rm -rf "$out"

if [ "$fail" = 0 ]; then
    printf '\n\033[32mAll passed\033[0m\n'
else
    printf '\n\033[31mFailures above\033[0m\n'
fi
exit $fail
