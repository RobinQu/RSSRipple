import type { FileResource } from '../types';

// ---------------------------------------------------------------------------
// Channel required-field column helpers — shared by the channel resource
// tables (flat + grouped views). Columns come from the channel's configured
// required_metadata_fields; per-row rendering filters by the resource's shape
// so type-irrelevant fields never show a misleading "—" (e.g. batch episode
// ranges on movies).
// ---------------------------------------------------------------------------

/** Row shape drives field applicability. */
export type RowShape = 'tv_single' | 'tv_batch' | 'franchise' | 'movie' | 'unknown';

const WORK_SHAPES: RowShape[] = ['tv_single', 'tv_batch', 'movie'];
const TV_SHAPES: RowShape[] = ['tv_single', 'tv_batch'];

/** Derive the row's shape from which work/collection FKs it carries. */
export function resourceShape(r: FileResource): RowShape {
  if (r.collection_id && !r.series_id && !r.movie_id && !r.audio_work_id) {
    return 'franchise';
  }
  if (r.series_id) return r.is_batch ? 'tv_batch' : 'tv_single';
  if (r.movie_id) return 'movie';
  return 'unknown';
}

/**
 * Field → applicable shapes. Missing entry = applicable to every shape
 * (resource-level parse fields). Work-pair keys need a linked work, hence the
 * WORK_SHAPES restriction; episode machinery is TV-only.
 */
const APPLICABILITY: Record<string, RowShape[] | undefined> = {
  // 基础必选（全形态适用；year/is_anime 需链接作品）
  title_cn: undefined,
  title_en: undefined,
  search_title: undefined,
  content_type: undefined,
  is_batch: undefined,
  year: WORK_SHAPES,
  is_anime: WORK_SHAPES,
  // TV 集数字段：按单集/合集区分
  season: TV_SHAPES,
  episode: ['tv_single'],
  episode_start: ['tv_batch'],
  episode_end: ['tv_batch'],
  absolute_episode: TV_SHAPES,
  episode_confidence: TV_SHAPES,
  // 多作品合集（franchise 包）专属
  resource_collection: ['franchise'],
  // 其余作品级字段需链接作品
  rating: WORK_SHAPES,
  genre: WORK_SHAPES,
  collection: WORK_SHAPES,
};

export function fieldApplicable(key: string, shape: RowShape): boolean {
  const shapes = APPLICABILITY[key];
  return !shapes || shapes.includes(shape);
}

// Column display order: work-type grouping first (基础必选 → 合集TV集数范围 →
// 多作品合集关联), then remaining fields in semantic/catalog order.
const GROUP_RANK: Record<string, number> = {
  title_cn: 0,
  title_en: 0,
  search_title: 0,
  content_type: 0,
  is_batch: 0,
  year: 0,
  is_anime: 0,
  season: 1,
  episode: 1,
  episode_start: 1,
  episode_end: 1,
  episode_confidence: 1,
  resource_collection: 2,
};

// Canonical catalog order (mirrors app/services/required_fields.py) for the
// rank-3 tail and tie-breaking inside each group.
const CATALOG_ORDER: string[] = [
  'title_cn', 'title_en', 'search_title',
  'content_type', 'is_batch', 'year', 'is_anime',
  'season', 'episode', 'episode_start', 'episode_end',
  'absolute_episode', 'episode_confidence',
  'resource_collection',
  'subtitle_group', 'resolution', 'source', 'video_codec', 'audio_codec',
  'subtitle_type', 'subtitle_langs', 'container', 'file_size',
  'rating', 'genre', 'collection',
];

/**
 * Keys never rendered as stacked columns — the 作品 column already carries
 * them: raw/original title in its link text (titles), the series/movie tag
 * (content_type) and the batch tag (is_batch).
 */
export const HIDDEN_COLUMN_KEYS: ReadonlySet<string> = new Set([
  'title_cn',
  'title_en',
  'search_title',
  'content_type',
  'is_batch',
]);

/** Order configured keys into the column display order, dropping the keys
 * surfaced by the work column itself. */
export function orderedRequiredKeys(keys: string[]): string[] {
  const visible = keys.filter((k) => !HIDDEN_COLUMN_KEYS.has(k));
  const catalogIdx = new Map(CATALOG_ORDER.map((k, i) => [k, i]));
  return visible.sort((a, b) => {
    const ra = GROUP_RANK[a] ?? 3;
    const rb = GROUP_RANK[b] ?? 3;
    if (ra !== rb) return ra - rb;
    return (catalogIdx.get(a) ?? 99) - (catalogIdx.get(b) ?? 99);
  });
}

