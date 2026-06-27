import { useState, useEffect, useRef, useCallback } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
  Pencil,
  RefreshCw,
  Wand2,
  Film,
  Tv,
  HelpCircle,
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

function posterFor(group: GroupedResource) {
  if (group.poster_url) return group.poster_url;
  const ch = (group.title || '?').charAt(0).toUpperCase();
  return `data:image/svg+xml;utf8,${encodeURIComponent(
    `<svg xmlns='http://www.w3.org/2000/svg' width='60' height='90' viewBox='0 0 60 90'><rect width='60' height='90' fill='%231a1a1a'/><text x='30' y='50' text-anchor='middle' fill='%236a6b6c' font-family='sans-serif' font-size='22'>${ch}</text></svg>`,
  )}`;
}

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

const PAGE_SIZE = 30;

export default function ChannelDetail() {
  const { id } = useParams<{ id: string }>();
  const { message } = App.useApp();
  const navigate = useNavigate();

  const [channel, setChannel] = useState<ChannelDetailData | null>(null);
  const [groups, setGroups] = useState<GroupedResource[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [selectedResource, setSelectedResource] = useState<FileResource | null>(null);
  const [fetchStatus, setFetchStatus] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [filterModalOpen, setFilterModalOpen] = useState(false);

  const loadChannel = useCallback(async () => {
    if (!id) return;
    const r = await channelsApi.get(id);
    if (r.success) setChannel(r.data);
  }, [id]);

  const loadResources = useCallback(async () => {
    if (!id) return;
    const r = await channelsApi.resources(id, page, PAGE_SIZE, true);
    if (r.success) {
      // Backend returns grouped array; meta.total is total resources count
      const data = r.data as GroupedResource[];
      setGroups(data);
      if (r.meta) setTotal(r.meta.total);
    }
    setLoading(false);
  }, [id, page]);

  useEffect(() => {
    setLoading(true);
    loadChannel();
    loadResources();
  }, [loadChannel, loadResources]);

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
      loadResources();
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
  }, [isFetching, id, loadResources, loadChannel]);

  const handleFetch = async () => {
    if (!id || isFetching) return;
    setFetchStatus('queued');
    const r = await channelsApi.fetch(id);
    if (!r.success) {
      setFetchStatus(null);
      message.error(r.error?.message || '抓取触发失败');
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

  const totalPages = Math.ceil(total / PAGE_SIZE);

  if (loading) {
    return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  }
  if (!channel) return <Text type="danger">频道未找到</Text>;

  // Separate unknown group
  const knownGroups = groups.filter((g) => g.type !== 'unknown');
  const unknownGroup = groups.find((g) => g.type === 'unknown');

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
              {channel.metadata_source === 'llm' && <Tag color="blue">LLM</Tag>}
            </Space>
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
              {channel.url}
            </Text>
            <Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
              上次抓取：{channel.last_fetched_at ? timeAgo(channel.last_fetched_at) : '从未'}
              {channel.last_fetch_error && (
                <span style={{ color: '#ff6161', marginLeft: 8 }}>
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
            {isFetching ? '抓取中...' : '手动抓取'}
          </Button>
          <Button icon={<Pencil size={14} />} onClick={() => navigate(`/channels/${id}/edit`)}>
            编辑
          </Button>
        </Space>
      </Space>

      {/* Info cards */}
      <Row gutter={[12, 12]} style={{ marginBottom: 24 }}>
        <Col xs={12} sm={6}>
          <Card size="small">
            <div style={{ fontSize: 12, color: '#9c9c9d' }}>抓取间隔</div>
            <div style={{ fontWeight: 500 }}>{Math.round(channel.fetch_interval / 60)} 分钟</div>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card size="small">
            <div style={{ fontSize: 12, color: '#9c9c9d' }}>资源总数</div>
            <div style={{ fontWeight: 500 }}>{total}</div>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card size="small">
            <div style={{ fontSize: 12, color: '#9c9c9d' }}>作品分组</div>
            <div style={{ fontWeight: 500 }}>{knownGroups.length}</div>
          </Card>
        </Col>
      </Row>

      {/* Selection bar */}
      {selectedIds.size > 0 && (
        <Card
          size="small"
          style={{
            marginBottom: 16,
            borderColor: 'rgba(89,212,153,0.4)',
            background: 'rgba(89,212,153,0.05)',
          }}
        >
          <Space style={{ width: '100%', justifyContent: 'space-between' }}>
            <Text style={{ color: '#59d499' }}>已选 {selectedIds.size} 个资源</Text>
            <Space>
              <Button size="small" onClick={() => setSelectedIds(new Set())}>
                取消选择
              </Button>
              <Button
                size="small"
                type="primary"
                icon={<Wand2 size={12} />}
                onClick={() => setFilterModalOpen(true)}
              >
                生成过滤规则
              </Button>
            </Space>
          </Space>
        </Card>
      )}

      {/* Known groups */}
      {knownGroups.length === 0 && !unknownGroup && (
        <Card>
          <Empty
            description={
              isFetching ? '抓取中...' : '还没有资源，点击"手动抓取"从 RSS 拉取'
            }
          />
        </Card>
      )}

      <Collapse
        defaultActiveKey={knownGroups.map((g) => g.id || g.title)}
        items={knownGroups.map((g) => ({
          key: g.id || g.title,
          label: (
            <Space>
              <img
                src={posterFor(g)}
                alt=""
                style={{ width: 32, height: 48, objectFit: 'cover', borderRadius: 4, flexShrink: 0 }}
                onError={(e) => ((e.target as HTMLImageElement).src = posterFor(g))}
              />
              <div>
                <Space size={6}>
                  <Text strong>{g.title}</Text>
                  <Tag color={groupColor(g.type)} icon={groupIcon(g.type)}>
                    {g.type === 'series' ? '剧集' : '电影'}
                  </Tag>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {g.resources.length} 个资源
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
                全选
              </Checkbox>
            </Space>
          ),
          children: (
            <div>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                <thead>
                  <tr style={{ color: '#9c9c9d', fontSize: 12 }}>
                    <th style={{ textAlign: 'left', padding: '6px 8px', width: 40 }}></th>
                    <th style={{ textAlign: 'left', padding: '6px 8px' }}>标题</th>
                    <th style={{ textAlign: 'left', padding: '6px 8px', width: 70 }}>集</th>
                    <th style={{ textAlign: 'left', padding: '6px 8px', width: 80 }}>分辨率</th>
                    <th style={{ textAlign: 'left', padding: '6px 8px', width: 100 }}>字幕组</th>
                    <th style={{ textAlign: 'left', padding: '6px 8px', width: 120 }}>发布时间</th>
                  </tr>
                </thead>
                <tbody>
                  {g.resources.map((r) => (
                    <tr
                      key={r.id}
                      style={{ borderTop: '1px solid rgba(255,255,255,0.04)', cursor: 'pointer' }}
                      onClick={() => setSelectedResource(r)}
                      className="resource-row"
                    >
                      <td
                        style={{ padding: '6px 8px' }}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <Checkbox
                          checked={selectedIds.has(r.id)}
                          onChange={(e) => toggleResource(r.id, e.target.checked)}
                        />
                      </td>
                      <td style={{ padding: '6px 8px' }}>
                        <Text ellipsis style={{ display: 'block' }}>
                          {r.title_cn || r.title_en || r.title_raw}
                        </Text>
                      </td>
                      <td style={{ padding: '6px 8px' }}>
                        {r.episode != null
                          ? r.season != null
                            ? `S${r.season}E${r.episode}`
                            : `E${r.episode}`
                          : '—'}
                      </td>
                      <td style={{ padding: '6px 8px' }}>{r.resolution || '—'}</td>
                      <td style={{ padding: '6px 8px' }}>{r.subtitle_group || '—'}</td>
                      <td style={{ padding: '6px 8px', color: '#9c9c9d' }}>
                        {r.published_at ? timeAgo(r.published_at) : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ),
        }))}
      />

      {/* Unknown group */}
      {unknownGroup && unknownGroup.resources.length > 0 && (
        <Card
          size="small"
          title={
            <Space>
              <HelpCircle size={14} />
              <span>未识别资源</span>
              <Tag>{unknownGroup.resources.length}</Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>
                点击资源可手动修正元数据
              </Text>
            </Space>
          }
          style={{ marginTop: 16 }}
          styles={{ body: { padding: 0 } }}
        >
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ color: '#9c9c9d', fontSize: 12 }}>
                <th style={{ textAlign: 'left', padding: '6px 8px', width: 40 }}></th>
                <th style={{ textAlign: 'left', padding: '6px 8px' }}>原始标题</th>
                <th style={{ textAlign: 'left', padding: '6px 8px', width: 80 }}>分辨率</th>
                <th style={{ textAlign: 'left', padding: '6px 8px', width: 100 }}>字幕组</th>
              </tr>
            </thead>
            <tbody>
              {unknownGroup.resources.map((r) => (
                <tr
                  key={r.id}
                  style={{ borderTop: '1px solid rgba(255,255,255,0.04)', cursor: 'pointer' }}
                  onClick={() => setSelectedResource(r)}
                  className="resource-row"
                >
                  <td style={{ padding: '6px 8px' }} onClick={(e) => e.stopPropagation()}>
                    <Checkbox
                      checked={selectedIds.has(r.id)}
                      onChange={(e) => toggleResource(r.id, e.target.checked)}
                    />
                  </td>
                  <td style={{ padding: '6px 8px' }}>
                    <Text ellipsis style={{ display: 'block' }}>
                      {r.title_raw}
                    </Text>
                  </td>
                  <td style={{ padding: '6px 8px' }}>{r.resolution || '—'}</td>
                  <td style={{ padding: '6px 8px' }}>{r.subtitle_group || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {/* Pagination */}
      {total > PAGE_SIZE && (
        <Space style={{ marginTop: 16, justifyContent: 'flex-end', width: '100%' }}>
          <Button size="small" disabled={page <= 1} onClick={() => setPage(page - 1)}>
            上一页
          </Button>
          <Text style={{ fontSize: 12 }}>
            {page} / {totalPages}
          </Text>
          <Button size="small" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>
            下一页
          </Button>
        </Space>
      )}

      <style>{`
        .resource-row:hover { background: rgba(255,255,255,0.03); }
      `}</style>

      <ResourceDetailDrawer
        resource={selectedResource}
        onClose={() => setSelectedResource(null)}
        onCorrected={() => {
          loadResources();
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
