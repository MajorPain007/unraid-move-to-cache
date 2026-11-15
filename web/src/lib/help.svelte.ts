export const shown = $state<Record<string, boolean>>({});

export const HELP: Record<string, string> = {
  URL: 'Address of the media server with port, e.g. http://192.168.1.10:32400. Use https only if the server offers it.',
  TOKEN: 'Plex: open any item, Get Info, View XML; the X-Plex-Token is at the end of the address. Emby/Jellyfin: Dashboard, API Keys. Stored on the flash drive and never shown again.',
  TLS: 'Checks that the https certificate belongs to the server, so nobody on the network can pose as it and read the token. Self-signed, or Plex at https://IP: Test connection, then Trust this certificate.',
  CACHE_ROOT: 'The fast pool that playing media is copied to, usually /mnt/cache. Only pools are offered.',
  ARRAY_ROOT: 'Where your media lives. /mnt/user covers all shares and is right for nearly everyone.',
  MAPPINGS: 'The media server sees other paths than Unraid. Enter each folder as the container sees it (e.g. /media) and as Unraid sees it (e.g. /mnt/user/Media) - the same as in the container settings.',
  COPY_DELAY: 'How long something must play before it is copied, so zapping through does not copy anything.',
  CHECK_INTERVAL: 'How often the media servers are asked what is playing.',
  CACHE_MAX_USAGE: 'Nothing new is copied once the pool is this full.',
  SERIES: 'For very long seasons only a part ahead of the current episode is copied instead of the whole season.',
  BATCH: 'Episodes per batch. Seasons up to batch + tolerance episodes are copied whole.',
  NEXT_SEASON: 'Near the end of a season the start of the next one is copied too.',
  AHEAD: 'When this many episodes are left, the next batch or season is started.',
  MEDIA_FILETYPES: 'Only files with these endings are copied, separated by spaces.',
  EXCLUDE_DIRS: 'Folders with these names are never copied, separated by commas.',
  CLEANUP_MODE: 'When copies go back to the array. When watched: after 90 % was played. After days: after a set time. Never: they stay until you move them in the Cache tab.',
  MOVIE_DELETE_DELAY: 'Wait this long after a movie was watched before it goes back.',
  EPISODE_KEEP_PREVIOUS: 'This many episodes before the current one stay on the cache; older ones go back.',
  CACHE_MAX_DAYS: 'Copies older than this go back to the array.',
  EVICTION: 'When the pool limit is reached, the oldest copies go back to make room. Nothing that is playing is moved.',
  NOTIFY_OFFLINE_MINUTES: 'An Unraid notification is sent when a media server has not answered for this long.'
};
