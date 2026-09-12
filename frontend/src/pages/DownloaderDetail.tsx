import { useState, useEffect, useCallback, useMemo } from 'react';
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
  Tabs,
  Tag,
  Progress,
  Spin,
  App,
  Alert,
} from 'antd';
import type { TableColumnsType } from 'antd';
import { withMobileLabels } from '../utils/table';
import {
  Edit,
  Zap,
  RefreshCw,
  ArrowDown,
  ArrowUp,
} from 'lucide-react';
import { downloadersApi } from '../api/downloaders';
import type { DownloaderInstance, DownloadTask, FileResource, TorrentInfo } from '../types';
import { formatBytes, formatDate, formatSpeed, formatEta, timeAgo } from '../utils/format';
import StatusBadge from '../components/StatusBadge';
import EllipsisText from '../components/EllipsisText';
import ColumnSettings from '../components/ColumnSettings';
import SortSettings, { type SortEntry, type SortField } from '../components/SortSettings';
import ResourceDetailDrawer from '../components/ResourceDetailDrawer';
import {
  RequiredFieldCell,
  WorkInfoIcon,
  WorkTypeTags,
} from '../components/resourceCells';
import { posterUrl, useDefaultPoster } from '../utils/poster';
import { seasonLabel } from '../utils/season';
import { ACTIVE_TORRENT_STATUSES, TORRENT_STATUS_TAG_COLORS } from '../utils/torrent';
import {
  DOWNLOAD_STATUS_TAG_COLORS,
  TASK_COLUMN_POOL,
  loadColumnConfig,
  requiredFieldWidth,
  resolveVisibleColumns,
  saveColumnConfig,
  taskColumnStorageKey,
  torrentColumnStorageKey,
  type ChannelColumnConfig,
} from '../utils/requiredFields';

import {
  loadSortEntries,
  saveSortEntries,
  serializeSortEntries,
  taskSortStorageKey,
  torrentSortStorageKey,
} from '../utils/sortSettings';

const { Title, Text } = Typography;

// Local-task table: default-visible metadata columns (the must-show fields —
// work title (with type/batch tags folded into its second line), status and
// progress — are fixed columns outside the configurable pool). ``created_at``
// is a task-level column (the task's enqueue time), not a resource catalog
// field; it renders from the task row.
const DEFAULT_TASK_COLUMNS: string[] = [
  'year',
  'season',
  'episode',
  'episode_start',
  'episode_end',
  'resolution',
  'subtitle_group',
  'file_size',
  'created_at',
];

// Task-table column pool: the resource metadata catalog plus the task's own
// enqueue time.
const TASK_POOL: readonly string[] = [...TASK_COLUMN_POOL, 'created_at'];

// Sortable task fields; the default puts unfinished tasks first, then the
// most recently enqueued.
const TASK_SORTABLE_KEYS = ['status', 'created_at', 'progress', 'title'] as const;
const DEFAULT_TASK_SORTS: SortEntry[] = [
  { key: 'status', dir: 'asc' },
  { key: 'created_at', dir: 'desc' },
];

// Transmission torrent table: same sort-option design as the local tasks
// (active torrents first, then most recently added), applied client-side —
// the RPC list arrives unpaginated and unsorted.
const TORRENT_SORTABLE_KEYS = ['tstatus', 'added_date', 'progress', 'name'] as const;
const DEFAULT_TORRENT_SORTS: SortEntry[] = [
  { key: 'tstatus', dir: 'asc' },
  { key: 'added_date', dir: 'desc' },
];

