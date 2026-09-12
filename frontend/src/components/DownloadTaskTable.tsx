import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { Progress, Select, Space, Table, Tag, Typography } from 'antd';
import type { TableColumnsType, TableProps } from 'antd';
import type { ReactNode } from 'react';
import { withMobileLabels } from '../utils/table';
import type { DownloadTask, TorrentInfo } from '../types';
import { formatDate, formatEta, formatSpeed } from '../utils/format';
import { posterUrl, useDefaultPoster } from '../utils/poster';
import { seasonLabel } from '../utils/season';
import ColumnSettings from './ColumnSettings';
import SortSettings, { type SortEntry, type SortField } from './SortSettings';import {
  RequiredFieldCell,
  WorkInfoIcon,
  WorkTypeTags,
} from './resourceCells';
import {
  DOWNLOAD_STATUS_TAG_COLORS,
  TASK_COLUMN_POOL,
  requiredFieldWidth,
  resolveVisibleColumns,
  type ChannelColumnConfig,
} from '../utils/requiredFields';

const { Text } = Typography;

// Default-visible columns (the must-show fields — work title (with type/batch
// tags folded into its second line), status and progress — are fixed columns
// outside the configurable pool). ``created_at`` is a task-level column (the
// task's enqueue time), not a resource catalog field; it renders from the
// task row.
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

// Column pool: the resource metadata catalog plus the task's own enqueue time.
const TASK_POOL: readonly string[] = [...TASK_COLUMN_POOL, 'created_at'];

// Sortable task fields; the default puts unfinished tasks first, then the
// most recently enqueued.
const TASK_SORTABLE_KEYS = ['status', 'created_at', 'progress', 'title'] as const;

// Status filter options — values follow the effective display status (an
// organized task is matched by "completed"; see the tasks API).
const STATUS_FILTER_VALUES = [
  'pending',
  'queued',
  'downloading',
  'paused',
  'completed',
  'error',
  'cancelled',
] as const;

interface Props {
  tasks: DownloadTask[];
  loading: boolean;
  page: number;
  total: number;
  onPageChange: (page: number) => void;
  sorts: SortEntry[];
  onSortsChange: (next: SortEntry[] | null) => void;
  columnCfg: ChannelColumnConfig | null;
  onColumnsChange: (next: ChannelColumnConfig | null) => void;
  /** Hint line in the column-settings popover (which scope it saves for). */
  columnSettingsHint: string;
  /** Live torrent list for the progress column's real-time rate lookup. */
  torrents?: TorrentInfo[];
  /** Status filter (effective status); the Select renders when the change
   * handler is provided. */
  statusFilter?: string;
  onStatusFilterChange?: (status: string | undefined) => void;
  /** Extra toolbar content on the left, next to the status filter (e.g. the
   * agent tab's batch-retry buttons). */
  toolbarExtra?: ReactNode;
  /** Extra fixed-right columns appended after progress (e.g. the agent tab's
   * error popover and row actions). */
  extraColumns?: TableColumnsType<DownloadTask>;
  rowSelection?: TableProps<DownloadTask>['rowSelection'];
  onRowClick?: (task: DownloadTask) => void;
  emptyText: ReactNode;
}

/** Shared download-task table (downloader local tasks, agent tasks): fixed
 * work/status/progress columns, a configurable metadata column pool, status
 * filter, sort and column settings. */
export default function DownloadTaskTable({
  tasks,
  loading,
  page,
  total,
  onPageChange,
  sorts,
  onSortsChange,
  columnCfg,
  onColumnsChange,
  columnSettingsHint,
  torrents,
  statusFilter,
  onStatusFilterChange,
  toolbarExtra,
  extraColumns,
  rowSelection,
  onRowClick,
  emptyText,
}: Props) {
  const { t } = useTranslation();

  const visibleMetaColumns = resolveVisibleColumns(columnCfg, DEFAULT_TASK_COLUMNS, TASK_POOL);
  const sortFields: SortField[] = TASK_SORTABLE_KEYS.map((key) => ({
    key,
    label: t(`downloaders.sortField_${key}`),
    ascLabel: t(`downloaders.sortDir_${key}_asc`),
    descLabel: t(`downloaders.sortDir_${key}_desc`),
  }));

  const columns: TableColumnsType<DownloadTask> = [
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
                    : torrents?.find((torrent) => torrent.id === record.transmission_torrent_id)
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
    ...(extraColumns ?? []),
  ];

  return (
    <>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 12,
          marginBottom: 12,
        }}
      >
        <Space wrap>
          {onStatusFilterChange && (
            <>
              <Text type="secondary" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
                {t('common.statusFilter')}
              </Text>
              <Select
                allowClear
                placeholder={t('common.all')}
                style={{ width: 140 }}
                value={statusFilter}
                onChange={onStatusFilterChange}
                options={STATUS_FILTER_VALUES.map((v) => ({
                  value: v,
                  label: t(`status.${v}`),
                }))}
              />
            </>
          )}
          {toolbarExtra}
        </Space>
        <Space size={12}>
          <SortSettings fields={sortFields} value={sorts} onChange={onSortsChange} />
          <ColumnSettings
            config={columnCfg}
            declared={DEFAULT_TASK_COLUMNS}
            pool={TASK_POOL}
            hint={columnSettingsHint}
            onChange={onColumnsChange}
          />
        </Space>
      </div>
      <Table<DownloadTask>
        className="stack-table"
        columns={withMobileLabels(columns)}
        dataSource={tasks}
        rowKey="id"
        loading={loading}
        size="small"
        scroll={{ x: 'max-content' }}
        rowSelection={rowSelection}
        onRow={
          onRowClick
            ? (record) => ({
                onClick: () => onRowClick(record),
                style: { cursor: record.file_resource ? 'pointer' : 'default' },
              })
            : undefined
        }
        pagination={{
          current: page,
          pageSize: 20,
          total,
          onChange: onPageChange,
          showSizeChanger: false,
        }}
        locale={{ emptyText }}
      />
    </>
  );
}
