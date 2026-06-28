import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Plus, Trash2, Play } from 'lucide-react';
import { Table, Button, Space, Typography, App, Empty, Tag } from 'antd';
import type { TableColumnsType } from 'antd';
import { agentsApi } from '../api/agents';
import StatusBadge from '../components/StatusBadge';
import { timeAgo } from '../utils/format';
import type { Agent } from '../types';

const { Title } = Typography;

export default function Agents() {
  const { t } = useTranslation();
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
      title: t('agents.deleteConfirm'),
      content: t('agents.deleteWarning'),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await agentsApi.delete(id);
        if (r.success) {
          message.success(t('agents.deleted'));
          fetchItems();
        } else {
          message.error(r.error?.message || t('agents.deleteFailed'));
        }
      },
    });
  };

  const handleRun = async (id: string) => {
    const r = await agentsApi.run(id);
    if (r.success) message.success(t('agents.runTriggered'));
    else message.error(r.error?.message || t('agents.runFailed'));
  };

  const columns: TableColumnsType<Agent> = [
    {
      title: t('common.name'),
      dataIndex: 'name',
      key: 'name',
      render: (name: string, record) => <Link to={`/agents/${record.id}`}>{name}</Link>,
    },
    {
      title: t('agents.channel'),
      key: 'channel',
      render: (_, record) =>
        record.channel ? (
          <Link to={`/channels/${record.channel_id}`}>{record.channel.name}</Link>
        ) : (
          record.channel_id.slice(0, 8)
        ),
    },
    {
      title: t('agents.downloader'),
      key: 'downloader',
      render: (_, record) => record.downloader?.name || record.downloader_id?.slice(0, 8) || t('format.dash'),
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (status: string) => <StatusBadge status={status} />,
    },
    {
      title: t('agents.scope'),
      key: 'scope',
      width: 120,
      render: (_, record) =>
        record.scope_channel_wide ? (
          <Tag color="purple">{t('agents.channelWide')}</Tag>
        ) : (
          <Tag color="blue">{t('agents.worksCount', { n: record.works?.length || 0 })}</Tag>
        ),
    },
    {
      title: t('agents.conflictResolution'),
      dataIndex: 'conflict_resolution',
      key: 'conflict_resolution',
      width: 120,
      render: (v: string) => (v === 'auto' ? <Tag>{t('agents.auto')}</Tag> : <Tag color="gold">{t('agents.ask')}</Tag>),
    },
    {
      title: t('agents.llm'),
      key: 'llm',
      width: 80,
      render: (_, record) => (record.llm_enabled ? <Tag color="blue">{t('agents.on')}</Tag> : <Tag>{t('agents.off')}</Tag>),
    },
    {
      title: t('agents.lastRun'),
      dataIndex: 'last_run_at',
      key: 'last_run_at',
      width: 150,
      render: (val: string | null) => (val ? timeAgo(val) : t('common.never')),
    },
    {
      title: t('common.actions'),
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
            title={t('agents.runNow')}
          />
          <Button
            type="text"
            size="small"
            danger
            icon={<Trash2 size={14} />}
            onClick={() => handleDelete(record.id)}
            title={t('common.delete')}
          />
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>
          {t('agents.title')}
        </Title>
        <Link to="/agents/new">
          <Button type="primary" icon={<Plus size={14} />}>
            {t('agents.newAgent')}
          </Button>
        </Link>
      </div>

      <Table
        columns={columns}
        dataSource={items}
        rowKey="id"
        loading={loading}
        locale={{ emptyText: <Empty description={t('agents.noAgents')} /> }}
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
