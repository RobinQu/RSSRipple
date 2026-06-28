import { useState, useEffect, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Plus, Trash2, Edit, RefreshCw } from 'lucide-react';
import { Table, Button, Space, Typography, App, Empty, Spin, Tag } from 'antd';
import type { TableColumnsType } from 'antd';
import { channelsApi } from '../api/channels';
import StatusBadge from '../components/StatusBadge';
import { timeAgo } from '../utils/format';
import type { Channel } from '../types';

const { Title } = Typography;

export default function Channels() {
  const { t } = useTranslation();
  const [channels, setChannels] = useState<Channel[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [fetchingIds, setFetchingIds] = useState<Set<string>>(new Set());
  const { message, modal } = App.useApp();

  const loadChannels = useCallback(async () => {
    const res = await channelsApi.list(page, 20);
    if (res.success) {
      setChannels(res.data);
      if (res.meta) setTotal(res.meta.total);
    }
    setLoading(false);
  }, [page]);

  useEffect(() => {
    setLoading(true);
    loadChannels();
  }, [loadChannels]);

  // Poll fetch status
  useEffect(() => {
    if (fetchingIds.size === 0) return;
    const timer = setInterval(async () => {
      const updates: Record<string, boolean> = {};
      await Promise.all(
        Array.from(fetchingIds).map(async (cid) => {
          try {
            const r = await channelsApi.fetchStatus(cid);
            if (r.success && r.data) {
              const s = r.data.status;
              if (s === 'done' || s === 'failed' || s === 'success') {
                updates[cid] = false;
              }
            }
          } catch {
            /* ignore */
          }
        }),
      );
      const done = Object.keys(updates);
      if (done.length > 0) {
        setFetchingIds((prev) => {
          const n = new Set(prev);
          done.forEach((id) => n.delete(id));
          return n;
        });
        loadChannels();
        done.forEach((_id) => message.success(t('channels.updated')));
      }
    }, 2000);
    return () => clearInterval(timer);
  }, [fetchingIds, loadChannels, message]);

  const handleDelete = (id: string) => {
    modal.confirm({
      title: t('channels.deleteConfirm'),
      content: t('channels.deleteWarning'),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await channelsApi.delete(id);
        if (r.success) {
          message.success(t('channels.deleted'));
          loadChannels();
        } else {
          message.error(r.error?.message || t('channels.deleteFailed'));
        }
      },
    });
  };

  const handleFetch = async (id: string) => {
    if (fetchingIds.has(id)) return;
    setFetchingIds((prev) => new Set(prev).add(id));
    const r = await channelsApi.fetch(id);
    if (!r.success) {
      setFetchingIds((prev) => {
        const n = new Set(prev);
        n.delete(id);
        return n;
      });
      message.error(r.error?.message || t('channels.fetchFailed'));
    }
  };

  const columns: TableColumnsType<Channel> = [
    {
      title: t('common.name'),
      dataIndex: 'name',
      key: 'name',
      render: (name: string, record) => <Link to={`/channels/${record.id}`}>{name}</Link>,
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status: string, record) => (
        <Space size={4}>
          <StatusBadge status={status} />
          {record.last_fetch_status && record.last_fetch_status === 'failed' && (
            <Tag color="error" style={{ fontSize: 10 }}>{t('channels.lastFailed')}</Tag>
          )}
        </Space>
      ),
    },
    {
      title: t('channels.fetchInterval'),
      dataIndex: 'fetch_interval',
      key: 'fetch_interval',
      width: 120,
      render: (v: number) => t('channels.minutesUnit', { n: Math.round(v / 60) }),
    },
    {
      title: t('channels.metadataSource'),
      dataIndex: 'metadata_source',
      key: 'metadata_source',
      width: 110,
      render: (v: string) =>
        v === 'llm' ? <Tag color="blue">{t('channels.llmSearch')}</Tag> : <Tag>{t('channels.localMatch')}</Tag>,
    },
    {
      title: t('channels.lastFetch'),
      dataIndex: 'last_fetched_at',
      key: 'last_fetched_at',
      width: 150,
      render: (val: string | null) => (val ? timeAgo(val) : t('common.never')),
    },
    {
      title: t('common.operation'),
      key: 'actions',
      width: 180,
      align: 'right',
      render: (_, record) => {
        const isFetching = fetchingIds.has(record.id);
        return (
          <Space size={4}>
            <Button
              type="text"
              size="small"
              disabled={isFetching}
              icon={isFetching ? <Spin size="small" /> : <RefreshCw size={14} />}
              onClick={() => handleFetch(record.id)}
              title={t('channels.fetchNow')}
            />
            <Link to={`/channels/${record.id}/edit`}>
              <Button type="text" size="small" icon={<Edit size={14} />} title={t('common.edit')} />
            </Link>
            <Button
              type="text"
              size="small"
              danger
              icon={<Trash2 size={14} />}
              onClick={() => handleDelete(record.id)}
              title={t('common.delete')}
            />
          </Space>
        );
      },
    },
  ];

  return (
    <div>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 24,
        }}
      >
        <Title level={3} style={{ margin: 0 }}>
          {t('channels.title')}
        </Title>
        <Link to="/channels/new">
            <Button type="primary" icon={<Plus size={14} />}>
              {t('channels.newChannel')}
            </Button>
        </Link>
      </div>

      <Table
        columns={columns}
        dataSource={channels}
        rowKey="id"
        loading={loading}
        locale={{
          emptyText: <Empty description={t('channels.noChannels')} />,
        }}
        pagination={{
          current: page,
          pageSize: 20,
          total,
          onChange: setPage,
          showSizeChanger: false,
        }}
      />
    </div>
  );
}
