import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Button, Card, Empty, Progress, Select, Statistic, Switch, Table, Tooltip, Typography } from 'antd';
import type { TableColumnsType } from 'antd';
import { RefreshCw } from 'lucide-react';
import {
  queueApi,
  type QueueJob,
  type QueueJobStatus,
  type QueueOverview,
  type QueueSchedulerResponse,
  type QueueTypeStats,
  type SchedulerJob,
} from '../api/queue';
import { usePolling } from '../hooks/usePolling';
import StatusBadge from './StatusBadge';
import EllipsisText from './EllipsisText';
import { formatDate, timeAgo } from '../utils/format';
import { withMobileLabels } from '../utils/table';

const { Text } = Typography;

const PAGE_SIZE = 50;

const STATUS_OPTIONS: (QueueJobStatus | 'all')[] = ['all', 'queued', 'running', 'done', 'failed'];

/** Settings > Queue tab: queue overview stats, per-type success rates,
 * scheduler timetable and the paginated job list. Polls fast while any job
 * is active, slow otherwise (usePolling swallows transient errors). */
export default function QueuePanel() {
  const { t } = useTranslation();

  const [overview, setOverview] = useState<QueueOverview | null>(null);
  const [scheduler, setScheduler] = useState<QueueSchedulerResponse | null>(null);

  const [jobs, setJobs] = useState<QueueJob[]>([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<QueueJobStatus | 'all'>('all');
  const [typeFilter, setTypeFilter] = useState<string | undefined>();
  const [jobsLoading, setJobsLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const fetchAll = useCallback(async () => {
    const [overviewRes, schedulerRes, jobsRes] = await Promise.all([
      queueApi.overview(),
      queueApi.scheduler(),
      queueApi.jobs({
        status: statusFilter === 'all' ? undefined : statusFilter,
        jobType: typeFilter,
        page,
        pageSize: PAGE_SIZE,
      }),
    ]);
    if (overviewRes.success) setOverview(overviewRes.data);
    if (schedulerRes.success) setScheduler(schedulerRes.data);
    if (jobsRes.success) {
      setJobs(jobsRes.data);
      if (jobsRes.meta) setJobsTotal(jobsRes.meta.total);
    }
    setJobsLoading(false);
    setLastUpdated(new Date());
  }, [page, statusFilter, typeFilter]);

  const hasActive = overview ? overview.counts.running + overview.counts.queued > 0 : true;
  // Fetch immediately on mount and whenever page/filters change — usePolling
  // watches only (interval, enabled, immediate), so without this effect a
  // filter switch would wait for the next scheduled tick (up to 30s).
  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);
  usePolling(fetchAll, 5000, autoRefresh && hasActive, false);
  usePolling(fetchAll, 30000, autoRefresh && !hasActive, false);

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      await fetchAll();
    } finally {
      setRefreshing(false);
    }
  };

  const resetFilters = () => {
    setJobsLoading(true);
    setPage(1);
  };

  const jobDuration = (job: QueueJob): number | null => {
    if (!job.started_at || !job.finished_at) return null;
    const ms = new Date(job.finished_at).getTime() - new Date(job.started_at).getTime();
    return ms >= 0 ? ms / 1000 : null;
  };

  const timeCell = (value: string | null) =>
    value ? (
      <Tooltip title={formatDate(value, 'yyyy-MM-dd HH:mm:ss')}>
        <Text type="secondary" style={{ fontSize: 12 }}>{timeAgo(value)}</Text>
      </Tooltip>
    ) : (
      <Text type="secondary">—</Text>
    );

  const typeColumns: TableColumnsType<QueueTypeStats> = [
    {
      title: t('queue.colJobType'),
      dataIndex: 'job_type',
      key: 'job_type',
      render: (v: string) => <Text style={{ fontSize: 13 }}>{v}</Text>,
    },
    {
      title: t('queue.colTotal'),
      dataIndex: 'total',
      key: 'total',
      width: 80,
      render: (v: number) => <Text type="secondary" style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      title: t('queue.colRunning'),
      dataIndex: 'running',
      key: 'running',
      width: 80,
      render: (v: number) => <Text type="secondary" style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      title: t('queue.colDone'),
      dataIndex: 'done',
      key: 'done',
      width: 80,
      render: (v: number) => <Text type="secondary" style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      title: t('queue.colFailed'),
      dataIndex: 'failed',
      key: 'failed',
      width: 80,
      render: (v: number) => <Text type="secondary" style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      title: t('queue.colSuccessRate'),
      dataIndex: 'success_rate',
      key: 'success_rate',
      width: 180,
      render: (v: number | null) =>
        v == null ? (
          <Text type="secondary">—</Text>
        ) : (
          <Progress percent={Math.round(v * 1000) / 10} size="small" style={{ margin: 0 }} />
        ),
    },
    {
      title: t('queue.colAvgDuration'),
      dataIndex: 'avg_duration_seconds',
      key: 'avg_duration_seconds',
      width: 120,
      render: (v: number | null) =>
        v == null ? (
          <Text type="secondary">—</Text>
        ) : (
          <Text type="secondary" style={{ fontSize: 12 }}>{v.toFixed(1)}s</Text>
        ),
    },
  ];

  const schedulerColumns: TableColumnsType<SchedulerJob> = [
    {
      title: t('queue.colSchedulerId'),
      dataIndex: 'id',
      key: 'id',
      render: (v: string) => <Text style={{ fontSize: 13 }}>{v}</Text>,
    },
    {
      title: t('queue.colTrigger'),
      dataIndex: 'trigger',
      key: 'trigger',
      width: 200,
      render: (v: string) => <Text type="secondary" style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      title: t('queue.colNextRun'),
      dataIndex: 'next_run_time',
      key: 'next_run_time',
      width: 220,
      render: (v: string | null) => timeCell(v),
    },
  ];

  const jobColumns: TableColumnsType<QueueJob> = [
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (v: string) => <StatusBadge status={v} />,
    },
    {
      title: t('queue.colJobType'),
      dataIndex: 'job_type',
      key: 'job_type',
      width: 160,
      render: (v: string) => <Text style={{ fontSize: 13 }}>{v}</Text>,
    },
    {
      title: t('queue.colKey'),
      dataIndex: 'key',
      key: 'key',
      render: (v: string | null) =>
        v ? <EllipsisText text={v} /> : <Text type="secondary">—</Text>,
    },
    {
      title: t('queue.colQueuedAt'),
      dataIndex: 'queued_at',
      key: 'queued_at',
      width: 140,
      render: timeCell,
    },
    {
      title: t('queue.colStartedAt'),
      dataIndex: 'started_at',
      key: 'started_at',
      width: 140,
      render: timeCell,
    },
    {
      title: t('queue.colFinishedAt'),
      dataIndex: 'finished_at',
      key: 'finished_at',
      width: 140,
      render: timeCell,
    },
    {
      title: t('queue.colDuration'),
      key: 'duration',
      width: 90,
      render: (_, record) => {
        const d = jobDuration(record);
        return d == null ? (
          <Text type="secondary">—</Text>
        ) : (
          <Text type="secondary" style={{ fontSize: 12 }}>{d.toFixed(1)}s</Text>
        );
      },
    },
    {
      title: t('queue.colError'),
      dataIndex: 'error',
      key: 'error',
      width: 160,
      render: (v: string | null) =>
        v ? <EllipsisText text={v} danger /> : <Text type="secondary">—</Text>,
    },
  ];

  return (
    <div>
      {/* Refresh controls */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 12, marginBottom: 12, flexWrap: 'wrap' }}>
        {lastUpdated && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t('queue.lastUpdated', { time: formatDate(lastUpdated.toISOString(), 'HH:mm:ss') })}
          </Text>
        )}
        <Text type="secondary" style={{ fontSize: 12 }}>{t('queue.autoRefresh')}</Text>
        <Switch size="small" checked={autoRefresh} onChange={setAutoRefresh} />
        <Button
          size="small"
          icon={<RefreshCw size={14} />}
          onClick={handleRefresh}
          loading={refreshing}
        >
          {t('common.refresh')}
        </Button>
      </div>

      {/* Overview */}
      <Card size="small" style={{ marginBottom: 16 }} title={t('queue.overviewTitle')}>
        <div style={{ display: 'flex', gap: 32, flexWrap: 'wrap', marginBottom: 8 }}>
          <Statistic title={t('queue.statRunning')} value={overview?.counts.running ?? 0} />
          <Statistic title={t('queue.statQueued')} value={overview?.counts.queued ?? 0} />
          <Statistic title={t('queue.statDone')} value={overview?.counts.done ?? 0} />
          <Statistic title={t('queue.statFailed')} value={overview?.counts.failed ?? 0} />
        </div>
        {overview && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {overview.stats_scope === 'since_restart'
              ? t('queue.scopeSinceRestart')
              : t('queue.scopeLast24h')}
          </Text>
        )}
        {overview && overview.backend === 'memory' && overview.app_role === 'web' && (
          <Alert
            type="info"
            showIcon
            style={{ marginTop: 12 }}
            message={t('queue.webRoleNotice')}
          />
        )}
      </Card>

      {/* Per-type success rates */}
      <Card size="small" style={{ marginBottom: 16 }} title={t('queue.byTypeTitle')}>
        <Table<QueueTypeStats>
          className="stack-table"
          columns={withMobileLabels(typeColumns)}
          dataSource={overview?.by_type ?? []}
          rowKey="job_type"
          size="small"
          pagination={false}
          locale={{ emptyText: <Empty description={t('queue.byTypeEmpty')} /> }}
        />
      </Card>

      {/* Scheduler */}
      <Card size="small" style={{ marginBottom: 16 }} title={t('queue.schedulerTitle')}>
        {scheduler && !scheduler.enabled ? (
          <Text type="secondary" style={{ fontSize: 13 }}>{t('queue.schedulerDisabled')}</Text>
        ) : (
          <Table<SchedulerJob>
            className="stack-table"
            columns={withMobileLabels(schedulerColumns)}
            dataSource={scheduler?.jobs ?? []}
            rowKey="id"
            size="small"
            pagination={false}
            locale={{ emptyText: <Empty description={t('queue.schedulerEmpty')} /> }}
          />
        )}
      </Card>

      {/* Jobs */}
      <Card size="small" title={t('queue.jobsTitle')}>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
          <Select
            size="small"
            style={{ minWidth: 120 }}
            value={statusFilter}
            onChange={(v) => {
              setStatusFilter(v);
              resetFilters();
            }}
            options={STATUS_OPTIONS.map((s) => ({
              value: s,
              label: s === 'all' ? t('common.all') : t(`status.${s}`),
            }))}
          />
          <Select
            size="small"
            style={{ minWidth: 180 }}
            value={typeFilter}
            onChange={(v) => {
              setTypeFilter(v);
              resetFilters();
            }}
            allowClear
            placeholder={t('queue.jobTypeFilterPlaceholder')}
            options={(overview?.by_type ?? []).map((r) => ({
              value: r.job_type,
              label: r.job_type,
            }))}
          />
        </div>
        <Table<QueueJob>
          className="stack-table"
          columns={withMobileLabels(jobColumns)}
          dataSource={jobs}
          rowKey="job_id"
          loading={jobsLoading}
          size="small"
          tableLayout="fixed"
          pagination={{
            current: page,
            pageSize: PAGE_SIZE,
            total: jobsTotal,
            onChange: (p) => {
              setJobsLoading(true);
              setPage(p);
            },
            showSizeChanger: false,
          }}
          locale={{ emptyText: <Empty description={t('queue.jobsEmpty')} /> }}
        />
      </Card>
    </div>
  );
}
