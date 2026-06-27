import { useState, useCallback, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { Bot, AlertTriangle, Download, Rss } from 'lucide-react';
import {
  Typography,
  Row,
  Col,
  Card,
  Statistic,
  Spin,
  Empty,
  Button,
  Space,
  Tag,
  List,
  App,
} from 'antd';
import { dashboardApi, decisionsApi } from '../api/tasks';
import { usePolling } from '../hooks/usePolling';
import ProgressBar from '../components/ProgressBar';
import { formatSpeed, formatEta, timeAgo, formatBytes } from '../utils/format';
import type { DashboardData, DashboardPendingItem, FileResource } from '../types';
import { resourcesApi } from '../api/channels';

const { Title, Text } = Typography;

function posterFallback(title: string) {
  const ch = (title || '?').charAt(0).toUpperCase();
  return `data:image/svg+xml;utf8,${encodeURIComponent(
    `<svg xmlns='http://www.w3.org/2000/svg' width='80' height='120' viewBox='0 0 80 120'><rect width='80' height='120' fill='%231a1a1a'/><text x='40' y='65' text-anchor='middle' fill='%236a6b6c' font-family='sans-serif' font-size='28'>${ch}</text></svg>`,
  )}`;
}

export default function Dashboard() {
  const { message } = App.useApp();
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [candidateCache, setCandidateCache] = useState<Record<string, FileResource>>({});

  // Preload candidate resources for pending decisions
  const loadCandidates = useCallback(
    async (items: DashboardPendingItem[]) => {
      const ids = new Set<string>();
      items.forEach((d) => d.candidates.forEach((c) => ids.add(c)));
      const missing = Array.from(ids).filter((id) => !candidateCache[id]);
      if (missing.length === 0) return;
      const results = await Promise.all(
        missing.map((id) =>
          resourcesApi.get(id).then((r) => (r.success ? [id, r.data] as const : null)),
        ),
      );
      const next: Record<string, FileResource> = { ...candidateCache };
      results.forEach((entry) => {
        if (entry) next[entry[0]] = entry[1];
      });
      setCandidateCache(next);
    },
    [candidateCache],
  );

  const fetchData = useCallback(async () => {
    const res = await dashboardApi.get();
    if (res.success) {
      setDashboard(res.data);
      loadCandidates(res.data.pending_decisions);
    }
    setLoading(false);
  }, [loadCandidates]);

  usePolling(fetchData, 10000);

  useEffect(() => {
    if (dashboard) loadCandidates(dashboard.pending_decisions);
  }, [dashboard, loadCandidates]);

  const handleConfirm = async (decisionId: string, resourceId: string) => {
    const r = await decisionsApi.confirm(decisionId, resourceId);
    if (r.success) {
      message.success('已确认，开始下载');
      fetchData();
    } else {
      message.error(r.error?.message || '操作失败');
    }
  };

  const handleSkip = async (decisionId: string) => {
    const r = await decisionsApi.skip(decisionId);
    if (r.success) {
      message.success('已跳过');
      fetchData();
    }
  };

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 48 }}>
        <Spin size="large" />
      </div>
    );
  }
  if (!dashboard) return <Empty description="无法加载数据" />;

  return (
    <div>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 24,
        }}
      >
        <Title level={3} style={{ margin: 0 }}>
          Dashboard
        </Title>
        <Space>
          <Link to="/channels/new">
            <Button type="primary" icon={<Rss size={14} />}>
              添加频道
            </Button>
          </Link>
          <Link to="/agents/new">
            <Button icon={<Bot size={14} />}>添加 Agent</Button>
          </Link>
        </Space>
      </div>

      {/* Stats */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title="活跃 Agent"
              value={dashboard.active_agents}
              prefix={<Bot size={18} />}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title="活跃频道"
              value={dashboard.active_channels}
              prefix={<Rss size={18} />}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title="下载中"
              value={dashboard.active_download_count}
              prefix={<Download size={18} />}
              valueStyle={{ color: '#1863dc' }}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title="待决策"
              value={dashboard.pending_decisions.length}
              prefix={<AlertTriangle size={18} />}
              valueStyle={{ color: dashboard.pending_decisions.length > 0 ? '#ff7759' : undefined }}
            />
          </Card>
        </Col>
      </Row>

      {/* Active downloads */}
      <Card
        title="活跃下载"
        style={{ marginBottom: 24 }}
        styles={{ body: { padding: 0 } }}
      >
        {dashboard.active_download_groups.length === 0 ? (
          <div style={{ padding: 32 }}>
            <Empty description="暂无活跃下载" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          </div>
        ) : (
          <List
            dataSource={dashboard.active_download_groups}
            renderItem={(group) => (
              <List.Item key={`${group.type}-${group.id || 'unknown'}`} style={{ padding: '16px 24px', borderBottom: '1px solid #e5e7eb' }}>
                <div style={{ display: 'flex', width: '100%', gap: 16 }}>
                  <img
                    src={group.poster_url || posterFallback(group.title)}
                    alt=""
                    style={{
                      width: 56,
                      height: 84,
                      objectFit: 'cover',
                      borderRadius: 6,
                      flexShrink: 0,
                      background: '#eeece7',
                    }}
                    onError={(e) => {
                      (e.target as HTMLImageElement).src = posterFallback(group.title);
                    }}
                  />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <Space size={8} style={{ marginBottom: 8 }}>
                      <Text strong>{group.title}</Text>
                      <Tag color={group.type === 'series' ? 'blue' : group.type === 'movie' ? 'green' : 'default'}>
                        {group.type === 'series' ? '剧集' : group.type === 'movie' ? '电影' : '未识别'}
                      </Tag>
                    </Space>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                      {group.tasks.map((t) => (
                        <div key={t.task_id}>
                          <div
                            style={{
                              display: 'flex',
                              justifyContent: 'space-between',
                              alignItems: 'center',
                              marginBottom: 4,
                              gap: 12,
                            }}
                          >
                            <Text ellipsis style={{ flex: 1, fontSize: 13 }}>
                              {t.resource_title}
                            </Text>
                            <Space size="small" style={{ color: '#93939f', fontSize: 12, flexShrink: 0 }}>
                              <span>{formatSpeed(0)}</span>
                              <span>ETA: {formatEta(null)}</span>
                              <Link to={`/agents/${t.agent_id}`}>
                                <Text style={{ fontSize: 12 }}>{t.agent_name}</Text>
                              </Link>
                            </Space>
                          </div>
                          <ProgressBar progress={t.progress} />
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </List.Item>
            )}
          />
        )}
      </Card>

      {/* Pending decisions */}
      <Card title="待决策" styles={{ body: { padding: 0 } }}>
        {dashboard.pending_decisions.length === 0 ? (
          <div style={{ padding: 32 }}>
            <Empty description="暂无待决策项" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          </div>
        ) : (
          <List
            dataSource={dashboard.pending_decisions}
            renderItem={(d) => (
              <List.Item
                key={d.id}
                style={{ padding: '16px 24px', borderBottom: '1px solid #e5e7eb', display: 'block' }}
              >
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
                      <Link to={`/agents/${d.agent_id}`}>
                        <Text style={{ fontSize: 12 }}>{d.agent_name}</Text>
                      </Link>
                      {' · '}
                      {d.candidates.length} 个候选 · {timeAgo(d.created_at)}
                    </div>
                  </div>
                  <Button size="small" onClick={() => handleSkip(d.id)}>
                    跳过
                  </Button>
                </div>

                {d.llm_suggestion && (
                  <div
                    style={{
                      padding: 10,
                      borderRadius: 6,
                      background: '#f1f5ff',
                      border: '1px solid #b8cdf7',
                      fontSize: 12,
                      color: '#1863dc',
                      marginBottom: 12,
                    }}
                  >
                    <strong>AI 建议：</strong>
                    {d.llm_suggestion}
                  </div>
                )}

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
                          background: '#f7f7f5',
                          gap: 12,
                        }}
                      >
                        {r ? (
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <Text ellipsis style={{ fontSize: 13 }}>
                              {r.title_cn || r.title_raw}
                            </Text>
                            <Space size={6} style={{ fontSize: 11, color: '#93939f', marginTop: 2 }} wrap>
                              {r.subtitle_group && <Tag style={{ margin: 0 }}>{r.subtitle_group}</Tag>}
                              {r.resolution && <Tag style={{ margin: 0 }}>{r.resolution}</Tag>}
                              {r.video_codec && <Tag style={{ margin: 0 }}>{r.video_codec}</Tag>}
                              {r.file_size != null && <span>{formatBytes(r.file_size)}</span>}
                            </Space>
                          </div>
                        ) : (
                          <Text ellipsis style={{ flex: 1, fontSize: 12, color: '#93939f' }}>
                            加载中...
                          </Text>
                        )}
                        <Button
                          type="primary"
                          size="small"
                          onClick={() => handleConfirm(d.id, cid)}
                        >
                          确认
                        </Button>
                      </div>
                    );
                  })}
                </Space>
              </List.Item>
            )}
          />
        )}
      </Card>
    </div>
  );
}
