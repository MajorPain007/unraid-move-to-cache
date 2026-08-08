### 2026.08.08.13

- "On cache" was hidden behind the To Array column. Pinning a column to the
  right edge means it covers whatever is underneath, and the table was wider
  than its box. The table now always fits: fixed columns, and a long name is
  shortened with the full path in the row's tooltip. Nothing scrolls sideways,
  so nothing needs pinning to the right and nothing gets covered.
- "../" is pinned under the header again, this time with the same shadow the
  header has. That edge was what was missing: a row passing behind it now reads
  as passing behind it.

### 2026.08.08.12

- The pinned "../" row was covering the buttons of the rows scrolling under it.
  It is an ordinary first row again - keeping it in place bought nothing, since
  the clickable path above the list is always visible and reaches any level in
  one click.
- The column headers stay pinned but now cast a shadow, so a row passing behind
  them reads as passing behind them rather than as something broken.

### 2026.08.08.11

- The top right corner of the list stayed black. The pinned column inherits the
  row colour, but a header cell inherits from its row, and only the header cells
  carried a colour - so that one cell inherited transparency. The colour sits on
  the header row now.
- Hidden entries such as .Recycle.Bin are no longer listed. They are not media
  and moving them to the array serves no purpose.

### 2026.08.08.10

- The list header was darker than the rows below it, so it read as a black band
  rather than a header. The shades now step up evenly from the card to the rows
  to the header, and every label clears the AA contrast threshold against the
  background it actually sits on.

### 2026.08.08.09

- One level up is the familiar "../" entry at the top of the list again, not a
  button beside it. It stays pinned under the column headers, so it is still
  reachable from anywhere in a long folder.
- The strip behind the To Array button had a fixed dark colour of its own, which
  showed as a black block against the grey rows. The pinned cell inherits the
  row colour now, striping and hover included.

### 2026.08.08.08

- Fixes 2026.08.08.07, whose manifest was not valid XML: a bare ampersand in the
  changelog. It goes into the manifest verbatim, so Unraid could not read the
  file. The build now refuses a changelog containing characters XML does not
  allow, rather than producing a broken plugin.

### 2026.08.08.07

- The Empty Cache button is gone. Picking the media folder in the browser does
  the same thing, and the settings section that governs it is now called Move
  Back to Array.
- "Save and Apply" was being clipped: the control bar was a flex row that could
  not wrap, and the submit button shrank past its own label before anything else
  gave way. It wraps now and keeps its width.
- The list has a frame that runs to the edge behind the To Array button, instead
  of stopping short of it.
- "Up" moved out of the table and next to the path, so it stays put however far
  you scroll. The column headers stay put too.
- The path is clickable now: each segment jumps to that level.
- Folders and files carry an icon, rows highlight under the pointer, and a
  button that cannot be used looks like it.

### 2026.08.08.06

- The "To Array" button stays put. A long name now keeps to one line and the
  list scrolls sideways, with the button column stuck to the right edge, so it
  is always where you left it and never has to be scrolled to.

### 2026.08.08.05

- The page no longer scrolls sideways. Grid items default to min-width:auto and
  refuse to shrink below their content, so one long file name in the list widened
  the whole layout. The columns may shrink now, and the list has a fixed table
  layout, so a name wraps instead of pushing the page open.
- "To Array" no longer asks for confirmation. It is not a destructive action -
  the file goes to the array and gets cached again the next time it is played -
  and the browser's own "JavaScript from ..." prompt on every click was worse
  than the risk. Emptying the whole cache still asks.
- Folders and episodes are sorted by name, naturally, so episode 2 comes before
  episode 10.
- Request parameters are read from POST and GET directly instead of $_REQUEST,
  which depends on the request_order php.ini setting.

### 2026.08.08.04

- The browser's requests are POSTs now. Called directly the endpoint answered
  fine, but from the page the request never reached the server at all - status 0
  with an empty body, and nothing in any server log. That is what a content
  blocker in the browser looks like, and the same thing once broke the snapshot
  browser in the other plugin. The file list, the per-file move and the flush
  button moved with it; the flush also stops putting its CSRF token in a URL,
  where it ends up in logs.

### 2026.08.08.03

- The browser reported "HTTP ?" - an empty response with no status code, which
  means PHP died before answering. A try/catch cannot see that, so the endpoint
  now reports through a shutdown handler as well and names the actual reason
  (execution timeout, memory, and so on).
- Removed the error handler that turned warnings into exceptions. It was the one
  construct the failing endpoint had and the working one did not, and it bought
  little that the try/catch does not already cover.

### 2026.08.08.02

- The cache browser could sit on "Loading..." forever: when the request failed
  the error went into the path line while the table kept its placeholder. The
  table now shows the actual status and response, so a failure says why.
