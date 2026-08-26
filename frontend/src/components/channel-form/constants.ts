/** Ordered web-fallback site whitelist (mirrors the backend registry default
 * order in app/services/metadata_source_registry.py). Selection order is
 * meaningful: earlier entries are preferred; clearing all disables the
 * fallback. */
export const DEFAULT_FALLBACK_SOURCES = [
  'bangumi',
  'mal',
  'anilist',
  'tmdb',
  'wikipedia',
  'imdb',
  'douban',
];

export const DEFAULT_FIELD_MAPPING_TEXT = JSON.stringify(
  {
    list_locator: { source: 'entries' },
    field_mappings: {
      title_cn: { source: 'title' },
      torrent_url: { source: 'link' },
      detail_url: { source: 'link' },
      published_at: { source: 'published', transform: 'iso_datetime' },
      resolution: { source: 'title', regex: '(720p|1080p|2160p|4K)', group: 1 },
      episode: { source: 'title', regex: '[第\\s]?(\\d{1,4})[话話集集]?', group: 1, transform: 'int' },
    },
  },
  null,
  2,
) as string;

/** Which tab owns a form field — drives cross-tab validation jumps. */
export const FIELD_TAB_KEYS: Record<string, 'basic' | 'metadata' | 'schedule'> = {
  name: 'basic',
  url: 'basic',
  metadata_agent_enabled: 'metadata',
  metadata_source: 'metadata',
  metadata_fallback_sources: 'metadata',
  required_metadata_fields: 'metadata',
  default_is_anime: 'metadata',
  fetch_interval: 'schedule',
  auto_cleanup_unresolved_enabled: 'schedule',
  auto_cleanup_unresolved_days: 'schedule',
  metadata_refresh_enabled: 'schedule',
  metadata_refresh_interval_minutes: 'schedule',
  metadata_refresh_full_scope: 'schedule',
};

export function tabKeyForField(fieldName: unknown): 'basic' | 'metadata' | 'schedule' | null {
  const key = Array.isArray(fieldName) ? fieldName[0] : fieldName;
  return (key && FIELD_TAB_KEYS[String(key)]) || null;
}