// Torrent-table optional columns (the torrent name — with the raw status tag
// folded into it — is the fixed must-show column locked to the left); all are
// visible by default.
const TORRENT_COLUMN_POOL = ['added_date', 'download_dir', 'progress', 'transfer'] as const;
const DEFAULT_TORRENT_COLUMNS: string[] = [...TORRENT_COLUMN_POOL];

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
  const [loadingTasks, setLoadingTasks] = useState(true);
  const [taskPage, setTaskPage] = useState(1);
  const [taskTotal, setTaskTotal] = useState(0);
  // Task-table column configuration (metadata pool only — work title,
  // type/tags, status and progress are fixed must-show columns), persisted
  // per downloader in localStorage.
  const [columnCfg, setColumnCfg] = useState<ChannelColumnConfig | null>(null);
  const [sorts, setSorts] = useState<SortEntry[]>(DEFAULT_TASK_SORTS);
  const [torrentColumnCfg, setTorrentColumnCfg] = useState<ChannelColumnConfig | null>(null);
  const [torrentSorts, setTorrentSorts] = useState<SortEntry[]>(DEFAULT_TORRENT_SORTS);
  const [selectedResource, setSelectedResource] = useState<FileResource | null>(null);

  useEffect(() => {
    setColumnCfg(id ? loadColumnConfig(taskColumnStorageKey(id)) : null);
    setSorts((id && loadSortEntries(taskSortStorageKey(id))) || DEFAULT_TASK_SORTS);
    setTorrentColumnCfg(id ? loadColumnConfig(torrentColumnStorageKey(id)) : null);
    setTorrentSorts((id && loadSortEntries(torrentSortStorageKey(id))) || DEFAULT_TORRENT_SORTS);
  }, [id]);
  const visibleMetaColumns = useMemo(
    () => resolveVisibleColumns(columnCfg, DEFAULT_TASK_COLUMNS, TASK_POOL),
    [columnCfg],
  );
  const visibleTorrentColumns = useMemo(
    () => resolveVisibleColumns(torrentColumnCfg, DEFAULT_TORRENT_COLUMNS, TORRENT_COLUMN_POOL),
    [torrentColumnCfg],
  );
  const handleColumnsChange = useCallback(
    (next: ChannelColumnConfig | null) => {
      setColumnCfg(next);
      if (id) saveColumnConfig(taskColumnStorageKey(id), next);
    },
    [id],
  );
  const handleTorrentColumnsChange = useCallback(
    (next: ChannelColumnConfig | null) => {
      setTorrentColumnCfg(next);
      if (id) saveColumnConfig(torrentColumnStorageKey(id), next);
    },
    [id],
  );
  const handleSortsChange = useCallback(
    (next: SortEntry[] | null) => {
      setSorts(next ?? DEFAULT_TASK_SORTS);
      setTaskPage(1);
      if (id) saveSortEntries(taskSortStorageKey(id), next);
    },
    [id],
  );
  const handleTorrentSortsChange = useCallback(
    (next: SortEntry[] | null) => {
      setTorrentSorts(next ?? DEFAULT_TORRENT_SORTS);
      if (id) saveSortEntries(torrentSortStorageKey(id), next);
    },
    [id],
  );
  const sortFields: SortField[] = useMemo(
    () =>
      TASK_SORTABLE_KEYS.map((key) => ({
        key,
        label: t(`downloaders.sortField_${key}`),
        ascLabel: t(`downloaders.sortDir_${key}_asc`),
        descLabel: t(`downloaders.sortDir_${key}_desc`),
      })),
    [t],
  );
  const torrentSortFields: SortField[] = useMemo(
    () =>
      TORRENT_SORTABLE_KEYS.map((key) => ({
        key,
        label: t(`downloaders.sortField_${key}`),
        ascLabel: t(`downloaders.sortDir_${key}_asc`),
        descLabel: t(`downloaders.sortDir_${key}_desc`),
      })),
    [t],
  );

  // Client-side sort chain for the live torrent list; ``tstatus`` ranks by
  // activity so asc puts in-progress torrents first. Falls back to the
  // torrent id for a stable order between refreshes.
  const sortedTorrents = useMemo(() => {
    const activeRank = (tor: TorrentInfo) => (ACTIVE_TORRENT_STATUSES.has(tor.status) ? 0 : 1);
    const comparators: Record<string, (a: TorrentInfo, b: TorrentInfo) => number> = {
      tstatus: (a, b) => activeRank(a) - activeRank(b),
      added_date: (a, b) => (a.added_date ?? '').localeCompare(b.added_date ?? ''),
      progress: (a, b) => a.percent_done - b.percent_done,
      name: (a, b) => a.name.localeCompare(b.name),
    };
    return [...torrents].sort((a, b) => {
      for (const entry of torrentSorts) {
        const cmp = comparators[entry.key]?.(a, b) ?? 0;
        if (cmp !== 0) return entry.dir === 'asc' ? cmp : -cmp;
      }
      return a.id - b.id;
    });
  }, [torrents, torrentSorts]);

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
    const res = await downloadersApi.listTasks(id, taskPage, 20, serializeSortEntries(sorts));
    if (res.success) {
      setTasks(res.data);
      if (res.meta) setTaskTotal(res.meta.total);
    }
    setLoadingTasks(false);
  }, [id, taskPage, sorts]);

  useEffect(() => {
    fetchDl();
  }, [fetchDl]);
  useEffect(() => {
    fetchTorrents();
  }, [fetchTorrents]);
  useEffect(() => {
    fetchTasks();
  }, [fetchTasks]);

  const hasActiveTorrents = torrents.some((torrent) => ACTIVE_TORRENT_STATUSES.has(torrent.status));
  const hasActiveTasks = tasks.some((task) =>
    ['pending', 'queued', 'downloading'].includes(task.status),
  );

  useEffect(() => {
    const hasActive = hasActiveTorrents || hasActiveTasks;
    if (!hasActive) return;
    const timer = setInterval(() => {
      fetchTorrents();
      fetchTasks();
    }, 3000);
    return () => clearInterval(timer);
  }, [hasActiveTorrents, hasActiveTasks, fetchTorrents, fetchTasks]);

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

  // Optional torrent columns, keyed so the column-settings order/visibility
  // config can pick them in any arrangement. The torrent name (with the raw
  // Transmission status enum as a tag on its second line) is the fixed
  // must-show column locked to the left.
  const torrentColumnDefs: Record<string, TableColumnsType<TorrentInfo>[number]> = {
    added_date: {
      title: t('downloaders.torrentCol_added_date'),
      dataIndex: 'added_date',
      key: 'added_date',
      width: 150,
      render: (v: string | null) => (
        <Text type="secondary">{v ? formatDate(v) : t('format.dash')}</Text>
      ),
    },
    download_dir: {
      title: t('common.directory'),
      dataIndex: 'download_dir',
      key: 'download_dir',
      width: 150,
      ellipsis: true,
      render: (v: string | null) => <Text type="secondary">{v || t('format.dash')}</Text>,
    },
    progress: {
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
    transfer: {
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
  };

  const torrentColumns: TableColumnsType<TorrentInfo> = [
    {
      title: t('common.name'),
      dataIndex: 'name',
      key: 'name',
      width: 240,
      fixed: 'left',
      render: (name: string, tor) => (
        <div>
          <EllipsisText text={name} danger={tor.error > 0} />
          {/* Raw Transmission status enum as a tag under the title. */}
          <div style={{ marginTop: 2 }}>
            <Tag
              color={TORRENT_STATUS_TAG_COLORS[tor.status] ?? 'default'}
              style={{ marginRight: 0 }}
            >
              {tor.status}
            </Tag>
          </div>
        </div>
      ),
    },
    ...visibleTorrentColumns.map((k) => torrentColumnDefs[k]),
  ];

  const taskColumns: TableColumnsType<DownloadTask> = [
    {
      title: t('channels.work'),
      key: 'work',
      width: 260,
      fixed: 'left',
      render: (_, task) => {
        const fr = task.file_resource;
        const work = fr?.series ?? fr?.movie ?? null;
        const workUrl = fr?.series_id
          ? `/series/${fr.series_id}`
          : fr?.movie_id
            ? `/movies/${fr.movie_id}`
            : null;
        const base =
          (work && (work.original_title || work.title_cn || work.title_en)) ||
          fr?.title_cn ||
          fr?.search_title ||
          fr?.title_raw ||
          task.file_resource_id.slice(0, 8);
        // Per-season works: the base title equals the collection name — the
        // season label is what makes the linked work identifiable.
        const s = fr?.series_id && work ? seasonLabel(t, work.season_number ?? null) : '';
        const workTitle = s ? `${base} · ${s}` : base;
        return (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
            <img
              src={posterUrl(work?.poster_url)}
              alt=""
              style={{
                width: 28,
                height: 40,
                objectFit: 'cover',
                borderRadius: 4,
                flexShrink: 0,
                background: 'var(--rr-border-soft)',
              }}
              onError={useDefaultPoster}
            />
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4, minWidth: 0 }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  {workUrl ? (
                    <Link to={workUrl} onClick={(e) => e.stopPropagation()}>
                      <Text strong ellipsis style={{ display: 'block', fontSize: 13 }}>
                        {workTitle}
                      </Text>
                    </Link>
                  ) : (
                    <Text strong ellipsis style={{ display: 'block', fontSize: 13 }}>
                      {workTitle}
                    </Text>
                  )}
                </div>
                <WorkInfoIcon work={work} isSeries={!!fr?.series_id} />
              </div>
              {/* Type/batch tags fold into the work column's second line (same
                  layout as the channel resource list) to keep the fixed
                  columns compact. */}
              {fr && (
                <div style={{ marginTop: 2 }}>
                  <WorkTypeTags r={fr} />
                </div>
              )}
            </div>
          </div>
        );
      },
    },
    // Configurable metadata columns — same catalog/cells as the channel
    // resource table, plus the task-level enqueue time.
    ...visibleMetaColumns.map(
      (k): TableColumnsType<DownloadTask>[number] => ({
        title: t(`channels.requiredField_${k}`, { defaultValue: k }),
        key: `meta_${k}`,
        width: requiredFieldWidth(k),
        render: (_, task) =>
          k === 'created_at' ? (
            <Text type="secondary">{formatDate(task.created_at)}</Text>
          ) : task.file_resource ? (
            <RequiredFieldCell r={task.file_resource} fieldKey={k} />
          ) : null,
      }),
    ),
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 96,
      fixed: 'right',
      render: (s: string) => (
        <Tag color={DOWNLOAD_STATUS_TAG_COLORS[s] ?? 'default'} style={{ marginRight: 0 }}>
          {t(`status.${s}`, { defaultValue: s })}
        </Tag>
      ),
    },
    {
      title: t('common.progress'),
      dataIndex: 'progress',
      key: 'progress',
      width: 200,
      fixed: 'right',
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
                ↓{formatSpeed(
                  record.transmission_torrent_id == null
                    ? record.download_speed
                    : torrents.find((torrent) => torrent.id === record.transmission_torrent_id)
                        ?.rate_download ?? record.download_speed,
                )}{' '}
                · ETA {formatEta(record.eta)}
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

      <Card>
        <Tabs
          defaultActiveKey="tasks"
          items={[
            {
              key: 'tasks',
              label: `${t('downloaders.localTasks')} (${taskTotal})`,
              children: (
                <>
                  <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 12, marginBottom: 12 }}>
                    <SortSettings
                      fields={sortFields}
                      value={sorts}
                      onChange={handleSortsChange}
                    />
                    <ColumnSettings
                      config={columnCfg}
                      declared={DEFAULT_TASK_COLUMNS}
                      pool={TASK_POOL}
                      hint={t('downloaders.columnSettingsHint')}
                      onChange={handleColumnsChange}
                    />
                  </div>
                  <Table
                    className="stack-table"
                    columns={withMobileLabels(taskColumns)}
                    dataSource={tasks}
                    rowKey="id"
                    loading={loadingTasks}
                    size="small"
                    scroll={{ x: 'max-content' }}
                    onRow={(record) => ({
                      onClick: () => {
                        if (record.file_resource) setSelectedResource(record.file_resource);
                      },
                      style: { cursor: record.file_resource ? 'pointer' : 'default' },
                    })}
                    pagination={{
                      current: taskPage,
                      pageSize: 20,
                      total: taskTotal,
                      onChange: setTaskPage,
                      showSizeChanger: false,
                    }}
                    locale={{ emptyText: t('common.noData') }}
                  />
                </>
              ),
            },
            {
              key: 'transmission',
              label: `${t('downloaders.transmissionTorrents')} (${torrents.length})`,
              children: torrentError ? (
                <Alert
                  type="error"
                  message={t('downloaders.transmissionUnreachable')}
                  description={torrentError}
                  showIcon
                />
              ) : (
                <>
                  <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 12, marginBottom: 12 }}>
                    <SortSettings
                      fields={torrentSortFields}
                      value={torrentSorts}
                      onChange={handleTorrentSortsChange}
                    />
                    <ColumnSettings
                      config={torrentColumnCfg}
                      declared={DEFAULT_TORRENT_COLUMNS}
                      pool={TORRENT_COLUMN_POOL}
                      labelNs="downloaders.torrentCol_"
                      hint={t('downloaders.columnSettingsHint')}
                      onChange={handleTorrentColumnsChange}
                    />
                  </div>
                  <Table
                    className="stack-table"
                    columns={withMobileLabels(torrentColumns)}
                    dataSource={sortedTorrents}
                    rowKey="id"
                    loading={loadingTorrents}
                    size="small"
                    scroll={{ x: 'max-content' }}
                    pagination={sortedTorrents.length > 20 ? { pageSize: 20, showSizeChanger: false } : false}
                    locale={{ emptyText: t('downloaders.noTransmissionTorrents') }}
                  />
                </>
              ),
            },
          ]}
        />
      </Card>

      {/* Read-only resource view: tasks open the shared resource drawer with
          all write actions disabled. */}
      <ResourceDetailDrawer
        resource={selectedResource}
        readOnly
        onClose={() => setSelectedResource(null)}
      />
    </div>
  );
}
