import { useState, useEffect, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { Plus, Trash2, Edit, RefreshCw } from 'lucide-react';
import { Table, Button, Space, Typography, App, Empty, Spin, Tag } from 'antd';
import type { TableColumnsType } from 'antd';
import { channelsApi } from '../api/channels';
import StatusBadge from '../components/StatusBadge';
import { timeAgo } from '../utils/format';
import type { Channel } from '../types';

const { Title } = Typography;

export default function Channels() {
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
        done.forEach((_id) => message.success(`频道已更新`));
      }
    }, 2000);
    return () => clearInterval(timer);
  }, [fetchingIds, loadChannels, message]);

  const handleDelete = (id: string) => {
    modal.confirm({
      title: '确定删除该频道？',
      content: '所有相关资源、Agent 和任务将被级联删除，此操作不可撤销。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        const r = await channelsApi.delete(id);
        if (r.success) {
          message.success('频道已删除');
          loadChannels();
        } else {
          message.error(r.error?.message || '删除失败');
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
      message.error(r.error?.message || '抓取失败');
    }
  };

  const columns: TableColumnsType<Channel> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      render: (name: string, record) => <Link to={`/channels/${record.id}`}>{name}</Link>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status: string, record) => (
        <Space size={4}>
          <StatusBadge status={status} />
          {record.last_fetch_status && record.last_fetch_status === 'failed' && (
            <Tag color="error" style={{ fontSize: 10 }}>上次失败</Tag>
          )}
        </Space>
      ),
    },
    {
      title: '抓取间隔',
      dataIndex: 'fetch_interval',
      key: 'fetch_interval',
      width: 120,
      render: (v: number) => `${Math.round(v / 60)} 分钟`,
    },
    {
      title: 'Metadata',
      dataIndex: 'metadata_source',
      key: 'metadata_source',
      width: 110,
      render: (v: string) =>
        v === 'llm' ? <Tag color="blue">LLM 搜索</Tag> : <Tag>本地匹配</Tag>,
    },
    {
      title: '上次抓取',
      dataIndex: 'last_fetched_at',
      key: 'last_fetched_at',
      width: 150,
      render: (val: string | null) => (val ? timeAgo(val) : '从未'),
    },
    {
      title: '操作',
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
              title="手动抓取"
            />
            <Link to={`/channels/${record.id}/edit`}>
              <Button type="text" size="small" icon={<Edit size={14} />} title="编辑" />
            </Link>
            <Button
              type="text"
              size="small"
              danger
              icon={<Trash2 size={14} />}
              onClick={() => handleDelete(record.id)}
              title="删除"
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
          频道
        </Title>
        <Link to="/channels/new">
          <Button type="primary" icon={<Plus size={14} />}>
            新建频道
          </Button>
        </Link>
      </div>

      <Table
        columns={columns}
        dataSource={channels}
        rowKey="id"
        loading={loading}
        locale={{
          emptyText: <Empty description="还没有频道，点击右上角新建" />,
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
