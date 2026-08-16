import type { TFunction } from 'i18next';
import type { FileResource } from '../types';

/** Batch badge label keyed off the torrent-content ``batch_scope`` (P1).
 *
 * - season       → 合集 / Batch
 * - multi_season → 跨季合集 / Multi-season pack
 * - franchise    → 作品集合集 / Franchise pack
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
    case 'season':
    default:
      return t('channels.batch');
  }
}
