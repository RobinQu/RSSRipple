import { useEffect, useMemo, useState } from 'react';
import {
  Button,
  Card,
  Input,
  Modal,
  Space,
  Switch,
  Tabs,
  Tag,
  Typography,
  Empty,
  Spin,
  App,
  Collapse,
} from 'antd';
import { PlusOutlined, DeleteOutlined, SearchOutlined } from '@ant-design/icons';
import { Film, Tv } from 'lucide-react';
import { seriesApi } from '../api/series';
import { moviesApi } from '../api/movies';
import FilterBuilder from './FilterBuilder';
import type { AgentWork, Movie, TVSeries } from '../types';

const { Text } = Typography;

interface SuggestionShortcut {
  sample_title: string;
  resources: string[];
}

interface WorkSelectorProps {
  channelId?: string;
  value: AgentWork[];
  onChange: (works: AgentWork[]) => void;
  maxWorks?: number;
  suggestions?: SuggestionShortcut[];
}

function resolvePoster(work: AgentWork): string | null {
  if (work.series?.poster_url) return work.series.poster_url;
  if (work.movie?.poster_url) return work.movie.poster_url;
  return null;
}

function resolveTitle(work: AgentWork): string {
  if (work.display_name_override) return work.display_name_override;
  if (work.series) return work.series.title_cn || work.series.title_en || work.series.original_title || '未知剧集';
  if (work.movie) return work.movie.title_cn || work.movie.title_en || work.movie.original_title || '未知电影';
  return '未知作品';
}

/** Temp id for newly added works before save */
let tmpIdCounter = 0;
function tmpId() {
  return `tmp_${++tmpIdCounter}_${Date.now()}`;
}

