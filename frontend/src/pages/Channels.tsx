import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { Plus, RefreshCw, Trash2 } from 'lucide-react';
import { Table, Button, Space, Typography, App, Empty } from 'antd';
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
  const { message, modal } = App.useApp();

  const fetchChannels = async () => {
    setLoading(true);
    const res = await channelsApi.list(page, 20);
    if (res.success) {
      setChannels(res.data);
      if (res.meta) setTotal(res.meta.total);
    }
    setLoading(false);
  };

  useEffect(() => { fetchChannels(); }, [page]);

  const handleDelete = (id: string) => {
    modal.confirm({
      title: 'Delete this channel?',
      content: 'This action cannot be undone.',
      okText: 'Delete',
      okButtonProps: { danger: true },
      onOk: async () => {
        await channelsApi.delete(id);
        message.success('Channel deleted');
        fetchChannels();
      },
    });
  };

  const handleFetch = async (id: string) => {
    await channelsApi.fetch(id);
    message.success('Fetch triggered');
  };

  const columns: TableColumnsType<Channel> = [
    {
      title: 'Name', dataIndex: 'name', key: 'name',
      render: (name: string, record: Channel) => (
        <Link to={`/channels/${record.id}`}>{name}</Link>
      ),
    },
    { title: 'Type', dataIndex: 'type', key: 'type' },
    {
      title: 'Status', dataIndex: 'status', key: 'status',
      render: (status: string) => <StatusBadge status={status} />,
    },
    {
      title: 'Last Fetched', dataIndex: 'last_fetched_at', key: 'last_fetched_at',
      render: (val: string | null) => (val ? timeAgo(val) : 'Never'),
    },
    {
      title: 'Actions', key: 'actions', align: 'right',
      render: (_, record) => (
        <Space>
          <Button type="text" size="small" icon={<RefreshCw size={16} />} onClick={() => handleFetch(record.id)} />
          <Button type="text" size="small" danger icon={<Trash2 size={16} />} onClick={() => handleDelete(record.id)} />
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>Channels</Title>
        <Link to="/channels/new">
          <Button type="primary" icon={<Plus size={16} />}>Create Channel</Button>
        </Link>
      </div>

      <Table
        columns={columns}
        dataSource={channels}
        rowKey="id"
        loading={loading}
        locale={{ emptyText: <Empty description="No channels yet. Create one to get started." /> }}
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
