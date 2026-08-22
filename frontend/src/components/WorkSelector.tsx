import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Alert,
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
import { Film, Tv, Headphones } from 'lucide-react';
import { Link } from 'react-router-dom';
import { seriesApi } from '../api/series';
import { moviesApi } from '../api/movies';
import { audioWorksApi } from '../api/audioWorks';
import Pagination from './Pagination';
import FilterBuilder from './FilterBuilder';
import {
  collectFieldConditions,
  describeCondition,
  isFilterEmpty,
} from './filterUtils';
import type { AgentWork, AudioWork, BoolCondition, FilterField, Movie, TVSeries } from '../types';
import type { TFunction } from 'i18next';

const { Text } = Typography;

type WorkTab = 'tv' | 'movie' | 'audio';

const MODAL_PAGE_SIZE = 10;

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
  /** Agent-level filter_config — shown read-only so users see the effective
   * filter = global AND work override. */
  globalFilter?: BoolCondition | null;
  /** Channel required-fields gate for the per-work override FilterBuilder. */
  allowedFields?: FilterField[] | null;
}

function resolvePoster(work: AgentWork): string | null {
  if (work.series?.poster_url) return work.series.poster_url;
  if (work.movie?.poster_url) return work.movie.poster_url;
  return null;
}

function resolveTitle(work: AgentWork, t: TFunction): string {
  if (work.display_name_override) return work.display_name_override;
  if (work.series) return work.series.title_cn || work.series.title_en || work.series.original_title || t('common.unknown');
  if (work.movie) return work.movie.title_cn || work.movie.title_en || work.movie.original_title || t('common.unknown');
  return t('common.unknown');
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
  channelId,
  globalFilter,
  allowedFields,
}: WorkSelectorProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [modalOpen, setModalOpen] = useState(false);
  const [tab, setTab] = useState<WorkTab>('tv');
  const [search, setSearch] = useState('');
  const [seriesList, setSeriesList] = useState<TVSeries[]>([]);
  const [movieList, setMovieList] = useState<Movie[]>([]);
  const [audioList, setAudioList] = useState<AudioWork[]>([]);
  const [pages, setPages] = useState<Record<WorkTab, number>>({ tv: 1, movie: 1, audio: 1 });
  const [totals, setTotals] = useState<Record<WorkTab, number>>({ tv: 0, movie: 0, audio: 0 });
  const [loading, setLoading] = useState(false);

  const existingIds = useMemo(() => {
    const s = new Set<string>();
    works.forEach((w) => {
      if (w.series_id) s.add(`series:${w.series_id}`);
      if (w.movie_id) s.add(`movie:${w.movie_id}`);
    });
    return s;
  }, [works]);

  const fetchTab = async (which: WorkTab, page: number, q: string) => {
    setLoading(true);
    try {
      // Empty query = latest rows (API default sort is created_at desc), so
      // the user gets a useful default view instead of an empty modal.
      const term = q.trim() || undefined;
      if (which === 'tv') {
        const r = await seriesApi.list(page, MODAL_PAGE_SIZE, term);
        if (r.success) {
          setSeriesList(r.data);
          setTotals((prev) => ({ ...prev, tv: r.meta?.total ?? 0 }));
        }
      } else if (which === 'movie') {
        const r = await moviesApi.list(page, MODAL_PAGE_SIZE, term);
        if (r.success) {
          setMovieList(r.data);
          setTotals((prev) => ({ ...prev, movie: r.meta?.total ?? 0 }));
        }
      } else {
        const r = await audioWorksApi.list(page, MODAL_PAGE_SIZE, term);
        if (r.success) {
          setAudioList(r.data);
          setTotals((prev) => ({ ...prev, audio: r.meta?.total ?? 0 }));
        }
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!modalOpen) return;
    // Tab / page / first-open changes fetch immediately; typed queries go
    // through a 300ms debounce to avoid hammering the API per keystroke.
    const delay = search.trim() ? 300 : 0;
    const timeout = setTimeout(() => {
      fetchTab(tab, pages[tab], search);
    }, delay);
    return () => clearTimeout(timeout);
  }, [search, modalOpen, tab, pages]);

  const addWork = (type: 'tv' | 'movie', item: TVSeries | Movie) => {
    if (works.length >= maxWorks) {
      message.warning(t('work.maxHint', { max: maxWorks }));
      return;
    }
    const key = `${type}:${item.id}`;
    if (existingIds.has(key)) {
      message.info(t('work.alreadySubscribed'));
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
    message.success(t('work.added', { type: t(type === 'tv' ? 'work.series' : 'work.movie') }));
  };

  const removeWork = (id: string) => {
    onChange(works.filter((w) => w.id !== id));
  };

  const updateWork = (id: string, patch: Partial<AgentWork>) => {
    onChange(works.map((w) => (w.id === id ? { ...w, ...patch } : w)));
  };

  const renderPagination = (which: WorkTab) => {
    const total = totals[which];
    if (total <= MODAL_PAGE_SIZE) return null;
    return (
      <div style={{ display: 'flex', justifyContent: 'center', marginTop: 12 }}>
        <Pagination
          page={pages[which]}
          pageSize={MODAL_PAGE_SIZE}
          total={total}
          onPageChange={(p) => setPages((prev) => ({ ...prev, [which]: p }))}
        />
      </div>
    );
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
      // Latest-N default view; if the initial fetch hasn't populated
      // anything (empty repo), show the neutral placeholder.
      if (items.length === 0) {
        return <Empty description={t('work.searchPlaceholder')} />;
      }
    } else if (items.length === 0) {
      return <Empty description={t('work.noResults')} />;
    }
    return (
      <>
        <div style={{ maxHeight: 440, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 8 }}>
          {items.map((item) => {
            const already = existingIds.has(`${type}:${item.id}`);
            const title =
              item.title_cn || item.title_en || item.original_title || t('common.unknown');
            const sub =
              item.title_en && item.title_en !== item.title_cn ? item.title_en : item.original_title;
            return (
              <div
                key={item.id}
                style={{
                  display: 'flex',
                  gap: 10,
                  padding: 10,
                  border: '1px solid var(--rr-border-soft)',
                  borderRadius: 8,
                  background: already ? 'var(--rr-success-soft)' : 'transparent',
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
                      background: 'var(--rr-surface-card)',
                    }}
                    onError={(e) => ((e.target as HTMLImageElement).style.display = 'none')}
                  />
                ) : (
                  <div
                    style={{
                      width: 40,
                      height: 60,
                      borderRadius: 4,
                      background: 'var(--rr-surface-card)',
                      flexShrink: 0,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      color: 'var(--rr-text-muted)',
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
                  htmlType="button"
                  type="primary"
                  size="small"
                  disabled={already}
                  onClick={() => addWork(type, item)}
                >
                  {already ? t('work.added_btn') : t('work.add_btn')}
                </Button>
              </div>
            );
          })}
        </div>
        {renderPagination(type)}
      </>
    );
  };

  // Audio works are browse-only here: AgentWork only supports series/movie
  // subscriptions (backend constraint), so the audio tab shows the catalog
  // with a pointer to channel-wide mode instead of an "add" button.
  const renderAudioResult = () => {
    if (loading) {
      return (
        <div style={{ textAlign: 'center', padding: 32 }}>
          <Spin />
        </div>
      );
    }
    return (
      <>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={t('work.audioUnsupported')}
        />
        {audioList.length === 0 ? (
          <Empty
            description={search.trim() ? t('work.noResults') : t('work.searchPlaceholder')}
          />
        ) : (
          <div style={{ maxHeight: 440, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 8 }}>
            {audioList.map((item) => {
              const title =
                item.title_cn || item.title_en || item.original_title || t('common.unknown');
              const sub =
                item.title_en && item.title_en !== item.title_cn ? item.title_en : item.original_title;
              return (
                <div
                  key={item.id}
                  style={{
                    display: 'flex',
                    gap: 10,
                    padding: 10,
                    border: '1px solid var(--rr-border-soft)',
                    borderRadius: 8,
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
                        background: 'var(--rr-surface-card)',
                      }}
                      onError={(e) => ((e.target as HTMLImageElement).style.display = 'none')}
                    />
                  ) : (
                    <div
                      style={{
                        width: 40,
                        height: 60,
                        borderRadius: 4,
                        background: 'var(--rr-surface-card)',
                        flexShrink: 0,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        color: 'var(--rr-text-muted)',
                      }}
                    >
                      <Headphones />
                    </div>
                  )}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <Text strong style={{ fontSize: 13 }}>{title}</Text>
                    {sub && sub !== title && (
                      <Text type="secondary" style={{ fontSize: 11, display: 'block' }}>{sub}</Text>
                    )}
                    <Space size={4} style={{ marginTop: 4 }}>
                      {item.content_type && (
                        <Tag color="purple" style={{ fontSize: 10 }}>
                          {t(`works.audioType.${item.content_type}`, String(item.content_type))}
                        </Tag>
                      )}
                      {item.rating != null && (
                        <Text type="warning" style={{ fontSize: 11 }}>★ {item.rating}</Text>
                      )}
                      {item.status && (
                        <Tag style={{ fontSize: 10 }}>{item.status}</Tag>
                      )}
                    </Space>
                  </div>
                  <Link to={`/audio-works/${item.id}`}>
                    <Button htmlType="button" size="small">
                      {t('work.viewDetail')}
                    </Button>
                  </Link>
                </div>
              );
            })}
          </div>
        )}
        {renderPagination('audio')}
      </>
    );
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <Space direction="vertical" size={0}>
          <Text strong>{t('work.subtitle', { n: works.length, max: maxWorks })}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t('work.selectorDesc')}
          </Text>
        </Space>
        <Button
          htmlType="button"
          type="primary"
          icon={<PlusOutlined />}
          disabled={works.length >= maxWorks}
          onClick={() => {
            setModalOpen(true);
            setTab('tv');
            setSearch('');
            setSeriesList([]);
            setMovieList([]);
            setAudioList([]);
            setPages({ tv: 1, movie: 1, audio: 1 });
            setTotals({ tv: 0, movie: 0, audio: 0 });
          }}
        >
          {t('work.addWork')}
        </Button>
      </div>

      {/* Suggestions from unrecognized resources */}
      {suggestions.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
            {t('work.suggestions')}
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
            border: '1px dashed var(--rr-border)',
            borderRadius: 8,
            textAlign: 'center',
          }}
        >
          <Text type="secondary" style={{ fontSize: 13 }}>
            {t('work.noWorks')}
          </Text>
        </div>
      ) : (
        <Space direction="vertical" style={{ width: '100%' }} size={10}>
          {works.map((work) => {
            const poster = resolvePoster(work);
            const title = resolveTitle(work, t);
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
                        background: 'var(--rr-surface-card)',
                        flexShrink: 0,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        color: 'var(--rr-text-muted)',
                      }}
                    >
                      {isTv ? <Tv /> : <Film />}
                    </div>
                  )}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                      <Link
                        to={isTv ? `/series/${work.series_id}` : `/movies/${work.movie_id}`}
                        style={{ minWidth: 0 }}
                      >
                        <Text strong style={{ fontSize: 13 }} ellipsis>
                          {title}
                        </Text>
                      </Link>
                      <Tag color={isTv ? 'blue' : 'green'}>
                        {t(isTv ? 'work.series' : 'work.movie')}
                      </Tag>
                      {isTv && work.latest_completed_episode != null && (
                        <Tag color="cyan">
                          {t('work.downloadedTo', {
                            pos:
                              work.latest_completed_season != null
                                ? `S${String(work.latest_completed_season).padStart(2, '0')}E${String(work.latest_completed_episode).padStart(2, '0')}`
                                : `E${String(work.latest_completed_episode).padStart(2, '0')}`,
                          })}
                        </Tag>
                      )}
                      <Button
                        htmlType="button"
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
                              {t('work.settingsPrefix')}{work.filter_overrides ? t('work.hasOverride') : ''}
                            </Text>
                          ),
                          children: (
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, paddingTop: 8 }}>
                              <div>
                                <Text style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                                  {t('work.customName')}
                                </Text>
                                <Input
                                  size="small"
                                  value={work.display_name_override || ''}
                                  placeholder={t('work.customNameHint')}
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
                                  <Text style={{ fontSize: 12 }}>{t('work.episodeDedup')}</Text>
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
                                  {t('work.workFilter')}
                                </Text>
                                {globalFilter && !isFilterEmpty(globalFilter) && (
                                  <div style={{ marginBottom: 6 }}>
                                    <Text type="secondary" style={{ fontSize: 12, marginRight: 6 }}>
                                      {t('work.mergedWithGlobal')}
                                    </Text>
                                    {collectFieldConditions(globalFilter).map((c, i) => (
                                      <Tag key={i} style={{ fontSize: 11, margin: 2 }}>
                                        {describeCondition(c, t)}
                                      </Tag>
                                    ))}
                                  </div>
                                )}
                                <FilterBuilder
                                  value={work.filter_overrides}
                                  compact
                                  channelId={channelId}
                                  allowedFields={allowedFields}
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
        title={t('work.addWorkModal')}
        footer={null}
        width={920}
        destroyOnClose
      >
        <Input
          placeholder={t('work.searchWorks')}
          prefix={<SearchOutlined />}
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            // New query always starts from the first page in every tab.
            setPages({ tv: 1, movie: 1, audio: 1 });
          }}
          style={{ marginBottom: 12 }}
          autoFocus
          allowClear
        />
        <Tabs
          activeKey={tab}
          onChange={(k) => setTab(k as WorkTab)}
          items={[
            {
              key: 'tv',
              label: t('work.series'),
              children: renderSearchResult(seriesList, 'tv'),
            },
            {
              key: 'movie',
              label: t('work.movie'),
              children: renderSearchResult(movieList, 'movie'),
            },
            {
              key: 'audio',
              label: t('work.audio'),
              children: renderAudioResult(),
            },
          ]}
        />
      </Modal>
    </div>
  );
}
