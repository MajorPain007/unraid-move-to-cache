# Move to Cache – Unraid plugin

Copies what Plex, Emby or Jellyfin are playing from the array to a cache pool,
so the array disks can spin down, and moves the files back when they have been
watched. Internal name `plex_to_cache` (the plugin was called Plex to Cache).

## Install

Plugins → Install Plugin, paste:

```
https://raw.githubusercontent.com/MajorPain007/unraid-move-to-cache/main/plex_to_cache.plg
```

Then Settings → Utilities → Move to Cache. Requires Unraid 6.12 or newer.

## How it works

- Every few seconds the plugin asks the enabled media servers what is playing.
  The path inside the container is turned into the host path with your Docker
  path mappings (for example `/media` → `/mnt/user/Media`).
- After the copy delay the file is copied to the cache with rsync. For series
  the following episodes of the season are copied too (optionally in batches,
  and the start of the next season).
- Auto Cleanup (smart: when watched, or after a number of days) and the
  Cached files tab move files back to the array. A file is only deleted from
  the cache when an equal copy exists on the array.
- When the cache is full, the oldest files the plugin copied are moved back
  first. Nothing that is playing is moved.

## Data, connections, uninstall

- `/boot/config/plugins/plex_to_cache/settings.cfg` holds the settings,
  including the Plex token and the Emby/Jellyfin API keys **in plain text**
  (mode 0600). They are never sent back to the browser and never logged.
- `/boot/config/plugins/plex_to_cache/tracked.json` lists the files the plugin
  copied (paths with media titles).
- Runtime data lives in RAM: `/tmp/plex_to_cache/`, log `/var/log/plex_to_cache.log`.
- Connections go only to the media server addresses you enter. HTTPS
  certificates are checked; a self-signed one can be trusted by fingerprint.
- Uninstalling deletes the settings, the list and the package. Media files are
  not touched.

## Development

Go backend (`cmd/`, `internal/mtc`), Svelte 5 interface (`web/`), thin PHP
bridge (`src/include/api.php`).

```
bash tests/run.sh                    # unit tests, checks, manifest
./scripts/build.sh 2026.11.15.01     # release package and manifest
MTC_PLUGIN_NAME=plex_to_cache.test MTC_OUT_DIR=/tmp/out ./scripts/build.sh 2026.11.15.90
```
