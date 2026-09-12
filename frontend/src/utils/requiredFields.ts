import type { TFunction } from 'i18next';
import { formatBytes } from './format';
import type { FileResource } from '../types';

// ---------------------------------------------------------------------------
// Channel required-field column helpers — shared by the channel resource
// tables (flat + grouped views). Columns come from the channel's configured
// required_metadata_fields; per-row rendering filters by the resource's shape
// so type-irrelevant fields never show a misleading "—" (e.g. flat episode
// ranges on cross-season batches or movies).
// ---------------------------------------------------------------------------

/** Row shape drives field applicability. */
export type RowShape = 'tv_single' | 'tv_season_batch' | 'tv_multi_season' | 'franchise' | 'movie' | 'audio' | 'unknown';

const WORK_SHAPES: RowShape[] = ['tv_single', 'tv_season_batch', 'tv_multi_season', 'movie'];

/** Derive the row's shape from which work/collection FKs it carries. */
export function resourceShape(r: FileResource): RowShape {
  if (r.audio_work_id) return 'audio';
  if (r.collection_id && !r.series_id && !r.movie_id && !r.audio_work_id) {
    return 'franchise';
  }
  if (r.series_id) {
    if (!r.is_batch) return 'tv_single';
    if (r.batch_scope === 'multi_season') return 'tv_multi_season';
    if (r.batch_scope === 'season') return 'tv_season_batch';
    return 'unknown';
  }
  if (r.movie_id) return 'movie';
  return 'unknown';
}

/**
 * Field → applicable shapes. Missing entry = applicable to every shape
 * (resource-level parse fields). Work-pair keys need a linked work, hence the
 * WORK_SHAPES restriction; episode machinery is TV-only.
 */
