import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  DatePicker,
  Divider,
  Drawer,
  Empty,
  Grid,
  Modal,
  Radio,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  App,
} from 'antd';
import dayjs from 'dayjs';
import type { Dayjs } from 'dayjs';
import type { TableColumnsType } from 'antd';
import { CalendarClock, ListTree, PlayCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { agentsApi } from '../../api/agents';
import StatusBadge from '../../components/StatusBadge';
import { formatBytes, timeAgo } from '../../utils/format';
import { withMobileLabels } from '../../utils/table';
import { createRequestGuard } from '../../utils/requestGuard';
import type { AgentRun } from '../../types';

const { Text } = Typography;

/** Run-control tab: immediate/windowed run triggers, live run-status polling
 * and the run-history table with the matched-resources drawer. Owns its own
 * loading (request-guarded) — the page only hears back via ``onActivity`` so
 * the tasks tab refreshes when a run finishes. */
export default function RunTab({
  agentId,
  active,
  onActivity,
  onShowFiles,
}: {
  agentId: string;
  /** Whether this tab is the visible one — entering it reloads the history. */
  active: boolean;
  /** A run finished (done/failed/success): the parent reloads the tasks. */
  onActivity: () => void;
  onShowFiles: (resourceId: string) => void;
}) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const screens = Grid.useBreakpoint();

  const [runStatus, setRunStatus] = useState<string | null>(null);
  const [runPolling, setRunPolling] = useState(false);
  // Windowed run ("run from a chosen start time") modal.
  const [scanModalOpen, setScanModalOpen] = useState(false);
  const [scanMode, setScanMode] = useState<'since' | 'all'>('since');
  const [scanTime, setScanTime] = useState<Dayjs>(() => dayjs().subtract(14, 'day'));
  // Run history (run-control tab).
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [runPage, setRunPage] = useState(1);
  const [runTotal, setRunTotal] = useState(0);
  const [loadingRuns, setLoadingRuns] = useState(false);
  // Hide routine no-op runs by default; only runs that dispatched tasks,
  // produced decisions, or are running/failed show up unless unchecked.
  const [runsNonEmptyOnly, setRunsNonEmptyOnly] = useState(true);
  // Drawer showing a single run's matched file resources.
  const [runDrawerRun, setRunDrawerRun] = useState<AgentRun | null>(null);

  // Latest-request-wins guard: a stale response from an earlier page never
  // overwrites newer state; cancelled on unmount so no setState fires after.
  const guardRef = useRef(createRequestGuard());
  useEffect(() => {
    const guard = guardRef.current;
    return () => guard.cancel();
  }, []);

  const loadRuns = useCallback(async () => {
    const token = guardRef.current.next();
    setLoadingRuns(true);
    const r = await agentsApi.listRuns(agentId, runPage, 20, runsNonEmptyOnly);
    if (!guardRef.current.isCurrent(token)) return;
    if (r.success) {
      setRuns(r.data);
      if (r.meta) setRunTotal(r.meta.total);
    }
    setLoadingRuns(false);
  }, [agentId, runPage, runsNonEmptyOnly]);

  useEffect(() => {
    if (active) loadRuns();
  }, [active, loadRuns]);

  // Poll run status
  useEffect(() => {
    if (!runPolling) return;
    const timer = setInterval(async () => {
      const r = await agentsApi.runStatus(agentId);
      if (r.success && r.data) {
        setRunStatus(r.data.status);
        if (r.data.status === 'done' || r.data.status === 'failed' || r.data.status === 'success') {
          setRunPolling(false);
          setRunStatus(r.data.status);
          onActivity();
          setRunPage(1);
          loadRuns();
          setTimeout(() => setRunStatus(null), 2000);
        }
      }
    }, 1500);
    return () => clearInterval(timer);
  }, [runPolling, agentId, onActivity, loadRuns]);

  const handleRun = async (scanWindow?: { scan_since: string | null }): Promise<boolean> => {
    const r = await agentsApi.run(agentId, scanWindow);
    if (r.success) {
      message.success(t('agents.runTriggered'));
      setRunStatus('queued');
      setRunPolling(true);
      return true;
    }
    message.error(r.error?.message || t('agents.runFailed'));
    return false;
  };

  const handleWindowedRun = async () => {
    const scan_since = scanMode === 'all' ? null : scanTime.toISOString();
    if (await handleRun({ scan_since })) setScanModalOpen(false);
  };

  return (
    <Card>
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Text type="secondary">
          {t('agents.runControlDesc')}
        </Text>
        <div>
          <Space size={12}>
            <Tooltip title={t('agents.runNowHint')}>
              <Button
                type="primary"
                size="large"
                icon={<PlayCircle size={16} />}
                loading={runPolling}
                onClick={() => handleRun()}
              >
                {t('agents.runNow')}
              </Button>
            </Tooltip>
            <Tooltip title={t('agents.runSinceHint')}>
              <Button
                size="large"
                icon={<CalendarClock size={16} />}
                loading={runPolling}
                onClick={() => setScanModalOpen(true)}
              >
                {t('agents.runSince')}
              </Button>
            </Tooltip>
          </Space>
        </div>
        <Modal
          title={t('agents.runSinceTitle')}
          open={scanModalOpen}
          onOk={handleWindowedRun}
          onCancel={() => setScanModalOpen(false)}
          okText={t('agents.runNow')}
          confirmLoading={runPolling}
          destroyOnHidden
        >
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Radio.Group
              value={scanMode}
              onChange={(e) => setScanMode(e.target.value)}
            >
              <Radio value="since">{t('agents.runSinceModeSince')}</Radio>
              <Radio value="all">{t('agents.runSinceModeAll')}</Radio>
            </Radio.Group>
            {scanMode === 'since' && (
              <DatePicker
                showTime
                value={scanTime}
                onChange={(v) => { if (v) setScanTime(v); }}
                disabledDate={(d) => d.isAfter(dayjs().endOf('day'))}
                allowClear={false}
                style={{ width: '100%' }}
              />
            )}
            <Alert type="info" showIcon message={t('agents.runSinceNotice')} />
          </Space>
        </Modal>
        {runStatus && (
          <div>
            <StatusBadge status={runStatus} />
            <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
              {runStatus === 'queued' && t('agents.queued')}
              {runStatus === 'running' && t('agents.processing')}
              {runStatus === 'done' && t('agents.runComplete')}
              {runStatus === 'failed' && t('status.failed')}
            </Text>
          </div>
        )}
        <Divider style={{ margin: '8px 0' }} />
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <Text strong>
              {t('agents.runHistory')}
            </Text>
            <Checkbox
              checked={runsNonEmptyOnly}
              onChange={(e) => {
                setRunsNonEmptyOnly(e.target.checked);
                setRunPage(1);
              }}
            >
              {t('agents.runsNonEmptyOnly')}
            </Checkbox>
          </div>
          <Table<AgentRun>
            className="stack-table"
            columns={withMobileLabels(runColumns(t, (r) => setRunDrawerRun(r)))}
            dataSource={runs}
            rowKey="id"
            loading={loadingRuns}
            size="small"
            pagination={{
              current: runPage,
              pageSize: 20,
              total: runTotal,
              onChange: setRunPage,
              showSizeChanger: false,
            }}
            locale={{ emptyText: <Empty description={t('agents.noRuns')} /> }}
          />
        </div>
      </Space>

      <Drawer
        open={!!runDrawerRun}
        onClose={() => setRunDrawerRun(null)}
        title={runDrawerRun ? `${t('agents.runMatchedResources')} · ${timeAgo(runDrawerRun.started_at)}` : ''}
        width={screens.md ? 680 : '100%'}
        destroyOnClose
      >
        {runDrawerRun && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Space size={12} wrap>
              <StatusBadge status={runDrawerRun.status} />
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t('agents.runStatsLine', {
                  total: runDrawerRun.total_resources,
                  dispatched: runDrawerRun.dispatched,
                  pd: runDrawerRun.pending_decisions,
                  failed: runDrawerRun.filter_failed,
                  dup: runDrawerRun.duplicates_skipped,
                })}
              </Text>
            </Space>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t('agents.matchedResources', { n: runDrawerRun.matched_resources.length })}
            </Text>
            {runDrawerRun.matched_resources.length === 0 ? (
              <Empty description={t('agents.noMatchedResources')} />
            ) : (
              runDrawerRun.matched_resources.map((r) => {
                const langs = r.subtitle_langs && r.subtitle_langs.length > 0 ? r.subtitle_langs.join('/') : null;
                return (
                  <div
                    key={r.id}
                    style={{
                      padding: 10,
                      border: '1px solid var(--rr-border-soft)',
                      borderRadius: 8,
                      background: 'var(--rr-surface-elevated)',
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8 }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <Text strong style={{ fontSize: 13, color: 'var(--rr-text)', wordBreak: 'break-word' }}>
                          {r.title_raw}
                        </Text>
                        {r.title_cn && r.title_cn !== r.title_raw && (
                          <div style={{ marginTop: 2, fontSize: 12, color: 'var(--rr-text-muted)', wordBreak: 'break-word' }}>
                            {r.title_cn}
                          </div>
                        )}
                      </div>
                      <Tooltip title={t('resource.files')}>
                        <Button
                          type="text"
                          size="small"
                          icon={<ListTree size={14} />}
                          aria-label={t('resource.files')}
                          onClick={() => onShowFiles(r.id)}
                        />
                      </Tooltip>
                    </div>
                    <Space size={4} wrap style={{ fontSize: 11, color: 'var(--rr-text-secondary)', marginTop: 4 }}>
                      {r.dispatched && (
                        <Tag color="success" style={{ margin: 0 }}>{t('agents.tagDispatched')}</Tag>
                      )}
                      {r.pending_decision && (
                        <Tag color="warning" style={{ margin: 0 }}>{t('agents.pendingDecisionAction')}</Tag>
                      )}
                      {r.subtitle_group && <Tag style={{ margin: 0 }}>{r.subtitle_group}</Tag>}
                      {r.resolution && <Tag style={{ margin: 0 }}>{r.resolution}</Tag>}
                      {r.source && <Tag style={{ margin: 0 }}>{r.source}</Tag>}
                      {r.video_codec && <Tag style={{ margin: 0 }}>{r.video_codec}</Tag>}
                      {r.audio_codec && <Tag style={{ margin: 0 }}>{r.audio_codec}</Tag>}
                      {r.season != null && <span>S{r.season}</span>}
                      {r.episode != null && <span>EP{r.episode}</span>}
                      {r.subtitle_type && <Tag style={{ margin: 0 }}>{r.subtitle_type}</Tag>}
                      {langs && <Tag color="blue" style={{ margin: 0 }}>{langs}</Tag>}
                      {r.file_size != null && <span>{formatBytes(r.file_size)}</span>}
                      {r.published_at && <span>· {timeAgo(r.published_at)}</span>}
                    </Space>
                  </div>
                );
              })
            )}
          </Space>
        )}
      </Drawer>
    </Card>
  );
}

