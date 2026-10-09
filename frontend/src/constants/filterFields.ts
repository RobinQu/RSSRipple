// ---------------------------------------------------------------------------
// Filter-DSL field metadata — the single source of truth for the field
// catalog. ``FilterField`` (and the per-type sub-unions), FilterBuilder's
// FIELD_TYPES, filterUtils' REQUIRED_FIELD_DSL_MAP / RESOURCE_LEVEL_FIELDS
// all derive from this table; add a field by adding ONE entry here.
// Backed by ``filter_engine.py`` on the server.
// ---------------------------------------------------------------------------

export type FilterFieldType = 'string' | 'number' | 'bool' | 'list';

export interface FilterFieldMeta {
  readonly name: string;
  readonly type: FilterFieldType;
  /** Resource-level fields live on FileResource itself and are always
   * allowed in agent filters; work-namespaced fields (series.x / movie.x)
   * resolve through the linked Movie/TVSeries and unlock only when the
   * channel declares ``requiredKey`` in its required_metadata_fields. */
  readonly resourceLevel: boolean;
  /** Channel required-metadata catalog key that unlocks this field. */
  readonly requiredKey?: string;
}

export const FILTER_FIELD_META = [
  { name: 'subtitle_group', type: 'string', resourceLevel: true },
  { name: 'subtitle_groups', type: 'list', resourceLevel: true },
  { name: 'resolution', type: 'string', resourceLevel: true },
  { name: 'source', type: 'string', resourceLevel: true },
  { name: 'video_codec', type: 'string', resourceLevel: true },
  { name: 'audio_codec', type: 'string', resourceLevel: true },
  { name: 'subtitle_type', type: 'string', resourceLevel: true },
  { name: 'container', type: 'string', resourceLevel: true },
  // episode_confidence / content_type are stored as plain strings on the
  // backend but the UI treats them as enums so users pick from a fixed list.
  { name: 'episode_confidence', type: 'string', resourceLevel: true },
  { name: 'content_type', type: 'string', resourceLevel: true },
  { name: 'title_cn', type: 'string', resourceLevel: true },
  { name: 'title_en', type: 'string', resourceLevel: true },
  { name: 'search_title', type: 'string', resourceLevel: true },
  { name: 'file_size', type: 'number', resourceLevel: true },
  { name: 'episode', type: 'number', resourceLevel: true },
  { name: 'season', type: 'number', resourceLevel: true },
  { name: 'episode_start', type: 'number', resourceLevel: true },
  { name: 'episode_end', type: 'number', resourceLevel: true },
  { name: 'absolute_episode', type: 'number', resourceLevel: true },
  { name: 'is_batch', type: 'bool', resourceLevel: true },
  { name: 'subtitle_langs', type: 'list', resourceLevel: true },
  // Resource-level collection display name — franchise packs link a
  // WorkCollection directly via collection_id (work FKs all empty).
  { name: 'collection', type: 'string', resourceLevel: true },
  // Work-namespaced fields resolve through the linked Movie/TVSeries on the
  // server (rating 0-10; year from release_date / start_date).
  { name: 'movie.rating', type: 'number', resourceLevel: false, requiredKey: 'rating' },
  { name: 'movie.year', type: 'number', resourceLevel: false, requiredKey: 'year' },
  // genre is a closed canonical set on the work (see constants/genres.ts);
  // element-wise list semantics, same as subtitle_langs.
  { name: 'movie.genre', type: 'list', resourceLevel: false, requiredKey: 'genre' },
  // Collection display name (WorkCollection.title_cn or title_en) on the work.
  { name: 'movie.collection', type: 'string', resourceLevel: false, requiredKey: 'collection' },
  // Tri-state booleans on the work: true = anime, false = live-action,
  // null (empty) = undetermined — use is_empty/is_not_empty to match null.
  { name: 'movie.is_anime', type: 'bool', resourceLevel: false, requiredKey: 'is_anime' },
  { name: 'series.rating', type: 'number', resourceLevel: false, requiredKey: 'rating' },
  { name: 'series.year', type: 'number', resourceLevel: false, requiredKey: 'year' },
  { name: 'series.genre', type: 'list', resourceLevel: false, requiredKey: 'genre' },
  { name: 'series.collection', type: 'string', resourceLevel: false, requiredKey: 'collection' },
  { name: 'series.is_anime', type: 'bool', resourceLevel: false, requiredKey: 'is_anime' },
] as const satisfies readonly FilterFieldMeta[];

export type FilterField = (typeof FILTER_FIELD_META)[number]['name'];

type NamesOfType<T extends FilterFieldType> = Extract<
  (typeof FILTER_FIELD_META)[number],
  { type: T }
>['name'];

export type StringFilterField = NamesOfType<'string'>;
export type NumberFilterField = NamesOfType<'number'>;
export type BoolFilterField = NamesOfType<'bool'>;
export type ListFilterField = NamesOfType<'list'>;

/** field name → value type (FilterBuilder's FIELD_TYPES). */
export const FILTER_FIELD_TYPES = Object.fromEntries(
  FILTER_FIELD_META.map((m) => [m.name, m.type]),
) as Record<FilterField, FilterFieldType>;

/** All resource-level fields (everything not work-namespaced). */
export const RESOURCE_LEVEL_FIELDS = FILTER_FIELD_META.filter(
  (m) => m.resourceLevel,
).map((m) => m.name) as FilterField[];

/** Catalog key → the work-namespaced DSL fields it unlocks. Resource-level
    catalog keys (title_cn, resolution, …) need no entry here — they map to
    themselves and are always allowed. */
export const REQUIRED_FIELD_DSL_MAP: Record<string, FilterField[]> = {};
for (const m of FILTER_FIELD_META) {
  if ('requiredKey' in m && m.requiredKey) {
    (REQUIRED_FIELD_DSL_MAP[m.requiredKey] ??= []).push(m.name);
  }
}

/**
 * Fields an agent on this channel may use in filter DSL. Returns null when
 * the channel has no declaration (unrestricted); otherwise resource-level
 * fields plus the DSL fields mapped from the declared catalog keys.
 * (Mirrors app/services/required_fields.py.)
 */
export function allowedAgentFilterFields(
  requiredMetadataFields: string[] | null | undefined,
): FilterField[] | null {
  if (requiredMetadataFields == null) return null;
  const allowed = new Set<FilterField>(RESOURCE_LEVEL_FIELDS);
  for (const key of requiredMetadataFields) {
    for (const f of REQUIRED_FIELD_DSL_MAP[key] ?? []) allowed.add(f);
  }
  return [...allowed];
}
