import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { Plus, Trash2, Zap } from 'lucide-react';
import { Table, Button, Space, Typography, App, Empty } from 'antd';
import type { TableColumnsType } from 'antd';
import { downloadersApi } from '../api/downloaders';
import StatusBadge from '../components/StatusBadge';
import type { DownloaderInstance } from '../types';

const { Title } = Typography;

export default function Downloaders() {
  const [items, setItems] = useState<DownloaderInstance[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const { message, modal } = App.useApp();

  const fetchItems = async () => {
    setLoading(true);
    const res = await downloadersApi.list(page, 20);
    if (res.success) {
      setItems(res.data);
      if (res.meta) setTotal(res.meta.total);
    }
    setLoading(false);
  };

  useEffect(() => { fetchItems(); }, [page]);

  const handleTest = async (id: string) => {
    const res = await downloadersApi.test(id);
    if (res.success) {
      message.success(res.data.message);
    } else {
      message.error(res.error?.message || 'Connection test failed');
    }
  };

  const handleDelete = (id: string) => {
    modal.confirm({
      title: 'Delete this downloader?',
      content: 'This action cannot be undone.',
      okText: 'Delete',
      okButtonProps: { danger: true },
      onOk: async () => {
        await downloadersApi.delete(id);
        message.success('Downloader deleted');
        fetchItems();
      },
    });
  };

  const columns: TableColumnsType<DownloaderInstance> = [
    { title: 'Name', dataIndex: 'name', key: 'name' },
    { title: 'Type', dataIndex: 'type', key: 'type' },
    {
      title: 'URL', dataIndex: 'url', key: 'url',
      render: (url: string) => (
        <span style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'inline-block' }}>{url}</span>
      ),
    },
    {
      title: 'Download Dir', dataIndex: 'download_dir', key: 'download_dir',
      render: (val: string | null) => val || '—',
    },
    {
      title: 'Status', dataIndex: 'status', key: 'status',
      render: (status: string) => <StatusBadge status={status} />,
    },
    {
      title: 'Actions', key: 'actions', align: 'right',
      render: (_, record) => (
        <Space>
          <Button type="text" size="small" icon={<Zap size={16} />} onClick={() => handleTest(record.id)} />
          <Button type="text" size="small" danger icon={<Trash2 size={16} />} onClick={() => handleDelete(record.id)} />
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>Downloaders</Title>
        <Link to="/downloaders/new">
          <Button type="primary" icon={<Plus size={16} />}>Add Downloader</Button>
        </Link>
      </div>

      <Table
        columns={columns}
        dataSource={items}
        rowKey="id"
        loading={loading}
        locale={{ emptyText: <Empty description="No downloaders configured." /> }}
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
