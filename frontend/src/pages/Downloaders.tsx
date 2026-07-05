import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Plus, Trash2, Zap, Edit } from 'lucide-react';
import { Table, Button, Space, Typography, App, Empty, Tag } from 'antd';
import type { TableColumnsType } from 'antd';
import { downloadersApi } from '../api/downloaders';
import type { DownloaderInstance } from '../types';
import { timeAgo } from '../utils/format';

const { Title } = Typography;

const STATUS_COLOR: Record<string, string> = {
  connected: 'success',
  disconnected: 'default',
  error: 'error',
};

export default function Downloaders() {
  const [items, setItems] = useState<DownloaderInstance[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const { t } = useTranslation();
  const { message, modal } = App.useApp();

  const fetchItems = async () => {
    setLoading(true);
    const res = await downloadersApi.list(page, 50);
    if (res.success) {
      setItems(res.data);
      if (res.meta) setTotal(res.meta.total);
    }
    setLoading(false);
  };

  useEffect(() => {
    fetchItems();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  const handleTest = async (id: string) => {
    const res = await downloadersApi.test(id);
    if (res.success) message.success(res.data.message);
    else message.error(res.error?.message || t('downloaders.testFailed'));
    fetchItems();
  };

  const handleDelete = (id: string) => {
    modal.confirm({
      title: t('downloaders.deleteConfirm'),
      content: t('downloaders.deleteLinked'),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await downloadersApi.delete(id);
        if (r.success) {
          message.success(t('downloaders.deleted'));
          fetchItems();
          return;
        }
        // Surface the specific agents keeping the downloader alive (409
        // response now carries `details.agents = [{id, name}]`). Offer a
        // one-click jump to each agent so the user can unbind before
        // retrying.
        const agents = (r.error?.details as { agents?: { id: string; name: string }[] } | undefined)?.agents;
        if (r.error?.code === 'CONFLICT' && agents && agents.length > 0) {
          modal.error({
            title: t('downloaders.deleteBlockedTitle'),
            content: (
              <div>
                <p style={{ marginBottom: 12 }}>
                  {t('downloaders.deleteBlockedBody', { count: agents.length })}
                </p>
                <ul style={{ margin: 0, paddingLeft: 20 }}>
                  {agents.map((a) => (
                    <li key={a.id} style={{ marginBottom: 4 }}>
                      <Link to={`/agents/${a.id}`}>{a.name}</Link>
                    </li>
                  ))}
                </ul>
              </div>
            ),
          });
          return;
        }
        message.error(r.error?.message || t('downloaders.deleteFailed'));
      },
    });
  };

  const columns: TableColumnsType<DownloaderInstance> = [
    {
      title: t('common.name'),
      dataIndex: 'name',
      key: 'name',
      render: (name: string, r) => <Link to={`/downloaders/${r.id}`}>{name}</Link>,
    },
    { title: t('common.type'), dataIndex: 'type', key: 'type', width: 120 },
    {
      title: t('common.url'),
      dataIndex: 'url',
      key: 'url',
      ellipsis: true,
    },
    {
      title: t('downloaders.defaultDir'),
      dataIndex: 'download_dir',
      key: 'download_dir',
      ellipsis: true,
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (s: string) => (
        <Tag color={STATUS_COLOR[s] ?? 'default'}>{s.toUpperCase()}</Tag>
      ),
    },
    {
      title: t('downloaders.lastCheck'),
      dataIndex: 'last_checked_at',
      key: 'last_checked_at',
      width: 150,
      render: (v: string | null) => (v ? timeAgo(v) : t('format.dash')),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 160,
      align: 'right',
      render: (_, record) => (
        <Space size={0}>
          <Button
            type="text"
            size="small"
            icon={<Zap size={14} />}
            title={t('downloaders.testConnection')}
            onClick={() => handleTest(record.id)}
          />
          <Link to={`/downloaders/${record.id}/edit`}>
            <Button type="text" size="small" icon={<Edit size={14} />} title={t('common.edit')} />
          </Link>
          <Button
            type="text"
            size="small"
            danger
            icon={<Trash2 size={14} />}
            title={t('common.delete')}
            onClick={() => handleDelete(record.id)}
          />
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>{t('downloaders.title')}</Title>
        <Link to="/downloaders/new">
          <Button type="primary" icon={<Plus size={14} />}>{t('downloaders.addDownloader')}</Button>
        </Link>
      </div>

      <Table
        columns={columns}
        dataSource={items}
        rowKey="id"
        loading={loading}
        locale={{ emptyText: <Empty description={t('downloaders.noDownloaders')} /> }}
        pagination={{
          current: page,
          pageSize: 50,
          total,
          onChange: setPage,
          showSizeChanger: false,
        }}
      />
    </div>
  );
}
