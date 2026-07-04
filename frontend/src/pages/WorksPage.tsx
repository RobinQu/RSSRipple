import { useState, useEffect, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Library, Search } from 'lucide-react';
import {
  Typography,
  Input,
  Spin,
  Empty,
  Tag,
  Segmented,
  Pagination,
} from 'antd';
import { worksApi } from '../api/works';
import { posterUrl, useDefaultPoster } from '../utils/poster';
import type { Work } from '../types';

const { Title, Text } = Typography;

const CONTENT_TYPES = ['all', 'tv', 'movie'] as const;
type ContentType = (typeof CONTENT_TYPES)[number];

function getDisplayTitle(w: Work): string {
  return w.title_cn || w.title_en || w.original_title || '—';
}

export default function WorksPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [works, setWorks] = useState<Work[]>([]);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState('');
  const [contentType, setContentType] = useState<ContentType>('all');

  const fetchWorks = useCallback(async () => {
    setLoading(true);
    try {
      const ct = contentType === 'all' ? undefined : contentType;
      const r = await worksApi.list(page, 20, search.trim() || undefined, ct);
      if (r.success) {
        setWorks(r.data);
        if (r.meta) setTotal(r.meta.total);
      }
    } finally {
      setLoading(false);
    }
  }, [page, search, contentType]);

  useEffect(() => {
    const timeout = setTimeout(fetchWorks, 300);
    return () => clearTimeout(timeout);
  }, [fetchWorks]);

  const handleCardClick = (w: Work) => {
    if (w.content_type === 'movie') {
      navigate(`/movies/${w.id}`);
    } else {
      navigate(`/series/${w.id}`);
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
          marginBottom: 24,
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
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <Segmented
            options={segmentedOptions}
            value={contentType}
            onChange={(v) => {
              setContentType(v as ContentType);
              setPage(1);
            }}
          />
          <Input
            prefix={<Search size={14} style={{ color: '#93939f' }} />}
            placeholder={t('works.searchPlaceholder')}
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
            style={{ width: 240 }}
            allowClear
          />
        </div>
      </div>

      {/* Content */}
      {loading ? (
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
              return (
                <div
                  key={w.id}
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
                    border: '1px solid #2a2a2a',
                    background: '#1a1a1a',
                    transition: 'border-color 0.2s, transform 0.2s, box-shadow 0.2s',
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.borderColor = '#1863dc';
                    e.currentTarget.style.transform = 'translateY(-2px)';
                    e.currentTarget.style.boxShadow = '0 8px 24px rgba(0,0,0,0.4)';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.borderColor = '#2a2a2a';
                    e.currentTarget.style.transform = 'translateY(0)';
                    e.currentTarget.style.boxShadow = 'none';
                  }}
                >
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
                    {/* Badges row */}
                    <div style={{ display: 'flex', gap: 6, marginBottom: 6, flexWrap: 'wrap' }}>
                      <Tag
                        color={w.content_type === 'movie' ? 'green' : 'blue'}
                        style={{ margin: 0, fontSize: 11, lineHeight: '18px' }}
                      >
                        {w.content_type === 'movie' ? t('works.movie') : t('works.tv')}
                      </Tag>
                      {w.rating != null && (
                        <Tag
                          color="gold"
                          style={{ margin: 0, fontSize: 11, lineHeight: '18px' }}
                        >
                          ★ {w.rating.toFixed(1)}
                        </Tag>
                      )}
                      {w.status && (
                        <Tag style={{ margin: 0, fontSize: 11, lineHeight: '18px' }}>{w.status}</Tag>
                      )}
                    </div>

                    {/* Title */}
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

                    {/* Sub-info */}
                    <Text
                      style={{
                        fontSize: 11,
                        color: '#616161',
                        display: 'block',
                        marginTop: 2,
                      }}
                    >
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

          {/* Pagination */}
          {total > 20 && (
            <div style={{ display: 'flex', justifyContent: 'center' }}>
              <Pagination
                current={page}
                pageSize={20}
                total={total}
                onChange={setPage}
                showSizeChanger={false}
              />
            </div>
          )}
        </>
      )}
    </div>
  );
}
