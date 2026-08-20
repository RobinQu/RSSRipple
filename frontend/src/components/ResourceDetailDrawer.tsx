import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Drawer,
  Spin,
  Typography,
  Tag,
  Divider,
  Empty,
  Button,
  Space,
  App,
  Tooltip,
  Descriptions,
  Popover,
  InputNumber,
  Form,
  Select,
  Switch,
  theme,
} from 'antd';
import { Copy, Pencil, Download } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { resourcesApi } from '../api/channels';
import { seriesApi } from '../api/series';
import { moviesApi } from '../api/movies';
import { formatBytes, formatDate } from '../utils/format';
import { batchScopeLabel } from '../utils/batch';
import MetadataCorrectionModal from './MetadataCorrectionModal';
import CreateTaskModal from './CreateTaskModal';
import { ResourceFilesView } from './ResourceFilesDrawer';
import { posterUrl, useDefaultPoster } from '../utils/poster';
import type { FileResource, Movie, ResourceCorrectionBody, TVSeries } from '../types';

type Work = TVSeries | Movie;

const { Text, Paragraph } = Typography;

interface LinkedMeta {
  type: 'series' | 'movie';
  title: string;
  poster_url?: string | null;
  year?: number | null;
  secondary_titles?: string[];
  genres?: string[];
  rating?: number | null;
  description?: string | null;
}

type SeasonCount = { season_number: number; episode_count: number };

/** Build the display extras for a linked work entity (series or movie):
 * premier year, alternate titles distinct from the display title, genres,
 * rating and description — the metadata block has room for these. */
function workMetaExtras(entity: {
  title_cn?: string | null;
  title_en?: string | null;
  original_title?: string | null;
  start_date?: string | null;
  release_date?: string | null;
  genre?: string[] | null;
  rating?: number | null;
  description?: string | null;
}, displayTitle: string) {
  const dateStr = entity.start_date || entity.release_date || null;
  const year = dateStr ? Number(dateStr.slice(0, 4)) || null : null;
  const secondary = [entity.title_cn, entity.title_en, entity.original_title]
    .filter((v): v is string => Boolean(v) && v !== displayTitle)
    .filter((v, i, arr) => arr.indexOf(v) === i);
  return {
    year,
    secondary_titles: secondary,
    genres: entity.genre ?? [],
    rating: entity.rating ?? null,
    description: entity.description ?? null,
  };
}

/** Client-side mirror of the backend ``locate_absolute_episode``
 * (app/services/metadata_episode_reconcile.py): walk seasons ascending,
 * subtract each ``episode_count`` until the absolute number lands in a
 * season. The last season gets a tolerance of 2 for still-airing shows
 * whose metadata lags behind. Returns null when out of range — the
 * server-side ambiguous marking handles that case. */
function locateAbsoluteSeason(absolute: number, seasons: SeasonCount[] | null): number | null {
  if (!seasons || absolute < 1) return null;
  const ordered = seasons
    .filter((s) => s.season_number >= 1 && s.episode_count >= 1)
    .sort((a, b) => a.season_number - b.season_number);
  let remaining = absolute;
  for (const s of ordered) {
    if (remaining <= s.episode_count) return s.season_number;
    remaining -= s.episode_count;
  }
  if (ordered.length > 0 && remaining <= 2) return ordered[ordered.length - 1].season_number;
  return null;
}interface ResourceDetailDrawerProps {
  resource: FileResource | null;
  onClose: () => void;
  onCorrected?: () => void;
}

function PosterBlock({ url }: { url: string | null | undefined }) {
  return (
    <img
      src={posterUrl(url)}
      alt="poster"
      style={{
        width: 80,
        height: 120,
        objectFit: 'cover',
        borderRadius: 6,
        border: '1px solid var(--rr-border)',
        background: 'var(--rr-surface-card)',
        flexShrink: 0,
      }}
      onError={useDefaultPoster}
    />
  );
}

