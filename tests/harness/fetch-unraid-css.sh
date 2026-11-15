#!/bin/bash
set -eu
REF="${1:-master}"
DIR="$(cd "$(dirname "$0")" && pwd)/.unraid"
BASE="https://raw.githubusercontent.com/unraid/webgui/$REF/emhttp/plugins/dynamix/styles"
mkdir -p "$DIR"
for f in default-color-palette.css default-base.css default-dynamix.css font-awesome.css clear-sans.woff clear-sans-bold.woff font-awesome.woff; do
    curl -fsSL -o "$DIR/$f" "$BASE/$f"
done
for t in black white azure gray; do
    curl -fsSL -o "$DIR/theme-$t.css" "$BASE/themes/$t.css"
done
echo "Unraid stylesheets ($REF) in $DIR"
