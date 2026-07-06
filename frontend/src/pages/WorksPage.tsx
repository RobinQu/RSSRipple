import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Library, Search, Settings2, RefreshCw, CheckCircle } from 'lucide-react';
import {
  Typography,
  Input,
  Spin,
  Empty,
  Tag,
  Segmented,
  Button,
  Space,
  App,
} from 'antd';
import { worksApi } from '../api/works';
import type { RefreshItem } from '../api/works';
import { posterUrl, useDefaultPoster } from '../utils/poster';
import type { Work } from '../types';
import MetadataConfigModal from '../components/MetadataConfigModal';

const { Title, Text } = Typography;

type ContentType = 'all' | 'tv' | 'movie';
const PAGE_SIZE = 20;

function getDisplayTitle(w: Work): string {
  return w.title_cn || w.title_en || w.original_title || '—';
}

function workKey(w: Work): string {
  return `${w.content_type}:${w.id}`;
}

export default function WorksPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { message } = App.useApp();

  const [works, setWorks] = useState<Work[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(true);
  const [search, setSearch] = useState('');
  const [contentType, setContentType] = useState<ContentType>('all');

  // Selection + batch refresh
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [batchRefreshing, setBatchRefreshing] = useState(false);

  // Configurator modal
  const [configOpen, setConfigOpen] = useState(false);

  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const loadingMoreRef = useRef(false);
  const hasMoreRef = useRef(true);

  useEffect(() => {
    loadingMoreRef.current = loadingMore;
  }, [loadingMore]);
  useEffect(() => {
    hasMoreRef.current = hasMore;
  }, [hasMore]);

  const fetchPage = useCallback(
    async (p: number, ct: ContentType, q: string, replace: boolean) => {
      if (replace) setLoading(true);
      else setLoadingMore(true);
      try {
        const ctParam = ct === 'all' ? undefined : ct;
        const r = await worksApi.list(p, PAGE_SIZE, q.trim() || undefined, ctParam);
        if (r.success) {
          setWorks((prev) => (replace ? r.data : [...prev, ...r.data]));
          const total = r.meta?.total ?? 0;
          setHasMore(r.data.length === PAGE_SIZE && p * PAGE_SIZE < total);
        }
      } finally {
        if (replace) setLoading(false);
        else setLoadingMore(false);
      }
    },
    [],
  );

  // Initial / filter-change load (page 1).
  useEffect(() => {
    const timeout = setTimeout(() => {
      fetchPage(1, contentType, search, true);
      setPage(1);
      setHasMore(true);
    }, 300);
    return () => clearTimeout(timeout);
  }, [contentType, search, fetchPage]);

  // Infinite scroll: load next page when the sentinel enters the viewport.
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && hasMoreRef.current && !loadingMoreRef.current) {
          setPage((prev) => {
            const next = prev + 1;
            fetchPage(next, contentType, search, false);
            return next;
          });
        }
      },
      { rootMargin: '400px' },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [contentType, search, fetchPage]);

  const handleCardClick = (w: Work) => {
    if (selectMode) {
      const key = workKey(w);
      setSelected((prev) => {
        const next = new Set(prev);
        if (next.has(key)) next.delete(key);
        else next.add(key);
        return next;
      });
      return;
    }
    if (w.content_type === 'movie') navigate(`/movies/${w.id}`);
    else navigate(`/series/${w.id}`);
  };

  const toggleSelectAll = () => {
    setSelected((prev) => {
      if (prev.size >= works.length) return new Set();
      return new Set(works.map(workKey));
    });
  };

  const selectedItems: RefreshItem[] = useMemo(() => {
    const set = selected;
    return works
      .filter((w) => set.has(workKey(w)) && (w.content_type === 'tv' || w.content_type === 'movie'))
      .map((w) => ({ id: w.id, content_type: w.content_type as 'tv' | 'movie' }));
  }, [selected, works]);

  const handleBatchRefresh = async () => {
    if (selectedItems.length === 0) return;
    setBatchRefreshing(true);
    try {
      const r = await worksApi.batchRefreshMetadata(selectedItems);
      if (r.success) {
        message.success(t('works.batchRefreshStarted', { n: r.data.count }));
        setSelectMode(false);
        setSelected(new Set());
        // Reload the first page so refreshed posters/titles appear.
        fetchPage(1, contentType, search, true);
        setPage(1);
      } else {
        message.error(r.error?.message || t('works.batchRefreshFailed'));
      }
    } finally {
      setBatchRefreshing(false);
    }
  };

  const segmentedOptions = useMemo(
    () => [
      { label: t('works.all'), value: 'all' },
      { label: t('works.tvSeries'), value: 'tv' },
      { label: t('works.movies'), value: 'movie' },
    ],
    [t],
  );

  return (
    <div>
      {/* Header */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 16,
          flexWrap: 'wrap',
          gap: 12,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <Library size={22} style={{ color: '#1863dc' }} />
          <Title level={3} style={{ margin: 0 }}>
            {t('works.title')}
          </Title>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <Segmented
            options={segmentedOptions}
            value={contentType}
            onChange={(v) => {
              setContentType(v as ContentType);
              setSelected(new Set());
            }}
          />
          <Input
            prefix={<Search size={14} style={{ color: '#93939f' }} />}
            placeholder={t('works.searchPlaceholder')}
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setSelected(new Set());
            }}
            style={{ width: 220 }}
            allowClear
          />
          <Button icon={<Settings2 size={14} />} onClick={() => setConfigOpen(true)}>
            {t('works.configButton')}
          </Button>
          <Button
            type={selectMode ? 'primary' : 'default'}
            icon={<CheckCircle size={14} />}
            onClick={() => {
              setSelectMode((v) => !v);
              setSelected(new Set());
            }}
          >
            {t('works.select')}
          </Button>
        </div>
      </div>

      {/* Selection action bar */}
      {selectMode && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 12,
            marginBottom: 16,
            padding: '8px 12px',
            background: '#1f1f24',
            border: '1px solid #2a2a2a',
            borderRadius: 8,
            flexWrap: 'wrap',
          }}
        >
          <Space>
            <Text style={{ color: '#d9d9dd' }}>
              {t('works.selectedCount', { n: selected.size })}
            </Text>
            <Button size="small" type="link" onClick={toggleSelectAll}>
              {selected.size >= works.length ? t('common.deselect') : t('common.selectAll')}
            </Button>
          </Space>
          <Space>
            <Button
              size="small"
              type="primary"
              icon={<RefreshCw size={13} />}
              loading={batchRefreshing}
              disabled={selectedItems.length === 0}
              onClick={handleBatchRefresh}
            >
              {t('works.batchRefresh')}
            </Button>
          </Space>
        </div>
      )}

      {/* Content */}
      {loading && works.length === 0 ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 80 }}>
          <Spin size="large" />
        </div>
      ) : works.length === 0 ? (
        <Empty
          description={t('works.noResults')}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          style={{ marginTop: 80 }}
        />
      ) : (
        <>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))',
              gap: 20,
              marginBottom: 32,
            }}
          >
            {works.map((w) => {
              const displayTitle = getDisplayTitle(w);
              const key = workKey(w);
              const isSelected = selected.has(key);
              return (
                <div
                  key={key}
                  role="button"
                  tabIndex={0}
                  onClick={() => handleCardClick(w)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') handleCardClick(w);
                  }}
                  style={{
                    cursor: 'pointer',
                    borderRadius: 10,
                    overflow: 'hidden',
                    border: isSelected ? '2px solid #1863dc' : '1px solid #2a2a2a',
                    background: '#1a1a1a',
                    transition: 'border-color 0.2s, transform 0.2s, box-shadow 0.2s',
                    position: 'relative',
                  }}
                  onMouseEnter={(e) => {
                    if (!isSelected) e.currentTarget.style.borderColor = '#1863dc';
                    e.currentTarget.style.transform = 'translateY(-2px)';
                    e.currentTarget.style.boxShadow = '0 8px 24px rgba(0,0,0,0.4)';
                  }}
                  onMouseLeave={(e) => {
                    if (!isSelected) e.currentTarget.style.borderColor = '#2a2a2a';
                    e.currentTarget.style.transform = 'translateY(0)';
                    e.currentTarget.style.boxShadow = 'none';
                  }}
                >
                  {/* Selection checkbox */}
                  {selectMode && (
                    <div
                      style={{
                        position: 'absolute',
                        top: 6,
                        left: 6,
                        zIndex: 2,
                        width: 22,
                        height: 22,
                        borderRadius: 6,
                        background: isSelected ? '#1863dc' : 'rgba(0,0,0,0.6)',
                        border: isSelected ? 'none' : '1px solid #d9d9dd',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                      }}
                    >
                      {isSelected && <CheckCircle size={16} color="#fff" />}
                    </div>
                  )}

                  {/* Poster */}
                  <div
                    style={{
                      width: '100%',
                      aspectRatio: '2 / 3',
                      overflow: 'hidden',
                      background: '#141414',
                    }}
                  >
                    <img
                      src={posterUrl(w.poster_url)}
                      alt={displayTitle}
                      loading="lazy"
                      style={{
                        width: '100%',
                        height: '100%',
                        objectFit: 'cover',
                        display: 'block',
                      }}
                      onError={useDefaultPoster}
                    />
                  </div>

                  {/* Info */}
                  <div style={{ padding: '10px 12px 12px' }}>
                    <div style={{ display: 'flex', gap: 6, marginBottom: 6, flexWrap: 'wrap' }}>
                      <Tag
                        color={w.content_type === 'movie' ? 'green' : 'blue'}
                        style={{ margin: 0, fontSize: 11, lineHeight: '18px' }}
                      >
                        {w.content_type === 'movie' ? t('works.movie') : t('works.tv')}
                      </Tag>
                      {w.rating != null && (
                        <Tag color="gold" style={{ margin: 0, fontSize: 11, lineHeight: '18px' }}>
                          ★ {w.rating.toFixed(1)}
                        </Tag>
                      )}
                      {w.status && (
                        <Tag style={{ margin: 0, fontSize: 11, lineHeight: '18px' }}>{w.status}</Tag>
                      )}
                    </div>

                    <Text
                      ellipsis={{ tooltip: displayTitle }}
                      style={{
                        fontSize: 13,
                        fontWeight: 600,
                        color: '#d9d9dd',
                        display: 'block',
                        lineHeight: 1.4,
                      }}
                    >
                      {displayTitle}
                    </Text>

                    <Text style={{ fontSize: 11, color: '#616161', display: 'block', marginTop: 2 }}>
                      {w.content_type === 'movie'
                        ? w.release_date || '—'
                        : w.number_of_seasons
                          ? `${w.number_of_seasons}S · ${w.number_of_episodes ?? '?'}E`
                          : '—'}
                    </Text>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Infinite scroll sentinel */}
          <div ref={sentinelRef} style={{ height: 1 }} />
          {loadingMore && (
            <div style={{ display: 'flex', justifyContent: 'center', padding: 16 }}>
              <Spin />
            </div>
          )}
          {!hasMore && works.length > 0 && (
            <div style={{ textAlign: 'center', padding: 16 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t('works.noMore')}
              </Text>
            </div>
          )}
        </>
      )}

      <MetadataConfigModal open={configOpen} onClose={() => setConfigOpen(false)} />
    </div>
  );
}
