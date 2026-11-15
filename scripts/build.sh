#!/bin/bash
set -euo pipefail

PLUGIN="${MTC_PLUGIN_NAME:-plex_to_cache}"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-$(date '+%Y.%m.%d').01}"
OUT_DIR="${MTC_OUT_DIR:-${ROOT_DIR}/archive}"
case "$OUT_DIR" in
    /*) ;;
    *) OUT_DIR="${ROOT_DIR}/${OUT_DIR}" ;;
esac
PLG="${ROOT_DIR}/plex_to_cache.plg"
ARCH="${MTC_ARCH:-amd64}"

if [[ ! "$VERSION" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$ ]]; then
    echo "ERROR: version must look like YYYY.MM.DD.NN (got '${VERSION}')" >&2
    exit 1
fi
if [[ ! "$PLUGIN" =~ ^plex_to_cache(\.[a-z0-9]+)?$ ]]; then
    echo "ERROR: plugin name must be plex_to_cache or plex_to_cache.<word> (got '${PLUGIN}')" >&2
    exit 1
fi

echo "Building ${PLUGIN}-${VERSION}-x86_64-1.txz ..."
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PDIR="${STAGE}/usr/local/emhttp/plugins/${PLUGIN}"
mkdir -p "${PDIR}/include" "${PDIR}/bin" "${PDIR}/assets" "${STAGE}/install"

( cd "${ROOT_DIR}" && GOOS=linux GOARCH="${ARCH}" CGO_ENABLED=0 go build -trimpath \
    -ldflags "-s -w -X github.com/MajorPain007/unraid-move-to-cache/internal/mtc.buildName=${PLUGIN}" \
    -o "${PDIR}/bin/move-to-cache" ./cmd/move-to-cache )
( cd "${ROOT_DIR}/web" && { [ -d node_modules ] || npm ci --silent; } && npm run --silent build >/dev/null ) \
    || { echo "the web interface did not build" >&2; exit 1; }

PAGE="plex_to_cache"
SUFFIX=""
if [ "$PLUGIN" != "plex_to_cache" ]; then
    WORD="${PLUGIN#plex_to_cache.}"
    PAGE="plex_to_cache_${WORD}"
    SUFFIX=" $(printf '%s' "${WORD:0:1}" | tr '[:lower:]' '[:upper:]')${WORD:1}"
fi
cp "${ROOT_DIR}/src/plex_to_cache.page" "${PDIR}/${PAGE}.page"
cp "${ROOT_DIR}/src/README.md" "${PDIR}/README.md"
cp "${ROOT_DIR}/src/include/page.php" "${ROOT_DIR}/src/include/api.php" "${PDIR}/include/"
cp "${ROOT_DIR}/web/dist/app.js" "${ROOT_DIR}/web/dist/app.css" "${PDIR}/assets/"
cp -R "${ROOT_DIR}/src/event" "${PDIR}/"
find "${PDIR}" -name '.DS_Store' -delete
if [ -n "$SUFFIX" ]; then
    for f in "${PDIR}/${PAGE}.page" "${PDIR}/include/page.php" "${PDIR}/event/"* "${PDIR}/README.md"; do
        sed -i.bak -e "s|plex_to_cache|${PLUGIN}|g" -e "s|^Title=\"\(.*\)\"|Title=\"\1${SUFFIX}\"|" "$f"
        rm -f "$f.bak"
    done
fi

find "${PDIR}" -type d -exec chmod 755 {} +
find "${PDIR}" -type f -exec chmod 644 {} +
chmod 755 "${PDIR}/event/"* "${PDIR}/bin/move-to-cache"

cat > "${STAGE}/install/slack-desc" << EOF2
${PLUGIN}: Move to Cache (MajorPain007)
${PLUGIN}:
${PLUGIN}: Copies what Plex, Emby or Jellyfin are playing from the array to
${PLUGIN}: the cache and moves it back when it has been watched.
${PLUGIN}:
EOF2

mkdir -p "${OUT_DIR}"
PKG="${PLUGIN}-${VERSION}-x86_64-1.txz"
( cd "${STAGE}" && COPYFILE_DISABLE=1 tar --owner=0 --group=0 --no-xattrs -cJf "${OUT_DIR}/${PKG}" install/ usr/ 2>/dev/null \
  || COPYFILE_DISABLE=1 tar --no-xattrs -cJf "${OUT_DIR}/${PKG}" install/ usr/ )
MD5=$( (md5sum "${OUT_DIR}/${PKG}" 2>/dev/null || md5 -r "${OUT_DIR}/${PKG}") | cut -d' ' -f1)

edit() {
    sed -e "s|<!ENTITY version   \"[^\"]*\">|<!ENTITY version   \"${VERSION}\">|" \
        -e "s|<!ENTITY md5       \"[^\"]*\">|<!ENTITY md5       \"${MD5}\">|" "$@"
}
if [ "$PLUGIN" = "plex_to_cache" ] && [ "${MTC_SKIP_PLG:-0}" != "1" ]; then
    edit "$PLG" > "${PLG}.tmp" && mv "${PLG}.tmp" "$PLG"
    find "${OUT_DIR}" -maxdepth 1 -name "${PLUGIN}-*-x86_64-1.txz" ! -name "${PKG}" -delete
    echo "  Updated plex_to_cache.plg"
elif [ "$PLUGIN" != "plex_to_cache" ]; then
    edit -e "s|<!ENTITY name      \"plex_to_cache\">|<!ENTITY name      \"${PLUGIN}\">|" \
         -e "s|<!ENTITY launch    \"Settings/plex_to_cache\">|<!ENTITY launch    \"Settings/${PAGE}\">|" \
         -e "s|<!ENTITY pluginURL \"[^\"]*\">|<!ENTITY pluginURL \"file:///boot/config/plugins/${PLUGIN}.plg\">|" \
         -e "s|<!ENTITY pkgURL    \"[^\"]*\">|<!ENTITY pkgURL    \"file:///boot/config/plugins/${PLUGIN}\">|" \
         "$PLG" > "${OUT_DIR}/${PLUGIN}.plg"
    echo "  Wrote ${OUT_DIR}/${PLUGIN}.plg"
fi
echo "  File : ${OUT_DIR}/${PKG}"
echo "  MD5  : ${MD5}"
