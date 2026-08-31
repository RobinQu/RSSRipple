import type {
  BoolCondition,
  FieldCondition,
  FilterConfig,
  FilterField,
  FilterOperator,
} from '../types';
import type { TFunction } from 'i18next';

export function subtitleGroupsText(resource: { subtitle_groups?: string[] | null; subtitle_group?: string | null }): string {
  return (resource.subtitle_groups?.length ? resource.subtitle_groups : (resource.subtitle_group ? [resource.subtitle_group] : [])).join(' & ');
}

// ---------------------------------------------------------------------------
// Filter tree guards & helpers — shared by FilterBuilder and its consumers.
// Lives outside FilterBuilder.tsx so that file only exports components
// (react-refresh/only-export-components).
// ---------------------------------------------------------------------------

const isBoolCondition = (node: unknown): node is BoolCondition => {
  return (
    typeof node === 'object' &&
    node !== null &&
    'combinator' in node &&
    'conditions' in node
  );
};

const isFieldCondition = (node: unknown): node is FieldCondition => {
  return (
    typeof node === 'object' &&
    node !== null &&
    'field' in node &&
    'operator' in node
  );
};

// A value-taking operator with an empty value is rejected by the backend at
// save time; blank strings count as empty too.
const isEmptyValue = (v: FieldCondition['value'] | null | undefined): boolean =>
  v === undefined ||
  v === null ||
  (typeof v === 'string' && v.trim() === '') ||
  (Array.isArray(v) && v.length === 0);

function emptyBool(): BoolCondition {
  return { combinator: 'and', conditions: [] };
}

export { isBoolCondition, isFieldCondition, isEmptyValue, emptyBool };

// Operators that take no value (is_empty / is_not_empty). Valid for every
// field type; the backend ignores ``value`` when one of these is selected.
const NO_VALUE_OPERATORS: FilterOperator[] = ['is_empty', 'is_not_empty'];
export const isNoValueOperator = (op: FilterOperator): boolean =>
  NO_VALUE_OPERATORS.includes(op);

export function normalizeFilter(v: FilterConfig | null | undefined): BoolCondition {
  if (isBoolCondition(v)) return v as BoolCondition;
  return emptyBool();
}

export function isFilterEmpty(v: FilterConfig | null | undefined): boolean {
  if (!v) return true;
  if (!isBoolCondition(v)) return true;
  if (!v.conditions || v.conditions.length === 0) return true;
  return false;
}

/**
 * Collapse an emptied-out tree (`{combinator, conditions: []}` — e.g. the
 * user just deleted the last condition) to `null` ("no filter", pass-all).
 * The backend rejects empty condition lists, so every save/preview payload
 * must pass through this.
 */
export function nullIfEmptyFilter(
  v: FilterConfig | null | undefined,
): BoolCondition | null {
  return isFilterEmpty(v) ? null : (v as BoolCondition);
}

/** Walk the tree and return every leaf field condition (depth-first). */
export function collectFieldConditions(
  config: BoolCondition | null | undefined,
): FieldCondition[] {
  if (!config || !isBoolCondition(config)) return [];
  const out: FieldCondition[] = [];
  const walk = (node: BoolCondition) => {
    for (const c of node.conditions || []) {
      if (isBoolCondition(c)) walk(c);
      else if (isFieldCondition(c)) out.push(c);
    }
  };
  walk(config);
  return out;
}

/**
 * Leaves whose operator takes a value but the value is empty — the backend
 * rejects these at save time (422), so callers check before submitting.
 * No-value operators (is_empty / is_not_empty) are never invalid.
 */
export function findInvalidConditions(
  config: BoolCondition | null | undefined,
): FieldCondition[] {
  return collectFieldConditions(config).filter(
    (c) => !isNoValueOperator(c.operator) && isEmptyValue(c.value),
  );
}

/** Fields whose value is a fixed enum — the one-liner localizes the raw value
    (e.g. content_type "tv" → "剧集") instead of dumping the storage token. */
const ENUM_FIELDS = new Set(['episode_confidence', 'content_type']);

function enumValueLabel(field: string, value: string, t: TFunction): string {
  return ENUM_FIELDS.has(field)
    ? t(`filter.enumValue_${value}` as never, { defaultValue: value })
    : value;
}

/** Human-readable one-liner for a leaf condition, e.g. "Resolution Equals 1080p". */
export function describeCondition(cond: FieldCondition, t: TFunction): string {
  const field = t(`filter.${cond.field}` as never, { defaultValue: cond.field });
  const op = t(`filter.${cond.operator}` as never, { defaultValue: cond.operator });
  if (isNoValueOperator(cond.operator)) return `${field} ${op}`;
  const v = cond.value;
  const text = Array.isArray(v)
    ? v.map((x) => enumValueLabel(cond.field, String(x), t)).join(', ')
    : typeof v === 'boolean'
      ? t(v ? 'filter.true' : 'filter.false')
      : enumValueLabel(cond.field, String(v ?? ''), t);
  return `${field} ${op} ${text}`.trim();
}

/** Whole tree as a `; `-joined one-liner (empty string when no conditions). */
export function describeFilter(
  config: BoolCondition | null | undefined,
  t: TFunction,
): string {
  return collectFieldConditions(config)
    .map((c) => describeCondition(c, t))
    .join('; ');
}

// ---------------------------------------------------------------------------
// Channel required-metadata-fields → agent filter-DSL gating (mirrors
// app/services/required_fields.py). The catalog covers every DSL field:
// resource-level fields are catalog keys under their own name and are always
// allowed in agent filters; work-namespaced fields unlock only when the
// channel declares the paired key below.
// ---------------------------------------------------------------------------

/** Catalog key → the work-namespaced DSL fields it unlocks. Resource-level
    catalog keys (title_cn, resolution, …) need no entry here — they map to
    themselves and are always allowed. */
export const REQUIRED_FIELD_DSL_MAP: Record<string, FilterField[]> = {
  rating: ['series.rating', 'movie.rating'],
  year: ['series.year', 'movie.year'],
  genre: ['series.genre', 'movie.genre'],
  is_anime: ['series.is_anime', 'movie.is_anime'],
  collection: ['series.collection', 'movie.collection'],
};

// All resource-level fields (everything not work-namespaced). Keep in sync
// with the field groups in FilterBuilder.
const RESOURCE_LEVEL_FIELDS: FilterField[] = [
  'subtitle_groups', 'subtitle_group', 'resolution', 'source', 'video_codec', 'audio_codec',
  'subtitle_type', 'subtitle_langs', 'container', 'file_size',
  'episode', 'season', 'episode_start', 'episode_end', 'absolute_episode',
  'is_batch', 'episode_confidence',
  'title_cn', 'title_en', 'search_title',
  'content_type', 'collection',
];

/**
 * Fields an agent on this channel may use in filter DSL. Returns null when
 * the channel has no declaration (unrestricted); otherwise resource-level
 * fields plus the DSL fields mapped from the declared catalog keys.
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
