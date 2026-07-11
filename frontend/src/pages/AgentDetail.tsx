import { useState, useEffect, useCallback } from 'react';
import { useParams, Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  Tabs,
  Table,
  Button,
  Space,
  Card,
  Tag,
  Typography,
  Empty,
  Spin,
  App,
  Select,
  Statistic,
  Row,
  Col,
  Divider,
  Alert,
  InputNumber,
  Tooltip,
  Checkbox,
  Drawer,
} from 'antd';
import type { TableColumnsType } from 'antd';
import {
  Pause,
  Play,
  RotateCcw,
  Trash2,
  CheckCircle,
  SkipForward,
  FlaskConical,
  PlayCircle,
  ArrowLeft,
  Edit,
} from 'lucide-react';
import { agentsApi } from '../api/agents';
import { tasksApi, decisionsApi } from '../api/tasks';
import StatusBadge from '../components/StatusBadge';
import ProgressBar from '../components/ProgressBar';
import FilterBuilder from '../components/FilterBuilder';
import WorkSelector from '../components/WorkSelector';
import BackfillPreviewModal from '../components/BackfillPreviewModal';
import { formatBytes, formatSpeed, formatEta, timeAgo, formatDate } from '../utils/format';
import type {
  Agent,
  AgentRun,
  AgentWork,
  DownloadTask,
  FileResource,
  PendingDecision,
  ResourceTestResult,
  RulesPreviewResponse,
} from '../types';
import { resourcesApi } from '../api/channels';

const { Title, Text } = Typography;

