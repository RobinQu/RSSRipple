import { useEffect, useState } from 'react';
import {
  App,
  Button,
  Empty,
  Input,
  Modal,
  Segmented,
  Select,
  Space,
  Tag,
  Typography,
} from 'antd';
import { useTranslation } from 'react-i18next';
import { collectionsApi } from '../../api/collections';
import { moviesApi } from '../../api/movies';
import { metadataApi } from '../../api/metadata';
import { seriesApi } from '../../api/series';
import { clientId } from '../../utils/uuid';
import { seasonLabel } from '../../utils/season';
import { DEFAULT_FALLBACK_SOURCES } from '../channel-form/constants';
import { workKeyOf } from '../wizardValidation';
import type {
  AssociationWorkRef,
  MetadataCandidate,
  MetadataSource,
} from '../../types';

const { Text } = Typography;

export default function WorkPickerModal({
  open,
  existingKeys,
  collectionId,
  defaultMetadataSource,
  defaultFallbackSources,
  initialQuery = '',
  onClose,
  onPick,
}: {
  open: boolean;
  existingKeys: Set<string>;
  /** Two-level association (per-season works): when set, the TV library tab
   * lists this collection's season works; picks outside it are blocked. */
  collectionId: string | null;
  defaultMetadataSource: MetadataSource;
  defaultFallbackSources: string[];
  /** Prefilled search text (e.g. the cluster title for whole-cluster picks). */
  initialQuery?: string;
  onClose: () => void;
  onPick: (
    ref: AssociationWorkRef,
    title: string,
    meta?: { season?: number | null; collectionId?: string | null },
  ) => void;
}) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [mode, setMode] = useState<'library' | 'online'>('library');
  const [kind, setKind] = useState<'tv' | 'movie'>('tv');
  const [metaType, setMetaType] = useState<'tv' | 'movie'>('tv');
  const [metadataSource, setMetadataSource] = useState<MetadataSource>(defaultMetadataSource);
  const [fallbackSources, setFallbackSources] = useState<string[]>(defaultFallbackSources);
  const [q, setQ] = useState('');
  const [searching, setSearching] = useState(false);
  const [libResults, setLibResults] = useState<
    { id: string; title: string; year: string | null; seasonNumber: number | null; collectionId: string | null }[]
  >([]);
  const [metaResults, setMetaResults] = useState<MetadataCandidate[]>([]);

  useEffect(() => {
    if (!open) return;
    setMetadataSource(defaultMetadataSource);
    setFallbackSources(defaultFallbackSources);
    setQ(initialQuery);
  }, [open, defaultMetadataSource, defaultFallbackSources, initialQuery]);

  const searchLibrary = async (query?: string) => {
    setSearching(true);
    try {
      if (kind === 'tv' && collectionId) {
        // Two-level association: browse the selected collection's season
        // works (server returns them sorted by season_number).
        const res = await collectionsApi.works(collectionId, 1, 100);
        if (res.success) {
          const needle = (query || '').trim().toLowerCase();
          setLibResults(
            res.data
              .filter((row) => row.content_type === 'tv')
              .map((row) => ({
                id: row.id,
                title: row.original_title || row.title_cn || row.title_en || row.id,
                year: row.year != null ? String(row.year) : null,
                seasonNumber: row.season_number ?? null,
                collectionId,
              }))
              .filter((row) => !needle || row.title.toLowerCase().includes(needle)),
          );
        }
        return;
      }
      const fn = kind === 'tv' ? seriesApi.list : moviesApi.list;
      const res = await fn(1, 10, query || undefined);
      if (res.success) {
        setLibResults(
          res.data.map((row) => ({
            id: row.id,
            title: row.original_title || row.title_cn || row.title_en || row.id,
            year:
              ((row as { start_date?: string | null }).start_date ||
                (row as { release_date?: string | null }).release_date ||
                '')?.slice(0, 4) || null,
            seasonNumber:
              kind === 'tv' ? ((row as { season_number?: number | null }).season_number ?? null) : null,
            collectionId:
              (row as { collection_id?: string | null }).collection_id ?? null,
          })),
        );
      }
    } finally {
      setSearching(false);
    }
  };

  const searchOnline = async () => {
    if (!q.trim()) {
      message.warning(t('metadata.enterSearch'));
      return;
    }
    setSearching(true);
    try {
      const res = await metadataApi.search({
        query: q.trim(),
        content_type: metaType,
        mode: 'online',
        source: metadataSource,
        trusted_sites: fallbackSources,
      });
      setMetaResults(res.success ? res.data.candidates : []);
      if (!res.success) message.error(res.error?.message || t('metadata.searchFailed'));
    } finally {
      setSearching(false);
    }
  };

  const pickOnline = async (r: MetadataCandidate) => {
    if (!r.selectable) return;
    const clientKey = `candidate:${clientId()}`;
    onPick(
      {
        work_type: metaType === 'tv' ? 'series' : 'movie',
        work_id: clientKey,
        client_key: clientKey,
        candidate: r,
      },
      r.title_cn || r.original_title || r.title_en || r.external_id || clientKey,
    );
  };

  return (
    <Modal
      open={open}
      title={t('resource.pickWorkTitle')}
      footer={null}
      onCancel={onClose}
      destroyOnHidden
      width={760}
    >
      <Segmented
        block
        value={mode}
        onChange={(v) => setMode(v as 'library' | 'online')}
        options={[
          { value: 'library', label: t('resource.pickFromLibrary') },
          { value: 'online', label: t('resource.pickOnline') },
        ]}
        style={{ marginBottom: 12 }}
      />
      {mode === 'library' ? (
        <>
          <Space.Compact style={{ width: '100%', marginBottom: 10 }}>
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t('works.searchPlaceholder')}
              onPressEnter={() => void searchLibrary(q)}
            />
            <Button type="primary" loading={searching} onClick={() => void searchLibrary(q)}>
              {t('common.search')}
            </Button>
          </Space.Compact>
          <Segmented
            value={kind}
            onChange={(v) => { setKind(v as 'tv' | 'movie'); setLibResults([]); }}
            options={[
              { value: 'tv', label: t('works.tv') },
              { value: 'movie', label: t('works.movie') },
            ]}
            style={{ marginBottom: 10 }}
          />
          {kind === 'tv' && collectionId && (
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
              {t('resource.pickSeasonWorkHint')}
            </Text>
          )}
          <div style={{ maxHeight: 320, overflowY: 'auto' }}>
            {libResults.map((row) => {
              const key = workKeyOf(kind === 'tv' ? 'series' : 'movie', row.id);
              const outsideCollection =
                kind === 'tv' &&
                !!collectionId &&
                !!row.collectionId &&
                row.collectionId !== collectionId;
              const disabled = existingKeys.has(key) || outsideCollection;
              return (
                <div
                  key={row.id}
                  style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 4px', borderBottom: '1px solid var(--rr-border-soft)' }}
                >
                  <Space size={6}>
                    <Text style={{ fontSize: 13 }}>{row.title}</Text>
                    {kind === 'tv' && row.seasonNumber != null && (
                      <Tag style={{ fontSize: 10, margin: 0 }}>{seasonLabel(t, row.seasonNumber)}</Tag>
                    )}
                    {row.year && <Text type="secondary" style={{ fontSize: 12 }}>{row.year}</Text>}
                  </Space>
                  <Button
                    size="small"
                    type="primary"
                    disabled={disabled}
                    title={outsideCollection ? t('resource.workNotInCollection') : undefined}
                    onClick={() =>
                      onPick(
                        { work_type: kind === 'tv' ? 'series' : 'movie', work_id: row.id },
                        row.title,
                        { season: row.seasonNumber, collectionId: row.collectionId },
                      )
                    }
                  >
                    {existingKeys.has(key) ? t('resource.workPicked') : t('works.select')}
                  </Button>
                </div>
              );
            })}
            {!searching && libResults.length === 0 && (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  kind === 'tv' && collectionId
                    ? t('resource.noSeasonWorksInCollection')
                    : t('common.noResults')
                }
              />
            )}
          </div>
        </>
      ) : (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(180px, 1fr) minmax(280px, 2fr)', gap: 10, marginBottom: 10 }}>
            <Select
              value={metadataSource}
              onChange={(value) => setMetadataSource(value as MetadataSource)}
              options={(['wikipedia', 'tmdb', 'bangumi'] as MetadataSource[]).map((value) => ({
                value,
                label: t(`channels.sources.${value}`),
              }))}
            />
            <Select
              mode="multiple"
              value={fallbackSources}
              onChange={setFallbackSources}
              placeholder={t('channels.metadataFallbackPlaceholder')}
              options={DEFAULT_FALLBACK_SOURCES.map((value) => ({
                value,
                label: t(`channels.sources.${value}`),
              }))}
            />
          </div>
          <Space.Compact style={{ width: '100%', marginBottom: 10 }}>
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t('metadata.searchPlaceholder')}
              onPressEnter={() => void searchOnline()}
            />
            <Select
              value={metaType}
              onChange={(v) => setMetaType(v as 'tv' | 'movie')}
              style={{ width: 100 }}
              options={[
                { value: 'tv', label: t('works.tv') },
                { value: 'movie', label: t('works.movie') },
              ]}
            />
            <Button type="primary" loading={searching} onClick={() => void searchOnline()}>
              {t('common.search')}
            </Button>
          </Space.Compact>
          <div style={{ maxHeight: 320, overflowY: 'auto' }}>
            {metaResults.map((r, idx) => (
              <div
                key={idx}
                style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, padding: '6px 4px', borderBottom: '1px solid var(--rr-border-soft)' }}
              >
                <Space size={6}>
                  <Text style={{ fontSize: 13 }}>{r.title_cn || r.original_title || r.title_en}</Text>
                  {r.year && <Text type="secondary" style={{ fontSize: 12 }}>{r.year}</Text>}
                </Space>
                <Button size="small" type="primary" disabled={!r.selectable} onClick={() => void pickOnline(r)}>
                  {t('metadata.confirmSelection')}
                </Button>
              </div>
            ))}
            {!searching && metaResults.length === 0 && (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('metadata.noResults')} />
            )}
          </div>
        </>
      )}
    </Modal>
  );
}
