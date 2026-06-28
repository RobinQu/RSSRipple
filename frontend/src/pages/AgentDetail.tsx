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
import { formatBytes, formatSpeed, formatEta, timeAgo } from '../utils/format';
import type {
  Agent,
  AgentWork,
  DownloadTask,
  FileResource,
  PendingDecision,
  ResourceTestResult,
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

  // Works
  const [works, setWorks] = useState<AgentWork[]>([]);
  const [loadingWorks, setLoadingWorks] = useState(false);

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
    if (r.success) setWorks(r.data);
    setLoadingWorks(false);
  }, [id]);

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
    if (tab === 'works') loadWorks();
  }, [tab, loadWorks]);

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
          setTimeout(() => setRunStatus(null), 2000);
        }
      }
    }, 1500);
    return () => clearInterval(t);
  }, [runPolling, id, loadTasks]);

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
      ellipsis: true,
      render: (text: string, record) => (
        <Text ellipsis>{text || record.file_resource_id.slice(0, 8)}</Text>
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
      render: (progress: number) => (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ flex: 1 }}>
            <ProgressBar progress={progress} />
          </div>
          <Text type="secondary" style={{ fontSize: 12, minWidth: 40 }}>
            {(progress * 100).toFixed(0)}%
          </Text>
        </div>
      ),
    },
    {
      title: t('agents.taskSpeed'),
      key: 'speed',
      width: 180,
      render: (_, record) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          ↓{formatSpeed(record.download_speed)} · ETA {formatEta(record.eta)}
        </Text>
      ),
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
            {t('agents.downloaderLabel')}{agent.downloader?.name || agent.downloader_id?.slice(0, 8)}
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
                  onChange={(w) => setWorks(w)}
                  maxWorks={10}
                />
                <Divider />
                <Alert message={t('agents.worksEditNote')} />
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
                    <Space direction="vertical" style={{ width: '100%' }} size={12}>
                      {decisions.map((d) => (
                        <Card key={d.id} size="small">
                          <div
                            style={{
                              display: 'flex',
                              justifyContent: 'space-between',
                              alignItems: 'flex-start',
                              marginBottom: 12,
                            }}
                          >
                            <div>
                              <Text strong>{d.reason}</Text>
                              <div style={{ fontSize: 12, color: '#93939f', marginTop: 4 }}>
                                {t('agents.candidateCount', { n: d.candidates.length })} · {timeAgo(d.created_at)}
                              </div>
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
                            <Button size="small" onClick={() => handleSkip(d.id)}>
                              <SkipForward size={12} /> {t('common.skip')}
                            </Button>
                          </div>
                          <Space direction="vertical" style={{ width: '100%' }} size={6}>
                            {d.candidates.map((cid) => {
                              const r = candidateCache[cid];
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
                                  {r ? (
                                    <div style={{ flex: 1, minWidth: 0 }}>
                                      <Text ellipsis style={{ fontSize: 13 }}>
                                        {r.title_cn || r.title_raw}
                                      </Text>
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
                      ))}
                    </Space>
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
                    <Button
                      type="primary"
                      size="large"
                      icon={<PlayCircle size={16} />}
                      loading={runPolling}
                      onClick={handleRun}
                    >
                      {t('agents.runNow')}
                    </Button>
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
                </Space>
              </Card>
            ),
          },
        ]}
      />
    </div>
  );
}
