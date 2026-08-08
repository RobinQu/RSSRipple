import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Search } from 'lucide-react';
import { App, Button, Empty, Input, Modal, Segmented, Space, Spin, Typography } from 'antd';
import { collectionsApi } from '../api/collections';
import { worksApi } from '../api/works';
import Pagination from './Pagination';
import type { Work } from '../types';

const { Text } = Typography;

function workTitle(w: { title_cn?: string | null; title_en?: string | null; original_title?: string | null; title?: string | null }) {
  return w.title_cn || w.title_en || w.original_title || w.title || '—';
}

/** Work picker modal — search series/movies via the works API, attach on click. */
export default function AttachWorkModal({
  open,
  collectionId,
  onClose,
  onAttached,
}: {
  open: boolean;
  collectionId: string;
  onClose: () => void;
  onAttached: () => void;
}) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [tab, setTab] = useState<'tv' | 'movie'>('tv');
  const [search, setSearch] = useState('');
  const [works, setWorks] = useState<Work[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [attachingId, setAttachingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!open) return;
    setLoading(true);
    try {
      const r = await worksApi.list(page, 10, search.trim() || undefined, tab);
      if (r.success) {
        setWorks(r.data);
        setTotal(r.meta?.total ?? 0);
      }
    } finally {
      setLoading(false);
    }
  }, [open, page, search, tab]);

  useEffect(() => {
    const timeout = setTimeout(load, 300);
    return () => clearTimeout(timeout);
  }, [load]);

  const attach = async (w: Work) => {
    setAttachingId(w.id);
    const res = await collectionsApi.attachWork(
      collectionId,
      tab === 'tv' ? 'series' : 'movie',
      w.id,
    );
    setAttachingId(null);
    if (res.success) {
      message.success(t('collections.attached'));
      onAttached();
    } else {
      message.error(res.error?.message || t('collections.attachFailed'));
    }
  };

  return (
    <Modal
      open={open}
      title={t('collections.attachTitle')}
      footer={null}
      onCancel={onClose}
      destroyOnHidden
      width={560}
    >
      <Space style={{ marginBottom: 12, flexWrap: 'wrap' }}>
        <Segmented
          options={[
            { label: t('collections.tabSeries'), value: 'tv' },
            { label: t('collections.tabMovie'), value: 'movie' },
          ]}
          value={tab}
          onChange={(v) => {
            setTab(v as 'tv' | 'movie');
            setPage(1);
          }}
        />
        <Input
          prefix={<Search size={14} style={{ color: 'var(--rr-text-muted)' }} />}
          placeholder={t('collections.attachSearchPlaceholder')}
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(1);
          }}
          style={{ width: 220 }}
          allowClear
        />
      </Space>
      <Spin spinning={loading}>
        <div style={{ minHeight: 120 }}>
          {works.length === 0 && !loading ? (
            <Empty description={t('common.noResults')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
          ) : (
            works.map((w) => (
              <div
                key={w.id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: 8,
                  padding: '6px 0',
                  borderBottom: '1px solid var(--rr-border-soft)',
                }}
              >
                <Text ellipsis style={{ flex: 1, minWidth: 0 }}>
                  {workTitle(w)}
                  {w.year ? ` (${w.year})` : ''}
                </Text>
                <Button
                  size="small"
                  loading={attachingId === w.id}
                  onClick={() => attach(w)}
                >
                  {t('collections.attach')}
                </Button>
              </div>
            ))
          )}
        </div>
      </Spin>
      <div style={{ display: 'flex', justifyContent: 'center', marginTop: 8 }}>
        <Pagination page={page} pageSize={10} total={total} onPageChange={setPage} />
      </div>
    </Modal>
  );
}
