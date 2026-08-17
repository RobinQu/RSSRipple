import { useState, useEffect, useCallback } from 'react';
import { useParams, Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useDocumentTitle from '../hooks/useDocumentTitle';
import {
  Typography,
  Card,
  Descriptions,
  Button,
  Space,
  Table,
  Progress,
  Spin,
  App,
  Alert,
  Tooltip,
} from 'antd';
import type { TableColumnsType } from 'antd';
import type { ReactNode } from 'react';
import { withMobileLabels } from '../utils/table';
import {
  Edit,
  Zap,
  RefreshCw,
  ArrowDown,
  ArrowUp,
  Clock,
  Pause,
  Loader,
} from 'lucide-react';
import { downloadersApi } from '../api/downloaders';
import type { DownloaderInstance, DownloadTask, TorrentInfo } from '../types';
import { formatBytes, formatSpeed, formatEta, timeAgo } from '../utils/format';
import StatusBadge from '../components/StatusBadge';
import EllipsisText from '../components/EllipsisText';
import TaskStatusIcon from '../components/TaskStatusIcon';

const { Title, Text } = Typography;

// Transmission status → icon-only mapping; the text label moves to a tooltip
// so the column can shrink to icon width (same pattern as the agent task list).
const TORRENT_STATUS_ICON: Record<string, { icon: ReactNode; color: string }> = {
  downloading: { icon: <ArrowDown size={15} />, color: 'var(--rr-primary)' },
  'download pending': { icon: <Clock size={15} />, color: 'var(--rr-text-secondary)' },
  checking: { icon: <Loader size={15} />, color: 'var(--rr-primary)' },
  'check pending': { icon: <Clock size={15} />, color: 'var(--rr-text-secondary)' },
  seeding: { icon: <ArrowUp size={15} />, color: 'var(--rr-success)' },
  'seed pending': { icon: <Clock size={15} />, color: 'var(--rr-text-secondary)' },
  stopped: { icon: <Pause size={15} />, color: 'var(--rr-warning)' },
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
  useDocumentTitle(dl?.name ?? t('downloaders.title'));
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
  }, [id, t]);

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
    if (res.success && res.data?.success !== false) {
      const freeSpace = res.data.free_space != null ? `, ${formatBytes(res.data.free_space)}` : '';
      message.success(res.data.message || `${t('downloaders.connectionSuccess')}${freeSpace}`);
    } else {
      message.error(res.error?.message || res.data?.message || t('downloaders.connectionFailed'));
    }
    fetchDl();
    fetchTorrents();
  };

  const torrentColumns: TableColumnsType<TorrentInfo> = [
    {
      title: t('common.name'),
      dataIndex: 'name',
      key: 'name',
      // No fixed width: the name flexes to take whatever the compact columns
      // leave.
      render: (name: string, t) => <EllipsisText text={name} danger={t.error > 0} />,
    },
    {
      title: t('common.directory'),
      dataIndex: 'download_dir',
      key: 'download_dir',
      width: 150,
      ellipsis: true,
      render: (v: string | null) => <Text type="secondary">{v || t('format.dash')}</Text>,
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 56,
      align: 'center',
      render: (s: string) => {
        const conf = TORRENT_STATUS_ICON[s] ?? TORRENT_STATUS_ICON.stopped;
        return (
          <Tooltip title={s}>
            <span style={{ color: conf.color, display: 'inline-flex' }}>{conf.icon}</span>
          </Tooltip>
        );
      },
    },
    {
      title: t('common.progress'),
      dataIndex: 'percent_done',
      key: 'percent_done',
      width: 180,
      render: (p: number, t) => (
        <Progress
          percent={Math.min(100, Math.max(0, p * 100))}
          size="small"
          format={(v) => `${v?.toFixed(2)}%`}
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
      // Combined transfer info: down/up speeds on the first line, ETA and
      // total size on the second — replaces four separate narrow columns.
      title: t('downloaders.transferInfo'),
      key: 'transfer',
      width: 200,
      render: (_, tor) => (
        <div style={{ fontSize: 12, lineHeight: '18px', fontVariantNumeric: 'tabular-nums' }}>
          <Space size={8}>
            <span>
              <ArrowDown size={11} style={{ verticalAlign: -1 }} />{' '}
              {tor.rate_download > 0 ? formatSpeed(tor.rate_download) : t('format.dash')}
            </span>
            <span>
              <ArrowUp size={11} style={{ verticalAlign: -1 }} />{' '}
              {tor.rate_upload > 0 ? formatSpeed(tor.rate_upload) : t('format.dash')}
            </span>
          </Space>
          <div>
            <Text type="secondary" style={{ fontSize: 11 }}>
              ETA {formatEta(tor.eta_seconds)} · {formatBytes(tor.total_size)}
            </Text>
          </div>
        </div>
      ),
    },
  ];

  const taskColumns: TableColumnsType<DownloadTask> = [
    {
      title: t('common.title'),
      key: 'title',
      // Flexes to take the remaining width (same as the torrent name column).
      render: (_, r) => (
        <EllipsisText text={r.file_resource?.title_raw || r.file_resource_id.slice(0, 8)} />
      ),
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 56,
      align: 'center',
      render: (s: string) => <TaskStatusIcon status={s} />,
    },
    {
      title: t('common.progress'),
      dataIndex: 'progress',
      key: 'progress',
      width: 200,
      // Progress bar on top, live speed + ETA stacked below while running —
      // the separate speed column is folded into this one.
      render: (p: number, record) => (
        <div>
          <Progress
            percent={Math.min(100, Math.max(0, p * 100))}
            size="small"
            format={(v) => `${v?.toFixed(2)}%`}
          />
          <div style={{ marginTop: 2 }}>
            {['pending', 'queued', 'downloading'].includes(record.status) ? (
              <Text type="secondary" style={{ fontSize: 11 }}>
                ↓{formatSpeed(record.download_speed)} · ETA {formatEta(record.eta)}
              </Text>
            ) : (
              <Text type="secondary" style={{ fontSize: 11 }}>—</Text>
            )}
          </div>
        </div>
      ),
    },
  ];

  if (loadingDl) return <Spin />;
  if (!dl) return <Text type="danger">{t('downloaders.notFound')}</Text>;

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 8, marginBottom: 24 }}>
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
          className="stack-table"
          columns={withMobileLabels(torrentColumns)}
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
        className="stack-table"
        columns={withMobileLabels(taskColumns)}
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
