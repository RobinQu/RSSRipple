import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { Plus, Trash2, Play } from 'lucide-react';
import { Table, Button, Space, Typography, App, Empty } from 'antd';
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

  useEffect(() => { fetchItems(); }, [page]);

  const handleDelete = (id: string) => {
    modal.confirm({
      title: 'Delete this agent?',
      content: 'This action cannot be undone.',
      okText: 'Delete',
      okButtonProps: { danger: true },
      onOk: async () => {
        await agentsApi.delete(id);
        message.success('Agent deleted');
        fetchItems();
      },
    });
  };

  const handleRun = async (id: string) => {
    await agentsApi.run(id);
    message.success('Agent run triggered');
  };

  const columns: TableColumnsType<Agent> = [
    {
      title: 'Name', dataIndex: 'name', key: 'name',
      render: (name: string, record: Agent) => (
        <Link to={`/agents/${record.id}`}>{name}</Link>
      ),
    },
    {
      title: 'Status', dataIndex: 'status', key: 'status',
      render: (status: string) => <StatusBadge status={status} />,
    },
    {
      title: 'Filters', key: 'filters',
      render: (_, record) => record.filters?.length || 0,
    },
    {
      title: 'Last Run', dataIndex: 'last_run_at', key: 'last_run_at',
      render: (val: string | null) => (val ? timeAgo(val) : 'Never'),
    },
    {
      title: 'LLM', key: 'llm',
      render: (_, record) => (record.llm_enabled ? 'On' : 'Off'),
    },
    {
      title: 'Actions', key: 'actions', align: 'right',
      render: (_, record) => (
        <Space>
          <Button type="text" size="small" icon={<Play size={16} />} onClick={() => handleRun(record.id)} />
          <Button type="text" size="small" danger icon={<Trash2 size={16} />} onClick={() => handleDelete(record.id)} />
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>Agents</Title>
        <Link to="/agents/new">
          <Button type="primary" icon={<Plus size={16} />}>Create Agent</Button>
        </Link>
      </div>

      <Table
        columns={columns}
        dataSource={items}
        rowKey="id"
        loading={loading}
        locale={{ emptyText: <Empty description="No agents created yet." /> }}
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
