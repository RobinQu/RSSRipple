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
  theme,
} from 'antd';
import { Copy, Download, Pencil } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { resourcesApi } from '../api/channels';
import { formatBytes, formatDate } from '../utils/format';
import { batchScopeLabel } from '../utils/batch';
import ResourceCorrectionModal from './ResourceCorrectionModal';
import CreateTaskModal from './CreateTaskModal';
import { ResourceFilesView } from './ResourceFilesDrawer';
import { posterUrl, useDefaultPoster } from '../utils/poster';
import type {
  FileResource,
  ResourceFileItem,
  TVSeries,
} from '../types';

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

interface ResourceDetailDrawerProps {
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

function episodeRangeLabel(
  start: number | null,
  end: number | null,
): string | null {
  if (start == null) return null;
  if (end != null && end !== start) return `E${start}-${end}`;
  return `E${start}`;
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
  const [createTaskOpen, setCreateTaskOpen] = useState(false);
  const [resourceData, setResourceData] = useState<FileResource | null>(null);
  // Torrent listing snapshot for the batch work↔file mapping display.
  const [filesList, setFilesList] = useState<ResourceFileItem[]>([]);
  const [parseEditOpen, setParseEditOpen] = useState(false);

  const loadMeta = useCallback(async (rid: string) => {
    setMetaLoading(true);
    try {
      const [metaRes, resRes, filesRes] = await Promise.all([
        resourcesApi.getMetadata(rid),
        resourcesApi.get(rid),
        resourcesApi.getFiles(rid),
      ]);
      if (resRes.success) setResourceData(resRes.data);
      if (filesRes.success) setFilesList(filesRes.data.files);
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
        } else if (d.series_id && d.series) {
          const title =
            d.series.original_title || d.series.title_cn || d.series.title_en || t('resource.unknownSeries');
          setMeta({
            type: 'series',
            title,
            poster_url: d.series.poster_url,
            ...workMetaExtras(d.series, title),
          });
        } else if (d.movie_id && d.movie) {
          const title =
            d.movie.original_title || d.movie.title_cn || d.movie.title_en || t('resource.unknownMovie');
          setMeta({
            type: 'movie',
            title,
            poster_url: d.movie.poster_url,
            ...workMetaExtras(d.movie, title),
          });
        } else {
          setMeta(null);
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
      setFilesList([]);
      setParseEditOpen(false);
      return;
    }
    setResourceData(resource);
    setParseEditOpen(false);
    loadMeta(resource.id);
  }, [resource, loadMeta]);

  const writeClipboard = (value: string): Promise<boolean> => {
    // Clipboard.writeText is restricted to secure contexts. Do not attempt it
    // on HTTP: waiting for its rejection can consume the transient user
    // activation required by the legacy copy command.
    if (window.isSecureContext && navigator.clipboard?.writeText) {
      return navigator.clipboard.writeText(value).then(
        () => true,
        () => false,
      );
    }

    const textarea = document.createElement('textarea');
    const previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    let copyEventHandled = false;
    const handleCopy = (event: ClipboardEvent) => {
      if (!event.clipboardData) return;
      event.clipboardData.setData('text/plain', value);
      event.preventDefault();
      copyEventHandled = true;
    };

    textarea.value = value;
    textarea.readOnly = true;
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    textarea.style.top = '0';
    document.body.appendChild(textarea);
    textarea.addEventListener('copy', handleCopy);
    textarea.focus();
    textarea.select();

    let commandSucceeded: boolean;
    try {
      commandSucceeded = document.execCommand('copy');
    } catch {
      commandSucceeded = false;
    } finally {
      textarea.removeEventListener('copy', handleCopy);
      textarea.remove();
      previouslyFocused?.focus();
    }

    // execCommand may return true without changing the clipboard. Requiring
    // the copy event confirms that this page actually supplied the payload.
    return Promise.resolve(commandSucceeded && copyEventHandled);
  };

  const copyTorrent = async (url: string) => {
    if (await writeClipboard(url)) {
      message.success(t('resource.magnetCopied'));
    } else {
      message.error(t('resource.copyFailed'));
    }
  };

  const copyResourceId = async (id: string) => {
    if (await writeClipboard(id)) {
      message.success(t('resource.idCopied'));
    } else {
      message.error(t('resource.copyFailed'));
    }
  };

  const r = resourceData || resource;
  const open = resource !== null;

  const dash = t('format.dash');
  const parsedItems: Array<{ key: string; label: string; children: React.ReactNode }> = r
    ? [
        {
          key: 'resource_id',
          label: t('resource.resourceId'),
          children: (
            <Space size={4} style={{ maxWidth: '100%' }}>
              <Text copyable={false} ellipsis style={{ maxWidth: 250, fontFamily: 'monospace', fontSize: 11 }}>
                {r.id}
              </Text>
              <Button type="text" size="small" aria-label={t('resource.copyResourceId')} icon={<Copy size={12} />} onClick={() => void copyResourceId(r.id)} />
            </Space>
          ),
        },
        { key: 'subtitle_groups', label: t('resource.subtitleGroup'), children: (r.subtitle_groups?.length ? r.subtitle_groups : (r.subtitle_group ? [r.subtitle_group] : [])).join(' & ') || dash },
        {
          key: 'is_batch',
          label: t('resource.isBatch'),
          children: r.is_batch
            ? `${t('common.yes')}${r.batch_scope ? ` · ${batchScopeLabel(t, r)}` : ''}`
            : t('common.no'),
        },
        {
          key: 'episode',
          label: t('resource.episode'),
          children: (
            <Space size={4}>
              <span>
                {r.is_batch
                  ? (r.episode_start != null && r.episode_end != null
                      ? `${r.season != null ? `S${r.season} · ` : ''}E${r.episode_start}-${r.episode_end}`
                      : `${r.season != null ? `S${r.season} · ` : ''}${batchScopeLabel(t, r)}`)
                  : (r.episode != null
                      ? (r.season != null ? `S${r.season}E${r.episode}` : t('resource.episodeFormat', { n: r.episode }))
                      : dash)}
              </span>
              {r.batch_scope === 'franchise' && r.collection_id && r.collection_name && (
                <Link to={`/collections/${r.collection_id}`}>{r.collection_name}</Link>
              )}
            </Space>
          ),
        },
        {
          key: 'absolute_episode',
          label: t('resource.absoluteEpisode'),
          children: r.absolute_episode ?? dash,
        },
        ...(r.season_ranges && r.season_ranges.length > 0
          ? [{
              key: 'season_ranges',
              label: t('resource.seasonRanges'),
              children: (
                <Space size={4} wrap>
                  {r.season_ranges.map((sr) => (
                    <Tag key={sr.season} style={{ fontSize: 11 }}>
                      S{sr.season}
                      {sr.episode_start != null
                        ? ` · E${sr.episode_start}${sr.episode_end != null && sr.episode_end !== sr.episode_start ? `-${sr.episode_end}` : ''}`
                        : ''}
                    </Tag>
                  ))}
                </Space>
              ),
            }]
          : []),
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
                onClick={() => void copyTorrent(r.torrent_url)}
              />
            </Space>
          ) : (
            dash
          ),
        },
      ]
    : [];

  // Work ↔ file mapping groups (batch resources only). The detail payload
  // (GET /resources/{id}) carries the assignment rows on top of the base
  // resource shape.
  const detailRow = r as (FileResource & {
    file_assignments?: Array<{
      file_path: string;
      file_size: number | null;
      series_id: string | null;
      movie_id: string | null;
      season: number | null;
      episode_start: number | null;
      episode_end: number | null;
      work_title: string | null;
    }>;
  }) | null;
  const assignments = detailRow?.file_assignments ?? [];
  const assignedPaths = new Set(assignments.map((a) => a.file_path));
  const unassigned = filesList.filter((f) => !assignedPaths.has(f.name));
  const showWorkLinks = !!r?.is_batch && assignments.length > 0;

  interface MappingGroup {
    key: string;
    label: string;
    color: string;
    seasons: Map<number | null, typeof assignments>;
    total: number;
  }

  const mappingGroups: MappingGroup[] = [];
  if (showWorkLinks) {
    const byKey = new Map<string, MappingGroup>();
    for (const a of assignments) {
      const wt = a.series_id ? 'series' : 'movie';
      const wid = (a.series_id || a.movie_id)!;
      const key = `${wt}:${wid}`;
      let group = byKey.get(key);
      if (!group) {
        group = {
          key,
          label: a.work_title || wid,
          color: wt === 'series' ? 'blue' : 'green',
          seasons: new Map(),
          total: 0,
        };
        byKey.set(key, group);
      }
      const rows = group.seasons.get(a.season) ?? [];
      rows.push(a);
      group.seasons.set(a.season, rows);
      group.total += 1;
    }
    mappingGroups.push(...byKey.values());
  }

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
            <Button icon={<Pencil size={14} />} onClick={() => setParseEditOpen(true)}>
              {t('resource.editFooter')}
            </Button>
            <Button icon={<Download size={14} />} onClick={() => setCreateTaskOpen(true)}>
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
                  className="resource-metadata-card"
                  style={{
                    display: 'flex',
                    gap: 12,
                    padding: 12,
                    color: 'var(--rr-text)',
                    border: '1px solid var(--rr-success-border)',
                    borderRadius: 8,
                    background: 'var(--rr-success-soft)',
                  }}
                >
                  <PosterBlock url={meta.poster_url} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <Space size={6} style={{ marginBottom: 6 }} wrap>
                      <Text strong style={{ color: 'var(--rr-text)' }}>{meta.title}</Text>
                      <Tag color={meta.type === 'series' ? 'blue' : 'green'}>
                        {meta.type === 'series' ? t('resource.series') : t('resource.movie')}
                      </Tag>
                      {meta.year != null && <Tag>{meta.year}</Tag>}
                      {meta.rating != null && <Tag color="gold">★ {meta.rating}</Tag>}
                    </Space>
                    {(meta.secondary_titles?.length ?? 0) > 0 && (
                      <div
                        style={{
                          fontSize: 12,
                          color: 'var(--rr-text-secondary)',
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
                        style={{ color: 'var(--rr-text-secondary)', fontSize: 12, marginBottom: 0 }}
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
                </div>
              )}
            </div>

            <Divider style={{ margin: '16px 0', borderColor: token.colorBorder }} />

            {/* Parsed fields — read-only; editing lives in the wizard behind
                the footer 编辑 button. */}
            <Text strong style={{ fontSize: 13, display: 'block', marginBottom: 10 }}>
              {t('resource.parsedFields')}
            </Text>
            <Descriptions
              column={1}
              size="small"
              labelStyle={{ color: token.colorTextTertiary, width: 100, padding: '4px 8px' }}
              contentStyle={{ color: token.colorTextSecondary, padding: '4px 8px' }}
              style={{ fontSize: 12 }}
              items={parsedItems}
            />

            {/* Batch work ↔ file mapping (read-only) */}
            {showWorkLinks && (
              <>
                <Divider style={{ margin: '16px 0', borderColor: token.colorBorder }} />
                <Text strong style={{ fontSize: 13, display: 'block', marginBottom: 10 }}>
                  {t('resource.workLinksTitle')}
                </Text>
                {mappingGroups.map((g) => (
                  <div
                    key={g.key}
                    style={{
                      border: '1px solid var(--rr-border-soft)',
                      borderRadius: 8,
                      padding: '8px 12px',
                      marginBottom: 8,
                    }}
                  >
                    <Space size={6}>
                      <Tag color={g.color}>
                        {g.key.startsWith('series:') ? t('works.tv') : t('works.movie')}
                      </Tag>
                      <Text strong style={{ fontSize: 12 }}>{g.label}</Text>
                      <Text type="secondary" style={{ fontSize: 11 }}>{g.total}</Text>
                    </Space>
                    {[...g.seasons.entries()]
                      .sort((x, y) => (x[0] ?? 9999) - (y[0] ?? 9999))
                      .map(([season, rows]) => (
                        <div key={season ?? 'na'} style={{ marginTop: 6 }}>
                          {season != null && (
                            <Tag style={{ fontSize: 11 }}>{`${t('resource.seasonLabel')} ${season}`}</Tag>
                          )}
                          {rows.map((row) => {
                            const range = episodeRangeLabel(row.episode_start, row.episode_end);
                            return (
                              <div
                                key={row.file_path}
                                style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0' }}
                              >
                                <span style={{ flex: 1, minWidth: 0, fontSize: 12, overflowWrap: 'anywhere' }}>
                                  {row.file_path}
                                </span>
                                {range && <Tag style={{ fontSize: 11, margin: 0 }}>{range}</Tag>}
                                {row.file_size != null && (
                                  <Text type="secondary" style={{ fontSize: 11, flexShrink: 0 }}>
                                    {formatBytes(row.file_size)}
                                  </Text>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      ))}
                  </div>
                ))}
                {unassigned.length > 0 && (
                  <div style={{ marginTop: 4 }}>
                    <Tag>{`${t('resource.unassignedFiles')}（${unassigned.length}）`}</Tag>
                    <div style={{ marginTop: 4 }}>
                      {unassigned.slice(0, 20).map((f) => (
                        <div key={f.name} style={{ fontSize: 12, color: token.colorTextSecondary, overflowWrap: 'anywhere' }}>
                          {f.name}
                        </div>
                      ))}
                      {unassigned.length > 20 && (
                        <Text type="secondary" style={{ fontSize: 11 }}>
                          …{unassigned.length - 20}
                        </Text>
                      )}
                    </div>
                  </div>
                )}
              </>
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
        <ResourceCorrectionModal
          resourceId={r.id}
          open={parseEditOpen}
          onClose={() => setParseEditOpen(false)}
          onSaved={(updated) => {
            setParseEditOpen(false);
            setResourceData(updated);
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
