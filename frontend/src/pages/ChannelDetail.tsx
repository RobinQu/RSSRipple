import { useState, useEffect, useRef, useCallback } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  ArrowLeft,
  Pencil,
  RefreshCw,
  Wand2,
  Film,
  Tv,
  HelpCircle,
  Info,
  Copy,
  Package,
  ExternalLink,
} from 'lucide-react';
import {
  Typography,
  Space,
  Button,
  Spin,
  Card,
  Row,
  Col,
  App,
  Checkbox,
  Collapse,
  Tag,
  Empty,
  Tooltip,
  Tabs,
} from 'antd';
import { channelsApi } from '../api/channels';
import StatusBadge from '../components/StatusBadge';
import ResourceDetailDrawer from '../components/ResourceDetailDrawer';
import FilterSummaryModal from '../components/FilterSummaryModal';
import { timeAgo } from '../utils/format';
import type {
  ChannelDetail as ChannelDetailData,
  FileResource,
  GroupedResource,
} from '../types';

const { Title, Text } = Typography;

function groupIcon(type: GroupedResource['type']) {
  if (type === 'series') return <Tv size={14} />;
  if (type === 'movie') return <Film size={14} />;
  return <HelpCircle size={14} />;
}

function groupColor(type: GroupedResource['type']) {
  if (type === 'series') return 'blue';
  if (type === 'movie') return 'green';
  return 'default';
}

function formatEpisodeCell(r: FileResource): {
  label: string;
  batch: boolean;
  confidence: FileResource['episode_confidence'];
  absoluteEpisode: number | null;
} {
  const confidence = r.episode_confidence ?? null;
  const absoluteEpisode = r.absolute_episode ?? null;
  if (r.is_batch) {
    const seasonPart = r.season != null ? `S${r.season} · ` : '';
    if (r.episode_start != null && r.episode_end != null) {
      return {
        label: `${seasonPart}E${r.episode_start}-${r.episode_end}`,
        batch: true,
        confidence,
        absoluteEpisode,
      };
    }
    if (r.episode_start != null) {
      return { label: `${seasonPart}E${r.episode_start}+`, batch: true, confidence, absoluteEpisode };
    }
    return { label: `${seasonPart || ''}Batch`.trim() || 'Batch', batch: true, confidence, absoluteEpisode };
  }
  if (r.episode == null) return { label: '—', batch: false, confidence, absoluteEpisode };
  const perSeason = r.season != null ? `S${r.season}E${r.episode}` : `E${r.episode}`;
  return { label: perSeason, batch: false, confidence, absoluteEpisode };
}

const PAGE_SIZE = 30;

