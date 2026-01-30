# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Plex to Cache is an Unraid plugin that automatically moves media from the array to the cache when a stream starts. It supports Plex, Emby, and Jellyfin media servers.

## Architecture

### Plugin Structure

- `plex_to_cache.plg` - Main Unraid plugin definition file (XML format). Contains:
  - All embedded source files (PHP, Python, shell scripts)
  - Installation/uninstallation logic
  - Version and metadata

- `src/` - Development source files (standalone versions for editing):
  - `plex_to_cache.py` - Core Python daemon that monitors streams and manages cache
  - `plex_to_cache.php` - Web UI for plugin settings (rendered in Unraid web interface)
  - `get_log.php` - AJAX endpoint for live log viewing

### Key Paths (on Unraid)

- Config: `/boot/config/plugins/plex_to_cache/settings.cfg`
- Scripts: `/usr/local/emhttp/plugins/plex_to_cache/scripts/`
- Logs: `/var/log/plex_to_cache.log`
- PID: `/var/run/plex_to_cache.pid`
- Lock: `/tmp/media_cache_cleaner.lock`

### Core Logic Flow (plex_to_cache.py)

1. Polls media server APIs at configured interval for active sessions
2. Translates Docker container paths to host paths via configurable mappings
3. After copy delay, copies currently playing media from array (`/mnt/user`) to cache (`/mnt/cache`)
4. For TV shows: caches current + upcoming episodes, cleans older episodes
5. Smart cleanup: removes watched content from cache after configurable delay
6. Mirrors file permissions from `/mnt/user0` (physical disks) to cache copies

### Configuration Options

Settings stored as key="value" in settings.cfg. Key options:
- Media server connections (Plex/Emby/Jellyfin URL + tokens)
- `ARRAY_ROOT` / `CACHE_ROOT` - Storage paths
- `DOCKER_MAPPINGS` - Path translation (format: `docker_path:host_path;...`)
- `CHECK_INTERVAL`, `COPY_DELAY`, `CACHE_MAX_USAGE`
- Smart cleanup settings for movies and episodes

## Development Notes

When modifying the plugin:
1. Edit files in `src/` for development
2. Changes must be synchronized to the embedded `<![CDATA[...]]>` sections in `plex_to_cache.plg`
3. The .plg file is the authoritative source distributed to users

The service control script (`rc.plex_to_cache`) supports: start, stop, restart
