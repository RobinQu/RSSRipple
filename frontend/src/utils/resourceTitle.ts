import type { TFunction } from 'i18next';
import { seasonLabel } from './season';
import type { FileResource, ResourceWorkRef } from '../types';

/** Minimal resource shape needed to resolve a display title. */
export interface ResourceTitleSource {
  title_raw?: string | null;
  title_cn?: string | null;
  title_en?: string | null;
  search_title?: string | null;
  series_id?: string | null;
  movie_id?: string | null;
  audio_work_id?: string | null;
  collection_id?: string | null;
  collection_name?: string | null;
}

/** Collection-only packs have no flat work FK: the collection is the identity. */
export function isCollectionOnlyResource(r: ResourceTitleSource): boolean {
  return (
    !r.series_id &&
    !r.movie_id &&
    !r.audio_work_id &&
    !!r.collection_id &&
    !!r.collection_name
  );
}

/** Primary resource title used by parsed-resource lists and previews.
 *
 * Collection-only packs show the collection name instead of the per-season
 * title parsed from the release name (e.g. an S01-S04 pack must not display
 * "西部世界 第一季"). All other resources keep the existing fallback chain.
 */
export function resourceDisplayTitle(
  r: ResourceTitleSource,
  fallback?: string | null,
): string {
  if (isCollectionOnlyResource(r)) return r.collection_name!;
  return (
    r.title_cn ||
    r.title_en ||
    r.search_title ||
    r.title_raw ||
    fallback ||
    '—'
  );
}

/** Work-column display info: collection link for collection-only packs,
 * otherwise the linked work title plus its season label. */
export function resourceWorkDisplay(
  r: FileResource,
  t: TFunction,
  fallback?: string | null,
): {
  title: string;
  href: string | null;
  work: ResourceWorkRef | null;
  isSeries: boolean;
  isCollectionOnly: boolean;
} {
  const work = r.series ?? r.movie ?? null;
  if (isCollectionOnlyResource(r)) {
    return {
      title: r.collection_name!,
      href: `/collections/${r.collection_id}`,
      work: null,
      isSeries: false,
      isCollectionOnly: true,
    };
  }
  const base =
    (work && (work.original_title || work.title_cn || work.title_en)) ||
    resourceDisplayTitle(r, fallback);
  const season =
    r.series_id && work ? seasonLabel(t, work.season_number ?? null) : '';
  return {
    title: season ? `${base} · ${season}` : base,
    href: r.series_id
      ? `/series/${r.series_id}`
      : r.movie_id
        ? `/movies/${r.movie_id}`
        : null,
    work,
    isSeries: !!r.series_id,
    isCollectionOnly: false,
  };
}