export default function ResourceDetailDrawer({
  resource,
  onClose,
  onCorrected,
}: ResourceDetailDrawerProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { token } = theme.useToken();
  const [meta, setMeta] = useState<LinkedMeta | null>(null);
  const [metaLoading, setMetaLoading] = useState(false);
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [createTaskOpen, setCreateTaskOpen] = useState(false);
  const [resourceData, setResourceData] = useState<FileResource | null>(null);
  // Inline parsed-fields editing (view/edit toggle inside the section).
  const [parseForm] = Form.useForm();
  const [parseEditing, setParseEditing] = useState(false);
  const [parseSaving, setParseSaving] = useState(false);
  const [editWork, setEditWork] = useState<Work | null>(null);
  const isBatchWatch = Form.useWatch('is_batch', parseForm) ?? false;
  const [episodeEditOpen, setEpisodeEditOpen] = useState(false);
  const [seasonDraft, setSeasonDraft] = useState<number | null>(null);
  const [episodeDraft, setEpisodeDraft] = useState<number | null>(null);
  const [absoluteDraft, setAbsoluteDraft] = useState<number | null>(null);
  const [savingEpisode, setSavingEpisode] = useState(false);
  // Per-season episode counts of the linked series (from the resource
  // metadata endpoint), used to prefill season from an absolute episode.
  const [seriesSeasons, setSeriesSeasons] = useState<SeasonCount[] | null>(null);
  // Once the user types a season themselves we stop auto-prefilling it.
  const [seasonTouched, setSeasonTouched] = useState(false);

  const loadMeta = useCallback(async (rid: string) => {
    setMetaLoading(true);
    try {
      const [metaRes, resRes] = await Promise.all([
        resourcesApi.getMetadata(rid),
        resourcesApi.get(rid),
      ]);
      if (resRes.success) setResourceData(resRes.data);
      if (metaRes.success && metaRes.data) {
        const d = metaRes.data;
        if (d.linked?.type === 'series') {
          const series = d.linked.entity as TVSeries;
          const title =
            series.original_title || series.title_cn || series.title_en || t('resource.unknownSeries');
          setMeta({
            type: 'series',
            title,
            poster_url: series.poster_url,
            ...workMetaExtras(series, title),
          });
          setSeriesSeasons(series.seasons ?? null);
        } else if (d.linked?.type === 'movie') {
          const movie = d.linked.entity;
          const title =
            movie.original_title || movie.title_cn || movie.title_en || t('resource.unknownMovie');
          setMeta({
            type: 'movie',
            title,
            poster_url: movie.poster_url,
            ...workMetaExtras(movie, title),
          });
          setSeriesSeasons(null);
        } else if (d.series_id && d.series) {
          const title =
            d.series.original_title || d.series.title_cn || d.series.title_en || t('resource.unknownSeries');
          setMeta({
            type: 'series',
            title,
            poster_url: d.series.poster_url,
            ...workMetaExtras(d.series, title),
          });
          // The summary payload carries no per-season counts — no prefill.
          setSeriesSeasons(null);
        } else if (d.movie_id && d.movie) {
          const title =
            d.movie.original_title || d.movie.title_cn || d.movie.title_en || t('resource.unknownMovie');
          setMeta({
            type: 'movie',
            title,
            poster_url: d.movie.poster_url,
            ...workMetaExtras(d.movie, title),
          });
          setSeriesSeasons(null);
        } else {
          setMeta(null);
          setSeriesSeasons(null);
        }
      }
    } finally {
      setMetaLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!resource) {
      setMeta(null);
      setResourceData(null);
      setSeriesSeasons(null);
      setParseEditing(false);
      return;
    }
    setResourceData(resource);
    setParseEditing(false);
    loadMeta(resource.id);
  }, [resource, loadMeta]);

  const copyTorrent = (url: string) => {
    navigator.clipboard.writeText(url).then(
      () => message.success(t('resource.magnetCopied')),
      () => message.error(t('resource.copyFailed')),
    );
  };

  const openEpisodeEditor = () => {
    if (!resourceData && !resource) return;
    const src = resourceData || resource;
    setSeasonDraft(src?.season ?? null);
    setEpisodeDraft(src?.episode ?? null);
    setAbsoluteDraft(src?.absolute_episode ?? null);
    setSeasonTouched(false);
    setEpisodeEditOpen(true);
  };

  const saveEpisode = async () => {
    const rid = (resourceData || resource)?.id;
    if (!rid) return;
    setSavingEpisode(true);
    const res = await resourcesApi.correctEpisode(rid, {
      episode: episodeDraft,
      // Only send season/absolute_episode when the user actually typed one;
      // the backend preserves the prior value when we omit it, per PATCH
      // semantics.
      ...(seasonDraft != null ? { season: seasonDraft } : {}),
      ...(absoluteDraft != null ? { absolute_episode: absoluteDraft } : {}),
    });
    setSavingEpisode(false);
    if (res.success) {
      setResourceData(res.data);
      setEpisodeEditOpen(false);
      message.success(t('resource.episodeSaved'));
      onCorrected?.();
    } else {
      message.error(res.error?.message || t('resource.episodeSaveFailed'));
    }
  };

  const r = resourceData || resource;
  const open = resource !== null;

  const workKind: 'series' | 'movie' | null = r?.series_id
    ? 'series'
    : r?.movie_id
      ? 'movie'
      : null;

  // Enter inline edit mode: prefill the form from the resource and pull the
  // full linked-work row (the resource payload doesn't carry content_type).
  const enterParseEdit = () => {
    if (!r) return;
    parseForm.setFieldsValue({
      season: r.season ?? null,
      episode: r.episode ?? null,
      absolute_episode: r.absolute_episode ?? null,
      episode_start: r.episode_start ?? null,
      episode_end: r.episode_end ?? null,
      is_batch: r.is_batch,
      batch_scope: r.batch_scope ?? null,
      is_anime: null,
      content_type: null,
    });
    setEditWork(null);
    setParseEditing(true);
    const workId = r.series_id || r.movie_id;
    if (!workId) return;
    const load = r.series_id ? seriesApi.get : moviesApi.get;
    load(workId).then((res) => {
      if (res.success) {
        setEditWork(res.data as Work);
        parseForm.setFieldsValue({
          is_anime: (res.data as Work).is_anime ?? null,
          content_type: (res.data as Work).content_type ?? null,
        });
      }
    });
  };

  const handleBatchToggle = (checked: boolean) => {
    // Batch resources have no single episode number; single-episode resources
    // have no batch scope/range. Clear the mutually exclusive group.
    if (checked) {
      parseForm.setFieldsValue({ episode: null, absolute_episode: null });
    } else {
      parseForm.setFieldsValue({ batch_scope: null, episode_start: null, episode_end: null });
    }
  };

  // Same save semantics as ResourceCorrectionModal: PUT the linked work first
  // (changed fields only), then PATCH the resource (changed fields only).
  const submitParseEdit = async () => {
    if (!r) return;
    const values = await parseForm.validateFields();
    setParseSaving(true);
    try {
      let changed = false;
      if (editWork && workKind) {
        const workPayload: Record<string, unknown> = {};
        const nextAnime = values.is_anime ?? null;
        if (nextAnime !== (editWork.is_anime ?? null)) workPayload.is_anime = nextAnime;
        const nextType = values.content_type ?? null;
        if (nextType !== (editWork.content_type ?? null)) workPayload.content_type = nextType;
        if (Object.keys(workPayload).length > 0) {
          const res =
            workKind === 'series'
              ? await seriesApi.update(editWork.id, workPayload)
              : await moviesApi.update(editWork.id, workPayload);
          if (!res.success) {
            message.error(res.error?.message || t('resource.correctSaveFailed'));
            return;
          }
          changed = true;
        }
      }

      const payload: ResourceCorrectionBody = {};
      const numField = (
        key: 'season' | 'episode' | 'absolute_episode' | 'episode_start' | 'episode_end',
      ) => {
        const next = (values[key] ?? null) as number | null;
        if (next !== (r[key] ?? null)) payload[key] = next;
      };
      numField('season');
      numField('episode');
      numField('absolute_episode');
      numField('episode_start');
      numField('episode_end');
      if (values.is_batch !== r.is_batch) payload.is_batch = values.is_batch;
      const nextScope = (values.batch_scope ?? null) as ResourceCorrectionBody['batch_scope'];
      if (nextScope !== (r.batch_scope ?? null)) payload.batch_scope = nextScope;

      let updated = r;
      if (Object.keys(payload).length > 0) {
        const res = await resourcesApi.correctParseFields(r.id, payload);
        if (!res.success) {
          message.error(res.error?.message || t('resource.correctSaveFailed'));
          return;
        }
        updated = res.data;
        changed = true;
      }

      setParseEditing(false);
      if (changed) {
        message.success(t('resource.correctSaved'));
        setResourceData(updated);
        loadMeta(r.id);
        onCorrected?.();
      }
    } finally {
      setParseSaving(false);
    }
  };

  const dash = t('format.dash');
  const parsedItems: Array<{ key: string; label: string; children: React.ReactNode }> = r
    ? [
        { key: 'subtitle_group', label: t('resource.subtitleGroup'), children: r.subtitle_group || dash },
        {
          key: 'is_batch',
          label: t('resource.isBatch'),
          children: r.is_batch ? t('common.yes') : t('common.no'),
        },
        { key: 'season', label: t('resource.seasonLabel'), children: r.season ?? dash },
        {
          key: 'episode',
          label: t('resource.episode'),
          children: (
            <Space size={4}>
              <span>
                {r.is_batch
                  ? (r.episode_start != null && r.episode_end != null
                      ? `${r.season != null ? `S${r.season} · ` : ''}E${r.episode_start}-${r.episode_end} · ${batchScopeLabel(t, r)}`
                      : `${r.season != null ? `S${r.season} · ` : ''}${batchScopeLabel(t, r)}`)
                  : (r.episode != null
                      ? (r.season != null ? `S${r.season}E${r.episode}` : t('resource.episodeFormat', { n: r.episode }))
                      : dash)}
              </span>
              {r.batch_scope === 'franchise' && r.collection_id && r.collection_name && (
                <Link to={`/collections/${r.collection_id}`}>{r.collection_name}</Link>
              )}
              {/* Only expose the manual editor for single-episode TV rows.
                  Batches don't have a single episode number to correct, and
                  movies don't carry episode metadata. */}
              {!r.is_batch && r.movie_id == null && (
                <Popover
                  open={episodeEditOpen}
                  onOpenChange={(vis) => {
                    if (vis) openEpisodeEditor();
                    else setEpisodeEditOpen(false);
                  }}
                  trigger="click"
                  placement="bottomLeft"
                  destroyOnHidden
                  title={t('resource.episodeCorrectionTitle')}
                  content={
                    <div style={{ minWidth: 240 }}>
                      <div style={{ marginBottom: 8 }}>
                        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                          {t('resource.seasonLabel')}
                        </Text>
                        <InputNumber
                          value={seasonDraft}
                          onChange={(v) => {
                            setSeasonTouched(true);
                            setSeasonDraft(typeof v === 'number' ? v : null);
                          }}
                          size="small"
                          min={0}
                          style={{ width: '100%' }}
                        />
                      </div>
                      <div style={{ marginBottom: 8 }}>
                        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                          {t('resource.episodePerSeasonLabel')}
                        </Text>
                        <InputNumber
                          value={episodeDraft}
                          onChange={(v) => setEpisodeDraft(typeof v === 'number' ? v : null)}
                          size="small"
                          min={0}
                          style={{ width: '100%' }}
                        />
                      </div>
                      <div style={{ marginBottom: 12 }}>
                        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                          {t('resource.absoluteEpisodePlaceholder')}
                        </Text>
                        <InputNumber
                          value={absoluteDraft}
                          onChange={(v) => {
                            const abs = typeof v === 'number' ? v : null;
                            setAbsoluteDraft(abs);
                            // Prefill the season from the linked series'
                            // per-season counts (same arithmetic as the
                            // backend locate_absolute_episode) while the
                            // user hasn't typed a season themselves. Out of
                            // range → leave it empty; the server marks it.
                            if (abs != null && !seasonTouched) {
                              const derived = locateAbsoluteSeason(abs, seriesSeasons);
                              if (derived != null) setSeasonDraft(derived);
                            }
                          }}
                          size="small"
                          min={0}
                          style={{ width: '100%' }}
                        />
                      </div>
                      <Space size={4} style={{ justifyContent: 'flex-end', width: '100%' }}>
                        <Button size="small" onClick={() => setEpisodeEditOpen(false)}>
                          {t('common.cancel')}
                        </Button>
                        <Button
                          size="small"
                          type="primary"
                          loading={savingEpisode}
                          onClick={saveEpisode}
                        >
                          {t('common.save')}
                        </Button>
                      </Space>
                    </div>
                  }
                >
                  <Button type="text" size="small" icon={<Pencil size={12} />} />
                </Popover>
              )}
            </Space>
          ),
        },
        {
          key: 'absolute_episode',
          label: t('resource.absoluteEpisode'),
          children: r.absolute_episode ?? dash,
        },
        {
          key: 'episode_start',
          label: t('resource.episodeStart'),
          children: r.episode_start ?? dash,
        },
        {
          key: 'episode_end',
          label: t('resource.episodeEnd'),
          children: r.episode_end ?? dash,
        },
        {
          key: 'batch_scope',
          label: t('resource.batchScope'),
          children: r.batch_scope ? batchScopeLabel(t, r) : dash,
        },
        // Confidence — only render when the reconciliation pipeline had
        // something to say. Keeps the drawer clean for the vast majority of
        // resources that don't need this metadata.
        ...(r.episode_confidence
          ? [{
              key: 'episode_confidence',
              label: t('resource.episodeConfidence'),
              children: t(`resource.confidence_${r.episode_confidence}` as never, { defaultValue: r.episode_confidence }),
            }]
          : []),
        { key: 'resolution', label: t('resource.resolution'), children: r.resolution || dash },
        { key: 'source', label: t('resource.source'), children: r.source || dash },
        { key: 'video_codec', label: t('resource.videoCodec'), children: r.video_codec || dash },
        { key: 'audio_codec', label: t('resource.audioCodec'), children: r.audio_codec || dash },
        { key: 'subtitle_type', label: t('resource.subtitleType'), children: r.subtitle_type || dash },
        {
          key: 'subtitle_langs',
          label: t('resource.subtitleLangs'),
          children: (r.subtitle_langs && r.subtitle_langs.length > 0)
            ? r.subtitle_langs.map((l) => (l === 'multi' ? t('channels.langMulti') : l)).join(', ')
            : dash,
        },
        { key: 'container', label: t('resource.container'), children: r.container || dash },
        {
          key: 'file_size',
          label: t('resource.fileSize'),
          children: r.file_size != null ? formatBytes(r.file_size) : dash,
        },
        {
          key: 'published_at',
          label: t('resource.publishedAt'),
          children: r.published_at ? formatDate(r.published_at) : dash,
        },
        {
          key: 'detail_url',
          label: t('resource.detailUrl'),
          children: r.detail_url ? (
            <a
              href={r.detail_url}
              target="_blank"
              rel="noreferrer"
              style={{ color: token.colorInfo }}
            >
              {t('resource.open')}
            </a>
          ) : (
            dash
          ),
        },
        {
          key: 'torrent_url',
          label: t('resource.downloadLink'),
          children: r.torrent_url ? (
            <Space size={4}>
              <Tooltip title={r.torrent_url}>
                <Text
                  ellipsis
                  style={{ maxWidth: 220, color: token.colorInfo, fontSize: 12 }}
                >
                  {r.torrent_url.startsWith('magnet:')
                    ? 'magnet:?xt=...'
                    : r.torrent_url}
                </Text>
              </Tooltip>
              <Button
                type="text"
                size="small"
                icon={<Copy size={12} />}
                onClick={() => copyTorrent(r.torrent_url)}
              />
            </Space>
          ) : (
            dash
          ),
        },
      ]
    : [];

  return (
    <>
      <Drawer
        title={
          // Keep the (often very long) resource title on one line so the
          // header never wraps or pushes the close button around.
          <Text ellipsis style={{ display: 'block', maxWidth: '100%' }}>
            {r ? r.title_cn || r.title_raw : t('resource.detail')}
          </Text>
        }
        open={open}
        onClose={onClose}
        width={window.innerWidth < 768 ? '100%' : 520}
        destroyOnHidden
        styles={{ body: { padding: 20 } }}
        footer={
          <Space style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Button
              icon={<Download size={14} />}
              onClick={() => setCreateTaskOpen(true)}
            >
              {t('tasks.createTask')}
            </Button>
          </Space>
        }
      >
        {r && (
          <div>
            {/* Raw title */}
            <Paragraph style={{ color: token.colorTextTertiary, fontSize: 12, marginBottom: 16 }}>
              {r.title_raw}
            </Paragraph>

            {/* Metadata section */}
            <div style={{ marginBottom: 16 }}>
              <Text strong style={{ fontSize: 13, display: 'block', marginBottom: 10 }}>
                {t('resource.metadata')}
              </Text>
              {metaLoading ? (
                <div style={{ textAlign: 'center', padding: '24px 0' }}>
                  <Spin />
                </div>
              ) : meta ? (
                <div
                  style={{
                    display: 'flex',
                    gap: 12,
                    padding: 12,
                    border: `1px solid ${token.colorSuccessBorder}`,
                    borderRadius: 8,
                    background: token.colorSuccessBg,
                  }}
                >
                  <PosterBlock url={meta.poster_url} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'flex-start',
                        gap: 8,
                      }}
                    >
                      <Space size={6} style={{ marginBottom: 6 }} wrap>
                        <Text strong>{meta.title}</Text>
                        <Tag color={meta.type === 'series' ? 'blue' : 'green'}>
                          {meta.type === 'series' ? t('resource.series') : t('resource.movie')}
                        </Tag>
                        {meta.year != null && <Tag>{meta.year}</Tag>}
                        {meta.rating != null && <Tag color="gold">★ {meta.rating}</Tag>}
                      </Space>
                      <Tooltip title={t('resource.correctMatch')}>
                        <Button
                          size="small"
                          type="text"
                          icon={<Pencil size={14} />}
                          onClick={() => setCorrectionOpen(true)}
                        />
                      </Tooltip>
                    </div>
                    {(meta.secondary_titles?.length ?? 0) > 0 && (
                      <div
                        style={{
                          fontSize: 12,
                          color: token.colorTextSecondary,
                          marginBottom: 6,
                          wordBreak: 'break-all',
                        }}
                      >
                        {meta.secondary_titles!.join(' / ')}
                      </div>
                    )}
                    {(meta.genres?.length ?? 0) > 0 && (
                      <div style={{ marginBottom: 6 }}>
                        {meta.genres!.map((g) => (
                          <Tag key={g} style={{ fontSize: 11, marginInlineEnd: 4 }}>{g}</Tag>
                        ))}
                      </div>
                    )}
                    {meta.description && (
                      <Paragraph
                        type="secondary"
                        style={{ fontSize: 12, marginBottom: 0 }}
                        ellipsis={{ rows: 3 }}
                      >
                        {meta.description}
                      </Paragraph>
                    )}
                  </div>
                </div>
              ) : (
                <div
                  style={{
                    padding: 16,
                    border: `1px dashed ${token.colorBorder}`,
                    borderRadius: 8,
                    textAlign: 'center',
                  }}
                >
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description={false}
                    style={{ marginBottom: 8 }}
                  />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {t('resource.noMetadata')}
                  </Text>
                  <div style={{ marginTop: 8 }}>
                    <Button
                      size="small"
                      icon={<Pencil size={12} />}
                      onClick={() => setCorrectionOpen(true)}
                    >
                      {t('resource.manualFix')}
                    </Button>
                  </div>
                </div>
              )}
            </div>

            <Divider style={{ margin: '16px 0', borderColor: token.colorBorder }} />

            {/* Parsed details — view mode (Descriptions) / inline edit mode (Form) */}
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                marginBottom: 10,
              }}
            >
              <Text strong style={{ fontSize: 13 }}>
                {t('resource.parsedFields')}
              </Text>
              {!parseEditing && (
                <Button
                  size="small"
                  type="text"
                  icon={<Pencil size={14} />}
                  onClick={enterParseEdit}
                >
                  {t('common.edit')}
                </Button>
              )}
            </div>
            {parseEditing ? (
              <Form form={parseForm} layout="vertical" size="small">
                <Form.Item name="is_batch" label={t('resource.isBatch')} valuePropName="checked">
                  <Switch onChange={handleBatchToggle} />
                </Form.Item>
                <Form.Item name="season" label={t('resource.seasonLabel')}>
                  <InputNumber min={0} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="episode" label={t('resource.episodePerSeasonLabel')}>
                  <InputNumber min={0} disabled={isBatchWatch} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="absolute_episode" label={t('resource.absoluteEpisodePlaceholder')}>
                  <InputNumber min={0} disabled={isBatchWatch} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="episode_start" label={t('resource.episodeStart')}>
                  <InputNumber min={0} disabled={!isBatchWatch} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="episode_end" label={t('resource.episodeEnd')}>
                  <InputNumber min={0} disabled={!isBatchWatch} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="batch_scope" label={t('resource.batchScope')}>
                  <Select
                    allowClear
                    disabled={!isBatchWatch}
                    options={[
                      { value: 'season', label: t('channels.batch') },
                      { value: 'multi_season', label: t('channels.batchMultiSeason') },
                      { value: 'franchise', label: t('channels.batchFranchise') },
                    ]}
                  />
                </Form.Item>

                {workKind && (
                  <>
                    <Divider style={{ margin: '8px 0 12px' }} />
                    <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
                      {t('resource.linkedWork')}
                    </div>
                    <Form.Item name="is_anime" label={t('works.animeStatus')}>
                      <Select
                        allowClear
                        placeholder={t('common.unknown')}
                        options={[
                          { value: true, label: t('works.anime') },
                          { value: false, label: t('works.liveAction') },
                        ]}
                      />
                    </Form.Item>
                    <Form.Item name="content_type" label={t('resource.contentType')}>
                      <Select
                        allowClear
                        placeholder={t('common.unknown')}
                        options={[
                          { value: 'tv', label: t('works.tv') },
                          { value: 'movie', label: t('works.movie') },
                        ]}
                      />
                    </Form.Item>
                  </>
                )}

                <Space size={8} style={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <Button size="small" onClick={() => setParseEditing(false)}>
                    {t('common.cancel')}
                  </Button>
                  <Button
                    size="small"
                    type="primary"
                    loading={parseSaving}
                    onClick={submitParseEdit}
                  >
                    {t('common.save')}
                  </Button>
                </Space>
              </Form>
            ) : (
              <Descriptions
                column={1}
                size="small"
                labelStyle={{ color: token.colorTextTertiary, width: 100, padding: '4px 8px' }}
                contentStyle={{ color: token.colorTextSecondary, padding: '4px 8px' }}
                style={{ fontSize: 12 }}
                items={parsedItems}
              />
            )}

            <Divider style={{ margin: '16px 0', borderColor: token.colorBorder }} />

            {/* File listing (torrent contents / downloader files) */}
            <Text strong style={{ fontSize: 13, display: 'block', marginBottom: 10 }}>
              {t('resource.files')}
            </Text>
            <ResourceFilesView resourceId={r.id} />
          </div>
        )}
      </Drawer>

      {r && (
        <MetadataCorrectionModal
          resourceId={r.id}
          open={correctionOpen}
          onClose={() => setCorrectionOpen(false)}
          onCorrected={() => {
            setCorrectionOpen(false);
            loadMeta(r.id);
            onCorrected?.();
          }}
        />
      )}

      {r && (
        <CreateTaskModal
          resourceId={r.id}
          open={createTaskOpen}
          onClose={() => setCreateTaskOpen(false)}
        />
      )}
    </>
  );
}
