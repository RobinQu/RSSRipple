// Shared Transmission torrent status helpers (downloader detail torrent
// table, dashboard untracked rows).

/** Raw Transmission status enum → tag color (the tag text is the enum). */
export const TORRENT_STATUS_TAG_COLORS: Record<string, string> = {
  downloading: 'processing',
  'download pending': 'default',
  checking: 'processing',
  'check pending': 'default',
  seeding: 'success',
  'seed pending': 'default',
  stopped: 'warning',
};

/** Statuses that count as "in progress" (everything except stopped). */
export const ACTIVE_TORRENT_STATUSES: ReadonlySet<string> = new Set([
  'downloading',
  'seeding',
  'checking',
  'check pending',
  'download pending',
  'seed pending',
]);
