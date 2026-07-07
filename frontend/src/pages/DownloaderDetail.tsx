import { useState, useEffect, useCallback } from 'react';
import { useParams, Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  Typography,
  Card,
  Descriptions,
  Button,
  Space,
  Table,
  Progress,
  Tag,
  Spin,
  App,
  Alert,
} from 'antd';
import type { TableColumnsType } from 'antd';
import { Edit, Zap, RefreshCw, ArrowDown, ArrowUp } from 'lucide-react';
import { downloadersApi } from '../api/downloaders';
import type { DownloaderInstance, DownloadTask, TorrentInfo } from '../types';
import { formatBytes, formatSpeed, formatEta, timeAgo } from '../utils/format';
import StatusBadge from '../components/StatusBadge';

const { Title, Text } = Typography;

const STATUS_COLOR: Record<string, string> = {
  stopped: 'default',
  'check pending': 'warning',
  checking: 'processing',
  'download pending': 'warning',
  downloading: 'blue',
  'seed pending': 'warning',
  seeding: 'success',
};

const ACTIVE_STATUSES = new Set([
  'downloading',
  'seeding',
  'checking',
  'check pending',
  'download pending',
  'seed pending',
]);

export default function DownloaderDetail() {
  const { id } = useParams<{ id: string }>();
  const { t } = useTranslation();
  const { message } = App.useApp();

  const [dl, setDl] = useState<DownloaderInstance | null>(null);
  const [torrents, setTorrents] = useState<TorrentInfo[]>([]);
  const [loadingDl, setLoadingDl] = useState(true);
  const [loadingTorrents, setLoadingTorrents] = useState(true);
  const [torrentError, setTorrentError] = useState<string | null>(null);
  const [tasks, setTasks] = useState<DownloadTask[]>([]);
  const [taskPage, setTaskPage] = useState(1);
  const [taskTotal, setTaskTotal] = useState(0);

  const fetchDl = useCallback(async () => {
    if (!id) return;
    const res = await downloadersApi.get(id);
    if (res.success) setDl(res.data);
    setLoadingDl(false);
  }, [id]);

  const fetchTorrents = useCallback(async () => {
    if (!id) return;
    setLoadingTorrents(true);
    const res = await downloadersApi.listTorrents(id);
    if (res.success) {
      setTorrents(res.data);
      setTorrentError(null);
    } else {
      setTorrentError(res.error?.message ?? t('downloaders.transmissionUnreachable'));
    }
    setLoadingTorrents(false);
  }, [id]);

  const fetchTasks = useCallback(async () => {
    if (!id) return;
    const res = await downloadersApi.listTasks(id, taskPage, 20);
    if (res.success) {
      setTasks(res.data);
      if (res.meta) setTaskTotal(res.meta.total);
    }
  }, [id, taskPage]);

  useEffect(() => {
    fetchDl();
  }, [fetchDl]);
  useEffect(() => {
    fetchTorrents();
  }, [fetchTorrents]);
  useEffect(() => {
    fetchTasks();
  }, [fetchTasks]);

  useEffect(() => {
    const hasActive = torrents.some((t) => ACTIVE_STATUSES.has(t.status));
    if (!hasActive) return;
    const timer = setInterval(fetchTorrents, 3000);
    return () => clearInterval(timer);
  }, [torrents, fetchTorrents]);

  const handleTest = async () => {
    if (!id) return;
    const res = await downloadersApi.test(id);
    if (res.success) {
      const freeSpace = res.data.free_space != null ? `, ${formatBytes(res.data.free_space)}` : '';
      message.success(res.data.message || `${t('downloaders.connectionSuccess')}${freeSpace}`);
    } else {
      message.error(res.error?.message || t('downloaders.connectionFailed'));
    }
    fetchDl();
    fetchTorrents();
  };

  const torrentColumns: TableColumnsType<TorrentInfo> = [
    {
      title: t('common.name'),
      dataIndex: 'name',
      key: 'name',
      ellipsis: true,
      render: (name: string, t) =>
        t.error > 0 ? (
          <Text type="danger">{name}</Text>
        ) : (
          name
        ),
    },
    {
      title: t('common.directory'),
      dataIndex: 'download_dir',
      key: 'download_dir',
      ellipsis: true,
      render: (v: string | null) => <Text type="secondary">{v || t('format.dash')}</Text>,
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 130,
      render: (s: string) => <Tag color={STATUS_COLOR[s] ?? 'default'}>{s}</Tag>,
    },
    {
      title: t('common.progress'),
      dataIndex: 'percent_done',
      key: 'percent_done',
      width: 160,
      render: (p: number, t) => (
        <Progress
          percent={Math.round(p * 100)}
          size="small"
          status={
            t.error > 0
              ? 'exception'
              : t.is_finished
              ? 'success'
              : t.status === 'downloading'
              ? 'active'
              : 'normal'
          }
          style={{ marginBottom: 0 }}
        />
      ),
    },
    {
      title: () => <Space size={4}><ArrowDown size={13} />{t('downloaders.download')}</Space>,
      dataIndex: 'rate_download',
      key: 'rate_download',
      width: 100,
      render: (v: number) =>
        v > 0 ? <span style={{ fontVariantNumeric: 'tabular-nums' }}>{formatSpeed(v)}</span> : <Text type="secondary">{t('format.dash')}</Text>,
    },
    {
      title: () => <Space size={4}><ArrowUp size={13} />{t('downloaders.upload')}</Space>,
      dataIndex: 'rate_upload',
      key: 'rate_upload',
      width: 100,
      render: (v: number) =>
        v > 0 ? <span style={{ fontVariantNumeric: 'tabular-nums' }}>{formatSpeed(v)}</span> : <Text type="secondary">{t('format.dash')}</Text>,
    },
    {
      title: t('downloaders.eta'),
      dataIndex: 'eta_seconds',
      key: 'eta',
      width: 80,
      render: (v: number | null) => <Text type="secondary">{formatEta(v)}</Text>,
    },
    {
      title: t('downloaders.size'),
      dataIndex: 'total_size',
      key: 'total_size',
      width: 90,
      render: (v: number) => <Text type="secondary">{formatBytes(v)}</Text>,
    },
  ];

  const taskColumns: TableColumnsType<DownloadTask> = [
    {
      title: t('common.title'),
      key: 'title',
      ellipsis: true,
      render: (_, r) => (
        <Text ellipsis>{r.file_resource?.title_raw || r.file_resource_id.slice(0, 8)}</Text>
      ),
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (s: string) => <StatusBadge status={s} />,
    },
    {
      title: t('common.progress'),
      dataIndex: 'progress',
      key: 'progress',
      width: 180,
      render: (p: number) => <Progress percent={Math.round(p * 100)} size="small" />,
    },
    { title: t('common.speed'), dataIndex: 'download_speed', key: 'speed', width: 110, render: (v: number) => formatSpeed(v) },
  ];

  if (loadingDl) return <Spin />;
  if (!dl) return <Text type="danger">{t('downloaders.notFound')}</Text>;

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 24 }}>
        <div>
          <Title level={3} style={{ margin: 0 }}>{dl.name}</Title>
          <Text type="secondary">{dl.type}</Text>
        </div>
        <Space>
          <Button icon={<RefreshCw size={14} />} onClick={fetchTorrents} loading={loadingTorrents}>
            {t('common.refresh')}
          </Button>
          <Button icon={<Zap size={14} />} onClick={handleTest}>
            {t('downloaders.testConnection')}
          </Button>
          <Link to={`/downloaders/${id}/edit`}>
            <Button type="primary" icon={<Edit size={14} />}>{t('common.edit')}</Button>
          </Link>
        </Space>
      </div>

      <Card style={{ marginBottom: 24 }}>
        <Descriptions column={2} size="small">
          <Descriptions.Item label={t('common.url')}>{dl.url}</Descriptions.Item>
          <Descriptions.Item label={t('downloaders.defaultDir')}>{dl.download_dir}</Descriptions.Item>
          <Descriptions.Item label={t('common.status')}>
            <StatusBadge status={dl.status} />
          </Descriptions.Item>
          <Descriptions.Item label={t('downloaders.lastCheck')}>
            {dl.last_checked_at ? timeAgo(dl.last_checked_at) : t('format.dash')}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      <Title level={4} style={{ marginBottom: 12 }}>
        {t('downloaders.transmissionTorrents')}
        <Text type="secondary" style={{ fontSize: 14, fontWeight: 'normal', marginLeft: 8 }}>
          ({torrents.length})
        </Text>
      </Title>

      {torrentError ? (
        <Alert type="error" message={t('downloaders.transmissionUnreachable')} description={torrentError} showIcon style={{ marginBottom: 16 }} />
      ) : (
        <Table
          columns={torrentColumns}
          dataSource={torrents}
          rowKey="id"
          loading={loadingTorrents}
          size="small"
          pagination={torrents.length > 20 ? { pageSize: 20, showSizeChanger: false } : false}
          locale={{ emptyText: t('downloaders.noTransmissionTorrents') }}
          style={{ marginBottom: 24 }}
        />
      )}

      <Title level={4} style={{ marginBottom: 12 }}>{t('downloaders.localTasks')}</Title>
      <Table
        columns={taskColumns}
        dataSource={tasks}
        rowKey="id"
        size="small"
        pagination={{
          current: taskPage,
          pageSize: 20,
          total: taskTotal,
          onChange: setTaskPage,
          showSizeChanger: false,
        }}
        locale={{ emptyText: t('common.noData') }}
      />
    </div>
  );
}
