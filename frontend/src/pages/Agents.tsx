import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { Plus, Trash2, Play } from 'lucide-react';
import { Table, Button, Space, Typography, App, Empty, Tag } from 'antd';
import type { TableColumnsType } from 'antd';
import { agentsApi } from '../api/agents';
import StatusBadge from '../components/StatusBadge';
import { timeAgo } from '../utils/format';
import type { Agent } from '../types';

const { Title } = Typography;

export default function Agents() {
  const [items, setItems] = useState<Agent[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const { message, modal } = App.useApp();

  const fetchItems = async () => {
    setLoading(true);
    const res = await agentsApi.list(page, 20);
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

  const handleDelete = (id: string) => {
    modal.confirm({
      title: '确定删除该 Agent？',
      content: '相关下载任务将被标记为取消，此操作不可撤销。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        const r = await agentsApi.delete(id);
        if (r.success) {
          message.success('Agent 已删除');
          fetchItems();
        } else {
          message.error(r.error?.message || '删除失败');
        }
      },
    });
  };

  const handleRun = async (id: string) => {
    const r = await agentsApi.run(id);
    if (r.success) message.success('已触发运行');
    else message.error(r.error?.message || '触发失败');
  };

  const columns: TableColumnsType<Agent> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      render: (name: string, record) => <Link to={`/agents/${record.id}`}>{name}</Link>,
    },
    {
      title: '频道',
      key: 'channel',
      render: (_, record) =>
        record.channel ? (
          <Link to={`/channels/${record.channel_id}`}>{record.channel.name}</Link>
        ) : (
          record.channel_id.slice(0, 8)
        ),
    },
    {
      title: '下载器',
      key: 'downloader',
      render: (_, record) => record.downloader?.name || record.downloader_id?.slice(0, 8) || '—',
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (status: string) => <StatusBadge status={status} />,
    },
    {
      title: '范围',
      key: 'scope',
      width: 120,
      render: (_, record) =>
        record.scope_channel_wide ? (
          <Tag color="purple">整个频道</Tag>
        ) : (
          <Tag color="blue">{record.works?.length || 0} 作品</Tag>
        ),
    },
    {
      title: '冲突处理',
      dataIndex: 'conflict_resolution',
      key: 'conflict_resolution',
      width: 120,
      render: (v: string) => (v === 'auto' ? <Tag>自动</Tag> : <Tag color="gold">询问</Tag>),
    },
    {
      title: 'LLM',
      key: 'llm',
      width: 80,
      render: (_, record) => (record.llm_enabled ? <Tag color="blue">开</Tag> : <Tag>关</Tag>),
    },
    {
      title: '上次运行',
      dataIndex: 'last_run_at',
      key: 'last_run_at',
      width: 150,
      render: (val: string | null) => (val ? timeAgo(val) : '从未'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 140,
      align: 'right',
      render: (_, record) => (
        <Space size={4}>
          <Button
            type="text"
            size="small"
            icon={<Play size={14} />}
            onClick={() => handleRun(record.id)}
            title="立即运行"
          />
          <Button
            type="text"
            size="small"
            danger
            icon={<Trash2 size={14} />}
            onClick={() => handleDelete(record.id)}
            title="删除"
          />
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>
          Agents
        </Title>
        <Link to="/agents/new">
          <Button type="primary" icon={<Plus size={14} />}>
            新建 Agent
          </Button>
        </Link>
      </div>

      <Table
        columns={columns}
        dataSource={items}
        rowKey="id"
        loading={loading}
        locale={{ emptyText: <Empty description="还没有 Agent" /> }}
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