export default function AgentDetail() {
  const { id } = useParams<{ id: string }>();
  const { t } = useTranslation();
  const { message, modal } = App.useApp();
  const [agent, setAgent] = useState<Agent | null>(null);
  const [tab, setTab] = useState('works');
  const [loadingAgent, setLoadingAgent] = useState(true);

  // Tasks
  const [tasks, setTasks] = useState<DownloadTask[]>([]);
  const [taskPage, setTaskPage] = useState(1);
  const [taskTotal, setTaskTotal] = useState(0);
  const [taskStatus, setTaskStatus] = useState<string | undefined>();
  const [loadingTasks, setLoadingTasks] = useState(false);

  // Decisions
  const [decisions, setDecisions] = useState<PendingDecision[]>([]);
  const [decPage] = useState(1);
  const [decTotal, setDecTotal] = useState(0);
  const [loadingDec, setLoadingDec] = useState(false);
  const [candidateCache, setCandidateCache] = useState<Record<string, FileResource>>({});
  // Ambiguous-episode decisions: per-candidate episode draft + in-flight flag
  // for the "correct episode" action.
  const [episodeDrafts, setEpisodeDrafts] = useState<Record<string, number | null>>({});
  const [savingEpisodeCid, setSavingEpisodeCid] = useState<string | null>(null);
  // Batch selection + AI auto-handle loading state for the decisions tab.
  const [selectedDecisionIds, setSelectedDecisionIds] = useState<string[]>([]);
  const [aiPickLoading, setAiPickLoading] = useState<string | null>(null);
  const [batchLoading, setBatchLoading] = useState(false);

  // Works
  const [works, setWorks] = useState<AgentWork[]>([]);
  const [loadingWorks, setLoadingWorks] = useState(false);
  // Buffered works editing: add/remove/edit only touch local state; the
  // list-level "Save" button batch-replaces works via PUT /agents/{id}. This
  // mirrors AgentForm's behaviour and lets the user configure per-work
  // filter_overrides before committing — the per-work inline Save is gone.
  const [worksDirty, setWorksDirty] = useState(false);
  const [savingWorks, setSavingWorks] = useState(false);
  // Works-tab rule-change preview (scenario ②): when the works list changes,
  // show the backfill selection modal before committing — same flow as
  // AgentForm, so editing works from the detail page also surfaces the
  // resource match diff instead of silently saving.
  const [worksPreview, setWorksPreview] = useState<RulesPreviewResponse | null>(null);
  const [worksPreviewSelected, setWorksPreviewSelected] = useState<Record<string, boolean>>({});
  const [pendingWorksSave, setPendingWorksSave] = useState<AgentWork[] | null>(null);
  const [worksPreviewSaving, setWorksPreviewSaving] = useState(false);

  // Filters
  const [filterConfig, setFilterConfig] = useState(agent?.filter_config ?? null);
  const [filterTest, setFilterTest] = useState<{
    results: ResourceTestResult[];
    stats: { total: number; passed: number; failed: number };
  } | null>(null);
  const [testingFilters, setTestingFilters] = useState(false);
  const [savingFilter, setSavingFilter] = useState(false);

  // Run status
  const [runStatus, setRunStatus] = useState<string | null>(null);
  const [runPolling, setRunPolling] = useState(false);
  // Run history (run-control tab).
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [runPage, setRunPage] = useState(1);
  const [runTotal, setRunTotal] = useState(0);
  const [loadingRuns, setLoadingRuns] = useState(false);
  // Drawer showing a single run's matched file resources.
  const [runDrawerRun, setRunDrawerRun] = useState<AgentRun | null>(null);

  const loadAgent = useCallback(async () => {
    if (!id) return;
    setLoadingAgent(true);
    const r = await agentsApi.get(id);
    if (r.success) {
      setAgent(r.data);
      setFilterConfig(r.data.filter_config ?? null);
      if (r.data.works) setWorks(r.data.works);
    }
    setLoadingAgent(false);
  }, [id]);

  const loadTasks = useCallback(async () => {
    if (!id) return;
    setLoadingTasks(true);
    const r = await tasksApi.listByAgent(id, taskPage, 20, taskStatus);
    if (r.success) {
      setTasks(r.data);
      if (r.meta) setTaskTotal(r.meta.total);
    }
    setLoadingTasks(false);
  }, [id, taskPage, taskStatus]);

  const loadDecisions = useCallback(async () => {
    if (!id) return;
    setLoadingDec(true);
    const r = await decisionsApi.listByAgent(id, decPage, 20, 'pending');
    if (r.success) {
      setDecisions(r.data);
      if (r.meta) setDecTotal(r.meta.total);
      // Prefetch candidates
      const ids = new Set<string>();
      r.data.forEach((d) => d.candidates.forEach((c) => ids.add(c)));
      const missing = Array.from(ids).filter((rid) => !candidateCache[rid]);
      if (missing.length > 0) {
        const fetched = await Promise.all(
          missing.map((rid) =>
            resourcesApi.get(rid).then((res) => (res.success ? [rid, res.data] as const : null)),
          ),
        );
        const next = { ...candidateCache };
        fetched.forEach((entry) => {
          if (entry) next[entry[0]] = entry[1];
        });
        setCandidateCache(next);
      }
    }
    setLoadingDec(false);
  }, [id, decPage, candidateCache]);

  const loadWorks = useCallback(async () => {
    if (!id) return;
    setLoadingWorks(true);
    const r = await agentsApi.listWorks(id);
    if (r.success) {
      setWorks(r.data);
      setWorksDirty(false);
    }
    setLoadingWorks(false);
  }, [id]);

  const loadRuns = useCallback(async () => {
    if (!id) return;
    setLoadingRuns(true);
    const r = await agentsApi.listRuns(id, runPage, 20);
    if (r.success) {
      setRuns(r.data);
      if (r.meta) setRunTotal(r.meta.total);
    }
    setLoadingRuns(false);
  }, [id, runPage]);

  useEffect(() => {
    loadAgent();
  }, [loadAgent]);

  useEffect(() => {
    if (tab === 'tasks') loadTasks();
  }, [tab, loadTasks]);

  useEffect(() => {
    if (tab === 'decisions') loadDecisions();
  }, [tab, loadDecisions]);

  useEffect(() => {
    if (tab === 'run') loadRuns();
  }, [tab, loadRuns]);

  useEffect(() => {
    // Reload works when entering the tab, but never overwrite unsaved edits.
    if (tab === 'works' && !worksDirty) loadWorks();
  }, [tab, loadWorks, worksDirty]);

  // Lightweight tab-count poll: refresh the works/tasks/decisions counts every
  // 15s so badges like "下载任务 (N)" stay current without a manual refresh.
  // Uses page_size=1 for the list endpoints to keep the payload tiny, and
  // never overwrites unsaved works edits (worksDirty guard).
  useEffect(() => {
    if (!id) return;
    const interval = setInterval(async () => {
      try {
        const [agentRes, taskRes, decRes] = await Promise.all([
          agentsApi.get(id),
          tasksApi.listByAgent(id, 1, 1, taskStatus),
          decisionsApi.listByAgent(id, 1, 1, 'pending'),
        ]);
        if (agentRes.success) {
          setAgent(agentRes.data);
          if (!worksDirty && agentRes.data.works) setWorks(agentRes.data.works);
        }
        if (taskRes.success && taskRes.meta) setTaskTotal(taskRes.meta.total);
        if (decRes.success && decRes.meta) setDecTotal(decRes.meta.total);
      } catch {
        /* ignore transient poll errors */
      }
    }, 15000);
    return () => clearInterval(interval);
  }, [id, worksDirty, taskStatus]);

  // Poll run status
  useEffect(() => {
    if (!runPolling || !id) return;
    const t = setInterval(async () => {
      const r = await agentsApi.runStatus(id);
      if (r.success && r.data) {
        setRunStatus(r.data.status);
        if (r.data.status === 'done' || r.data.status === 'failed' || r.data.status === 'success') {
          setRunPolling(false);
          setRunStatus(r.data.status);
          loadTasks();
          setRunPage(1);
          loadRuns();
          setTimeout(() => setRunStatus(null), 2000);
        }
      }
    }, 1500);
    return () => clearInterval(t);
  }, [runPolling, id, loadTasks, loadRuns]);

  const handleRun = async () => {
    if (!id) return;
    const r = await agentsApi.run(id);
    if (r.success) {
      message.success(t('agents.runTriggered'));
      setRunStatus('queued');
      setRunPolling(true);
    } else {
      message.error(r.error?.message || t('agents.runFailed'));
    }
  };

  const handlePause = async (tid: string) => {
    await tasksApi.pause(tid);
    loadTasks();
  };
  const handleResume = async (tid: string) => {
    await tasksApi.resume(tid);
    loadTasks();
  };
  const handleRetry = async (tid: string) => {
    await tasksApi.retry(tid);
    loadTasks();
  };
  const handleDeleteTask = (tid: string) => {
    modal.confirm({
      title: t('agents.deleteTaskConfirm'),
      content: t('agents.deleteTaskWarning'),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      onOk: async () => {
        await tasksApi.delete(tid);
        loadTasks();
      },
    });
  };

  const handleConfirm = async (did: string, rid: string) => {
    const r = await decisionsApi.confirm(did, rid);
    if (r.success) {
      message.success(t('dashboard.confirmed'));
      loadDecisions();
      loadTasks();
    } else message.error(r.error?.message || t('dashboard.failed'));
  };
  const handleSkip = async (did: string) => {
    await decisionsApi.skip(did);
    loadDecisions();
  };

  const handleAiPick = async (did: string) => {
    setAiPickLoading(did);
    const r = await decisionsApi.aiPick(did);
    setAiPickLoading(null);
    if (r.success) {
      message.success(t('agents.aiHandled'));
      loadDecisions();
      loadTasks();
    } else {
      message.error(r.error?.message || t('agents.aiHandleFailed'));
    }
  };

  const handleBatch = async (action: 'skip' | 'ai') => {
    if (!id || selectedDecisionIds.length === 0) return;
    setBatchLoading(true);
    const r = await decisionsApi.batch(id, selectedDecisionIds, action);
    setBatchLoading(false);
    if (r.success) {
      const { dispatched, skipped, failed } = r.data;
      message.success(t('agents.batchDone', { dispatched, skipped, failed }));
      setSelectedDecisionIds([]);
      loadDecisions();
      loadTasks();
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };

  const handleCorrectEpisode = async (cid: string) => {
    const draft = episodeDrafts[cid];
    if (draft == null) return;
    setSavingEpisodeCid(cid);
    const r = await resourcesApi.correctEpisode(cid, { episode: draft });
    setSavingEpisodeCid(null);
    if (r.success) {
      message.success(t('agents.episodeSaved'));
      setEpisodeDrafts((prev) => {
        const next = { ...prev };
        delete next[cid];
        return next;
      });
      loadDecisions();
      loadTasks();
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };

  const isAmbiguousDecision = (d: PendingDecision): boolean => {
    const cands = d.candidate_resources;
    return !!cands && cands.length > 0 && cands.every((r) => r.episode_confidence === 'ambiguous');
  };

  const handleSaveFilter = async () => {
    if (!id || !agent) return;
    setSavingFilter(true);
    const r = await agentsApi.update(id, {
      name: agent.name,
      channel_id: agent.channel_id,
      downloader_id: agent.downloader_id,
      filter_config: filterConfig,
    });
    setSavingFilter(false);
    if (r.success) {
      message.success(t('agents.filterSaved'));
      loadAgent();
    } else message.error(r.error?.message || t('agents.saveFailed'));
  };

  const _serializeWorks = () =>
    works.map((w) => ({
      content_type: w.content_type,
      series_id: w.series_id,
      movie_id: w.movie_id,
      enable_episode_dedup: w.enable_episode_dedup,
      filter_overrides: w.filter_overrides,
      display_name_override: w.display_name_override,
    }));

  const doSaveWorks = async (worksList: AgentWork[], dispatchIds: string[]) => {
    if (!id || !agent) return;
    setWorksPreviewSaving(true);
    const r = await agentsApi.update(id, {
      name: agent.name,
      channel_id: agent.channel_id,
      downloader_id: agent.downloader_id,
      works: worksList.map((w) => ({
        content_type: w.content_type,
        series_id: w.series_id,
        movie_id: w.movie_id,
        enable_episode_dedup: w.enable_episode_dedup,
        filter_overrides: w.filter_overrides,
        display_name_override: w.display_name_override,
      })),
      dispatch_resource_ids: dispatchIds,
    });
    setWorksPreviewSaving(false);
    if (r.success) {
      message.success(t('agents.worksSaved'));
      if (r.data.works) {
        setWorks(r.data.works);
        setAgent(r.data);
      }
      setWorksDirty(false);
      setWorksPreview(null);
      setPendingWorksSave(null);
      loadTasks();
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };

  const handleSaveWorks = async () => {
    if (!id || !agent) return;
    setSavingWorks(true);
    try {
      // Preview the rule diff before committing. The works tab only changes
      // works, so scope_channel_wide + filter_config come from the current
      // agent (unchanged).
      const pv = await agentsApi.rulesPreview({
        agent_id: id,
        scope_channel_wide: agent.scope_channel_wide,
        filter_config: agent.filter_config,
        works: _serializeWorks(),
      });
      if (!pv.success) {
        message.error(pv.error?.message || t('agents.previewFailed'));
        return;
      }
      const newly = pv.data.newly_matching;
      const noLonger = pv.data.no_longer_matching;
      if (newly.length > 0 || noLonger.length > 0) {
        const initSel: Record<string, boolean> = {};
        newly.forEach((r) => { initSel[r.id] = true; });
        setWorksPreview(pv.data);
        setWorksPreviewSelected(initSel);
        setPendingWorksSave(works);
        return;
      }
      // No match impact: save directly with empty backfill (still advances
      // the watermark).
      await doSaveWorks(works, []);
    } finally {
      setSavingWorks(false);
    }
  };

  const handleWorksPreviewConfirm = (dispatchIds: string[]) => {
    if (!pendingWorksSave) return;
    doSaveWorks(pendingWorksSave, dispatchIds);
  };

  const handleTestFilters = async () => {
    if (!id) return;
    setTestingFilters(true);
    const r = await agentsApi.testFilters(id);
    setTestingFilters(false);
    if (r.success) setFilterTest(r.data);
    else message.error(r.error?.message || t('agents.testFailed'));
  };

  const taskColumns: TableColumnsType<DownloadTask> = [
    {
      title: t('agents.taskTitle'),
      dataIndex: ['file_resource', 'title_raw'],
      key: 'title',
      // No ellipsis: let the raw title wrap so the user sees the whole thing
      // instead of a truncated single line.
      render: (text: string, record) => (
        <Text style={{ fontSize: 13, wordBreak: 'break-word' }}>
          {text || record.file_resource_id.slice(0, 8)}
        </Text>
      ),
    },
    {
      title: t('agents.taskStatus'),
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (status: string) => <StatusBadge status={status} />,
    },
    {
      title: t('agents.taskProgress'),
      dataIndex: 'progress',
      key: 'progress',
      width: 220,
      // ProgressBar already renders the percentage via its `format` prop —
      // don't duplicate it with a sibling <Text>.
      render: (progress: number) => <ProgressBar progress={progress} />,
    },
    {
      title: t('agents.taskSpeed'),
      key: 'speed',
      width: 180,
      render: (_, record) => {
        // Only show live speed/ETA while the task is actually running; hide
        // the numbers for paused/completed/error/cancelled tasks.
        if (!['pending', 'queued', 'downloading'].includes(record.status)) {
          return <Text type="secondary" style={{ fontSize: 12 }}>—</Text>;
        }
        return (
          <Text type="secondary" style={{ fontSize: 12 }}>
            ↓{formatSpeed(record.download_speed)} · ETA {formatEta(record.eta)}
          </Text>
        );
      },
    },
    {
      title: t('agents.taskDir'),
      dataIndex: 'download_dir',
      key: 'download_dir',
      width: 180,
      ellipsis: true,
      render: (v: string | null) => <Text type="secondary" style={{ fontSize: 12 }}>{v || t('format.dash')}</Text>,
    },
    {
      title: t('agents.taskError'),
      dataIndex: 'error_message',
      key: 'err',
      width: 120,
      ellipsis: true,
      render: (v: string | null) =>
        v ? <Text type="danger" style={{ fontSize: 11 }}>{v}</Text> : null,
    },
    {
      title: t('agents.taskCreatedAt'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 150,
      // Absolute timestamp so users can correlate creation across tasks; the
      // tooltip carries the relative "x ago" for a quick sense of recency.
      render: (v: string) => (
        <Tooltip title={timeAgo(v)}>
          <Text type="secondary" style={{ fontSize: 12 }}>{formatDate(v)}</Text>
        </Tooltip>
      ),
    },
    {
      title: t('agents.taskUpdatedAt'),
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 150,
      render: (v: string) => (
        <Tooltip title={timeAgo(v)}>
          <Text type="secondary" style={{ fontSize: 12 }}>{formatDate(v)}</Text>
        </Tooltip>
      ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 160,
      align: 'right',
      render: (_, record) => (
        <Space size={0}>
          {record.status === 'downloading' && (
            <Button type="text" size="small" icon={<Pause size={14} />} onClick={() => handlePause(record.id)} />
          )}
          {record.status === 'paused' && (
            <Button type="text" size="small" icon={<Play size={14} style={{ color: '#003c33' }} />} onClick={() => handleResume(record.id)} />
          )}
          {(record.status === 'error' || record.status === 'paused') && (
            <Button type="text" size="small" icon={<RotateCcw size={14} style={{ color: '#1863dc' }} />} onClick={() => handleRetry(record.id)} />
          )}
          <Button type="text" size="small" danger icon={<Trash2 size={14} />} onClick={() => handleDeleteTask(record.id)} />
        </Space>
      ),
    },
  ];

  if (loadingAgent || !agent) {
    return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  }

  return (
    <div>
      <Space align="center" style={{ marginBottom: 24 }}>
        <Link to="/agents">
          <Button type="text" icon={<ArrowLeft size={18} />} />
        </Link>
        <Title level={3} style={{ margin: 0 }}>
          {agent.name}
        </Title>
        <StatusBadge status={agent.status} />
        <Link to={`/agents/${id}/edit`}>
          <Button size="small" icon={<Edit size={12} />}>{t('common.edit')}</Button>
        </Link>
      </Space>

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space size="large" wrap>
          <Text type="secondary">
            {t('agents.channelLabel')}
            <Link to={`/channels/${agent.channel_id}`}>
              <Text>{agent.channel?.name || agent.channel_id.slice(0, 8)}</Text>
            </Link>
          </Text>
          <Text type="secondary">
            {t('agents.downloaderLabel')}
            <Link to={`/downloaders/${agent.downloader_id}`}>
              <Text>{agent.downloader?.name || agent.downloader_id?.slice(0, 8)}</Text>
            </Link>
          </Text>
          <Text type="secondary">
            {t('agents.subdirLabel')}{agent.download_subdir || t('format.dash')}
          </Text>
          <Text type="secondary">
            {t('agents.scopeLabel')}{agent.scope_channel_wide ? t('agents.channelWide') : t('agents.worksCount', { n: works.length })}
          </Text>
          <Text type="secondary">
            {t('agents.conflictLabel')}{agent.conflict_resolution === 'auto' ? t('agents.auto') : t('agents.ask')}
          </Text>
          <Text type="secondary">
            {t('agents.llmLabel')}{agent.llm_enabled ? t('agents.on') : t('agents.off')}
          </Text>
          {agent.last_run_at && (
            <Text type="secondary">{t('agents.lastRunLabel')}{timeAgo(agent.last_run_at)}</Text>
          )}
        </Space>
      </Card>

      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          {
            key: 'works',
            label: `${t('agents.subscribedWorks')} (${works.length})`,
            children: (
              <Card loading={loadingWorks}>
                <WorkSelector
                  value={works}
                  onChange={(w) => {
                    setWorks(w);
                    setWorksDirty(true);
                  }}
                  maxWorks={10}
                  channelId={agent.channel_id}
                />
                <Divider />
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    gap: 12,
                  }}
                >
                  <Alert
                    type="info"
                    showIcon
                    message={t('agents.worksEditNote')}
                    style={{ flex: 1 }}
                  />
                  <Button
                    type="primary"
                    loading={savingWorks}
                    disabled={!worksDirty}
                    onClick={handleSaveWorks}
                  >
                    {t('common.save')}
                  </Button>
                </div>
                <BackfillPreviewModal
                  open={!!worksPreview}
                  data={worksPreview}
                  selected={worksPreviewSelected}
                  onSelectedChange={setWorksPreviewSelected}
                  onCancel={() => { setWorksPreview(null); setPendingWorksSave(null); }}
                  onConfirm={handleWorksPreviewConfirm}
                  onSkip={() => handleWorksPreviewConfirm([])}
                  saving={worksPreviewSaving}
                />
              </Card>
            ),
          },
          {
            key: 'tasks',
            label: `${t('agents.downloadTasks')} (${taskTotal})`,
            children: (
              <Card>
                <Space style={{ marginBottom: 12 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>{t('agents.statusFilter')}</Text>
                  <Select
                    allowClear
                    placeholder={t('common.all')}
                    style={{ width: 140 }}
                    value={taskStatus}
                    onChange={(v) => {
                      setTaskStatus(v);
                      setTaskPage(1);
                    }}
                    options={[
                      { value: 'pending', label: t('status.pending') },
                      { value: 'queued', label: t('status.queued') },
                      { value: 'downloading', label: t('status.downloading') },
                      { value: 'paused', label: t('status.paused') },
                      { value: 'completed', label: t('status.completed') },
                      { value: 'error', label: t('status.error') },
                    ]}
                  />
                </Space>
                <Table<DownloadTask>
                  columns={taskColumns}
                  dataSource={tasks}
                  rowKey="id"
                  loading={loadingTasks}
                  size="small"
                  pagination={{
                    current: taskPage,
                    pageSize: 20,
                    total: taskTotal,
                    onChange: setTaskPage,
                    showSizeChanger: false,
                  }}
                  locale={{ emptyText: <Empty description={t('agents.noTasks')} /> }}
                />
              </Card>
            ),
          },
          {
            key: 'decisions',
            label: `${t('dashboard.pendingDecisions')} (${decTotal})`,
            children: (
              <Card>
                <Spin spinning={loadingDec}>
                  {decisions.length === 0 ? (
                    <Empty description={t('dashboard.noPendingDecisions')} />
                  ) : (
                    <>
                      <Space style={{ marginBottom: 12 }}>
                        <Button
                          size="small"
                          loading={batchLoading}
                          disabled={selectedDecisionIds.length === 0}
                          onClick={() => handleBatch('skip')}
                        >
                          {t('agents.batchSkip', { n: selectedDecisionIds.length })}
                        </Button>
                        <Button
                          size="small"
                          type="primary"
                          loading={batchLoading}
                          disabled={selectedDecisionIds.length === 0}
                          onClick={() => handleBatch('ai')}
                        >
                          {t('agents.batchAi', { n: selectedDecisionIds.length })}
                        </Button>
                      </Space>
                      <Space direction="vertical" style={{ width: '100%' }} size={12}>
                        {decisions.map((d) => {
                          const ambiguous = isAmbiguousDecision(d);
                          const checked = selectedDecisionIds.includes(d.id);
                          return (
                        <Card key={d.id} size="small">
                          <div
                            style={{
                              display: 'flex',
                              justifyContent: 'space-between',
                              alignItems: 'flex-start',
                              marginBottom: 12,
                            }}
                          >
                            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                              <Checkbox
                                checked={checked}
                                onChange={(e) => {
                                  setSelectedDecisionIds((prev) =>
                                    e.target.checked
                                      ? [...prev, d.id]
                                      : prev.filter((x) => x !== d.id),
                                  );
                                }}
                              />
                              <div>
                                <Text strong>{d.reason}</Text>
                                <div style={{ fontSize: 12, color: '#93939f', marginTop: 4 }}>
                                  {t('agents.candidateCount', { n: d.candidates.length })} · {timeAgo(d.created_at)}
                                </div>
                                {ambiguous && (
                                  <div style={{ fontSize: 12, color: '#b88500', marginTop: 4 }}>
                                    {t('agents.ambiguousHint')}
                                  </div>
                                )}
                                {d.llm_suggestion && (
                                  <div
                                    style={{
                                      marginTop: 8,
                                      padding: 8,
                                      borderRadius: 6,
                                      background: '#f1f5ff',
                                      border: '1px solid #b8cdf7',
                                      fontSize: 12,
                                      color: '#1863dc',
                                    }}
                                  >
                                    <strong>{t('dashboard.aiSuggestion')}</strong>
                                    {d.llm_suggestion}
                                  </div>
                                )}
                              </div>
                            </div>
                            <Space size={6}>
                              {!ambiguous && (
                                <Button
                                  size="small"
                                  type="primary"
                                  loading={aiPickLoading === d.id}
                                  onClick={() => handleAiPick(d.id)}
                                >
                                  {t('agents.aiHandle')}
                                </Button>
                              )}
                              <Button size="small" onClick={() => handleSkip(d.id)}>
                                <SkipForward size={12} /> {t('common.skip')}
                              </Button>
                            </Space>
                          </div>
                          <Space direction="vertical" style={{ width: '100%' }} size={6}>
                            {d.candidates.map((cid) => {
                              const r = candidateCache[cid] ?? d.candidate_resources?.find((x) => x.id === cid);
                              const isAiPick = !ambiguous && cid === d.llm_picked_resource_id;
                              if (ambiguous) {
                                const draft = episodeDrafts[cid] ?? r?.episode ?? null;
                                return (
                                  <div
                                    key={cid}
                                    style={{
                                      display: 'flex',
                                      justifyContent: 'space-between',
                                      alignItems: 'center',
                                      padding: '8px 12px',
                                      borderRadius: 6,
                                      border: '1px solid #e5e7eb',
                                      gap: 12,
                                    }}
                                  >
                                    <div style={{ flex: 1, minWidth: 0 }}>
                                      <Text ellipsis style={{ fontSize: 13 }}>
                                        {r?.title_cn || r?.title_raw || cid.slice(0, 8)}
                                      </Text>
                                      <Space size={4} wrap style={{ fontSize: 11, color: '#93939f', marginTop: 2 }}>
                                        {r?.subtitle_group && <Tag style={{ margin: 0 }}>{r.subtitle_group}</Tag>}
                                        {r?.resolution && <Tag style={{ margin: 0 }}>{r.resolution}</Tag>}
                                        {r?.episode != null && (
                                          <span>{t('agents.rawEpisode', { n: r.episode })}</span>
                                        )}
                                      </Space>
                                    </div>
                                    <Space size={6}>
                                      <InputNumber
                                        size="small"
                                        min={1}
                                        value={draft}
                                        placeholder={t('agents.correctEpisodePlaceholder')}
                                        onChange={(v) =>
                                          setEpisodeDrafts((prev) => ({ ...prev, [cid]: v as number | null }))
                                        }
                                        style={{ width: 90 }}
                                      />
                                      <Button
                                        type="primary"
                                        size="small"
                                        loading={savingEpisodeCid === cid}
                                        disabled={episodeDrafts[cid] == null}
                                        onClick={() => handleCorrectEpisode(cid)}
                                      >
                                        {t('agents.correctEpisode')}
                                      </Button>
                                    </Space>
                                  </div>
                                );
                              }
                              return (
                                <div
                                  key={cid}
                                  style={{
                                    display: 'flex',
                                    justifyContent: 'space-between',
                                    alignItems: 'center',
                                    padding: '8px 12px',
                                    borderRadius: 6,
                                    border: `1px solid ${isAiPick ? '#1863dc' : '#e5e7eb'}`,
                                    background: isAiPick ? '#f1f5ff' : 'transparent',
                                    gap: 12,
                                  }}
                                >
                                  {r ? (
                                    <div style={{ flex: 1, minWidth: 0 }}>
                                      <Space size={6} align="center" style={{ marginBottom: 2 }}>
                                        <Text ellipsis style={{ fontSize: 13 }}>
                                          {r.title_cn || r.title_raw}
                                        </Text>
                                        {isAiPick && (
                                          <Tag color="blue" style={{ margin: 0 }}>{t('agents.aiPickTag')}</Tag>
                                        )}
                                      </Space>
                                      <Space size={4} wrap style={{ fontSize: 11, color: '#93939f', marginTop: 2 }}>
                                        {r.subtitle_group && <Tag style={{ margin: 0 }}>{r.subtitle_group}</Tag>}
                                        {r.resolution && <Tag style={{ margin: 0 }}>{r.resolution}</Tag>}
                                        {r.video_codec && <Tag style={{ margin: 0 }}>{r.video_codec}</Tag>}
                                        {r.file_size != null && <span>{formatBytes(r.file_size)}</span>}
                                      </Space>
                                    </div>
                                  ) : (
                                    <Text type="secondary" style={{ fontSize: 12 }}>{t('common.loading')}</Text>
                                  )}
                                  <Button
                                    type="primary"
                                    size="small"
                                    icon={<CheckCircle size={12} />}
                                    onClick={() => handleConfirm(d.id, cid)}
                                  >
                                    {t('common.confirm')}
                                  </Button>
                                </div>
                              );
                            })}
                          </Space>
                        </Card>
                        );
                      })}
                      </Space>
                    </>
                  )}
                </Spin>
              </Card>
            ),
          },
          {
            key: 'filters',
            label: t('agents.filter'),
            children: (
              <div>
                <Card style={{ marginBottom: 16 }}>
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      marginBottom: 12,
                    }}
                  >
                    <Text strong>{t('agents.globalFilter')}</Text>
                    <Space>
                      <Button
                        icon={<FlaskConical size={14} />}
                        onClick={handleTestFilters}
                        loading={testingFilters}
                      >
                        {t('agents.test')}
                      </Button>
                      <Button type="primary" onClick={handleSaveFilter} loading={savingFilter}>
                        {t('common.save')}
                      </Button>
                    </Space>
                  </div>
                  <FilterBuilder value={filterConfig} onChange={setFilterConfig} />
                </Card>

                {filterTest && (
                  <Card title={t('agents.testResults')} size="small">
                    <Row gutter={16} style={{ marginBottom: 16 }}>
                      <Col span={8}>
                        <Statistic title={t('agents.totalResources')} value={filterTest.stats.total} />
                      </Col>
                      <Col span={8}>
                        <Statistic
                          title={t('agents.passed')}
                          value={filterTest.stats.passed}
                          valueStyle={{ color: '#003c33' }}
                        />
                      </Col>
                      <Col span={8}>
                        <Statistic
                          title={t('agents.failed')}
                          value={filterTest.stats.failed}
                          valueStyle={{ color: '#b30000' }}
                        />
                      </Col>
                    </Row>
                    <div style={{ maxHeight: 500, overflow: 'auto' }}>
                      {filterTest.results.map((r) => (
                        <div
                          key={r.resource_id}
                          style={{
                            padding: 10,
                            marginBottom: 6,
                            borderRadius: 6,
                            border: `1px solid ${r.passed ? '#8fbfb7' : '#f2b8b8'}`,
                            background: r.passed
                              ? '#edfce9'
                              : '#fff1f0',
                          }}
                        >
                          <Space style={{ marginBottom: 4 }}>
                            {r.passed ? (
                              <CheckCircle size={14} color="#003c33" />
                            ) : (
                              <Tag color="error">FAIL</Tag>
                            )}
                            <Text strong ellipsis style={{ fontSize: 13 }}>
                              {r.title}
                            </Text>
                          </Space>
                          <div>
                            {r.conditions.map((c, i) => (
                              <Tag
                                key={i}
                                color={c.passed ? 'green' : 'red'}
                                style={{ fontSize: 11, margin: 2 }}
                              >
                                {c.field} {c.operator} {String(c.value)}
                              </Tag>
                            ))}
                          </div>
                        </div>
                      ))}
                    </div>
                  </Card>
                )}
              </div>
            ),
          },
          {
            key: 'run',
            label: t('agents.runControl'),
            children: (
              <Card>
                <Space direction="vertical" size={16} style={{ width: '100%' }}>
                  <Text type="secondary">
                    {t('agents.runControlDesc')}
                  </Text>
                  <div>
                    <Tooltip title={t('agents.runNowHint')}>
                      <Button
                        type="primary"
                        size="large"
                        icon={<PlayCircle size={16} />}
                        loading={runPolling}
                        onClick={handleRun}
                      >
                        {t('agents.runNow')}
                      </Button>
                    </Tooltip>
                  </div>
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
                    <Text strong style={{ display: 'block', marginBottom: 8 }}>
                      {t('agents.runHistory')}
                    </Text>
                    <Table<AgentRun>
                      columns={runColumns(t, (r) => setRunDrawerRun(r))}
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
              </Card>
            ),
          },
        ]}
      />

      <Drawer
        open={!!runDrawerRun}
        onClose={() => setRunDrawerRun(null)}
        title={runDrawerRun ? `${t('agents.runMatchedResources')} · ${timeAgo(runDrawerRun.started_at)}` : ''}
        width={680}
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
                      border: '1px solid #e5e7eb',
                      borderRadius: 8,
                      background: '#fafafa',
                    }}
                  >
                    <Text strong style={{ fontSize: 13, color: '#17171c', wordBreak: 'break-word' }}>
                      {r.title_cn || r.title_raw}
                    </Text>
                    <Space size={4} wrap style={{ fontSize: 11, color: '#616161', marginTop: 4 }}>
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
    </div>
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
    render: (v: string) => <Text type="secondary" style={{ fontSize: 12 }}>{timeAgo(v)}</Text>,
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
        disabled={r.matched_resources.length === 0}
        onClick={() => onView(r)}
      >
        {t('agents.viewResources', { n: r.matched_resources.length })}
      </Button>
    ),
  },
];
