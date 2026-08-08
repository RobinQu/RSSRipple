import { useState, useEffect, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useDocumentTitle from '../hooks/useDocumentTitle';
import { Plus, Search, Layers, Pencil, Trash2, X } from 'lucide-react';
import {
  Table,
  Button,
  Space,
  Typography,
  App,
  Empty,
  Tag,
  Input,
  Drawer,
  Modal,
  Form,
  Spin,
  Segmented,
  Divider,
} from 'antd';
import type { TableColumnsType } from 'antd';
import { collectionsApi } from '../api/collections';
import { worksApi } from '../api/works';
import { withMobileLabels } from '../utils/table';
import Pagination from '../components/Pagination';
import type { WorkCollection, CollectionWork, Work } from '../types';

const { Title, Text } = Typography;
const PAGE_SIZE = 20;

function workTitle(w: { title_cn?: string | null; title_en?: string | null; original_title?: string | null; title?: string | null }) {
  return w.title_cn || w.title_en || w.original_title || w.title || '—';
}

/** Create / rename modal — title_cn required, title_en/description optional. */
function CollectionFormModal({
  open,
  collection,
  onClose,
  onSaved,
}: {
  open: boolean;
  collection: WorkCollection | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) {
      form.setFieldsValue({
        title_cn: collection?.title_cn ?? '',
        title_en: collection?.title_en ?? '',
        description: collection?.description ?? '',
      });
    }
  }, [open, collection, form]);

  const submit = async () => {
    const values = await form.validateFields();
    setSaving(true);
    const body = {
      title_cn: values.title_cn.trim(),
      title_en: values.title_en?.trim() || null,
      description: values.description?.trim() || null,
    };
    const res = collection
      ? await collectionsApi.update(collection.id, body)
      : await collectionsApi.create(body);
    setSaving(false);
    if (res.success) {
      message.success(t(collection ? 'collections.saved' : 'collections.created'));
      onSaved();
      onClose();
    } else {
      message.error(res.error?.message || t(collection ? 'collections.saveFailed' : 'collections.createFailed'));
    }
  };

  return (
    <Modal
      open={open}
      title={t(collection ? 'collections.editTitle' : 'collections.createTitle')}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      onOk={submit}
      onCancel={onClose}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
        <Form.Item
          name="title_cn"
          label={t('collections.nameCn')}
          rules={[{ required: true, message: t('collections.nameCnRequired') }]}
        >
          <Input maxLength={200} />
        </Form.Item>
        <Form.Item name="title_en" label={t('collections.nameEn')}>
          <Input maxLength={200} />
        </Form.Item>
        <Form.Item name="description" label={t('collections.description')}>
          <Input.TextArea rows={2} maxLength={2000} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

/** Work picker modal — search series/movies via the works API, attach on click. */
function AttachWorkModal({
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

export default function Collections() {
  const { t } = useTranslation();
  useDocumentTitle(t('collections.title'));
  const { message, modal } = App.useApp();

  const [collections, setCollections] = useState<WorkCollection[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<WorkCollection | null>(null);

  // Detail drawer
  const [detail, setDetail] = useState<WorkCollection | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [attachOpen, setAttachOpen] = useState(false);

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

  const loadDetail = useCallback(async (id: string) => {
    setDetailLoading(true);
    try {
      const r = await collectionsApi.get(id, true);
      if (r.success) setDetail(r.data);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const openDetail = (c: WorkCollection) => {
    // Show the list row immediately; hydrate works/untracked_parts async.
    setDetail(c);
    loadDetail(c.id);
  };

  const handleDelete = (c: WorkCollection) => {
    modal.confirm({
      title: t('collections.deleteConfirm'),
      content: t('collections.deleteWarning'),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await collectionsApi.remove(c.id);
        if (r.success) {
          message.success(t('collections.deleted'));
          setDetail((d) => (d?.id === c.id ? null : d));
          loadList();
        } else {
          message.error(r.error?.message || t('collections.deleteFailed'));
        }
      },
    });
  };

  const handleDetach = (w: CollectionWork) => {
    if (!detail) return;
    modal.confirm({
      title: t('collections.detachConfirm', { title: w.title || '' }),
      okText: t('common.confirm'),
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await collectionsApi.detachWork(detail.id, w.type, w.id);
        if (r.success) {
          message.success(t('collections.detached'));
          loadDetail(detail.id);
          loadList();
        } else {
          message.error(r.error?.message || t('collections.detachFailed'));
        }
      },
    });
  };

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
        <a onClick={() => openDetail(record)}>
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
    {
      title: t('common.operation'),
      key: 'actions',
      width: 120,
      align: 'right',
      render: (_, record) => (
        <Space size={4}>
          <Button
            type="text"
            size="small"
            icon={<Pencil size={14} />}
            title={t('common.edit')}
            onClick={() => {
              setEditing(record);
              setFormOpen(true);
            }}
          />
          <Button
            type="text"
            size="small"
            danger
            icon={<Trash2 size={14} />}
            title={t('common.delete')}
            onClick={() => handleDelete(record)}
          />
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 12,
          marginBottom: 24,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <Layers size={22} style={{ color: '#1863dc' }} />
          <Title level={3} style={{ margin: 0 }}>
            {t('collections.title')}
          </Title>
        </div>
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
          <Button
            type="primary"
            icon={<Plus size={14} />}
            onClick={() => {
              setEditing(null);
              setFormOpen(true);
            }}
          >
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
        collection={editing}
        onClose={() => setFormOpen(false)}
        onSaved={() => {
          loadList();
          if (editing && detail?.id === editing.id) loadDetail(editing.id);
        }}
      />

      <Drawer
        open={detail !== null}
        title={detail?.title_cn ?? ''}
        width={window.innerWidth < 768 ? '100%' : 520}
        onClose={() => setDetail(null)}
        destroyOnHidden
        extra={
          detail && (
            <Space size={4}>
              <Button
                size="small"
                icon={<Pencil size={13} />}
                onClick={() => {
                  setEditing(detail);
                  setFormOpen(true);
                }}
              >
                {t('common.edit')}
              </Button>
              <Button
                size="small"
                danger
                icon={<Trash2 size={13} />}
                onClick={() => handleDelete(detail)}
              >
                {t('common.delete')}
              </Button>
            </Space>
          )
        }
      >
        {detail && (
          <Spin spinning={detailLoading}>
            <Space size={6} style={{ marginBottom: 8 }}>
              {sourceTag(detail)}
              {detail.title_en && <Text type="secondary">{detail.title_en}</Text>}
            </Space>
            {detail.description && (
              <Text type="secondary" style={{ display: 'block', marginBottom: 12, fontSize: 13 }}>
                {detail.description}
              </Text>
            )}

            <Divider style={{ margin: '12px 0' }} />
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                marginBottom: 8,
              }}
            >
              <Text strong style={{ fontSize: 13 }}>
                {t('collections.works')}
              </Text>
              <Button size="small" icon={<Plus size={13} />} onClick={() => setAttachOpen(true)}>
                {t('collections.attach')}
              </Button>
            </div>
            {!detail.works || detail.works.length === 0 ? (
              <Text type="secondary" style={{ fontSize: 13 }}>
                {t('collections.noWorks')}
              </Text>
            ) : (
              detail.works.map((w) => (
                <div
                  key={`${w.type}-${w.id}`}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    padding: '4px 0',
                  }}
                >
                  <Tag color={w.type === 'series' ? 'blue' : 'green'} style={{ margin: 0 }}>
                    {w.type === 'series' ? t('collections.tabSeries') : t('collections.tabMovie')}
                  </Tag>
                  <Link
                    to={w.type === 'series' ? `/series/${w.id}` : `/movies/${w.id}`}
                    style={{ flex: 1, minWidth: 0 }}
                  >
                    <Text ellipsis style={{ display: 'block' }}>
                      {w.title}
                      {w.year ? ` (${w.year})` : ''}
                    </Text>
                  </Link>
                  <Button
                    type="text"
                    size="small"
                    icon={<X size={13} />}
                    title={t('collections.detach')}
                    onClick={() => handleDetach(w)}
                  />
                </div>
              ))
            )}

            {detail.untracked_parts && detail.untracked_parts.length > 0 && (
              <>
                <Divider style={{ margin: '12px 0' }} />
                <Text strong style={{ fontSize: 13, display: 'block', marginBottom: 8 }}>
                  {t('collections.untrackedParts')}
                </Text>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                  {detail.untracked_parts.map((p) => (
                    <Tag key={p.tmdb_id} style={{ margin: 0, opacity: 0.55 }}>
                      {p.title}
                      {p.year ? ` (${p.year})` : ''} · {t('collections.untracked')}
                    </Tag>
                  ))}
                </div>
              </>
            )}
          </Spin>
        )}
      </Drawer>

      {detail && (
        <AttachWorkModal
          open={attachOpen}
          collectionId={detail.id}
          onClose={() => setAttachOpen(false)}
          onAttached={() => {
            loadDetail(detail.id);
            loadList();
          }}
        />
      )}
    </div>
  );
}
