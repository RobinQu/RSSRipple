import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
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
    else message.error(res.error?.message || '连接测试失败');
    fetchItems();
  };

  const handleDelete = (id: string) => {
    modal.confirm({
      title: '确定删除该下载器？',
      content: '关联的 Agent 将被暂停。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        const r = await downloadersApi.delete(id);
        if (r.success) {
          message.success('下载器已删除');
          fetchItems();
        } else message.error(r.error?.message || '删除失败');
      },
    });
  };

  const columns: TableColumnsType<DownloaderInstance> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      render: (name: string, r) => <Link to={`/downloaders/${r.id}`}>{name}</Link>,
    },
    { title: '类型', dataIndex: 'type', key: 'type', width: 120 },
    {
      title: 'URL',
      dataIndex: 'url',
      key: 'url',
      ellipsis: true,
    },
    {
      title: '下载目录',
      dataIndex: 'download_dir',
      key: 'download_dir',
      ellipsis: true,
      render: (v: string | null) => v || '—',
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (s: string) => (
        <Tag color={STATUS_COLOR[s] ?? 'default'}>{s.toUpperCase()}</Tag>
      ),
    },
    {
      title: '上次检查',
      dataIndex: 'last_checked_at',
      key: 'last_checked_at',
      width: 150,
      render: (v: string | null) => (v ? timeAgo(v) : '—'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 160,
      align: 'right',
      render: (_, record) => (
        <Space size={0}>
          <Button
            type="text"
            size="small"
            icon={<Zap size={14} />}
            title="测试连接"
            onClick={() => handleTest(record.id)}
          />
          <Link to={`/downloaders/${record.id}/edit`}>
            <Button type="text" size="small" icon={<Edit size={14} />} title="编辑" />
          </Link>
          <Button
            type="text"
            size="small"
            danger
            icon={<Trash2 size={14} />}
            title="删除"
            onClick={() => handleDelete(record.id)}
          />
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>下载器</Title>
        <Link to="/downloaders/new">
          <Button type="primary" icon={<Plus size={14} />}>添加下载器</Button>
        </Link>
      </div>

      <Table
        columns={columns}
        dataSource={items}
        rowKey="id"
        loading={loading}
        locale={{ emptyText: <Empty description="还没有下载器" /> }}
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
