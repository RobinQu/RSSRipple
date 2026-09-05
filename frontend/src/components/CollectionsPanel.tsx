import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Empty, Tag, Spin, Typography } from 'antd';
import { collectionsApi } from '../api/collections';
import Pagination from './Pagination';
import type { WorkCollection } from '../types';

const PAGE_SIZE = 20;

/** Collections browse list — rendered by WorksPage in 合集 view.
 * Row click navigates to the collection detail page (/collections/:id);
 * rename/delete/attach live on that page. The search box and the create
 * button live in the WorksPage header so every view shares the same frame. */
export default function CollectionsPanel({
  search,
  refreshKey,
}: {
  search: string;
  refreshKey: number;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [collections, setCollections] = useState<WorkCollection[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);

  const loadList = useCallback(async () => {
    setLoading(true);
    try {
      const r = await collectionsApi.list(page, PAGE_SIZE, search.trim() || undefined);
      if (r.success) {
        setCollections(r.data);
        setTotal(r.meta?.total ?? 0);
      }
    } finally {
      setLoading(false);
    }
  }, [page, search]);

  // A new search text restarts from page 1 (the parent header owns the input).
  useEffect(() => {
    setPage(1);
  }, [search]);

  useEffect(() => {
    const timeout = setTimeout(loadList, 300);
    return () => clearTimeout(timeout);
  }, [loadList, refreshKey]);

  const sourceTag = (c: WorkCollection) => {
    const src = c.external_source || 'manual';
    const color =
      src === 'tmdb_collection'
        ? 'blue'
        : src === 'wikidata'
          ? 'purple'
          : src === 'series_group'
            ? 'green'
            : src === 'franchise_pack'
              ? 'orange'
              : 'default';
    return <Tag color={color}>{t(`collections.source_${src}`, src)}</Tag>;
  };

  return (
    <div>
      {loading && collections.length === 0 ? (
        <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />
      ) : collections.length === 0 ? (
        <Empty description={t('collections.noCollections')} style={{ marginTop: 48 }} />
      ) : (
        <>
          <Spin spinning={loading}>
            <div className="resource-table-wrap" style={{ marginBottom: 16 }}>
              <table className="resource-table works-table">
                <colgroup>
                  <col />
                  <col style={{ width: 130 }} />
                  <col style={{ width: 90 }} />
                </colgroup>
                <thead>
                  <tr style={{ color: 'var(--rr-text-muted)', fontSize: 12 }}>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('collections.colName')}</th>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('collections.colSource')}</th>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('collections.colWorks')}</th>
                  </tr>
                </thead>
                <tbody>
                  {collections.map((record) => (
                    <tr
                      key={record.id}
                      className="resource-row works-row"
                      style={{ borderTop: '1px solid var(--rr-border-soft)', cursor: 'pointer' }}
                      onClick={() => navigate(`/collections/${record.id}`)}
                    >
                      <td style={{ padding: '8px' }} data-label={t('collections.colName')}>
                        <Typography.Text strong>{record.title_cn}</Typography.Text>
                        {record.title_en && <Typography.Text type="secondary"> / {record.title_en}</Typography.Text>}
                      </td>
                      <td style={{ padding: '8px' }} data-label={t('collections.colSource')}>
                        {sourceTag(record)}
                      </td>
                      <td style={{ padding: '8px' }} data-label={t('collections.colWorks')}>
                        {record.work_count ?? 0}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Spin>
          <div style={{ display: 'flex', justifyContent: 'center', padding: '8px 0' }}>
            <Pagination page={page} pageSize={PAGE_SIZE} total={total} onPageChange={setPage} />
          </div>
        </>
      )}
    </div>
  );
}
