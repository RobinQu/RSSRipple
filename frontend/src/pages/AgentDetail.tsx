import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import {
  Tabs,
  Table,
  Button,
  Space,
  Card,
  Tag,
  Typography,
  Empty,
  Row,
  Col,
  Statistic,
  Spin,
  App,
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
  Loader,
} from 'lucide-react';
import { agentsApi } from '../api/agents';
import { tasksApi, decisionsApi } from '../api/tasks';
import StatusBadge from '../components/StatusBadge';
import ProgressBar from '../components/ProgressBar';
import Pagination from '../components/Pagination';
import { formatSpeed, formatEta, timeAgo } from '../utils/format';
import type {
  Agent,
  DownloadTask,
  PendingDecision,
  FilterTestResponse,
} from '../types';

export default function AgentDetail() {
  const { id } = useParams<{ id: string }>();
  const { message } = App.useApp();
  const [agent, setAgent] = useState<Agent | null>(null);
  const [tab, setTab] = useState<'tasks' | 'decisions' | 'filters'>('tasks');
  const [tasks, setTasks] = useState<DownloadTask[]>([]);
  const [decisions, setDecisions] = useState<PendingDecision[]>([]);
  const [taskPage, setTaskPage] = useState(1);
  const [taskTotal, setTaskTotal] = useState(0);
  const [decPage, setDecPage] = useState(1);
  const [decTotal, setDecTotal] = useState(0);
  const [filterTest, setFilterTest] = useState<FilterTestResponse | null>(null);
  const [testingFilters, setTestingFilters] = useState(false);
  const [loadingTasks, setLoadingTasks] = useState(false);
  const [loadingDecisions, setLoadingDecisions] = useState(false);

  useEffect(() => {
    if (id) agentsApi.get(id).then((r) => { if (r.success) setAgent(r.data); });
  }, [id]);

  useEffect(() => {
    if (!id) return;
    setLoadingTasks(true);
    tasksApi.listByAgent(id, taskPage).then((r) => {
      if (r.success) {
        setTasks(r.data);
        if (r.meta) setTaskTotal(r.meta.total);
      }
      setLoadingTasks(false);
    });
  }, [id, taskPage]);

  useEffect(() => {
    if (!id) return;
    setLoadingDecisions(true);
    decisionsApi.listByAgent(id, decPage).then((r) => {
      if (r.success) {
        setDecisions(r.data);
        if (r.meta) setDecTotal(r.meta.total);
      }
      setLoadingDecisions(false);
    });
  }, [id, decPage]);

  const refreshTasks = () => setTaskPage((p) => p);
  const refreshDecisions = () => setDecPage((p) => p);

  const handlePause = async (taskId: string) => {
    await tasksApi.pause(taskId);
    refreshTasks();
  };
  const handleResume = async (taskId: string) => {
    await tasksApi.resume(taskId);
    refreshTasks();
  };
  const handleRetry = async (taskId: string) => {
    await tasksApi.retry(taskId);
    refreshTasks();
  };
  const handleDeleteTask = async (taskId: string) => {
    await tasksApi.delete(taskId);
    refreshTasks();
  };

  const handleConfirm = async (decisionId: string, resourceId: string) => {
    const res = await decisionsApi.confirm(decisionId, resourceId);
    if (res.success) {
      message.success('Decision confirmed');
      refreshDecisions();
    }
  };
  const handleSkip = async (decisionId: string) => {
    const res = await decisionsApi.skip(decisionId);
    if (res.success) {
      message.success('Decision skipped');
      refreshDecisions();
    }
  };

  const handleTestFilters = async () => {
    if (!id) return;
    setTestingFilters(true);
    const res = await agentsApi.testFilters(id);
    setTestingFilters(false);
    if (res.success) setFilterTest(res.data);
  };

  const taskColumns: TableColumnsType<DownloadTask> = [
    {
      title: 'Title',
      dataIndex: ['file_resource', 'title_raw'],
      key: 'title',
      ellipsis: true,
      render: (text: string, record: DownloadTask) =>
        text || record.id.slice(0, 8),
    },
    {
      title: 'Status',
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (status: string) => <StatusBadge status={status} />,
    },
    {
      title: 'Progress',
      dataIndex: 'progress',
      key: 'progress',
      width: 200,
      render: (progress: number) => (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ flex: 1 }}>
            <ProgressBar progress={progress} />
          </div>
          <Typography.Text type="secondary" style={{ fontSize: 12, minWidth: 40 }}>
            {progress.toFixed(0)}%
          </Typography.Text>
        </div>
      ),
    },
    {
      title: 'Speed',
      key: 'speed',
      width: 160,
      render: (_: unknown, record: DownloadTask) => (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {formatSpeed(record.download_speed)} ETA:{formatEta(record.eta)}
        </Typography.Text>
      ),
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 160,
      align: 'right',
      render: (_: unknown, record: DownloadTask) => (
        <Space size={4}>
          {record.status === 'downloading' && (
            <Button
              type="text"
              size="small"
              icon={<Pause size={14} />}
              onClick={() => handlePause(record.id)}
              title="Pause"
            />
          )}
          {record.status === 'paused' && (
            <Button
              type="text"
              size="small"
              icon={<Play size={14} style={{ color: '#59d499' }} />}
              onClick={() => handleResume(record.id)}
              title="Resume"
            />
          )}
          {record.status === 'error' && (
            <Button
              type="text"
              size="small"
              icon={<RotateCcw size={14} style={{ color: '#57c1ff' }} />}
              onClick={() => handleRetry(record.id)}
              title="Retry"
            />
          )}
          <Button
            type="text"
            size="small"
            danger
            icon={<Trash2 size={14} />}
            onClick={() => handleDeleteTask(record.id)}
            title="Delete"
          />
        </Space>
      ),
    },
  ];

  if (!agent) {
    return (
      <div style={{ textAlign: 'center', padding: 48 }}>
        <Spin />
      </div>
    );
  }

  return (
    <div>
      <div style={{ marginBottom: 24 }}>
        <Space align="center" size={12}>
          <Typography.Title level={3} style={{ margin: 0 }}>
            {agent.name}
          </Typography.Title>
          <StatusBadge status={agent.status} />
        </Space>
        <Typography.Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
          {agent.filters?.length || 0} filters · LLM: {agent.llm_enabled ? 'On' : 'Off'}
        </Typography.Text>
      </div>

      <Tabs
        activeKey={tab}
        onChange={(key) => setTab(key as 'tasks' | 'decisions' | 'filters')}
        items={[
          {
            key: 'tasks',
            label: `Download Tasks (${taskTotal})`,
            children: (
              <Table<DownloadTask>
                columns={taskColumns}
                dataSource={tasks}
                rowKey="id"
                loading={loadingTasks}
                pagination={{
                  current: taskPage,
                  pageSize: 20,
                  total: taskTotal,
                  onChange: setTaskPage,
                  showSizeChanger: false,
                }}
                locale={{ emptyText: <Empty description="No tasks yet" /> }}
              />
            ),
          },
          {
            key: 'decisions',
            label: `Pending Decisions (${decTotal})`,
            children: (
              <Spin spinning={loadingDecisions}>
                {decisions.length === 0 ? (
                  <Empty description="No pending decisions" />
                ) : (
                  <Space direction="vertical" style={{ width: '100%' }} size={12}>
                    {decisions.map((d) => (
                      <Card key={d.id} size="small">
                        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                          <div>
                            <Typography.Text strong>{d.reason}</Typography.Text>
                            <Typography.Text
                              type="secondary"
                              style={{ display: 'block', fontSize: 12 }}
                            >
                              {d.candidates.length} candidates · {timeAgo(d.created_at)}
                            </Typography.Text>
                            {d.llm_suggestion && (
                              <Typography.Text
                                type="secondary"
                                style={{ display: 'block', fontSize: 12, color: '#57c1ff' }}
                              >
                                LLM: {d.llm_suggestion}
                              </Typography.Text>
                            )}
                          </div>
                          <StatusBadge status={d.status} />
                        </div>
                        {d.status === 'pending' && (
                          <Space style={{ marginTop: 12 }}>
                            {d.candidates.map((cid) => (
                              <Button
                                key={cid}
                                size="small"
                                type="primary"
                                icon={<CheckCircle size={12} />}
                                onClick={() => handleConfirm(d.id, cid)}
                              >
                                {cid.slice(0, 8)}
                              </Button>
                            ))}
                            <Button
                              size="small"
                              icon={<SkipForward size={12} />}
                              onClick={() => handleSkip(d.id)}
                            >
                              Skip
                            </Button>
                          </Space>
                        )}
                      </Card>
                    ))}
                  </Space>
                )}
                <div style={{ marginTop: 16 }}>
                  <Pagination
                    page={decPage}
                    pageSize={20}
                    total={decTotal}
                    onPageChange={setDecPage}
                  />
                </div>
              </Spin>
            ),
          },
          {
            key: 'filters',
            label: `Filters (${agent.filters?.length || 0})`,
            children: (
              <div>
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    marginBottom: 16,
                  }}
                >
                  <Typography.Text type="secondary">
                    Test filters against the channel's synced resources to see which ones match.
                  </Typography.Text>
                  <Button
                    type="primary"
                    icon={
                      testingFilters ? (
                        <Loader size={14} style={{ animation: 'spin 1s linear infinite' }} />
                      ) : (
                        <FlaskConical size={14} />
                      )
                    }
                    loading={testingFilters}
                    disabled={!agent.filters?.length}
                    onClick={handleTestFilters}
                  >
                    {testingFilters ? 'Testing...' : 'Test Filters'}
                  </Button>
                </div>

                {!agent.filters?.length && (
                  <Empty description="No filters configured for this agent." />
                )}

                {filterTest && (
                  <div>
                    <Row gutter={16} style={{ marginBottom: 16 }}>
                      <Col span={8}>
                        <Card size="small">
                          <Statistic title="Total" value={filterTest.total_resources} />
                        </Card>
                      </Col>
                      <Col span={8}>
                        <Card size="small">
                          <Statistic
                            title="Matched"
                            value={filterTest.matched}
                            valueStyle={{ color: '#59d499' }}
                          />
                        </Card>
                      </Col>
                      <Col span={8}>
                        <Card size="small">
                          <Statistic
                            title="Failed"
                            value={filterTest.failed}
                            valueStyle={{ color: '#ff6161' }}
                          />
                        </Card>
                      </Col>
                    </Row>
                    <div style={{ maxHeight: 600, overflow: 'auto' }}>
                      {filterTest.results.map((r) => (
                        <Card
                          key={r.resource_id}
                          size="small"
                          style={{
                            marginBottom: 8,
                            borderColor: r.all_required_passed ? '#59d499' : '#ff6161',
                          }}
                        >
                          <div
                            style={{
                              display: 'flex',
                              alignItems: 'center',
                              gap: 8,
                              marginBottom: 8,
                            }}
                          >
                            {r.all_required_passed ? (
                              <CheckCircle size={14} style={{ color: '#59d499' }} />
                            ) : (
                              <Tag color="error">FAIL</Tag>
                            )}
                            <Typography.Text strong ellipsis>
                              {r.title_raw}
                            </Typography.Text>
                          </div>
                          <Space wrap>
                            {r.filters.map((f, i) => (
                              <Tag key={i} color={f.passed ? 'green' : 'red'}>
                                {f.field}: {f.resource_value || '\u2205'} {f.passed ? '=' : '\u2260'}{' '}
                                {f.filter_value}
                                {f.is_required && '*'}
                              </Tag>
                            ))}
                          </Space>
                        </Card>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ),
          },
        ]}
      />
    </div>
  );
}
