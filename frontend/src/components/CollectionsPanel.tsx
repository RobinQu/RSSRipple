import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Plus, Search } from 'lucide-react';
import { Table, Button, Space, Empty, Tag, Input } from 'antd';
import type { TableColumnsType } from 'antd';
import { collectionsApi } from '../api/collections';
import { withMobileLabels } from '../utils/table';
import CollectionFormModal from './CollectionFormModal';
import type { WorkCollection } from '../types';

const PAGE_SIZE = 20;

/** Collections browse list — rendered by WorksPage in 合集 view.
 * Row click navigates to the collection detail page (/collections/:id);
 * rename/delete/attach live on that page. */
export default function CollectionsPanel() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [collections, setCollections] = useState<WorkCollection[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);

  const [formOpen, setFormOpen] = useState(false);

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

  useEffect(() => {
    const timeout = setTimeout(loadList, 300);
    return () => clearTimeout(timeout);
  }, [loadList]);

  const sourceTag = (c: WorkCollection) => {
    const src = c.external_source || 'manual';
    const color = src === 'tmdb_collection' ? 'blue' : src === 'wikidata' ? 'purple' : 'default';
    return <Tag color={color}>{t(`collections.source_${src}`, src)}</Tag>;
  };

  const columns: TableColumnsType<WorkCollection> = [
    {
      title: t('collections.colName'),
      key: 'name',
      render: (_, record) => (
        <a onClick={() => navigate(`/collections/${record.id}`)}>
          {record.title_cn}
          {record.title_en ? ` / ${record.title_en}` : ''}
        </a>
      ),
    },
    {
      title: t('collections.colSource'),
      key: 'source',
      width: 130,
      render: (_, record) => sourceTag(record),
    },
    {
      title: t('collections.colWorks'),
      key: 'work_count',
      width: 90,
      render: (_, record) => record.work_count ?? 0,
    },
  ];

  return (
    <div>
      <div
        style={{
          display: 'flex',
          justifyContent: 'flex-end',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 12,
          marginBottom: 16,
        }}
      >
        <Space wrap>
          <Input
            prefix={<Search size={14} style={{ color: 'var(--rr-text-muted)' }} />}
            placeholder={t('collections.searchPlaceholder')}
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
            style={{ width: 220 }}
            allowClear
          />
          <Button type="primary" icon={<Plus size={14} />} onClick={() => setFormOpen(true)}>
            {t('collections.new')}
          </Button>
        </Space>
      </div>

      <Table
        className="stack-table"
        columns={withMobileLabels(columns)}
        dataSource={collections}
        rowKey="id"
        loading={loading}
        locale={{ emptyText: <Empty description={t('collections.noCollections')} /> }}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total,
          onChange: setPage,
          showSizeChanger: false,
        }}
      />

      <CollectionFormModal
        open={formOpen}
        collection={null}
        onClose={() => setFormOpen(false)}
        onSaved={loadList}
      />
    </div>
  );
}
