import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, App, Button, Card, Descriptions, Drawer, Select, Space, Table, Tag, Tooltip, Typography } from 'antd';
import type { TableColumnsType } from 'antd';
import { Eye, RefreshCw, RotateCcw } from 'lucide-react';
import { appLogsApi, type AppLogItem } from '../api/appLogs';
import { formatDate, timeAgo } from '../utils/format';

const { Text } = Typography;
const PAGE_SIZE = 20;

export default function AppLogsPanel() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [items, setItems] = useState<AppLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [category, setCategory] = useState<string>();
  const [status, setStatus] = useState<string>('failed');
  const [loading, setLoading] = useState(true);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  const [selectedLog, setSelectedLog] = useState<AppLogItem | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await appLogsApi.list({ page, pageSize: PAGE_SIZE, category, status });
      if (response.success && response.data) {
        setItems(response.data);
        setTotal(response.meta?.total ?? 0);
      } else {
        message.error(response.error?.message || t('settings.logs.loadFailed'));
      }
    } finally {
      setLoading(false);
    }
  }, [category, message, page, status, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const retry = async (row: AppLogItem) => {
    setRetryingId(row.id);
    try {
      const response = await appLogsApi.retry(row.id);
      if (response.success) {
        message.success(t('settings.logs.retryAccepted'));
        await load();
      } else {
        message.error(response.error?.message || t('settings.logs.retryFailed'));
      }
    } finally {
      setRetryingId(null);
    }
  };

  const columns: TableColumnsType<AppLogItem> = [
    {
      title: t('settings.logs.colTime'),
      dataIndex: 'updated_at',
      width: 120,
      render: (value: string) => (
        <Tooltip title={formatDate(value, 'yyyy-MM-dd HH:mm:ss')}>
          <Text type="secondary">{timeAgo(value)}</Text>
        </Tooltip>
      ),
    },
    {
      title: t('settings.logs.colCategory'),
      dataIndex: 'category',
      width: 110,
      render: (value: string) => <Tag>{t(`settings.logs.category_${value}`)}</Tag>,
    },
    {
      title: t('settings.logs.colResource'),
      dataIndex: 'resource_title',
      ellipsis: true,
      render: (value: string | null, row) => (
        <div>
          <Tooltip title={value}><Text>{value || '—'}</Text></Tooltip>
          {row.channel_name && <div><Text type="secondary" style={{ fontSize: 12 }}>{row.channel_name}</Text></div>}
        </div>
      ),
    },
    {
      title: t('settings.logs.colError'),
      dataIndex: 'message',
      ellipsis: true,
      render: (value: string) => <Tooltip title={value}><Text type="secondary">{value}</Text></Tooltip>,
    },
    {
      title: t('settings.logs.colAttempts'),
      dataIndex: 'attempts',
      width: 80,
      render: (value: number) => <Text>{value}</Text>,
    },
    {
      title: t('settings.logs.colStatus'),
      dataIndex: 'status',
      width: 100,
      render: (value: string) => (
        <Tag color={value === 'resolved' ? 'green' : value === 'retrying' ? 'processing' : 'red'}>
          {t(`settings.logs.status_${value}`)}
        </Tag>
      ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 190,
      render: (_, row) => (
        <Space size={4}>
          <Button size="small" icon={<Eye size={13} />} onClick={() => setSelectedLog(row)}>
            {t('settings.logs.details')}
          </Button>
          <Button
            size="small"
            icon={<RotateCcw size={13} />}
            loading={retryingId === row.id}
            disabled={row.status !== 'failed'}
            onClick={() => void retry(row)}
          >
            {t('settings.logs.retry')}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <>
    <Card
      size="small"
      title={t('settings.logs.title')}
      extra={(
        <Button icon={<RefreshCw size={14} />} onClick={() => void load()} loading={loading}>
          {t('common.refresh')}
        </Button>
      )}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={t('settings.logs.retention')}
        description={t('settings.logs.desc')}
      />
      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          allowClear
          placeholder={t('settings.logs.allCategories')}
          value={category}
          onChange={(value) => { setCategory(value); setPage(1); }}
          style={{ width: 180 }}
          options={['metadata', 'torrent', 'magnet', 'listing'].map((value) => ({
            value, label: t(`settings.logs.category_${value}`),
          }))}
        />
        <Select
          value={status}
          onChange={(value) => { setStatus(value); setPage(1); }}
          style={{ width: 150 }}
          options={['failed', 'retrying', 'resolved'].map((value) => ({
            value, label: t(`settings.logs.status_${value}`),
          }))}
        />
      </Space>
      <Table
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={items}
        scroll={{ x: 1100 }}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total,
          showTotal: (count) => t('settings.logs.total', { count }),
          onChange: setPage,
        }}
      />
    </Card>
    <Drawer
      width={680}
      open={selectedLog !== null}
      onClose={() => setSelectedLog(null)}
      title={selectedLog ? `${t(`settings.logs.category_${selectedLog.category}`)} / ${selectedLog.resource_title || selectedLog.id}` : ''}
    >
      {selectedLog && (
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <Alert
            type={selectedLog.status === 'resolved' ? 'success' : 'error'}
            message={t('settings.logs.rootCause')}
            description={<Text style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{selectedLog.message}</Text>}
          />
          <Descriptions
            bordered
            size="small"
            column={1}
            items={[
              { key: 'attempts', label: t('settings.logs.colAttempts'), children: selectedLog.attempts },
              { key: 'status', label: t('settings.logs.colStatus'), children: t(`settings.logs.status_${selectedLog.status}`) },
              { key: 'retry_count', label: t('settings.logs.retry'), children: selectedLog.retry_count },
              { key: 'updated', label: t('settings.logs.colTime'), children: formatDate(selectedLog.updated_at, 'yyyy-MM-dd HH:mm:ss') },
            ]}
          />
          {selectedLog.resource ? (
            <Descriptions
              title={t('settings.logs.resourceDetails')}
              bordered
              size="small"
              column={1}
              items={[
                { key: 'search', label: t('settings.logs.searchTitle'), children: selectedLog.resource.search_title || '—' },
                { key: 'raw', label: t('settings.logs.rawTitle'), children: <Text style={{ whiteSpace: 'pre-wrap' }}>{selectedLog.resource.title_raw}</Text> },
                { key: 'work', label: t('settings.logs.workTitle'), children: selectedLog.resource.work_title || '—' },
                { key: 'guid', label: 'GUID', children: <Text style={{ wordBreak: 'break-all' }}>{selectedLog.resource.guid}</Text> },
                { key: 'id', label: t('settings.logs.resourceId'), children: selectedLog.resource.id },
                {
                  key: 'episode',
                  label: t('settings.logs.seasonEpisode'),
                  children: `S${selectedLog.resource.season ?? '—'} / E${selectedLog.resource.episode ?? '—'} / absolute ${selectedLog.resource.absolute_episode ?? '—'}`,
                },
                {
                  key: 'batch',
                  label: t('settings.logs.batch'),
                  children: selectedLog.resource.is_batch ? selectedLog.resource.batch_scope || '—' : '—',
                },
                {
                  key: 'metadata',
                  label: t('settings.logs.category_metadata'),
                  children: `${selectedLog.resource.metadata_failure_type || '—'} (${selectedLog.resource.metadata_attempts})`,
                },
                {
                  key: 'torrent',
                  label: t('settings.logs.category_torrent'),
                  children: `${selectedLog.resource.torrent_fetch_status || '—'} ${selectedLog.resource.torrent_fetch_error || ''}`,
                },
                {
                  key: 'magnet',
                  label: t('settings.logs.category_magnet'),
                  children: `${selectedLog.resource.magnet_resolve_status || '—'} ${selectedLog.resource.magnet_resolve_error || ''}`,
                },
                {
                  key: 'listing',
                  label: t('settings.logs.category_listing'),
                  children: `${selectedLog.resource.listing_reconcile_status || '—'} ${selectedLog.resource.listing_reconcile_error || ''}`,
                },
                ...(selectedLog.resource.detail_url ? [{
                  key: 'url',
                  label: t('settings.logs.detailUrl'),
                  children: /^https?:\/\//.test(selectedLog.resource.detail_url)
                    ? <a href={selectedLog.resource.detail_url} target="_blank" rel="noreferrer">{selectedLog.resource.detail_url}</a>
                    : <Text>{selectedLog.resource.detail_url}</Text>,
                }] : []),
              ]}
            />
          ) : (
            <Text type="secondary">{t('settings.logs.noResource')}</Text>
          )}
        </Space>
      )}
    </Drawer>
    </>
  );
}
