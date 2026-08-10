import { useState, useCallback, useEffect, useRef } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useDocumentTitle from '../hooks/useDocumentTitle';
import { Bot, AlertTriangle, CheckCircle, Download, Rss } from 'lucide-react';
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
  Tabs,
  theme,
} from 'antd';
import { dashboardApi, decisionsApi } from '../api/tasks';
import { agentsApi } from '../api/agents';
import { usePolling } from '../hooks/usePolling';
import ProgressBar from '../components/ProgressBar';
import {
  collectFieldConditions,
  describeCondition,
  isFilterEmpty,
} from '../components/filterUtils';
import { timeAgo, formatBytes } from '../utils/format';
import { posterUrl, useDefaultPoster } from '../utils/poster';
import type { Agent, DashboardData, DashboardPendingItem, FileResource } from '../types';
import { resourcesApi } from '../api/channels';

const { Title, Text } = Typography;

/** GET /agents rows carry a few extra joined fields beyond the Agent type. */
type AgentListItem = Agent & {
  channel_name?: string | null;
  downloader_name?: string | null;
  active_task_count?: number;
};

// Download-group type → tag color and label key. Anything not listed (the
// "unknown" group) falls back to the unidentified styling.
const GROUP_TYPE_TAG: Record<string, { color: string; labelKey: string }> = {
  series: { color: 'blue', labelKey: 'dashboard.series' },
  movie: { color: 'green', labelKey: 'dashboard.movie' },
  untracked: { color: 'orange', labelKey: 'dashboard.untracked' },
};
const UNKNOWN_GROUP_TAG = { color: 'default', labelKey: 'dashboard.unidentified' };

