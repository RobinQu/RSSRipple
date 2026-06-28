import { useState, useCallback, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
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
import { posterUrl, useDefaultPoster } from '../utils/poster';
import type { DashboardData, DashboardPendingItem, FileResource } from '../types';
import { resourcesApi } from '../api/channels';

const { Title, Text } = Typography;

export default function Dashboard() {
  const { t } = useTranslation();
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
      message.success(t('dashboard.confirmed'));
      fetchData();
    } else {
      message.error(r.error?.message || t('dashboard.failed'));
    }
  };

  const handleSkip = async (decisionId: string) => {
    const r = await decisionsApi.skip(decisionId);
    if (r.success) {
      message.success(t('dashboard.skipped'));
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
  if (!dashboard) return <Empty description={t('dashboard.failedToLoad')} />;

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
          {t('dashboard.title')}
        </Title>
        <Space>
          <Link to="/channels/new">
            <Button type="primary" icon={<Rss size={14} />}>
              {t('dashboard.addChannel')}
            </Button>
          </Link>
          <Link to="/agents/new">
            <Button icon={<Bot size={14} />}>{t('dashboard.addAgent')}</Button>
          </Link>
        </Space>
      </div>

      {/* Stats */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={12} md={6}>
          <Link to="/agents" style={{ textDecoration: 'none' }}>
            <Card hoverable>
              <Statistic
                title={t('dashboard.activeAgents')}
                value={dashboard.active_agents}
                prefix={<Bot size={18} />}
              />
            </Card>
          </Link>
        </Col>
        <Col xs={12} md={6}>
          <Link to="/channels" style={{ textDecoration: 'none' }}>
            <Card hoverable>
              <Statistic
                title={t('dashboard.activeChannels')}
                value={dashboard.active_channels}
                prefix={<Rss size={18} />}
              />
            </Card>
          </Link>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title={t('dashboard.downloading')}
              value={dashboard.active_download_count}
              prefix={<Download size={18} />}
              valueStyle={{ color: '#1863dc' }}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title={t('dashboard.pendingDecisions')}
              value={dashboard.pending_decisions.length}
              prefix={<AlertTriangle size={18} />}
              valueStyle={{ color: dashboard.pending_decisions.length > 0 ? '#ff7759' : undefined }}
            />
          </Card>
        </Col>
      </Row>

      {/* Active downloads */}
      <Card
        title={t('dashboard.activeDownloads')}
        style={{ marginBottom: 24 }}
        styles={{ body: { padding: 0 } }}
      >
        {dashboard.active_download_groups.length === 0 ? (
          <div style={{ padding: 32 }}>
            <Empty description={t('dashboard.noActiveDownloads')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
          </div>
        ) : (
          <List
            dataSource={dashboard.active_download_groups}
            renderItem={(group) => (
              <List.Item key={`${group.type}-${group.id || 'unknown'}`} style={{ padding: '16px 24px', borderBottom: '1px solid #e5e7eb' }}>
                <div style={{ display: 'flex', width: '100%', gap: 16 }}>
                  <img
                    src={posterUrl(group.poster_url)}
                    alt=""
                    style={{
                      width: 56,
                      height: 84,
                      objectFit: 'cover',
                      borderRadius: 6,
                      flexShrink: 0,
                      background: '#eeece7',
                    }}
                    onError={useDefaultPoster}
                  />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <Space size={8} style={{ marginBottom: 8 }}>
                      <Text strong>{group.title}</Text>
                      <Tag color={group.type === 'series' ? 'blue' : group.type === 'movie' ? 'green' : 'default'}>
                        {group.type === 'series' ? t('dashboard.series') : group.type === 'movie' ? t('dashboard.movie') : t('dashboard.unidentified')}
                      </Tag>
                    </Space>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                      {group.tasks.map((task) => (
                        <div key={task.task_id}>
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
                              {task.resource_title}
                            </Text>
                            <Space size="small" style={{ color: '#93939f', fontSize: 12, flexShrink: 0 }}>
                              <span>{formatSpeed(0)}</span>
                              <span>{t('dashboard.eta')} {formatEta(null)}</span>
                              <Link to={`/agents/${task.agent_id}`}>
                                <Text style={{ fontSize: 12 }}>{task.agent_name}</Text>
                              </Link>
                            </Space>
                          </div>
                          <ProgressBar progress={task.progress} />
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
      <Card title={t('dashboard.pendingDecisions')} styles={{ body: { padding: 0 } }}>
        {dashboard.pending_decisions.length === 0 ? (
          <div style={{ padding: 32 }}>
            <Empty description={t('dashboard.noPendingDecisions')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
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
                      {t('dashboard.candidateCount', { n: d.candidates.length })} · {timeAgo(d.created_at)}
                    </div>
                  </div>
                  <Button size="small" onClick={() => handleSkip(d.id)}>
                    {t('common.skip')}
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
                    <strong>{t('dashboard.aiSuggestion')}</strong>
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
                              {t('common.loading')}
                            </Text>
                        )}
                        <Button
                          type="primary"
                          size="small"
                          onClick={() => handleConfirm(d.id, cid)}
                        >
                          {t('common.confirm')}
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