export default function WorkSelector({
  value: works,
  onChange,
  maxWorks = 10,
  suggestions = [],
}: WorkSelectorProps) {
  const { message } = App.useApp();
  const [modalOpen, setModalOpen] = useState(false);
  const [tab, setTab] = useState<'tv' | 'movie'>('tv');
  const [search, setSearch] = useState('');
  const [seriesList, setSeriesList] = useState<TVSeries[]>([]);
  const [movieList, setMovieList] = useState<Movie[]>([]);
  const [loading, setLoading] = useState(false);

  const existingIds = useMemo(() => {
    const s = new Set<string>();
    works.forEach((w) => {
      if (w.series_id) s.add(`series:${w.series_id}`);
      if (w.movie_id) s.add(`movie:${w.movie_id}`);
    });
    return s;
  }, [works]);

  const searchWorks = async (q: string) => {
    if (!q.trim()) {
      setSeriesList([]);
      setMovieList([]);
      return;
    }
    setLoading(true);
    try {
      const [sRes, mRes] = await Promise.all([
        seriesApi.list(1, 20, q.trim()),
        moviesApi.list(1, 20, q.trim()),
      ]);
      if (sRes.success) setSeriesList(sRes.data);
      if (mRes.success) setMovieList(mRes.data);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!modalOpen) return;
    const t = setTimeout(() => {
      if (search.trim()) searchWorks(search);
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, modalOpen]);

  const addWork = (type: 'tv' | 'movie', item: TVSeries | Movie) => {
    if (works.length >= maxWorks) {
      message.warning(`最多订阅 ${maxWorks} 个作品`);
      return;
    }
    const key = `${type}:${item.id}`;
    if (existingIds.has(key)) {
      message.info('该作品已在订阅列表中');
      return;
    }
    const newWork: AgentWork = {
      id: tmpId(),
      agent_id: '',
      content_type: type,
      series_id: type === 'tv' ? item.id : null,
      movie_id: type === 'movie' ? item.id : null,
      enable_episode_dedup: type === 'tv',
      filter_overrides: null,
      display_name_override: null,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      series: type === 'tv' ? (item as TVSeries) : undefined,
      movie: type === 'movie' ? (item as Movie) : undefined,
    };
    onChange([...works, newWork]);
    message.success(`已添加 ${type === 'tv' ? '剧集' : '电影'}`);
  };

  const removeWork = (id: string) => {
    onChange(works.filter((w) => w.id !== id));
  };

  const updateWork = (id: string, patch: Partial<AgentWork>) => {
    onChange(works.map((w) => (w.id === id ? { ...w, ...patch } : w)));
  };

  const renderSearchResult = (items: (TVSeries | Movie)[], type: 'tv' | 'movie') => {
    if (loading) {
      return (
        <div style={{ textAlign: 'center', padding: 32 }}>
          <Spin />
        </div>
      );
    }
    if (!search.trim()) {
      return (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="输入关键词搜索剧集/电影"
        />
      );
    }
    if (items.length === 0) {
      return <Empty description="未找到结果" />;
    }
    return (
      <div style={{ maxHeight: 400, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 8 }}>
        {items.map((item) => {
          const already = existingIds.has(`${type}:${item.id}`);
          const title =
            item.title_cn || item.title_en || item.original_title || '未命名';
          const sub =
            item.title_en && item.title_en !== item.title_cn ? item.title_en : item.original_title;
          return (
            <div
              key={item.id}
              style={{
                display: 'flex',
                gap: 10,
                padding: 10,
                border: '1px solid rgba(255,255,255,0.08)',
                borderRadius: 8,
                background: already ? 'rgba(89,212,153,0.05)' : 'transparent',
              }}
            >
              {item.poster_url ? (
                <img
                  src={item.poster_url}
                  alt=""
                  style={{
                    width: 40,
                    height: 60,
                    objectFit: 'cover',
                    borderRadius: 4,
                    flexShrink: 0,
                    background: '#1a1a1a',
                  }}
                  onError={(e) => ((e.target as HTMLImageElement).style.display = 'none')}
                />
              ) : (
                <div
                  style={{
                    width: 40,
                    height: 60,
                    borderRadius: 4,
                    background: '#1a1a1a',
                    flexShrink: 0,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    color: '#434345',
                  }}
                >
                  {type === 'tv' ? <Tv /> : <Film />}
                </div>
              )}
              <div style={{ flex: 1, minWidth: 0 }}>
                <Text strong style={{ fontSize: 13 }}>{title}</Text>
                {sub && sub !== title && (
                  <Text type="secondary" style={{ fontSize: 11, display: 'block' }}>{sub}</Text>
                )}
                <Space size={4} style={{ marginTop: 4 }}>
                  {item.rating != null && (
                    <Text type="warning" style={{ fontSize: 11 }}>★ {item.rating}</Text>
                  )}
                  {item.status && (
                    <Tag style={{ fontSize: 10 }}>{item.status}</Tag>
                  )}
                </Space>
              </div>
              <Button
                type="primary"
                size="small"
                disabled={already}
                onClick={() => addWork(type, item)}
              >
                {already ? '已添加' : '添加'}
              </Button>
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <Space direction="vertical" size={0}>
          <Text strong>订阅作品 ({works.length}/{maxWorks})</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            选择该 Agent 负责自动下载的作品
          </Text>
        </Space>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          disabled={works.length >= maxWorks}
          onClick={() => {
            setModalOpen(true);
            setSearch('');
            setSeriesList([]);
            setMovieList([]);
          }}
        >
          添加作品
        </Button>
      </div>

      {/* Suggestions from unrecognized resources */}
      {suggestions.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
            频道未识别资源建议：
          </Text>
          <Space wrap size={[8, 8]}>
            {suggestions.slice(0, 6).map((s, i) => (
              <Tag
                key={i}
                style={{ cursor: 'pointer', padding: '4px 8px' }}
                onClick={() => {
                  setSearch(s.sample_title);
                  setModalOpen(true);
                }}
              >
                {s.sample_title}
              </Tag>
            ))}
          </Space>
        </div>
      )}

      {works.length === 0 ? (
        <div
          style={{
            padding: 32,
            border: '1px dashed rgba(255,255,255,0.12)',
            borderRadius: 8,
            textAlign: 'center',
          }}
        >
          <Text type="secondary" style={{ fontSize: 13 }}>
            暂未订阅任何作品，点击上方"添加作品"开始
          </Text>
        </div>
      ) : (
        <Space direction="vertical" style={{ width: '100%' }} size={10}>
          {works.map((work) => {
            const poster = resolvePoster(work);
            const title = resolveTitle(work);
            const isTv = work.content_type === 'tv';
            return (
              <Card
                key={work.id}
                size="small"
                styles={{ body: { padding: 12 } }}
              >
                <div style={{ display: 'flex', gap: 12 }}>
                  {poster ? (
                    <img
                      src={poster}
                      alt=""
                      style={{
                        width: 48,
                        height: 72,
                        objectFit: 'cover',
                        borderRadius: 4,
                        flexShrink: 0,
                      }}
                      onError={(e) => ((e.target as HTMLImageElement).style.display = 'none')}
                    />
                  ) : (
                    <div
                      style={{
                        width: 48,
                        height: 72,
                        borderRadius: 4,
                        background: '#1a1a1a',
                        flexShrink: 0,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        color: '#434345',
                      }}
                    >
                      {isTv ? <Tv /> : <Film />}
                    </div>
                  )}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                      <Text strong style={{ fontSize: 13 }} ellipsis>
                        {title}
                      </Text>
                      <Tag color={isTv ? 'blue' : 'green'}>
                        {isTv ? '剧集' : '电影'}
                      </Tag>
                      <Button
                        type="text"
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={() => removeWork(work.id)}
                      />
                    </div>

                    <Collapse
                      size="small"
                      ghost
                      items={[
                        {
                          key: 'overrides',
                          label: (
                            <Text type="secondary" style={{ fontSize: 12 }}>
                              作品设置 {work.filter_overrides ? '(已设独立过滤)' : ''}
                            </Text>
                          ),
                          children: (
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, paddingTop: 8 }}>
                              <div>
                                <Text style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                                  自定义展示名
                                </Text>
                                <Input
                                  size="small"
                                  value={work.display_name_override || ''}
                                  placeholder="留空使用作品原名"
                                  onChange={(e) =>
                                    updateWork(work.id, {
                                      display_name_override: e.target.value || null,
                                    })
                                  }
                                />
                              </div>
                              {isTv && (
                                <div
                                  style={{
                                    display: 'flex',
                                    alignItems: 'center',
                                    justifyContent: 'space-between',
                                  }}
                                >
                                  <Text style={{ fontSize: 12 }}>按集数去重</Text>
                                  <Switch
                                    size="small"
                                    checked={work.enable_episode_dedup}
                                    onChange={(v) =>
                                      updateWork(work.id, { enable_episode_dedup: v })
                                    }
                                  />
                                </div>
                              )}
                              <div>
                                <Text style={{ fontSize: 12, display: 'block', marginBottom: 6 }}>
                                  作品级过滤条件（与全局过滤按 AND 合并）
                                </Text>
                                <FilterBuilder
                                  value={work.filter_overrides}
                                  compact
                                  onChange={(v) =>
                                    updateWork(work.id, { filter_overrides: v })
                                  }
                                />
                              </div>
                            </div>
                          ),
                        },
                      ]}
                    />
                  </div>
                </div>
              </Card>
            );
          })}
        </Space>
      )}

      <Modal
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        title="添加订阅作品"
        footer={null}
        width={640}
        destroyOnClose
      >
        <Input
          placeholder="搜索剧集或电影..."
          prefix={<SearchOutlined />}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ marginBottom: 12 }}
          autoFocus
          allowClear
        />
        <Tabs
          activeKey={tab}
          onChange={(k) => setTab(k as 'tv' | 'movie')}
          items={[
            {
              key: 'tv',
              label: '剧集',
              children: renderSearchResult(seriesList, 'tv'),
            },
            {
              key: 'movie',
              label: '电影',
              children: renderSearchResult(movieList, 'movie'),
            },
          ]}
        />
      </Modal>
    </div>
  );
}
