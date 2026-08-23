import type { TFunction } from 'i18next';
import type { FileResource } from '../types';

/** Batch badge label keyed off the torrent-content ``batch_scope`` (P1).
 *
 * - season       → 合集 / Batch
 * - multi_season → 跨季合集 / Multi-season pack
 * - movies       → 电影合集 / Movie pack (LLM-refined pure-movie bundle)
 * - franchise    → 混合合集 / Mixed multi-work pack
 *
 * Legacy rows (``is_batch`` set before scope sub-classification existed) have
 * ``batch_scope === null`` and fall back to the generic 合集 tag.
 */
export function batchScopeLabel(
  t: TFunction,
  r: Pick<FileResource, 'is_batch' | 'batch_scope'>,
): string {
  switch (r.batch_scope) {
    case 'multi_season':
      return t('channels.batchMultiSeason');
    case 'franchise':
      return t('channels.batchFranchise');
    case 'movies':
      return t('channels.batchMovies');
    case 'season':
    default:
      return t('channels.batch');
  }
}
