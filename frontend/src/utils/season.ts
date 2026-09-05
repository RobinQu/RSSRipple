import type { TFunction } from 'i18next';

/**
 * Per-season works helpers (docs/design/per-season-works.md): a TVSeries work
 * IS exactly one season — ``season_number`` 0 denotes specials (Plex
 * Specials convention).
 */

/** Short season label: "特典" for 0, "第N季" otherwise; '' when unknown. */
export function seasonLabel(t: TFunction, season: number | null | undefined): string {
  if (season == null) return '';
  return season === 0 ? t('works.specials') : t('works.seasonN', { n: season });
}

/** Info-cell text for a season work: "第N季 · M集" (either part may drop out). */
export function seasonWorkInfo(
  t: TFunction,
  season: number | null | undefined,
  episodes: number | null | undefined,
): string {
  const s = seasonLabel(t, season);
  const e = episodes != null ? `${episodes}${t('series.episode')}` : '';
  if (s && e) return `${s} · ${e}`;
  return s || e || '';
}
