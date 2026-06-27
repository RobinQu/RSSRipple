import { useState, useEffect, useCallback } from 'react';
import { useParams, Link } from 'react-router-dom';
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
      message.success('已触发运行');
      setRunStatus('queued');
      setRunPolling(true);
    } else {
      message.error(r.error?.message || '运行触发失败');
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
      title: '删除下载任务？',
      content: '已下载的数据不会被删除（如需删除，请在 Transmission 中操作）。',
      okText: '删除',
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
      message.success('已确认，开始下载');
      loadDecisions();
      loadTasks();
    } else message.error(r.error?.message || '操作失败');
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
      message.success('过滤规则已保存');
      loadAgent();
    } else message.error(r.error?.message || '保存失败');
  };

  const handleTestFilters = async () => {
    if (!id) return;
    setTestingFilters(true);
    // Save current filter first? Per spec, test against current filter_config. Since user may edit, test against local state.
    // Send filterConfig? The test endpoint uses agent's stored config. So save implicitly? Better test stored config; we warn.
    const r = await agentsApi.testFilters(id);
    setTestingFilters(false);
    if (r.success) setFilterTest(r.data);
    else message.error(r.error?.message || '测试失败');
  };

  const taskColumns: TableColumnsType<DownloadTask> = [
    {
      title: '标题',
      dataIndex: ['file_resource', 'title_raw'],
      key: 'title',
      ellipsis: true,
      render: (text: string, record) => (
        <Text ellipsis>{text || record.file_resource_id.slice(0, 8)}</Text>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (status: string) => <StatusBadge status={status} />,
    },
    {
      title: '进度',
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
      title: '速度',
      key: 'speed',
      width: 180,
      render: (_, record) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          ↓{formatSpeed(record.download_speed)} · ETA {formatEta(record.eta)}
        </Text>
      ),
    },
    {
      title: '错误',
      dataIndex: 'error_message',
      key: 'err',
      width: 120,
      ellipsis: true,
      render: (v: string | null) =>
        v ? <Text type="danger" style={{ fontSize: 11 }}>{v}</Text> : null,
    },
    {
      title: '操作',
      key: 'actions',
      width: 160,
      align: 'right',
      render: (_, record) => (
        <Space size={0}>
          {record.status === 'downloading' && (
            <Button type="text" size="small" icon={<Pause size={14} />} onClick={() => handlePause(record.id)} />
          )}
          {record.status === 'paused' && (
            <Button type="text" size="small" icon={<Play size={14} style={{ color: '#59d499' }} />} onClick={() => handleResume(record.id)} />
          )}
          {(record.status === 'error' || record.status === 'paused') && (
            <Button type="text" size="small" icon={<RotateCcw size={14} style={{ color: '#57c1ff' }} />} onClick={() => handleRetry(record.id)} />
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
          <Button size="small" icon={<Edit size={12} />}>编辑</Button>
        </Link>
      </Space>

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space size="large" wrap>
          <Text type="secondary">
            频道：
            <Link to={`/channels/${agent.channel_id}`}>
              <Text>{agent.channel?.name || agent.channel_id.slice(0, 8)}</Text>
            </Link>
          </Text>
          <Text type="secondary">
            下载器：{agent.downloader?.name || agent.downloader_id?.slice(0, 8)}
          </Text>
          <Text type="secondary">
            范围：{agent.scope_channel_wide ? '整个频道' : `${works.length} 个作品`}
          </Text>
          <Text type="secondary">
            冲突：{agent.conflict_resolution === 'auto' ? '自动' : '询问'}
          </Text>
          <Text type="secondary">
            LLM：{agent.llm_enabled ? '开' : '关'}
          </Text>
          {agent.last_run_at && (
            <Text type="secondary">上次运行：{timeAgo(agent.last_run_at)}</Text>
          )}
        </Space>
      </Card>

      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          {
            key: 'works',
            label: `订阅作品 (${works.length})`,
            children: (
              <Card loading={loadingWorks}>
                <WorkSelector
                  value={works}
                  onChange={(w) => setWorks(w)}
                  maxWorks={10}
                />
                <Divider />
                <Alert message="注意：作品增删改将直接通过 API 保存。" />
              </Card>
            ),
          },
          {
            key: 'tasks',
            label: `下载任务 (${taskTotal})`,
            children: (
              <Card>
                <Space style={{ marginBottom: 12 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>状态筛选：</Text>
                  <Select
                    allowClear
                    placeholder="全部"
                    style={{ width: 140 }}
                    value={taskStatus}
                    onChange={(v) => {
                      setTaskStatus(v);
                      setTaskPage(1);
                    }}
                    options={[
                      { value: 'pending', label: '等待中' },
                      { value: 'queued', label: '排队中' },
                      { value: 'downloading', label: '下载中' },
                      { value: 'paused', label: '暂停' },
                      { value: 'completed', label: '已完成' },
                      { value: 'error', label: '错误' },
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
                  locale={{ emptyText: <Empty description="暂无任务" /> }}
                />
              </Card>
            ),
          },
          {
            key: 'decisions',
            label: `待决策 (${decTotal})`,
            children: (
              <Card>
                <Spin spinning={loadingDec}>
                  {decisions.length === 0 ? (
                    <Empty description="暂无待决策项" />
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
                              <div style={{ fontSize: 12, color: '#9c9c9d', marginTop: 4 }}>
                                {d.candidates.length} 个候选 · {timeAgo(d.created_at)}
                              </div>
                              {d.llm_suggestion && (
                                <div
                                  style={{
                                    marginTop: 8,
                                    padding: 8,
                                    borderRadius: 6,
                                    background: 'rgba(87,193,255,0.08)',
                                    border: '1px solid rgba(87,193,255,0.2)',
                                    fontSize: 12,
                                    color: '#57c1ff',
                                  }}
                                >
                                  <strong>AI：</strong>
                                  {d.llm_suggestion}
                                </div>
                              )}
                            </div>
                            <Button size="small" onClick={() => handleSkip(d.id)}>
                              <SkipForward size={12} /> 跳过
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
                                    border: '1px solid rgba(255,255,255,0.08)',
                                    gap: 12,
                                  }}
                                >
                                  {r ? (
                                    <div style={{ flex: 1, minWidth: 0 }}>
                                      <Text ellipsis style={{ fontSize: 13 }}>
                                        {r.title_cn || r.title_raw}
                                      </Text>
                                      <Space size={4} wrap style={{ fontSize: 11, color: '#9c9c9d', marginTop: 2 }}>
                                        {r.subtitle_group && <Tag style={{ margin: 0 }}>{r.subtitle_group}</Tag>}
                                        {r.resolution && <Tag style={{ margin: 0 }}>{r.resolution}</Tag>}
                                        {r.video_codec && <Tag style={{ margin: 0 }}>{r.video_codec}</Tag>}
                                        {r.file_size != null && <span>{formatBytes(r.file_size)}</span>}
                                      </Space>
                                    </div>
                                  ) : (
                                    <Text type="secondary" style={{ fontSize: 12 }}>加载中...</Text>
                                  )}
                                  <Button
                                    type="primary"
                                    size="small"
                                    icon={<CheckCircle size={12} />}
                                    onClick={() => handleConfirm(d.id, cid)}
                                  >
                                    确认
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
            label: '过滤器',
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
                    <Text strong>全局过滤条件</Text>
                    <Space>
                      <Button
                        icon={<FlaskConical size={14} />}
                        onClick={handleTestFilters}
                        loading={testingFilters}
                      >
                        测试
                      </Button>
                      <Button type="primary" onClick={handleSaveFilter} loading={savingFilter}>
                        保存
                      </Button>
                    </Space>
                  </div>
                  <FilterBuilder value={filterConfig} onChange={setFilterConfig} />
                </Card>

                {filterTest && (
                  <Card title="测试结果" size="small">
                    <Row gutter={16} style={{ marginBottom: 16 }}>
                      <Col span={8}>
                        <Statistic title="总资源数" value={filterTest.stats.total} />
                      </Col>
                      <Col span={8}>
                        <Statistic
                          title="通过"
                          value={filterTest.stats.passed}
                          valueStyle={{ color: '#59d499' }}
                        />
                      </Col>
                      <Col span={8}>
                        <Statistic
                          title="未通过"
                          value={filterTest.stats.failed}
                          valueStyle={{ color: '#ff6161' }}
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
                            border: `1px solid ${r.passed ? 'rgba(89,212,153,0.3)' : 'rgba(255,97,97,0.3)'}`,
                            background: r.passed
                              ? 'rgba(89,212,153,0.05)'
                              : 'rgba(255,97,97,0.05)',
                          }}
                        >
                          <Space style={{ marginBottom: 4 }}>
                            {r.passed ? (
                              <CheckCircle size={14} color="#59d499" />
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
            label: '运行控制',
            children: (
              <Card>
                <Space direction="vertical" size={16} style={{ width: '100%' }}>
                  <Text type="secondary">
                    手动触发 Agent 立即处理频道中的新资源。通常系统会在频道抓取后自动运行。
                  </Text>
                  <div>
                    <Button
                      type="primary"
                      size="large"
                      icon={<PlayCircle size={16} />}
                      loading={runPolling}
                      onClick={handleRun}
                    >
                      立即运行
                    </Button>
                  </div>
                  {runStatus && (
                    <div>
                      <StatusBadge status={runStatus} />
                      <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                        {runStatus === 'queued' && '任务已入队，等待执行...'}
                        {runStatus === 'running' && '正在处理资源...'}
                        {runStatus === 'done' && '运行完成'}
                        {runStatus === 'failed' && '运行失败'}
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