// ---------------------------------------------------------------------------
// Per-channel column configuration (persisted in localStorage)
// ---------------------------------------------------------------------------

/** Every catalog key is a configurable column (作品/操作 are fixed table
 * columns outside this pool). Mirrors app/services/required_fields.py. */
export const COLUMN_POOL: readonly string[] = CATALOG_ORDER;

export interface ChannelColumnConfig {
  /** Full ordered key list — the display order of all known columns. */
  order: string[];
  /** Explicitly hidden keys; everything else in ``order`` shows. */
  hidden: string[];
}

function columnStorageKey(channelId: string): string {
  return `rssripple:channel-columns:${channelId}`;
}

/** Default ordering: declared required fields first (work-type applicability
 * ranking), remaining pool keys appended in canonical catalog order. */
export function defaultColumnOrder(declared: string[]): string[] {
  const ranked = orderedRequiredKeys(declared);
  const out = [...ranked];
  for (const k of COLUMN_POOL) if (!out.includes(k)) out.push(k);
  return out;
}

/**
 * Effective (order, hidden) state, merging a saved config with the current
 * catalog pool: stale keys drop, keys added to the catalog later append to
 * the end and follow the declared-required default visibility. Without a
 * saved config the defaults apply — declared required fields are visible
 * (minus those surfaced by the work column), everything else hidden.
 */
export function effectiveColumnState(
  cfg: ChannelColumnConfig | null,
  declared: string[],
): { order: string[]; hidden: Set<string> } {
  if (!cfg) {
    const hidden = new Set(
      COLUMN_POOL.filter((k) => !(declared.includes(k) && !HIDDEN_COLUMN_KEYS.has(k))),
    );
    return { order: defaultColumnOrder(declared), hidden };
  }
  const known = new Set(COLUMN_POOL);
  const order = cfg.order.filter((k) => known.has(k));
  for (const k of COLUMN_POOL) if (!order.includes(k)) order.push(k);
  const hidden = new Set(cfg.hidden.filter((k) => known.has(k)));
  return { order, hidden };
}

/** Ordered visible column keys for the resource tables. */
export function resolveVisibleColumns(
  cfg: ChannelColumnConfig | null,
  declared: string[],
): string[] {
  const { order, hidden } = effectiveColumnState(cfg, declared);
  return order.filter((k) => !hidden.has(k));
}

/** Load a channel's saved column config; null = never customized. */
export function loadColumnConfig(channelId: string): ChannelColumnConfig | null {
  try {
    const raw = localStorage.getItem(columnStorageKey(channelId));
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (
      parsed &&
      typeof parsed === 'object' &&
      Array.isArray((parsed as ChannelColumnConfig).order) &&
      Array.isArray((parsed as ChannelColumnConfig).hidden) &&
      (parsed as ChannelColumnConfig).order.every((k) => typeof k === 'string') &&
      (parsed as ChannelColumnConfig).hidden.every((k) => typeof k === 'string')
    ) {
      const cfg = parsed as ChannelColumnConfig;
      return { order: [...cfg.order], hidden: [...cfg.hidden] };
    }
  } catch {
    // Corrupted JSON or storage unavailable — fall back to defaults.
  }
  return null;
}

/** Persist (or clear with null) a channel's column config. Failures
 * (private mode etc.) degrade silently to session-only state. */
export function saveColumnConfig(
  channelId: string,
  config: ChannelColumnConfig | null,
): void {
  try {
    if (config) localStorage.setItem(columnStorageKey(channelId), JSON.stringify(config));
    else localStorage.removeItem(columnStorageKey(channelId));
  } catch {
    // ignore
  }
}

/** Column width hints (px) so the fixed layout distributes sensibly. */
export function requiredFieldWidth(key: string): number {
  switch (key) {
    case 'title_cn':
    case 'title_en':
    case 'search_title':
    case 'resource_collection':
    case 'collection':
      return 140;
    case 'subtitle_group':
      return 120;
    case 'subtitle_langs':
      return 130;
    case 'file_size':
      return 90;
    case 'episode_confidence':
      return 100;
    case 'content_type':
      return 84;
    case 'is_batch':
      return 76;
    default:
      return 88;
  }
}
