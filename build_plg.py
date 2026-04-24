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

# Read src files
py_code  = read(SRC / "plex_to_cache.py")
php_code = read(SRC / "plex_to_cache.php")
page     = read(SRC / "plex_to_cache.page")
rc       = read(SRC / "rc.plex_to_cache")

# Determine version
version_arg = sys.argv[1] if len(sys.argv) > 1 else None
if version_arg is None and PLG.exists():
    m = re.search(r'<!ENTITY version\s+"([^"]+)"', read(PLG))
    version_arg = m.group(1) if m else "2026.04.24a"
version = version_arg or "2026.04.24a"

# CHANGES section — preserved if present in current .plg + new entry prepended
new_entry = """### 2026.04.24a
- No internet required on boot: requests dependency removed, daemon
  now uses Python's built-in urllib. On Unraid the root filesystem is
  tmpfs — the previous install step downloaded pip and installed
  `requests` on every install/boot. Without internet the service
  failed to start. Now the plugin has zero external Python
  dependencies.
- Automatic service start on boot: the rc.d script is now registered
  at /etc/rc.d/rc.plex_to_cache and called by Unraid at boot, so the
  daemon comes up on its own. Previously it only started on install
  and when settings were saved.
- Bugfix (cleanup): `cleanup_empty_dirs` no longer computes wrong
  protected paths when docker mappings use absolute host paths. The
  set of paths that must never be rmdir'd is now derived correctly
  by translating ARRAY_ROOT → CACHE_ROOT.
- Bugfix (exceptions): replaced bare `except:` in the API/file path
  with specific exception classes so Ctrl-C / SIGTERM and programming
  errors are no longer silently swallowed.
- Log rotation: /var/log/plex_to_cache.log is now rotated at 5 MB
  on daemon start (one .1 copy is kept).
- rc.plex_to_cache: adds a `status` verb, cleans up stale PID files
  on start, and waits up to 5 s on SIGTERM before SIGKILL.

"""

existing_changes = ""
if PLG.exists():
    m = re.search(r"<CHANGES>\s*(.*?)\s*</CHANGES>", read(PLG), re.S)
    if m:
        existing_changes = m.group(1).strip() + "\n"
        # drop any prior entry with the same version so re-running this
        # script doesn't stack duplicates
        existing_changes = re.sub(
            rf"### {re.escape(version)}\n(?:(?!^### ).*\n)*",
            "",
            existing_changes,
            flags=re.M,
        )

changes_body = new_entry + existing_changes

def cdata(s):
    # Nothing in our src files contains the literal "]]>" string, but belt
    # and braces: split it if it ever appears.
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

<!-- Pre-install: stop running service, clean old files -->
<FILE Run="/bin/bash">
<INLINE>
{cdata('''# Stop previous service (if any)
if [ -f /var/run/plex_to_cache.pid ]; then
    kill $(cat /var/run/plex_to_cache.pid) 2>/dev/null
    rm /var/run/plex_to_cache.pid
fi
pkill -f plex_to_cache.py 2>/dev/null

# Remove old plugin files so updates take effect cleanly
rm -f /usr/local/emhttp/plugins/plex_to_cache/plex_to_cache.php
rm -f /usr/local/emhttp/plugins/plex_to_cache/plex_to_cache.page
rm -f /usr/local/emhttp/plugins/plex_to_cache/scripts/plex_to_cache.py
rm -f /usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache

# Create plugin directories
mkdir -p /usr/local/emhttp/plugins/plex_to_cache/scripts
mkdir -p /boot/config/plugins/plex_to_cache

# NOTE: no pip / curl here on purpose — this plugin has no external
# Python dependencies any more. Everything the daemon needs is in the
# stdlib (urllib, ssl, json, fcntl, subprocess). Install & boot work
# offline.''')}
</INLINE>
</FILE>

<!-- Plugin page wrapper -->
<FILE Name="/usr/local/emhttp/plugins/plex_to_cache/plex_to_cache.page">
<INLINE>
{cdata(page)}
</INLINE>
</FILE>

<!-- Web UI (PHP) -->
<FILE Name="/usr/local/emhttp/plugins/plex_to_cache/plex_to_cache.php">
<INLINE>
{cdata(php_code)}
</INLINE>
</FILE>

<!-- Daemon (Python) -->
<FILE Name="/usr/local/emhttp/plugins/plex_to_cache/scripts/plex_to_cache.py" Mode="0755">
<INLINE>
{cdata(py_code)}
</INLINE>
</FILE>

<!-- Service control script -->
<FILE Name="/usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache" Mode="0755">
<INLINE>
{cdata(rc)}
</INLINE>
</FILE>

<!-- Register a boot-time autostart. Unraid runs /etc/rc.d/rc.<name> at
     boot if present (tmpfs root — must be re-created on every install). -->
<FILE Name="/etc/rc.d/rc.plex_to_cache" Mode="0755">
<INLINE>
{cdata('''#!/bin/bash
# Auto-start wrapper — Unraid calls this at boot.
/usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache start >> /var/log/plex_to_cache.log 2>&1''')}
</INLINE>
</FILE>

<!-- Post-install: permissions, logfile, start the service -->
<FILE Run="/bin/bash">
<INLINE>
{cdata(f'''chmod +x /usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache
chmod +x /usr/local/emhttp/plugins/plex_to_cache/scripts/plex_to_cache.py
chmod +x /etc/rc.d/rc.plex_to_cache
touch /var/log/plex_to_cache.log
chmod 666 /var/log/plex_to_cache.log
/usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache restart
echo "Plex to Cache v{version} installed successfully."''')}
</INLINE>
</FILE>

<!-- Uninstall -->
<FILE Run="/bin/bash" Method="remove">
<INLINE>
{cdata('''if [ -f /var/run/plex_to_cache.pid ]; then
    kill $(cat /var/run/plex_to_cache.pid) 2>/dev/null
    rm /var/run/plex_to_cache.pid
fi
pkill -f plex_to_cache.py 2>/dev/null
rm -f /etc/rc.d/rc.plex_to_cache
rm -rf /usr/local/emhttp/plugins/plex_to_cache
rm -f /var/log/plex_to_cache.log
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
