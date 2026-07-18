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
new_entry = f"""### {version}
- Watched detection is now session-based: a title counts as watched
  when playback reached >= 90% in the session that just ended, instead
  of relying on the server's lifetime "played" flag (Plex viewCount /
  Emby-Jellyfin Played). Fixes rewatches being deleted from cache
  immediately after starting them. Server flags remain as fallback.
- Cache eviction (new option, default on): when the cache is full, the
  oldest plugin-cached files are moved back to the array (LRU) to make
  room for the currently streamed media. Active streams and queued
  files are never evicted.
- Next-season prefetch (new option, default off): near the end of a
  season the beginning of the next season is pre-cached (first batch
  if batching is enabled, otherwise the whole season).
- Startup consistency check: stale entries are removed from the
  tracked-files list and orphaned plugin copies (cache duplicates of
  array files inside the mapped media dirs) are re-adopted, so cleanup
  keeps working after crashes. Only mapped media directories are
  scanned — appdata/system shares and cache-only files are untouched.
- Proper log rotation: the daemon now logs through Python's
  RotatingFileHandler (5 MB, one backup), which also rotates while
  running. Previously rotation only happened at start and the stdout
  redirect kept writing to the renamed file.
- Live status in the web UI: the daemon writes a JSON snapshot
  (/var/run/plex_to_cache.status.json) with cached files/GB, cache
  usage, copy queue, current transfer and active streams; the settings
  page shows it above the log.
- Manual stop is now persistent: stopping the service (UI or CLI)
  writes a flag to the flash drive, and the service stays stopped
  across reboots, plugin updates and settings saves until you start
  it again. New rc verbs: `boot` (autostart, respects the flag) and
  `condrestart` (restart only if enabled).
- Season batching (new option): long seasons are now cached in
  configurable batches (default 30 episodes) instead of all at once.
  The next batch starts copying automatically when only a few episodes
  (default 4, i.e. around episode 26/30) remain in the current batch.
  A buffer/tolerance (default 10) makes sure seasons that only
  slightly exceed the batch size (e.g. 36 episodes) are still cached
  completely in one go.
- rsync robustness: stderr is now captured and logged (previously
  discarded, so "exit status 23" gave no clue what went wrong).
  Transfers are retried up to 3 times and resume via --partial.
  Exit codes 23/24 are tolerated when the destination file is
  verifiably complete (size check) — exit 23 is often only a
  chown/chmod/utime problem on FUSE shares although the data copied
  fine; permissions are re-applied afterwards anyway.
- Copies run in a background worker thread: a multi-gigabyte transfer
  no longer blocks stream detection and cleanup for minutes.
- Failed copies get a 5-minute cooldown instead of being retried
  (and logged) every poll interval.
- Log lines now carry timestamps.
- Free-space check now also verifies the specific file fits on the
  cache (with 512 MB headroom), not just the max-usage percentage.
- Episode detection additionally understands the "1x05" naming style
  (without matching resolutions like 1920x1080).
- Hardened config parsing: invalid numeric settings fall back to
  their defaults instead of 0 (which could busy-loop the daemon);
  poll interval is clamped to >= 1 s.
- Clean shutdown: SIGTERM/SIGINT now also terminates a running rsync
  instead of leaving it orphaned; tracked-files list is written
  atomically.

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
# `boot` respects the user's persisted stop: if the service was stopped
# manually, it stays stopped until the user starts it again.
/usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache boot >> /var/log/plex_to_cache.log 2>&1''')}
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
/usr/local/emhttp/plugins/plex_to_cache/scripts/rc.plex_to_cache condrestart
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