const runColumns = (
  t: (k: string, opts?: Record<string, unknown>) => string,
  onView: (r: AgentRun) => void,
): TableColumnsType<AgentRun> => [
  {
    title: t('agents.runStarted'),
    dataIndex: 'started_at',
    key: 'started_at',
    width: 180,
    render: (v: string, r: AgentRun) => (
      <Space size={4}>
        <Text type="secondary" style={{ fontSize: 12 }}>{timeAgo(v)}</Text>
        {r.scan_since && (
          <Tag color="blue" style={{ fontSize: 10, marginInlineEnd: 0 }}>
            {new Date(r.scan_since).getFullYear() <= 1970
              ? t('agents.runSinceAllShort')
              : t('agents.runSinceTag', { time: new Date(r.scan_since).toLocaleDateString() })}
          </Tag>
        )}
      </Space>
    ),
  },
  {
    title: t('agents.runFinished'),
    dataIndex: 'finished_at',
    key: 'finished_at',
    width: 180,
    render: (v: string | null) =>
      v ? <Text type="secondary" style={{ fontSize: 12 }}>{timeAgo(v)}</Text> : <Text type="secondary">—</Text>,
  },
  {
    title: t('agents.taskStatus'),
    dataIndex: 'status',
    key: 'status',
    width: 120,
    render: (status: string) => <StatusBadge status={status} />,
  },
  {
    title: t('agents.runMatched'),
    key: 'matched',
    width: 100,
    render: (_, r) => (
      <Text style={{ fontSize: 12 }}>
        {r.matched}
        {r.matched > 0 && <Text type="secondary" style={{ fontSize: 11 }}> · {t('agents.runDispatched', { n: r.dispatched })}</Text>}
      </Text>
    ),
  },
  {
    title: t('agents.runStats'),
    key: 'stats',
    render: (_, r) => (
      <Text type="secondary" style={{ fontSize: 11 }}>
        {t('agents.runStatsLine', {
          total: r.total_resources,
          dispatched: r.dispatched,
          pd: r.pending_decisions,
          failed: r.filter_failed,
          dup: r.duplicates_skipped,
        })}
      </Text>
    ),
  },
  {
    title: t('common.actions'),
    key: 'actions',
    width: 120,
    align: 'right',
    render: (_, r) => (
      <Button
        size="small"
        disabled={r.matched_resources.length === 0 && r.pending_decisions === 0}
        onClick={() => onView(r)}
      >
        {r.status === 'pending_decisions'
          ? t('agents.pendingDecisionAction')
          : t('agents.viewResources', { n: r.matched_resources.length })}
      </Button>
    ),
  },
];