export default function Dashboard() {
  const { t } = useTranslation();
  useDocumentTitle(t('nav.dashboard'));
  const { message } = App.useApp();
  const { token } = theme.useToken();
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [topAgents, setTopAgents] = useState<AgentListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [candidateCache, setCandidateCache] = useState<Record<string, FileResource>>({});
  const [dlFilter, setDlFilter] = useState<'all' | 'agent' | 'untracked'>('all');
  // Set by clicking an agent's "downloading" badge: filters the active
  // downloads card to that agent's tasks only.
  const [dlAgentFilter, setDlAgentFilter] = useState<{ id: string; name: string } | null>(null);
  const downloadsRef = useRef<HTMLDivElement>(null);

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
    const [res, agentsRes] = await Promise.all([
      dashboardApi.get(),
      agentsApi.list(1, 100),
    ]);
    if (res.success) {
      setDashboard(res.data);
      loadCandidates(res.data.pending_decisions);
    }
    if (agentsRes.success) {
      // Top 4 active agents, busiest first.
      const active = (agentsRes.data as AgentListItem[]).filter(
        (a) => a.status === 'active',
      );
      active.sort((a, b) => (b.active_task_count ?? 0) - (a.active_task_count ?? 0));
      setTopAgents(active.slice(0, 4));
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

  // Tab filter + optional per-agent filter (from the agent badge) for the
  // active downloads card. The agent filter narrows tasks within each group
  // and drops groups left empty.
  const filteredGroups = dashboard.active_download_groups
    .filter((g) =>
      dlFilter === 'all' ? true : dlFilter === 'untracked' ? g.type === 'untracked' : g.type !== 'untracked',
    )
    .map((g) =>
      dlAgentFilter
        ? { ...g, tasks: g.tasks.filter((task) => task.agent_id === dlAgentFilter.id) }
        : g,
    )
    .filter((g) => g.tasks.length > 0);

  return (
    <div>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 8,
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

      {/* All-clear hint directly under the metrics when nothing is pending */}
      {dashboard.pending_decisions.length === 0 && (
        <div style={{ marginTop: -8, marginBottom: 24 }}>
          <Text type="secondary" style={{ fontSize: 13 }}>
            <CheckCircle
              size={14}
              style={{ marginRight: 6, color: '#52c41a', verticalAlign: 'text-bottom' }}
            />
            {t('dashboard.noPendingHint')}
          </Text>
        </div>
      )}

      {/* Download agents: top 4 active agents, busiest first. The task-count
          badge filters the active downloads card below to that agent. */}
      <Card title={t('dashboard.agentsSection')} style={{ marginBottom: 24 }}>
        {topAgents.length === 0 ? (
          <Empty
            description={t('dashboard.noActiveAgents')}
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          />
        ) : (
          <Row gutter={[16, 16]}>
            {topAgents.map((agent) => {
              // Subscription summary = global filter conditions + every
              // work-level override (tagged with the work title).
              const globalConds =
                agent.filter_config && !isFilterEmpty(agent.filter_config)
                  ? collectFieldConditions(agent.filter_config)
                  : [];
              const condTags: { key: string; label: string; work?: string }[] =
                globalConds.map((c, i) => ({
                  key: `g${i}`,
                  label: describeCondition(c, t),
                }));
              (agent.works ?? []).forEach((w) => {
                if (!w.filter_overrides || isFilterEmpty(w.filter_overrides)) return;
                const workTitle =
                  w.display_name_override ||
                  w.series?.title_cn ||
                  w.series?.title_en ||
                  w.series?.original_title ||
                  w.movie?.title_cn ||
                  w.movie?.title_en ||
                  w.movie?.original_title ||
                  t('common.unknown');
                collectFieldConditions(w.filter_overrides).forEach((c, i) => {
                  condTags.push({
                    key: `w${w.id}-${i}`,
                    label: describeCondition(c, t),
                    work: workTitle,
                  });
                });
              });
              const MAX_COND_TAGS = 6;
              const visibleConds = condTags.slice(0, MAX_COND_TAGS);
              const hiddenConds = condTags.length - visibleConds.length;
              const posters = (agent.works ?? [])
                .map((w) => ({
                  id: w.id,
                  url: w.series?.poster_url || w.movie?.poster_url || null,
                }))
                .filter((p) => p.url)
                .slice(0, 6);
              return (
                <Col xs={24} md={12} key={agent.id}>
                  <div
                    style={{
                      border: '1px solid #e5e7eb',
                      borderRadius: 8,
                      padding: 16,
                      height: '100%',
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        gap: 8,
                        marginBottom: 6,
                      }}
                    >
                      <Link to={`/agents/${agent.id}`} style={{ minWidth: 0 }}>
                        <Text strong style={{ fontSize: 14 }} ellipsis>
                          {agent.name}
                        </Text>
                      </Link>
                      {(agent.active_task_count ?? 0) > 0 && (
                        <Tag
                          color="blue"
                          style={{ flexShrink: 0, cursor: 'pointer' }}
                          onClick={() => {
                            setDlAgentFilter({ id: agent.id, name: agent.name });
                            setDlFilter('all');
                            downloadsRef.current?.scrollIntoView({ behavior: 'smooth' });
                          }}
                        >
                          {t('dashboard.downloadingCount', { n: agent.active_task_count })}
                        </Tag>
                      )}
                    </div>
                    <div style={{ fontSize: 12, color: '#93939f', marginBottom: 8 }}>
                      {agent.channel_name && (
                        <>
                          <Link to={`/channels/${agent.channel_id}`}>{agent.channel_name}</Link>
                          {' · '}
                        </>
                      )}
                      {agent.downloader_name && <>{agent.downloader_name}{' · '}</>}
                      {agent.scope_channel_wide
                        ? t('dashboard.channelWide')
                        : t('dashboard.worksCount', { n: agent.works?.length ?? 0 })}
                    </div>
                    {posters.length > 0 && (
                      <div
                        style={{
                          display: 'flex',
                          gap: 6,
                          flexWrap: 'wrap',
                          marginBottom: 8,
                        }}
                      >
                        {posters.map((p) => (
                          <img
                            key={p.id}
                            src={posterUrl(p.url)}
                            alt=""
                            style={{
                              width: 28,
                              height: 42,
                              objectFit: 'cover',
                              borderRadius: 4,
                              background: '#eeece7',
                            }}
                            onError={useDefaultPoster}
                          />
                        ))}
                      </div>
                    )}
                    <div>
                      <Text type="secondary" style={{ fontSize: 12, marginRight: 6 }}>
                        {t('dashboard.filterConditions')}
                      </Text>
                      {condTags.length === 0 ? (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {t('dashboard.noFilterConditions')}
                        </Text>
                      ) : (
                        <>
                          {visibleConds.map((c) =>
                            c.work ? (
                              <Tag key={c.key} color="blue" style={{ fontSize: 11, margin: 2 }}>
                                {c.work} · {c.label}
                              </Tag>
                            ) : (
                              <Tag key={c.key} style={{ fontSize: 11, margin: 2 }}>
                                {c.label}
                              </Tag>
                            ),
                          )}
                          {hiddenConds > 0 && (
                            <Text type="secondary" style={{ fontSize: 11 }}>
                              +{hiddenConds}
                            </Text>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                </Col>
              );
            })}
          </Row>
        )}
      </Card>

      {/* Active downloads — the page's single task-level download list.
          The agents card above links here via the per-agent filter chip. */}
      <div ref={downloadsRef}>
      <Card
        title={t('dashboard.activeDownloads')}
        style={{ marginBottom: 24 }}
        styles={{ body: { padding: 0 } }}
      >
        <Tabs
          activeKey={dlFilter}
          onChange={(k) => setDlFilter(k as typeof dlFilter)}
          size="small"
          style={{ paddingLeft: 24, paddingRight: 24 }}
          items={[
            { key: 'all', label: t('dashboard.dlFilterAll') },
            { key: 'agent', label: t('dashboard.dlFilterAgent') },
            { key: 'untracked', label: t('dashboard.dlFilterUntracked') },
          ]}
          tabBarExtraContent={
            dlAgentFilter ? (
              <Tag closable color="blue" onClose={() => setDlAgentFilter(null)}>
                {dlAgentFilter.name}
              </Tag>
            ) : null
          }
        />
        {filteredGroups.length === 0 ? (
          <div style={{ padding: 32 }}>
            <Empty description={t('dashboard.noActiveDownloads')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
          </div>
        ) : (
          <List
            dataSource={filteredGroups}
            renderItem={(group, index) => {
              // Divider color comes from the theme token (dark-mode safe) and
              // is skipped on the last row so the card has no trailing line.
              const tag = GROUP_TYPE_TAG[group.type] ?? UNKNOWN_GROUP_TAG;
              return (
              <List.Item
                key={`${group.type}-${group.id || 'unknown'}`}
                style={{
                  padding: '16px 24px',
                  borderBottom:
                    index < filteredGroups.length - 1 ? `1px solid ${token.colorBorderSecondary}` : 'none',
                }}
              >
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
                      background: token.colorFillSecondary,
                    }}
                    onError={useDefaultPoster}
                  />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <Space size={8} style={{ marginBottom: 8 }}>
                      <Text strong>{group.type === 'untracked' ? t('dashboard.untracked') : group.title}</Text>
                      <Tag color={tag.color}>{t(tag.labelKey)}</Tag>
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
                              {task.agent_id ? (
                                <Link to={`/agents/${task.agent_id}`}>
                                  <Text style={{ fontSize: 12 }}>{task.agent_name}</Text>
                                </Link>
                              ) : task.downloader_id ? (
                                <Link to={`/downloaders/${task.downloader_id}`}>
                                  <Text style={{ fontSize: 12 }}>{task.downloader_name}</Text>
                                </Link>
                              ) : null}
                            </Space>
                          </div>
                          <ProgressBar progress={task.progress} />
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </List.Item>
              );
            }}
          />
        )}
      </Card>
      </div>

      {/* Pending decisions — only shown when there is something to decide;
          the warning border/title sets it apart from the other sections. */}
      {dashboard.pending_decisions.length > 0 && (
      <Card
        title={
          <Space size={8}>
            <AlertTriangle size={16} color="#ff7759" />
            <span style={{ color: '#ff7759' }}>{t('dashboard.pendingDecisions')}</span>
          </Space>
        }
        style={{ border: '1px solid #ff7759' }}
        styles={{ body: { padding: 0 } }}
      >
        {(
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
      )}
    </div>
  );
}
