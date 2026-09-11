import { Tag, Tooltip } from 'antd';
import { Info, Tv, Film } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { seasonWorkInfo } from '../utils/season';
import {
  fieldApplicable,
  requiredFieldValue,
  resourceShape,
} from '../utils/requiredFields';
import type { FileResource, ResourceWorkRef } from '../types';

// Row-level renderers shared by the channel resource tables and the
// downloader local-task table — keep both lists pixel-consistent.

/** Single required-field column cell: type-irrelevant fields render blank
 * (e.g. batch ranges on movies), applicable-but-missing values render — so
 * unparsed fields stay visible without misleading dashes elsewhere. */
export function RequiredFieldCell({ r, fieldKey }: { r: FileResource; fieldKey: string }) {
  const { t } = useTranslation();
  if (!fieldApplicable(fieldKey, resourceShape(r))) return null;
  const v = requiredFieldValue(r, fieldKey, t);
  if (v == null) {
    return <span style={{ color: 'var(--rr-text-muted)' }}>—</span>;
  }
  return <span>{v}</span>;
}

/** Content-type / batch / pending-decision tags carried by the work column
 * (the catalog keys content_type/is_batch never get their own column). */
export function WorkTypeTags({ r }: { r: FileResource }) {
  const { t } = useTranslation();
  if (!r.series_id && !r.movie_id && !r.is_batch && !r.pending_decision) return null;
  return (
    <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
      {r.series_id && (
        <Tag
          color="blue"
          icon={<Tv size={10} />}
          style={{ marginRight: 0, fontSize: 11, lineHeight: '16px' }}
        >
          {t('dashboard.series')}
        </Tag>
      )}
      {r.movie_id && (
        <Tag
          color="green"
          icon={<Film size={10} />}
          style={{ marginRight: 0, fontSize: 11, lineHeight: '16px' }}
        >
          {t('dashboard.movie')}
        </Tag>
      )}
      {r.pending_decision && (
        <Tag color="orange" style={{ marginRight: 0, fontSize: 11, lineHeight: '16px' }}>
          {t('status.pending_decisions')}
        </Tag>
      )}
      {r.is_batch && (
        <Tag style={{ marginRight: 0, fontSize: 11, lineHeight: '16px' }} color="orange">
          {t('channels.tagBatch')}
        </Tag>
      )}
    </div>
  );
}

export function WorkInfoIcon({ work, isSeries }: { work: ResourceWorkRef | null; isSeries: boolean }) {
  const { t } = useTranslation();
  if (!work) return null;
  const dateStr = isSeries ? work.start_date : work.release_date;
  const year = dateStr ? dateStr.slice(0, 4) : null;
  const rows: Array<{ label: string; value: string }> = [];
  if (year) rows.push({ label: t('works.year'), value: year });
  if (work.is_anime != null) {
    rows.push({
      label: t('works.animeStatus'),
      value: work.is_anime ? t('works.anime') : t('works.liveAction'),
    });
  }
  if (work.rating != null) rows.push({ label: t('works.colRating'), value: work.rating.toFixed(1) });
  if (work.genre && work.genre.length > 0) rows.push({ label: t('works.colGenre'), value: work.genre.join(' · ') });
  if (work.status) rows.push({ label: t('works.colStatus'), value: work.status });
  if (isSeries && (work.season_number != null || work.number_of_episodes != null)) {
    rows.push({
      label: t('series.seasonsEpisodes'),
      // Per-season works: a series work IS one season.
      value: seasonWorkInfo(t, work.season_number ?? null, work.number_of_episodes ?? null) || '—',
    });
  }
  if (rows.length === 0 && !work.description) return null;
  return (
    <Tooltip
      title={
        <div style={{ maxWidth: 320 }}>
          <div style={{ fontWeight: 600, marginBottom: 6, color: '#fff', wordBreak: 'break-word' }}>
            {work.title_cn || work.title_en || work.original_title || work.id}
          </div>
          {rows.map((row) => (
            <div key={row.label} style={{ display: 'flex', gap: 8, fontSize: 12, lineHeight: '18px' }}>
              <span style={{ color: '#b0b0ba', flexShrink: 0 }}>{row.label}</span>
              <span style={{ color: '#fff', wordBreak: 'break-word' }}>{row.value}</span>
            </div>
          ))}
          {work.description && (
            <div
              style={{
                marginTop: 6,
                fontSize: 12,
                color: '#c8c8d0',
                wordBreak: 'break-word',
                maxHeight: 80,
                overflow: 'hidden',
              }}
            >
              {work.description}
            </div>
          )}
        </div>
      }
      placement="topLeft"
    >
      <span
        onClick={(e) => e.stopPropagation()}
        style={{ display: 'inline-flex', alignItems: 'center' }}
      >
        <Info size={12} style={{ color: 'var(--rr-text-muted)', flexShrink: 0, cursor: 'help' }} />
      </span>
    </Tooltip>
  );
}
