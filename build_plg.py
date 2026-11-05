#!/usr/bin/env python3
"""
Regenerate plex_to_cache.plg from src/ files.

This avoids the dual-maintenance problem mentioned in the project notes: we only
edit files under src/, and this script re-emits the inline <![CDATA[...]]>
blocks in plex_to_cache.plg so the distributed .plg stays in sync.

Usage:
    python3 build_plg.py [VERSION]

If VERSION is omitted, the current version in plex_to_cache.plg is kept.
"""
import os, sys, re, pathlib

ROOT = pathlib.Path(__file__).parent.resolve()
SRC  = ROOT / "src"
PLG  = ROOT / "plex_to_cache.plg"

def read(p):
    return p.read_text()

py_code  = read(SRC / "plex_to_cache.py")
php_code = read(SRC / "plex_to_cache.php")
page     = read(SRC / "plex_to_cache.page")
rc       = read(SRC / "rc.plex_to_cache")
events   = {name: read(SRC / "event" / name) for name in ("stopping", "disks_mounted")}

version_arg = sys.argv[1] if len(sys.argv) > 1 else None
if version_arg is None and PLG.exists():
    m = re.search(r'<!ENTITY version\s+"([^"]+)"', read(PLG))
    version_arg = m.group(1) if m else None
if not version_arg:
    sys.exit("No version given and none found in the .plg - pass one, e.g. 2026.08.08.01")
version = version_arg
if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d+", version):
    sys.exit(f"Version {version!r} must look like YYYY.MM.DD.NN")

changes_body = read(SRC / "CHANGES.md").strip()
_bad = [(n, ln) for n, ln in enumerate(changes_body.splitlines(), 1)
        if re.search(r"&(?!(amp|lt|gt|quot|apos);)", ln) or re.search(r"<[A-Za-z/!?]", ln)]
if _bad:
    for n, ln in _bad[:10]:
        print(f"  src/CHANGES.md:{n}: {ln.strip()}")
    sys.exit("Changelog contains characters that are not valid inside XML (& or <)")
if not changes_body.startswith(f"### {version}"):
    print(f"  note: src/CHANGES.md does not start with an entry for {version}")



def cdata(s):
    s = s.replace("]]>", "]]]]><![CDATA[>")
    return "<![CDATA[\n" + s.rstrip() + "\n]]>"

plg = f"""<?xml version='1.0' standalone='yes'?>
<!DOCTYPE PLUGIN [
<!ENTITY name      "plex_to_cache">
<!ENTITY author    "MajorPain007">
<!ENTITY version   "{version}">
<!ENTITY launch    "Utilities/plex_to_cache">
<!ENTITY pluginURL "https://raw.githubusercontent.com/MajorPain007/unraid-move-to-cache/main/plex_to_cache.plg">
]>
<PLUGIN name="&name;" author="&author;" version="&version;" launch="&launch;" pluginURL="&pluginURL;" icon="server">

<DESCRIPTION>
Plex to Cache: Automatically moves media from array to cache on stream start. Includes smart cleanup, permission mirroring and season-ahead caching for Plex, Emby and Jellyfin.
</DESCRIPTION>

<CHANGES>
{changes_body.strip()}
</CHANGES>

<FILE Run="/bin/bash">
<INLINE>
{cdata('''if [ -f /var/run/plex_to_cache.pid ]; then
    kill $(cat /var/run/plex_to_cache.pid) 2>/dev/null
    rm -f /var/run/plex_to_cache.pid
fi
pkill -f 'plex_to_cache/scripts/plex_to_cache.py$' 2>/dev/null

rm -f /usr/local/emhttp/plugins/plex_to_cache/plex_to_cache.php
rm -f /usr/local/emhttp/plugins/plex_to_cache/plex_to_cache.page
rm -f /usr/local/emhttp/plugins/plex_to_cache/scripts/plex_to_cache.py
rm -f /usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache
rm -f /usr/local/emhttp/plugins/plex_to_cache/event/stopping
rm -f /usr/local/emhttp/plugins/plex_to_cache/event/disks_mounted

mkdir -p /usr/local/emhttp/plugins/plex_to_cache/scripts
mkdir -p /usr/local/emhttp/plugins/plex_to_cache/event
mkdir -p /boot/config/plugins/plex_to_cache''')}
</INLINE>
</FILE>

<FILE Name="/usr/local/emhttp/plugins/plex_to_cache/plex_to_cache.page">
<INLINE>
{cdata(page)}
</INLINE>
</FILE>

<FILE Name="/usr/local/emhttp/plugins/plex_to_cache/plex_to_cache.php">
<INLINE>
{cdata(php_code)}
</INLINE>
</FILE>

<FILE Name="/usr/local/emhttp/plugins/plex_to_cache/scripts/plex_to_cache.py" Mode="0755">
<INLINE>
{cdata(py_code)}
</INLINE>
</FILE>

<FILE Name="/usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache" Mode="0755">
<INLINE>
{cdata(rc)}
</INLINE>
</FILE>

<FILE Name="/usr/local/emhttp/plugins/plex_to_cache/event/stopping" Mode="0755">
<INLINE>
{cdata(events["stopping"])}
</INLINE>
</FILE>

<FILE Name="/usr/local/emhttp/plugins/plex_to_cache/event/disks_mounted" Mode="0755">
<INLINE>
{cdata(events["disks_mounted"])}
</INLINE>
</FILE>

<FILE Name="/etc/rc.d/rc.plex_to_cache" Mode="0755">
<INLINE>
{cdata('''#!/bin/bash
/usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache boot >> /var/log/plex_to_cache.log 2>&1''')}
</INLINE>
</FILE>

<FILE Run="/bin/bash">
<INLINE>
{cdata(f'''chmod +x /usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache
chmod +x /usr/local/emhttp/plugins/plex_to_cache/scripts/plex_to_cache.py
chmod +x /usr/local/emhttp/plugins/plex_to_cache/event/stopping
chmod +x /usr/local/emhttp/plugins/plex_to_cache/event/disks_mounted
chmod +x /etc/rc.d/rc.plex_to_cache
touch /var/log/plex_to_cache.log
chmod 666 /var/log/plex_to_cache.log
/usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache condrestart
echo "Plex to Cache v{version} installed successfully."''')}
</INLINE>
</FILE>

<FILE Run="/bin/bash" Method="remove">
<INLINE>
{cdata('''if [ -f /var/run/plex_to_cache.pid ]; then
    kill $(cat /var/run/plex_to_cache.pid) 2>/dev/null
    rm -f /var/run/plex_to_cache.pid
fi
pkill -f plex_to_cache.py 2>/dev/null
rm -f /etc/rc.d/rc.plex_to_cache
rm -rf /usr/local/emhttp/plugins/plex_to_cache
rm -f /var/log/plex_to_cache.log /var/log/plex_to_cache.log.1
rm -f /var/run/plex_to_cache.*
echo "Plex to Cache uninstalled. Settings in /boot/config/plugins/plex_to_cache preserved."''')}
</INLINE>
</FILE>

</PLUGIN>
"""

PLG.write_text(plg)
print(f"Wrote {PLG}")
print(f"  Version : {version}")
print(f"  Size    : {len(plg)} bytes")
print(f"  Lines   : {plg.count(chr(10))}")