export default function ChannelDetail() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const { message } = App.useApp();
  const navigate = useNavigate();

  const [channel, setChannel] = useState<ChannelDetailData | null>(null);
  const [tab, setTab] = useState<'parsed' | 'unparsed'>('parsed');
  const [parsedGroups, setParsedGroups] = useState<GroupedResource[]>([]);
  const [parsedPage, setParsedPage] = useState(1);
  const [parsedTotal, setParsedTotal] = useState(0);
  const [parsedLoading, setParsedLoading] = useState(true);
  const [unparsed, setUnparsed] = useState<FileResource[]>([]);
  const [unparsedPage, setUnparsedPage] = useState(1);
  const [unparsedTotal, setUnparsedTotal] = useState(0);
  const [unparsedLoading, setUnparsedLoading] = useState(true);
  const [channelLoading, setChannelLoading] = useState(true);
  const [selectedResource, setSelectedResource] = useState<FileResource | null>(null);
  const [fetchStatus, setFetchStatus] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [filterModalOpen, setFilterModalOpen] = useState(false);

  const loadChannel = useCallback(async () => {
    if (!id) return;
    const r = await channelsApi.get(id);
    if (r.success) setChannel(r.data);
    setChannelLoading(false);
  }, [id]);

  const loadParsed = useCallback(async (p: number, silent = false) => {
    if (!id) return;
    if (!silent) setParsedLoading(true);
    const r = await channelsApi.resources(id, p, PAGE_SIZE, true, true);
    if (r.success) {
      setParsedGroups(r.data as GroupedResource[]);
      if (r.meta) setParsedTotal(r.meta.total);
    }
    if (!silent) setParsedLoading(false);
  }, [id]);

  const loadUnparsed = useCallback(async (p: number, silent = false) => {
    if (!id) return;
    if (!silent) setUnparsedLoading(true);
    const r = await channelsApi.resources(id, p, PAGE_SIZE, false, false);
    if (r.success) {
      setUnparsed(r.data as FileResource[]);
      if (r.meta) setUnparsedTotal(r.meta.total);
    }
    if (!silent) setUnparsedLoading(false);
  }, [id]);

  const reloadActiveTab = useCallback(async () => {
    // Silent: polling calls this every few seconds during a fetch. Toggling
    // the loading spinners each time caused the page to flicker, so refresh
    // the data in place without the loading state.
    if (tab === 'parsed') await loadParsed(parsedPage, true);
    else await loadUnparsed(unparsedPage, true);
  }, [tab, parsedPage, unparsedPage, loadParsed, loadUnparsed]);

  useEffect(() => {
    setChannelLoading(true);
    loadChannel();
    loadParsed(1);
    loadUnparsed(1);
  }, [loadChannel, loadParsed, loadUnparsed]);

  useEffect(() => {
    if (!id) return;
    channelsApi.fetchStatus(id).then((r) => {
      if (r.success && r.data) {
        const s = r.data.status;
        if (s === 'queued' || s === 'running' || s === 'running...') setFetchStatus(s);
      }
    });
  }, [id]);

  const isFetching = fetchStatus === 'queued' || fetchStatus === 'running';

  useEffect(() => {
    if (!isFetching || !id) {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
      return;
    }
    if (pollRef.current) return;
    pollRef.current = setInterval(async () => {
      const r = await channelsApi.fetchStatus(id);
      if (!r.success || !r.data) return;
      setFetchStatus(r.data.status);
      reloadActiveTab();
      if (r.data.status === 'success' || r.data.status === 'failed' || r.data.status === 'done') {
        loadChannel();
        if (pollRef.current) clearInterval(pollRef.current);
        pollRef.current = null;
        setTimeout(() => setFetchStatus(null), 1500);
      }
    }, 3000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [isFetching, id, reloadActiveTab, loadChannel]);

  const handleFetch = async () => {
    if (!id || isFetching) return;
    setFetchStatus('queued');
    const r = await channelsApi.fetch(id);
    if (!r.success) {
      setFetchStatus(null);
      message.error(r.error?.message || t('channels.fetchTriggerFailed'));
    }
  };

  const toggleAllInGroup = (group: GroupedResource, checked: boolean) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      group.resources.forEach((r) => (checked ? next.add(r.id) : next.delete(r.id)));
      return next;
    });
  };

  const toggleResource = (rid: string, checked: boolean) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      checked ? next.add(rid) : next.delete(rid);
      return next;
    });
  };

  const copyRawTitle = async (raw: string) => {
    try {
      await navigator.clipboard.writeText(raw);
      message.success(t('channels.rawTitleCopied'));
    } catch {
      message.error(t('channels.copyFailed'));
    }
  };

  const parsedTotalPages = Math.ceil(parsedTotal / PAGE_SIZE);
  const unparsedTotalPages = Math.ceil(unparsedTotal / PAGE_SIZE);

  if (channelLoading) {
    return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  }
  if (!channel) return <Text type="danger">{t('channels.notFound')}</Text>;

  return (
    <div>
      {/* Header */}
      <Space align="start" style={{ marginBottom: 24, width: '100%', justifyContent: 'space-between', flexWrap: 'wrap' }}>
        <Space align="start">
          <Link to="/channels">
            <Button type="text" icon={<ArrowLeft size={18} />} />
          </Link>
          <div>
            <Space align="center">
              <Title level={3} style={{ margin: 0 }}>
                {channel.name}
              </Title>
              <StatusBadge status={channel.status} />
              {channel.metadata_agent_enabled && <Tag color="blue">Agent</Tag>}
              {channel.metadata_agent_enabled && channel.metadata_source && (
                <Tag color="geekblue">
                  {t(`channels.sources.${channel.metadata_source}`, {
                    defaultValue: channel.metadata_source,
                  })}
                </Tag>
              )}
            </Space>
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
              {channel.url}
            </Text>
            <Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
               {t('channels.lastFetchPrefix')}{channel.last_fetched_at ? timeAgo(channel.last_fetched_at) : t('common.never')}
              {channel.last_fetch_error && (
                <span style={{ color: '#b30000', marginLeft: 8 }}>
                  ⚠ {channel.last_fetch_error}
                </span>
              )}
            </Text>
          </div>
        </Space>
        <Space>
          <Button
            icon={<RefreshCw size={14} />}
            onClick={handleFetch}
            disabled={isFetching}
            loading={isFetching}
          >
            {isFetching ? t('channels.fetching') : t('channels.fetchNow')}
          </Button>
          <Button icon={<Pencil size={14} />} onClick={() => navigate(`/channels/${id}/edit`)}>
            {t('common.edit')}
          </Button>
        </Space>
      </Space>

      {/* Info cards */}
      <Row gutter={[12, 12]} style={{ marginBottom: 24 }}>
        <Col xs={12} sm={6}>
          <Card size="small">
            <div style={{ fontSize: 12, color: '#93939f' }}>{t('channels.fetchInterval')}</div>
            <div style={{ fontWeight: 500 }}>{channel.fetch_interval}s</div>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card size="small">
            <div style={{ fontSize: 12, color: '#93939f' }}>{t('channels.unparsedResources')}</div>
            <div style={{ fontWeight: 500 }}>{unparsedTotal}</div>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card size="small">
            <div style={{ fontSize: 12, color: '#93939f' }}>{t('channels.workGroups')}</div>
            <div style={{ fontWeight: 500 }}>{parsedTotal}</div>
          </Card>
        </Col>
      </Row>

      {/* Selection bar */}
      {selectedIds.size > 0 && (
        <Card
          size="small"
          style={{
            marginBottom: 16,
            borderColor: '#8fbfb7',
            background: '#edfce9',
          }}
        >
          <Space style={{ width: '100%', justifyContent: 'space-between' }}>
            <Text style={{ color: '#003c33' }}>{t('common.selected')} {selectedIds.size} {t('channels.resources')}</Text>
            <Space>
              <Button size="small" onClick={() => setSelectedIds(new Set())}>
                {t('common.deselect')}
              </Button>
              <Button
                size="small"
                type="primary"
                icon={<Wand2 size={12} />}
                onClick={() => setFilterModalOpen(true)}
              >
                {t('channels.generateFilterRules')}
              </Button>
            </Space>
          </Space>
        </Card>
      )}

      <Tabs
        activeKey={tab}
        onChange={(k) => {
          setTab(k as 'parsed' | 'unparsed');
          setSelectedIds(new Set());
        }}
        style={{ marginTop: 16 }}
        items={[
          {
            key: 'parsed',
            label: (
              <Space size={6}>
                {t('channels.parsedResources')}
                {parsedTotal > 0 && <Tag>{parsedTotal}</Tag>}
              </Space>
            ),
            children: (
              <>
                {/* Parsed groups */}
                {parsedLoading ? (
                  <Spin style={{ display: 'flex', justifyContent: 'center', padding: 24 }} />
                ) : parsedGroups.length === 0 ? (
                  <Card>
                    <Empty
                      description={
                        isFetching ? t('channels.fetching') : t('channels.noResources')
                      }
                    />
                  </Card>
                ) : (
                <Collapse
                  defaultActiveKey={parsedGroups.map((g) => g.id || g.title)}
                  items={parsedGroups.map((g) => ({
          key: g.id || g.title,
          label: (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%' }}>
              <div>
                <Space size={6}>
                  <Text strong>{g.title}</Text>
                  <Tag color={groupColor(g.type)} icon={groupIcon(g.type)}>
                    {g.type === 'series' ? t('dashboard.series') : t('dashboard.movie')}
                  </Tag>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {g.resources.length}{t('channels.resources')}
                  </Text>
                </Space>
              </div>
              <Checkbox
                style={{ marginLeft: 'auto' }}
                checked={g.resources.every((r) => selectedIds.has(r.id))}
                indeterminate={
                  g.resources.some((r) => selectedIds.has(r.id)) &&
                  !g.resources.every((r) => selectedIds.has(r.id))
                }
                onChange={(e) => toggleAllInGroup(g, e.target.checked)}
                onClick={(e) => e.stopPropagation()}
              >
                {t('common.selectAll')}
              </Checkbox>
              {g.id && (g.type === 'series' || g.type === 'movie') && (
                <Tooltip title={t('channels.openWorkDetail')}>
                  <Link
                    to={g.type === 'series' ? `/series/${g.id}` : `/movies/${g.id}`}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <Button type="text" size="small" icon={<ExternalLink size={14} />} />
                  </Link>
                </Tooltip>
              )}
            </div>
          ),
          children: (
            <div>
              <div className="resource-table-wrap">
              <table className="resource-table resource-table-known">
                <colgroup>
                  <col style={{ width: 40 }} />
                  <col style={{ width: 140 }} />
                  <col style={{ width: 84 }} />
                  <col style={{ width: 88 }} />
                  <col style={{ width: 84 }} />
                  <col style={{ width: 72 }} />
                  <col style={{ width: 150 }} />
                  <col />
                  <col style={{ width: 120 }} />
                  <col style={{ width: 76 }} />
                </colgroup>
                <thead>
                  <tr style={{ color: '#93939f', fontSize: 12 }}>
                    <th style={{ textAlign: 'left', padding: '6px 8px' }}></th>
                    <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('channels.episode')}</th>
                    <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('channels.resolution')}</th>
                    <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('channels.videoCodec')}</th>
                    <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('channels.audioCodec')}</th>
                    <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('channels.container')}</th>
                    <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('channels.subtitleLangs')}</th>
                    <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('channels.subtitleGroup')}</th>
                    <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('channels.publishedAt')}</th>
                    <th style={{ textAlign: 'right', padding: '6px 8px' }}></th>
                  </tr>
                </thead>
                <tbody>
                  {g.resources.map((r) => (
                    <tr
                      key={r.id}
                      style={{ borderTop: '1px solid var(--rr-border-soft)', cursor: 'pointer' }}
                      onClick={() => setSelectedResource(r)}
                      className="resource-row"
                    >
                      <td
                        className="resource-check-cell"
                        style={{ padding: '6px 8px' }}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <Checkbox
                          checked={selectedIds.has(r.id)}
                          onChange={(e) => toggleResource(r.id, e.target.checked)}
                        />
                      </td>
                      <td style={{ padding: '6px 8px', whiteSpace: 'nowrap' }} data-label={t('channels.episode')}>
                        {(() => {
                          const ep = formatEpisodeCell(r);
                          // Confidence indicators — only render when the
                          // metadata agent flagged the number, so the common
                          // "raw" case stays visually quiet.
                          const showReconciled = ep.confidence === 'reconciled';
                          const showAmbiguous = ep.confidence === 'ambiguous';
                          const showManual = ep.confidence === 'manual';
                          const reconciledTip = ep.absoluteEpisode != null
                            ? t('channels.episodeReconciledFrom', { n: ep.absoluteEpisode })
                            : t('channels.episodeReconciled');
                          return (
                            <Space size={4} style={{ flexWrap: 'nowrap' }}>
                              <span>{ep.label}</span>
                              {ep.batch && (
                                <Tag color="purple" style={{ marginRight: 0 }} icon={<Package size={10} />}>
                                  {t('channels.batch')}
                                </Tag>
                              )}
                              {showReconciled && (
                                <Tooltip title={reconciledTip}>
                                  <Tag color="blue" style={{ marginRight: 0 }}>
                                    {t('channels.episodeReconciledTag')}
                                  </Tag>
                                </Tooltip>
                              )}
                              {showAmbiguous && (
                                <Tooltip title={t('channels.episodeAmbiguousTip')}>
                                  <Tag color="warning" style={{ marginRight: 0 }}>
                                    {t('channels.episodeAmbiguousTag')}
                                  </Tag>
                                </Tooltip>
                              )}
                              {showManual && (
                                <Tag color="green" style={{ marginRight: 0 }}>
                                  {t('channels.episodeManualTag')}
                                </Tag>
                              )}
                            </Space>
                          );
                        })()}
                      </td>
                      <td style={{ padding: '6px 8px' }} data-label={t('channels.resolution')}>{r.resolution || '—'}</td>
                      <td style={{ padding: '6px 8px' }} data-label={t('channels.videoCodec')}>{r.video_codec || '—'}</td>
                      <td style={{ padding: '6px 8px' }} data-label={t('channels.audioCodec')}>{r.audio_codec || '—'}</td>
                      <td style={{ padding: '6px 8px' }} data-label={t('channels.container')}>{r.container || '—'}</td>
                      <td style={{ padding: '6px 8px' }} data-label={t('channels.subtitleLangs')}>
                        {(() => {
                          const langs = r.subtitle_langs || [];
                          if (langs.length === 0) return <span style={{ color: '#93939f' }}>—</span>;
                          const shown = langs.slice(0, 2);
                          const rest = langs.length - shown.length;
                          const inner = (
                            <Space size={2} style={{ flexWrap: 'nowrap' }}>
                              {shown.map((l) => (
                                <Tag key={l} style={{ margin: 0, fontSize: 11, lineHeight: '18px' }}>
                                  {l === 'multi' ? t('channels.langMulti') : l}
                                </Tag>
                              ))}
                              {rest > 0 && (
                                <Tag style={{ margin: 0, fontSize: 11, lineHeight: '18px' }}>+{rest}</Tag>
                              )}
                            </Space>
                          );
                          return langs.length > shown.length ? (
                            <Tooltip
                              title={
                                <span>
                                  {langs.map((l) => (l === 'multi' ? t('channels.langMulti') : l)).join(', ')}
                                </span>
                              }
                            >
                              {inner}
                            </Tooltip>
                          ) : inner;
                        })()}
                      </td>
                      <td className="resource-text-cell" style={{ padding: '6px 8px' }} data-label={t('channels.subtitleGroup')}>
                        <Text ellipsis style={{ display: 'block' }}>
                          {r.subtitle_group || '—'}
                        </Text>
                      </td>
                      <td style={{ padding: '6px 8px', color: '#93939f' }} data-label={t('channels.publishedAt')}>
                        {r.published_at ? timeAgo(r.published_at) : '—'}
                      </td>
                      <td
                        style={{ padding: '6px 8px', textAlign: 'right', whiteSpace: 'nowrap' }}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <Space size={2}>
                          <Tooltip
                            title={<span className="raw-title-tooltip-content">{r.title_raw}</span>}
                            placement="topRight"
                            classNames={{ root: 'raw-title-tooltip' }}
                          >
                            <Button
                              type="text"
                              size="small"
                              icon={<Info size={14} />}
                              aria-label={t('channels.showRawTitle')}
                            />
                          </Tooltip>
                          <Tooltip title={t('channels.copyRawTitle')}>
                            <Button
                              type="text"
                              size="small"
                              icon={<Copy size={14} />}
                              aria-label={t('channels.copyRawTitle')}
                              onClick={() => copyRawTitle(r.title_raw)}
                            />
                          </Tooltip>
                        </Space>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            </div>
          ),
        }))}
      />
                )}

      {/* Parsed pagination */}
      {parsedTotal > PAGE_SIZE && (
        <Space style={{ marginTop: 16, justifyContent: 'flex-end', width: '100%' }}>
          <Button size="small" disabled={parsedPage <= 1} onClick={() => setParsedPage(parsedPage - 1)}>
            {t('common.previous')}
          </Button>
          <Text style={{ fontSize: 12 }}>
            {parsedPage} / {parsedTotalPages}
          </Text>
          <Button size="small" disabled={parsedPage >= parsedTotalPages} onClick={() => setParsedPage(parsedPage + 1)}>
            {t('common.next')}
          </Button>
        </Space>
      )}
              </>
            ),
          },
          {
            key: 'unparsed',
            label: (
              <Space size={6}>
                {t('channels.unparsedResources')}
                {unparsedTotal > 0 && <Tag>{unparsedTotal}</Tag>}
              </Space>
            ),
            children: (
              <>
                {unparsedLoading ? (
                  <Spin style={{ display: 'flex', justifyContent: 'center', padding: 24 }} />
                ) : unparsed.length === 0 ? (
                  <Card>
                    <Empty description={t('channels.noResources')} />
                  </Card>
                ) : (
                  <Card
                    size="small"
                    title={
                      <Space>
                        <HelpCircle size={14} />
                        <span>{t('channels.unidentifiedResources')}</span>
                        <Tag>{unparsedTotal}</Tag>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {t('channels.clickToCorrect')}
                        </Text>
                      </Space>
                    }
                    styles={{ body: { padding: 0 } }}
                  >
                    <div className="resource-table-wrap">
                      <table className="resource-table resource-table-unknown">
                        <colgroup>
                          <col style={{ width: 40 }} />
                          <col />
                          <col style={{ width: 84 }} />
                          <col style={{ width: 180 }} />
                        </colgroup>
                        <thead>
                          <tr style={{ color: '#93939f', fontSize: 12 }}>
                            <th style={{ textAlign: 'left', padding: '6px 8px' }}></th>
                            <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('channels.rawTitle')}</th>
                            <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('channels.resolution')}</th>
                            <th style={{ textAlign: 'left', padding: '6px 8px' }}>{t('channels.subtitleGroup')}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {unparsed.map((r) => (
                            <tr
                              key={r.id}
                              style={{ borderTop: '1px solid var(--rr-border-soft)', cursor: 'pointer' }}
                              onClick={() => setSelectedResource(r)}
                              className="resource-row"
                            >
                              <td className="resource-check-cell" style={{ padding: '6px 8px' }} onClick={(e) => e.stopPropagation()}>
                                <Checkbox
                                  checked={selectedIds.has(r.id)}
                                  onChange={(e) => toggleResource(r.id, e.target.checked)}
                                />
                              </td>
                              <td className="resource-title-cell" style={{ padding: '6px 8px' }} data-label={t('channels.rawTitle')}>
                                <Text ellipsis style={{ display: 'block' }}>
                                  {r.title_raw}
                                </Text>
                              </td>
                              <td style={{ padding: '6px 8px' }} data-label={t('channels.resolution')}>{r.resolution || '-'}</td>
                              <td className="resource-text-cell" style={{ padding: '6px 8px' }} data-label={t('channels.subtitleGroup')}>
                                <Text ellipsis style={{ display: 'block' }}>
                                  {r.subtitle_group || '-'}
                                </Text>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </Card>
                )}
                {unparsedTotal > PAGE_SIZE && (
                  <Space style={{ marginTop: 16, justifyContent: 'flex-end', width: '100%' }}>
                    <Button size="small" disabled={unparsedPage <= 1} onClick={() => setUnparsedPage(unparsedPage - 1)}>
                      {t('common.previous')}
                    </Button>
                    <Text style={{ fontSize: 12 }}>
                      {unparsedPage} / {unparsedTotalPages}
                    </Text>
                    <Button size="small" disabled={unparsedPage >= unparsedTotalPages} onClick={() => setUnparsedPage(unparsedPage + 1)}>
                      {t('common.next')}
                    </Button>
                  </Space>
                )}
              </>
            ),
          },
        ]}
      />

      <ResourceDetailDrawer
        resource={selectedResource}
        onClose={() => setSelectedResource(null)}
        onCorrected={() => {
          reloadActiveTab();
          loadChannel();
        }}
      />

      {id && (
        <FilterSummaryModal
          open={filterModalOpen}
          channelId={id}
          selectedIds={Array.from(selectedIds)}
          onClose={() => setFilterModalOpen(false)}
          onAgentCreated={() => {
            setFilterModalOpen(false);
            setSelectedIds(new Set());
          }}
        />
      )}
    </div>
  );
}