- The browse endpoint always answers with JSON, including when something throws.
- Folder totals come from the tracked list instead of walking the tree for every
  row. The old way did one recursive scan per line, which is fine on a handful
  of files and not on a real library over FUSE.
- A folder shows how many of its cached files are being streamed. Those stay on
  the cache when the folder is moved - that was already true, it just was not
  visible.

### 2026.08.08.01

- Cache browser: walk the mapped media folders in the web UI, see how much of
  each folder sits on the cache, and send a single file, one season or a whole
  series back to the array. Replaces the flat file list.
- Version numbers now follow YYYY.MM.DD.NN, matching the other plugin.
- The changelog is a source file again. build_plg.py had a hardcoded entry that
  every build re-stamped with the current version, so the last four releases all
  shipped identical notes under different numbers, none of it describing what
  had actually changed.

### 2026.08.08b

- The web UI lists what is on the cache, biggest first, with a button per file
  to send it back to the array.
- Daily schedule for emptying the cache (off by default). Due-based: a run
  missed because the server was off happens at the next opportunity.
- New Scope setting. "All media in mapped folders" also moves files this plugin
  never copied, so media that landed on the cache another way does not stay
  there. A file whose name already exists on the array is skipped rather than
  overwritten, and recently written files are left alone.

### 2026.08.08a

- Empty Cache button: moves everything this plugin cached back to the array.
  Files being streamed and files still being copied stay put and are reported
  as skipped.

### 2026.08.08

- Fixed files being moved to the wrong place: a cache path was mapped back to
  the user share instead of the physical array, and docker mappings with a
  shared prefix could pick the wrong one.
- rsync no longer uses --inplace, so an interrupted transfer cannot leave a
  truncated file under the final name.
- A season finale check that failed to read its folder no longer assumes the
  season is over and evicts it.
- CSRF protection on the settings page and its endpoints.
- Test suite and CI.

### 2026.07.18

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

### 2026.04.24a

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

### 2026.01.30l

- Removed: "Move to Array" buttons (clearcache, moveother, moveall) - feature was unstable
- Simplified: Cleaner codebase without unused CLI actions

### 2026.01.30g

- Fixed: rsync now uses --inplace (no temp files with random suffixes)
- Fixed: Files now get correct ownership (nobody:users) and permissions
- Fixed: No more .filename.mkv.XXXXX temp files left behind

### 2026.01.30f

- Changed: Removed "Running/Stopped" text, only status dot remains (hover for tooltip)
- Fixed: Test button now tests current form values BEFORE saving

### 2026.01.30e

- Fixed: Plugin update now properly overwrites old files (no reinstall needed)
- Removed: Restart button (less cluttered UI)
- Changed: Reduced gap between columns (12px instead of 20px)
- Changed: Adjusted column widths for better balance

### 2026.01.30d

- Fixed: Test button now properly validates connections using curl with HTTP status codes
- Fixed: Grid layout - settings columns narrower, log column wider (2fr)
- Fixed: UI shows proper error messages (No Token, 401 Unauthorized, Connection error)

### 2026.01.30c

- Added: "Move ALL to Array" button - moves all cache files including plugin-cached media
- Added: "Move Cached Media to Array" button (renamed from Clear Cache)
- Changed: All move operations now check /mnt/user0 before deleting (safety check)
- Changed: Start/Stop/Restart buttons moved to same row as Save button
- Changed: Settings columns narrower, Log column wider for better readability
- Fixed: Files are moved to array instead of deleted when possible

### 2026.01.30b

- Added: Cleanup mode selector (None / Smart / Days-based)
- Added: Days-based cleanup - automatically moves files after X days
- Changed: Smart cleanup and days-based cleanup are mutually exclusive
- Note: Files now track when they were cached for days-based cleanup

### 2026.01.30a

- Added: File tracking system for cached media
- Added: "Move Other Files to Array" button (moves all non-plugin files to array)
- Added: Tracked files counter in UI
- Changed: Clear cache now only removes plugin-cached files

### 2026.01.30

- Added: Service status indicator with Start/Stop/Restart buttons
- Added: Connection test buttons for all media servers
- Added: Clear cache button to remove all cached media
- Changed: All text now in English
- Changed: Integrated log endpoint into main PHP file
- Fixed: SSL warnings are now suppressed
- Fixed: Errors are now logged instead of silently ignored

### 2025.12.31.12

- Fix: Settings are no longer deleted during update/uninstall (Safe Update)
- UI: Finalized CSS Grid Layout (25/25/50 split)

### 2025.12.31.11

- UI: New Layout Ratio (25% Server / 25% Tuning / 50% Log)
