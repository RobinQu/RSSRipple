import { useEffect, useState } from 'react';
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
} from 'antd';
import { Copy, Pencil } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { resourcesApi } from '../api/channels';
import { formatBytes, formatDate } from '../utils/format';
import MetadataCorrectionModal from './MetadataCorrectionModal';
import { posterUrl, useDefaultPoster } from '../utils/poster';
import type { FileResource } from '../types';

const { Text, Paragraph } = Typography;

interface LinkedMeta {
  type: 'series' | 'movie';
  title: string;
  poster_url?: string | null;
  description?: string | null;
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
        border: '1px solid #d9d9dd',
        background: '#eeece7',
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
  const [meta, setMeta] = useState<LinkedMeta | null>(null);
  const [metaLoading, setMetaLoading] = useState(false);
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [resourceData, setResourceData] = useState<FileResource | null>(null);
  const [episodeEditOpen, setEpisodeEditOpen] = useState(false);
  const [seasonDraft, setSeasonDraft] = useState<number | null>(null);
  const [episodeDraft, setEpisodeDraft] = useState<number | null>(null);
  const [absoluteDraft, setAbsoluteDraft] = useState<number | null>(null);
  const [savingEpisode, setSavingEpisode] = useState(false);

  const loadMeta = async (rid: string) => {
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
          const series = d.linked.entity;
          setMeta({
            type: 'series',
            title:
              series.title_cn || series.title_en || series.original_title || t('resource.unknownSeries'),
            poster_url: series.poster_url,
          });
        } else if (d.linked?.type === 'movie') {
          const movie = d.linked.entity;
          setMeta({
            type: 'movie',
            title:
              movie.title_cn || movie.title_en || movie.original_title || t('resource.unknownMovie'),
            poster_url: movie.poster_url,
          });
        } else if (d.series_id && d.series) {
          setMeta({
            type: 'series',
            title:
              d.series.title_cn || d.series.title_en || t('resource.unknownSeries'),
            poster_url: d.series.poster_url,
          });
        } else if (d.movie_id && d.movie) {
          setMeta({
            type: 'movie',
            title:
              d.movie.title_cn || d.movie.title_en || t('resource.unknownMovie'),
            poster_url: d.movie.poster_url,
          });
        } else {
          setMeta(null);
        }
      }
    } finally {
      setMetaLoading(false);
    }
  };

  useEffect(() => {
    if (!resource) {
      setMeta(null);
      setResourceData(null);
      return;
    }
    setResourceData(resource);
    loadMeta(resource.id);
  }, [resource]);

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

  const dash = t('format.dash');
  const parsedItems: Array<{ key: string; label: string; children: React.ReactNode }> = r
    ? [
        { key: 'subtitle_group', label: t('resource.subtitleGroup'), children: r.subtitle_group || dash },
        {
          key: 'episode',
          label: t('resource.episode'),
          children: (
            <Space size={4}>
              <span>
                {r.is_batch
                  ? (r.episode_start != null && r.episode_end != null
                      ? `${r.season != null ? `S${r.season} · ` : ''}E${r.episode_start}-${r.episode_end} · ${t('channels.batch')}`
                      : `${r.season != null ? `S${r.season} · ` : ''}${t('channels.batch')}`)
                  : (r.episode != null
                      ? (r.season != null ? `S${r.season}E${r.episode}` : t('resource.episodeFormat', { n: r.episode }))
                      : dash)}
              </span>
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
                          onChange={(v) => setSeasonDraft(typeof v === 'number' ? v : null)}
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
                          onChange={(v) => setAbsoluteDraft(typeof v === 'number' ? v : null)}
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
        // Absolute episode + confidence — only render when the reconciliation
        // pipeline had something to say. Keeps the drawer clean for the
        // vast majority of resources that don't need this metadata.
        ...(r.absolute_episode != null || r.episode_confidence
          ? [{
              key: 'episode_confidence',
              label: t('resource.episodeConfidence'),
              children: (() => {
                const parts: string[] = [];
                if (r.absolute_episode != null) {
                  parts.push(t('resource.absoluteEpisodeLabel', { n: r.absolute_episode }));
                }
                if (r.episode_confidence) {
                  parts.push(t(`resource.confidence_${r.episode_confidence}` as never, { defaultValue: r.episode_confidence }));
                }
                return parts.join(' · ');
              })(),
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
              style={{ color: '#1863dc' }}
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
                  style={{ maxWidth: 220, color: '#1863dc', fontSize: 12 }}
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
        title={r ? r.title_cn || r.title_raw : t('resource.detail')}
        open={open}
        onClose={onClose}
        width={window.innerWidth < 768 ? '100%' : 520}
        destroyOnHidden
        styles={{ body: { padding: 20 } }}
        extra={
          <Button
            type="primary"
            size="small"
            icon={<Pencil size={12} />}
            onClick={() => setCorrectionOpen(true)}
          >
            {t('resource.correctMetadata')}
          </Button>
        }
      >
        {r && (
          <div>
            {/* Raw title */}
            <Paragraph style={{ color: '#93939f', fontSize: 12, marginBottom: 16 }}>
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
                    border: '1px solid #b7d9d3',
                    borderRadius: 8,
                    background: '#edfce9',
                  }}
                >
                  <PosterBlock url={meta.poster_url} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <Space size={6} style={{ marginBottom: 6 }}>
                      <Text strong>{meta.title}</Text>
                      <Tag color={meta.type === 'series' ? 'blue' : 'green'}>
                        {meta.type === 'series' ? t('resource.series') : t('resource.movie')}
                      </Tag>
                    </Space>
                  </div>
                </div>
              ) : (
                <div
                  style={{
                    padding: 16,
                    border: '1px dashed #d9d9dd',
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

            <Divider style={{ margin: '16px 0', borderColor: '#d9d9dd' }} />

            {/* Parsed details */}
            <Text strong style={{ fontSize: 13, display: 'block', marginBottom: 10 }}>
              {t('resource.parsedFields')}
            </Text>
            <Descriptions
              column={1}
              size="small"
              labelStyle={{ color: '#93939f', width: 100, padding: '4px 8px' }}
              contentStyle={{ color: '#cdcdcd', padding: '4px 8px' }}
              style={{ fontSize: 12 }}
              items={parsedItems}
            />
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
    </>
  );
}