const APPLICABILITY: Record<string, RowShape[] | undefined> = {
  // 基础字段（全形态适用；year/is_anime 需链接作品）
  title_cn: undefined,
  title_en: undefined,
  search_title: undefined,
  // A franchise collection may mix TV/movie/OVA and deliberately has no flat
  // work FK, so it has no single content_type value.
  content_type: ['tv_single', 'tv_season_batch', 'tv_multi_season', 'movie', 'audio'],
  is_batch: undefined,
  year: WORK_SHAPES,
  is_anime: WORK_SHAPES,
  // TV 集数字段：按单集/合集区分。Per-season works: the season number is
  // carried by the work identity, so ``season`` is optional (declarable);
  // ``absolute_episode`` / ``episode_confidence`` are retired catalog keys
  // (the fields still exist on the resource model and in the Filter DSL).
  season: ['tv_single', 'tv_season_batch'],
  episode: ['tv_single'],
  episode_start: ['tv_season_batch'],
  episode_end: ['tv_season_batch'],
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

/** Tag color per latest download-task status (dispatch outcome). */
export const DOWNLOAD_STATUS_TAG_COLORS: Record<string, string> = {
  organized: 'green',
  completed: 'green',
  downloading: 'cyan',
  queued: 'blue',
  pending: 'blue',
  paused: 'default',
  cancelled: 'default',
  error: 'red',
};

/** Resolve the display value for one required-field column. Resource-level
 * keys read straight off FileResource; work-level keys resolve through the
 * linked series/movie; enum keys localize via filter.enumValue_*. */
export function requiredFieldValue(
  r: FileResource,
  key: string,
  t: TFunction,
): string | null {
  const num = (v: number | null | undefined): string | null =>
    v != null ? String(v) : null;
  const str = (v: string | null | undefined): string | null => {
    const s = (v ?? '').trim();
    return s.length > 0 ? s : null;
  };
  const work = r.series ?? r.movie ?? null;
  switch (key) {
    // ── Resource-level fields ──
    case 'title_cn':
      return str(r.title_cn);
    case 'title_en':
      return str(r.title_en);
    case 'search_title':
      return str(r.search_title);
    case 'episode':
      return num(r.episode);
    case 'season':
      return num(r.season);
    case 'episode_start':
      return num(r.episode_start);
    case 'episode_end':
      return num(r.episode_end);
    case 'absolute_episode':
      return num(r.absolute_episode);
    case 'is_batch':
      return r.is_batch ? t('filter.true') : t('filter.false');
    case 'episode_confidence':
      return r.episode_confidence
        ? t(`filter.enumValue_${r.episode_confidence}`, { defaultValue: r.episode_confidence })
        : null;
    case 'content_type':
      // Derived from which work FK the resource carries (mirrors the DSL).
      if (r.series_id) return t('filter.enumValue_tv', { defaultValue: 'tv' });
      if (r.movie_id) return t('filter.enumValue_movie', { defaultValue: 'movie' });
      if (r.audio_work_id) return t('filter.enumValue_audio', { defaultValue: 'audio' });
      return null;
    case 'subtitle_group':
      return str(r.subtitle_group);
    case 'resolution':
      return str(r.resolution);
    case 'source':
      return str(r.source);
    case 'video_codec':
      return str(r.video_codec);
    case 'audio_codec':
      return str(r.audio_codec);
    case 'subtitle_type':
      return str(r.subtitle_type);
    case 'subtitle_langs':
      return r.subtitle_langs && r.subtitle_langs.length > 0
        ? r.subtitle_langs.join(' · ')
        : null;
    case 'container':
      return str(r.container);
    case 'file_size':
      return r.file_size != null ? formatBytes(r.file_size) : null;
    case 'resource_collection':
      return str(r.collection_name);
    // ── Work-level fields (resolve through the linked work) ──
    default:
      if (!work) return null;
      switch (key) {
        case 'rating':
          return work.rating != null ? work.rating.toFixed(1) : null;
        case 'year': {
          const d = work.start_date || work.release_date;
          return d ? d.slice(0, 4) : null;
        }
        case 'genre':
          return work.genre && work.genre.length > 0 ? work.genre.join(' · ') : null;
        case 'is_anime':
          return work.is_anime == null
            ? null
            : work.is_anime
              ? t('works.anime')
              : t('works.liveAction');
        case 'collection': {
          const c = work.collection;
          return c ? (c.title_cn || c.title_en || null) : null;
        }
        default:
          return null;
      }
  }
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
  resource_collection: 2,
};

// Canonical catalog order (mirrors app/services/required_fields.py) for the
// rank-3 tail and tie-breaking inside each group. ``absolute_episode`` /
// ``episode_confidence`` are retired catalog keys (per-season works) and no
// longer appear here; legacy channel declarations carrying them are dropped
// from the column pool by the known-key filter.
const CATALOG_ORDER: string[] = [
  'title_cn', 'title_en', 'search_title',
  'content_type', 'is_batch', 'year', 'is_anime',
  'season', 'episode', 'episode_start', 'episode_end',
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
// Column configuration (persisted in localStorage, keyed per table instance:
// channel resource tables and the downloader local-task table)
// ---------------------------------------------------------------------------

/** Every catalog key is a configurable column (作品/操作 are fixed table
 * columns outside this pool). Mirrors app/services/required_fields.py. */
export const COLUMN_POOL: readonly string[] = CATALOG_ORDER;

/** Downloader local-task table pool: same catalog minus the keys already
 * carried by the fixed 作品/类型标签 columns (titles, content_type, is_batch). */
export const TASK_COLUMN_POOL: readonly string[] = CATALOG_ORDER.filter(
  (k) => !HIDDEN_COLUMN_KEYS.has(k),
);

export interface ChannelColumnConfig {
  /** Full ordered key list — the display order of all known columns. */
  order: string[];
  /** Explicitly hidden keys; everything else in ``order`` shows. */
  hidden: string[];
}

export function channelColumnStorageKey(channelId: string): string {
  return `rssripple:channel-columns:${channelId}`;
}

export function taskColumnStorageKey(downloaderId: string): string {
  return `rssripple:downloader-task-columns:${downloaderId}`;
}

export function torrentColumnStorageKey(downloaderId: string): string {
  return `rssripple:downloader-torrent-columns:${downloaderId}`;
}

/** Default ordering: declared required fields first (work-type applicability
 * ranking), remaining pool keys appended in canonical catalog order. */
export function defaultColumnOrder(
  declared: string[],
  pool: readonly string[] = COLUMN_POOL,
): string[] {
  const ranked = orderedRequiredKeys(declared);
  const out = [...ranked];
  for (const k of pool) if (!out.includes(k)) out.push(k);
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
  pool: readonly string[] = COLUMN_POOL,
): { order: string[]; hidden: Set<string> } {
  if (!cfg) {
    const hidden = new Set(
      pool.filter((k) => !(declared.includes(k) && !HIDDEN_COLUMN_KEYS.has(k))),
    );
    return { order: defaultColumnOrder(declared, pool), hidden };
  }
  const known = new Set(pool);
  const order = cfg.order.filter((k) => known.has(k));
  for (const k of pool) if (!order.includes(k)) order.push(k);
  const hidden = new Set(cfg.hidden.filter((k) => known.has(k)));
  return { order, hidden };
}

/** Ordered visible column keys for the resource tables. */
export function resolveVisibleColumns(
  cfg: ChannelColumnConfig | null,
  declared: string[],
  pool: readonly string[] = COLUMN_POOL,
): string[] {
  const { order, hidden } = effectiveColumnState(cfg, declared, pool);
  return order.filter((k) => !hidden.has(k));
}

/** Load a saved column config by storage key; null = never customized. */
export function loadColumnConfig(storageKey: string): ChannelColumnConfig | null {
  try {
    const raw = localStorage.getItem(storageKey);
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

/** Persist (or clear with null) a column config under the given storage key.
 * Failures (private mode etc.) degrade silently to session-only state. */
export function saveColumnConfig(
  storageKey: string,
  config: ChannelColumnConfig | null,
): void {
  try {
    if (config) localStorage.setItem(storageKey, JSON.stringify(config));
    else localStorage.removeItem(storageKey);
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
    case 'created_at':
      return 150;
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
