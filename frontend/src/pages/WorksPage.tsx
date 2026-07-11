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
  Checkbox,
  App,
} from 'antd';
import { worksApi } from '../api/works';
import type { RefreshItem } from '../api/works';
import type { Work } from '../types';
import MetadataConfigModal from '../components/MetadataConfigModal';
import Pagination from '../components/Pagination';

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
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState('');
  const [contentType, setContentType] = useState<ContentType>('all');

  // Selection + batch refresh
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [batchRefreshing, setBatchRefreshing] = useState(false);

  // Configurator modal
  const [configOpen, setConfigOpen] = useState(false);

  const topRef = useRef<HTMLDivElement | null>(null);

  const fetchPage = useCallback(
    async (p: number, ct: ContentType, q: string) => {
      setLoading(true);
      try {
        const ctParam = ct === 'all' ? undefined : ct;
        const r = await worksApi.list(p, PAGE_SIZE, q.trim() || undefined, ctParam);
        if (r.success) {
          setWorks(r.data);
          setTotal(r.meta?.total ?? 0);
        }
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  // Load the current page whenever page / filter / search changes.
  // The short debounce keeps search typing from firing a request per keystroke.
  useEffect(() => {
    const timeout = setTimeout(() => {
      fetchPage(page, contentType, search);
    }, 300);
    return () => clearTimeout(timeout);
  }, [page, contentType, search, fetchPage]);

  const handlePageChange = (p: number) => {
    setSelected(new Set());
    setPage(p);
    // Scroll back to the top of the list when navigating between pages.
    topRef.current?.scrollIntoView({ block: 'start' });
  };

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
        // Reload the current page so refreshed titles appear.
        fetchPage(page, contentType, search);
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
    <div ref={topRef}>
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
              setPage(1);
              setSelected(new Set());
            }}
          />
          <Input
            prefix={<Search size={14} style={{ color: 'var(--rr-text-muted)' }} />}
            placeholder={t('works.searchPlaceholder')}
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
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
        <div className="works-selection-bar">
          <Space>
            <Text>{t('works.selectedCount', { n: selected.size })}</Text>
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
          <Spin spinning={loading}>
            <div className="resource-table-wrap" style={{ marginBottom: 16 }}>
              <table className={`resource-table works-table${selectMode ? ' selectable' : ''}`}>
                <colgroup>
                  {selectMode && <col style={{ width: 40 }} />}
                  <col style={{ width: 60 }} />
                  <col />
                  <col style={{ width: 84 }} />
                  <col style={{ width: 96 }} />
                  <col style={{ width: 116 }} />
                  <col style={{ width: 200 }} />
                </colgroup>
                <thead>
                  <tr style={{ color: 'var(--rr-text-muted)', fontSize: 12 }}>
                    {selectMode && <th style={{ textAlign: 'left', padding: '8px' }} />}
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('works.colType')}</th>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('works.colTitle')}</th>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('works.colRating')}</th>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('works.colStatus')}</th>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('works.colInfo')}</th>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('works.colGenre')}</th>
                  </tr>
                </thead>
                <tbody>
                  {works.map((w) => {
                    const displayTitle = getDisplayTitle(w);
                    const key = workKey(w);
                    const isSelected = selected.has(key);
                    const info =
                      w.content_type === 'movie'
                        ? (w.year ?? '—')
                        : w.number_of_seasons
                          ? `${w.number_of_seasons}S · ${w.number_of_episodes ?? '?'}E`
                          : '—';
                    return (
                      <tr
                        key={key}
                        className={`resource-row works-row${isSelected ? ' selected' : ''}`}
                        style={{ borderTop: '1px solid var(--rr-border-soft)', cursor: 'pointer' }}
                        onClick={() => handleCardClick(w)}
                      >
                        {selectMode && (
                          <td
                            className="resource-check-cell"
                            style={{ padding: '8px' }}
                            onClick={(e) => e.stopPropagation()}
                          >
                            <Checkbox checked={isSelected} onChange={() => handleCardClick(w)} />
                          </td>
                        )}
                        <td style={{ padding: '8px' }} data-label={t('works.colType')}>
                          <Tag color={w.content_type === 'movie' ? 'green' : 'blue'} style={{ margin: 0 }}>
                            {w.content_type === 'movie' ? t('works.movie') : t('works.tv')}
                          </Tag>
                        </td>
                        <td className="resource-title-cell" style={{ padding: '8px' }} data-label={t('works.colTitle')}>
                          <Text ellipsis={{ tooltip: displayTitle }} style={{ fontWeight: 600 }}>
                            {displayTitle}
                          </Text>
                        </td>
                        <td style={{ padding: '8px' }} data-label={t('works.colRating')}>
                          {w.rating != null ? `★ ${w.rating.toFixed(1)}` : '—'}
                        </td>
                        <td style={{ padding: '8px' }} data-label={t('works.colStatus')}>
                          {w.status || '—'}
                        </td>
                        <td style={{ padding: '8px', whiteSpace: 'nowrap' }} data-label={t('works.colInfo')}>
                          {info}
                        </td>
                        <td className="resource-text-cell" style={{ padding: '8px' }} data-label={t('works.colGenre')}>
                          <Text ellipsis style={{ display: 'block' }}>
                            {w.genre && w.genre.length ? w.genre.join(', ') : '—'}
                          </Text>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Spin>

          {/* Paginator */}
          <div style={{ display: 'flex', justifyContent: 'center', padding: '8px 0' }}>
            <Pagination
              page={page}
              pageSize={PAGE_SIZE}
              total={total}
              onPageChange={handlePageChange}
            />
          </div>
        </>
      )}

      <MetadataConfigModal open={configOpen} onClose={() => setConfigOpen(false)} />
    </div>
  );
}
