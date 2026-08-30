import { useState, useCallback, useEffect, useRef } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useDocumentTitle from '../hooks/useDocumentTitle';
import useUrlTab from '../hooks/useUrlTab';
import { Bot, AlertTriangle, CheckCircle, Download, Rss, Check, Play, XCircle, Eye, ListTree, PencilLine } from 'lucide-react';
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
  InputNumber,
  Tooltip,
  Checkbox,
} from 'antd';
import { dashboardApi, decisionsApi } from '../api/tasks';
import { agentsApi } from '../api/agents';
import { organizeApi } from '../api/organize';
import { usePolling } from '../hooks/usePolling';
import ProgressBar from '../components/ProgressBar';
import OrganizeOpPaths from '../components/OrganizeOpPaths';
import OrganizePlanDrawer from '../components/OrganizePlanDrawer';
import StatusBadge from '../components/StatusBadge';
import ResourceFilesDrawer from '../components/ResourceFilesDrawer';
import ResourceCorrectionModal from '../components/ResourceCorrectionModal';
import SeasonInput from '../components/SeasonInput';
import {
  collectFieldConditions,
  describeCondition,
  isFilterEmpty,
} from '../components/filterUtils';
import { timeAgo, formatBytes, formatSpeed } from '../utils/format';
import { posterUrl, useDefaultPoster } from '../utils/poster';
import type { Agent, DashboardData, DashboardPendingItem, FileResource, Library, OrganizePlanListItem } from '../types';
import { resourcesApi } from '../api/channels';

const { Title, Text } = Typography;

// Per-candidate draft for the ambiguous episode/season correction form.
interface EpisodeDraft {
  season: number | null;
  episode: number | null;
  absolute_episode: number | null;
}

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
const TODO_PAGE_SIZE = 10;

export default function Dashboard() {
  const { t } = useTranslation();
  useDocumentTitle(t('nav.dashboard'));
  const { message, modal } = App.useApp();
  const { token } = theme.useToken();
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [decisionPage, setDecisionPage] = useState(1);
  const [confirmationPage, setConfirmationPage] = useState(1);
  const [planPage, setPlanPage] = useState(1);
  const [topAgents, setTopAgents] = useState<AgentListItem[]>([]);
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [loading, setLoading] = useState(true);
  const [dlFilter, setDlFilter] = useUrlTab('all', ['all', 'agent', 'untracked'] as const, 'dl');
  // Set by clicking an agent's "downloading" badge: filters the active
  // downloads card to that agent's tasks only.
  const [dlAgentFilter, setDlAgentFilter] = useState<{ id: string; name: string } | null>(null);
  const downloadsRef = useRef<HTMLDivElement>(null);
  // Ambiguous episode/season correction (mirrors the agent detail decisions tab).
  const [episodeDrafts, setEpisodeDrafts] = useState<Record<string, EpisodeDraft>>({});
  const [savingEpisodeCid, setSavingEpisodeCid] = useState<string | null>(null);
  // Pending organize plans — in-place actions + detail drawer.
  const [planDrawerId, setPlanDrawerId] = useState<string | null>(null);
  const [executingPlanId, setExecutingPlanId] = useState<string | null>(null);
  // Resource file listing drawer + parse-field correction modal, shared by
  // the decisions candidates and the confirmations rows.
  const [filesResourceId, setFilesResourceId] = useState<string | null>(null);
  const [correctionResource, setCorrectionResource] = useState<FileResource | null>(null);
  const [selectedTodos, setSelectedTodos] = useState<Record<string, string[]>>({
    decision: [], confirmation: [], plan: [],
  });
  const [ignoringTodos, setIgnoringTodos] = useState(false);

  const fetchData = useCallback(async () => {
    const [res, agentsRes, libRes] = await Promise.all([
      dashboardApi.get({
        decisionPage,
        confirmationPage,
        planPage,
        pageSize: TODO_PAGE_SIZE,
      }),
      agentsApi.list(1, 100),
      organizeApi.listLibraries(),
    ]);
    if (res.success) {
      setDashboard(res.data);
      const decisionLastPage = Math.max(
        1,
        Math.ceil(res.data.pending_decisions_total / TODO_PAGE_SIZE),
      );
      const confirmationLastPage = Math.max(
        1,
        Math.ceil(res.data.pending_confirmations_total / TODO_PAGE_SIZE),
      );
      const planLastPage = Math.max(
        1,
        Math.ceil(res.data.pending_plans_total / TODO_PAGE_SIZE),
      );
      if (decisionPage > decisionLastPage) setDecisionPage(decisionLastPage);
      if (confirmationPage > confirmationLastPage) setConfirmationPage(confirmationLastPage);
      if (planPage > planLastPage) setPlanPage(planLastPage);
    }
    if (agentsRes.success) {
      // Top 4 active agents, busiest first.
      const active = (agentsRes.data as AgentListItem[]).filter(
        (a) => a.status === 'active',
      );
      active.sort((a, b) => (b.active_task_count ?? 0) - (a.active_task_count ?? 0));
      setTopAgents(active.slice(0, 4));
    }
    if (libRes.success) setLibraries(libRes.data);
    setLoading(false);
  }, [confirmationPage, decisionPage, planPage]);

  usePolling(fetchData, 10000);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

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
    await handleIgnoreTodos('decision', [decisionId]);
  };

  const handleIgnoreTodos = (
    kind: 'decision' | 'confirmation' | 'plan',
    ids: string[],
  ): Promise<void> => new Promise((resolve) => {
    modal.confirm({
      title: t('dashboard.ignoreConfirmTitle'),
      content: t('dashboard.ignoreConfirmContent', { n: ids.length }),
      okText: t('dashboard.ignore'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        setIgnoringTodos(true);
        const r = await dashboardApi.ignoreTodos(kind, ids);
        setIgnoringTodos(false);
        if (r.success) {
          message.success(t('dashboard.ignoredCount', { n: r.data.ignored }));
          setSelectedTodos((prev) => ({ ...prev, [kind]: [] }));
          await fetchData();
        } else {
          message.error(r.error?.message || t('dashboard.failed'));
        }
        resolve();
      },
      onCancel: () => resolve(),
    });
  });

  const toggleTodo = (kind: string, id: string, checked: boolean) => {
    setSelectedTodos((prev) => ({
      ...prev,
      [kind]: checked
        ? [...new Set([...prev[kind], id])]
        : prev[kind].filter((value) => value !== id),
    }));
  };

  const renderTodoToolbar = (
    kind: 'decision' | 'confirmation' | 'plan',
    pageIds: string[],
  ) => {
    const selected = selectedTodos[kind];
    const allSelected = pageIds.length > 0 && pageIds.every((id) => selected.includes(id));
    return (
      <div className="dashboard-todo-toolbar">
        <Checkbox
          checked={allSelected}
          indeterminate={!allSelected && pageIds.some((id) => selected.includes(id))}
          onChange={(event) => setSelectedTodos((prev) => ({
            ...prev,
            [kind]: event.target.checked
              ? [...new Set([...prev[kind], ...pageIds])]
              : prev[kind].filter((id) => !pageIds.includes(id)),
          }))}
        >
          {t('common.selectAll')}
        </Checkbox>
        <Button
          size="small"
          danger
          disabled={selected.length === 0}
          loading={ignoringTodos}
          onClick={() => handleIgnoreTodos(kind, selected)}
        >
          {t('dashboard.ignoreSelected', { n: selected.length })}
        </Button>
      </div>
    );
  };

  const handleCorrectEpisode = async (cid: string, displayedDraft?: EpisodeDraft) => {
    // The inputs render resource values before the user edits anything.  In
    // that case no entry exists in episodeDrafts yet, so submit the values the
    // form actually displays instead of silently returning.
    const draft = episodeDrafts[cid] ?? displayedDraft;
    if (!draft || draft.episode == null) return;
    setSavingEpisodeCid(cid);
    const r = await resourcesApi.correctEpisode(cid, {
      episode: draft.episode,
      ...(draft.season != null ? { season: draft.season } : {}),
      ...(draft.absolute_episode != null ? { absolute_episode: draft.absolute_episode } : {}),
    });
    setSavingEpisodeCid(null);
    if (r.success) {
      message.success(t('agents.episodeSaved'));
      setEpisodeDrafts((prev) => {
        const next = { ...prev };
        delete next[cid];
        return next;
      });
      fetchData();
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };

  const isAmbiguousDecision = (d: DashboardPendingItem): boolean => {
    const cands = d.candidate_resources;
    return !!cands && cands.length > 0 && cands.every((r) => r.episode_confidence === 'ambiguous');
  };

  // ---- Pending organize plans (in-place actions mirror the plans list) ----
  const isPlanExecutable = (p: OrganizePlanListItem) =>
    (p.status === 'pending' || p.status === 'failed') &&
    p.library_id !== null &&
    p.pending_reason !== 'unbound';
  const isPlanCancellable = (p: OrganizePlanListItem) =>
    p.status === 'pending' || p.status === 'failed';

  const handleExecutePlan = async (id: string) => {
    setExecutingPlanId(id);
    const r = await organizeApi.executePlan(id);
    setExecutingPlanId(null);
    if (r.success) {
      message.success(t('organize.executed'));
      fetchData();
    } else {
      message.error(r.error?.message || t('organize.executeFailed'));
    }
  };

  const handleCancelPlan = (record: OrganizePlanListItem) => {
    modal.confirm({
      title: t('organize.cancelConfirm'),
      okText: t('common.confirm'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await organizeApi.cancelPlan(record.id);
        if (r.success) {
          message.success(t('organize.cancelled'));
          fetchData();
        } else {
          message.error(r.error?.message || t('organize.cancelFailed'));
        }
      },
    });
  };

  // Show the raw release title whenever it differs from the parsed/formatted
  // title — the raw title carries the subtitle group / SxxExx markers a human
  // needs to disambiguate candidates.
  const renderRawTitle = (r: FileResource | undefined) => {
    const raw = r?.title_raw;
    if (!raw) return null;
    const formatted = r?.title_cn || r?.title_en;
    if (!formatted || raw === formatted) return null;
    return (
      <div
        style={{
          fontSize: 12,
          color: 'var(--rr-text-muted)',
          marginTop: 2,
          display: 'flex',
          alignItems: 'center',
          gap: 4,
          minWidth: 0,
        }}
      >
        <span style={{ flexShrink: 0 }}>{t('channels.rawTitle')}:</span>
        <Tooltip title={raw}>
          <span
            style={{
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              flex: 1,
              minWidth: 0,
            }}
          >
            {raw}
          </span>
        </Tooltip>
      </div>
    );
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

  // Combined pending todo count (agent decisions + resource confirmations +
  // organize plans) shown as a single headline metric.
  const pendingCount =
    dashboard.pending_decisions_total +
    dashboard.pending_confirmations_total +
    dashboard.pending_plans_total;
  const firstPendingTab =
    dashboard.pending_decisions_total > 0
      ? 'decisions'
      : dashboard.pending_confirmations_total > 0
        ? 'confirmations'
        : 'plans';

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
              valueStyle={{ color: 'var(--rr-primary)' }}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card>
            <Statistic
              title={t('dashboard.pendingItems')}
              value={pendingCount}
              prefix={<AlertTriangle size={18} />}
              valueStyle={{ color: pendingCount > 0 ? 'var(--rr-accent)' : undefined }}
            />
          </Card>
        </Col>
      </Row>

      {/* Pending block — agent decisions, resource confirmations and organize
          plans share one tabbed card right under the metrics so every
          actionable todo is the first thing on screen. */}
      {pendingCount > 0 ? (
      <Card
        title={
          <Space size={8}>
            <AlertTriangle size={16} color="var(--rr-accent)" />
            <span style={{ color: 'var(--rr-accent)' }}>{t('dashboard.pendingItems')}</span>
          </Space>
        }
        style={{ border: '1px solid var(--rr-accent)', marginBottom: 24 }}
        styles={{ header: { borderBottom: 'none' }, body: { padding: 0 } }}
      >
        <Tabs
          defaultActiveKey={firstPendingTab}
          style={{ paddingLeft: 24, paddingRight: 24 }}
          items={[
            {
              key: 'decisions',
              label: `${t('dashboard.pendingDecisions')} (${dashboard.pending_decisions_total})`,
              children: (
                <>
          {renderTodoToolbar('decision', dashboard.pending_decisions.map((item) => item.id))}
          <List
            className="dashboard-todo-list"
            dataSource={dashboard.pending_decisions}
            pagination={dashboard.pending_decisions_total > TODO_PAGE_SIZE ? {
              current: decisionPage,
              pageSize: TODO_PAGE_SIZE,
              total: dashboard.pending_decisions_total,
              showSizeChanger: false,
              onChange: setDecisionPage,
            } : false}
            renderItem={(d) => (
              <List.Item
                key={d.id}
                className="dashboard-todo-item"
                style={{ display: 'block' }}
              >
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'flex-start',
                    marginBottom: 12,
                  }}
                >
                  <div style={{ display: 'flex', gap: 8, minWidth: 0 }}>
                    <Checkbox
                      checked={selectedTodos.decision.includes(d.id)}
                      onChange={(event) => toggleTodo('decision', d.id, event.target.checked)}
                      aria-label={t('dashboard.selectTodo')}
                    />
                    <div style={{ minWidth: 0 }}>
                    <Text strong>{d.reason}</Text>
                    <div style={{ fontSize: 12, color: 'var(--rr-text-muted)', marginTop: 4 }}>
                      <Link to={`/agents/${d.agent_id}`}>
                        <Text style={{ fontSize: 12 }}>{d.agent_name}</Text>
                      </Link>
                      {d.title && (
                        <>
                          {' · '}
                          {d.series_id || d.movie_id ? (
                            <Link to={d.series_id ? `/series/${d.series_id}` : `/movies/${d.movie_id}`}>
                              <Text style={{ fontSize: 12 }}>{d.title}</Text>
                            </Link>
                          ) : (
                            <Text style={{ fontSize: 12 }}>{d.title}</Text>
                          )}
                        </>
                      )}
                      {' · '}
                      {t('dashboard.candidateCount', { n: d.candidates.length })} · {timeAgo(d.created_at)}
                    </div>
                    {isAmbiguousDecision(d) && (
                      <div style={{ fontSize: 12, color: 'var(--rr-warning)', marginTop: 4 }}>
                        {t('agents.ambiguousHint')}
                      </div>
                    )}
                    </div>
                  </div>
                  <Button size="small" danger onClick={() => handleSkip(d.id)}>
                    {t('dashboard.ignore')}
                  </Button>
                </div>

                {d.llm_suggestion && (
                  <div
                    style={{
                      padding: 10,
                      borderRadius: 6,
                      background: 'var(--rr-primary-soft)',
                      border: '1px solid var(--rr-info-border)',
                      fontSize: 12,
                      color: 'var(--rr-primary)',
                      marginBottom: 12,
                    }}
                  >
                    <strong>{t('dashboard.aiSuggestion')}</strong>
                    {d.llm_suggestion}
                  </div>
                )}

                <Space direction="vertical" style={{ width: '100%' }} size={6}>
                  {d.candidate_resources?.map((r) => {
                    const ambiguous = isAmbiguousDecision(d);
                    const base = {
                      season: r.season ?? null,
                      episode: r.episode ?? null,
                      absolute_episode: r.absolute_episode ?? null,
                    };
                    const draft = { ...base, ...(episodeDrafts[r.id] ?? {}) };
                    const patchDraft = (patch: Partial<EpisodeDraft>) =>
                      setEpisodeDrafts((prev) => ({
                        ...prev,
                        [r.id]: { ...base, ...(prev[r.id] ?? {}), ...patch },
                      }));
                    return (
                      <div
                        key={r.id}
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          alignItems: 'center',
                          padding: '8px 12px',
                          borderRadius: 6,
                          border: '1px solid var(--rr-border-soft)',
                          background: 'var(--rr-surface-elevated)',
                          gap: 12,
                        }}
                      >
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <Text ellipsis style={{ fontSize: 13 }}>
                            {r.title_cn || r.title_raw}
                          </Text>
                          {renderRawTitle(r)}
                          <Space size={6} style={{ fontSize: 11, color: 'var(--rr-text-muted)', marginTop: 2 }} wrap>
                            {r.subtitle_group && <Tag style={{ margin: 0 }}>{r.subtitle_group}</Tag>}
                            {r.resolution && <Tag style={{ margin: 0 }}>{r.resolution}</Tag>}
                            {r.video_codec && <Tag style={{ margin: 0 }}>{r.video_codec}</Tag>}
                            {r.season != null && <span>S{r.season}</span>}
                            {r.episode != null && <span>EP{r.episode}</span>}
                            {r.file_size != null && <span>{formatBytes(r.file_size)}</span>}
                          </Space>
                        </div>
                        {ambiguous ? (
                          <Space size={6} align="center" wrap>
                            <Tooltip title={t('resource.files')}>
                              <Button
                                type="text"
                                size="small"
                                icon={<ListTree size={14} />}
                                onClick={() => setFilesResourceId(r.id)}
                              />
                            </Tooltip>
                            <SeasonInput
                              size="small"
                              value={draft.season}
                              placeholder={t('resource.seasonLabel')}
                              onChange={(v) => patchDraft({ season: v })}
                              style={{ width: 72 }}
                            />
                            <InputNumber
                              size="small"
                              min={1}
                              value={draft.episode}
                              placeholder={t('agents.correctEpisodePlaceholder')}
                              onChange={(v) => patchDraft({ episode: typeof v === 'number' ? v : null })}
                              style={{ width: 72 }}
                            />
                            <InputNumber
                              size="small"
                              min={0}
                              value={draft.absolute_episode}
                              placeholder={t('resource.absoluteEpisodePlaceholder')}
                              onChange={(v) => patchDraft({ absolute_episode: typeof v === 'number' ? v : null })}
                              style={{ width: 130 }}
                            />
                            <Button
                              type="primary"
                              size="small"
                              loading={savingEpisodeCid === r.id}
                              disabled={draft.episode == null}
                              onClick={() => handleCorrectEpisode(r.id, draft)}
                            >
                              {t('agents.correctEpisode')}
                            </Button>
                          </Space>
                        ) : (
                          <Space size={6} align="center" wrap>
                            <Tooltip title={t('resource.files')}>
                              <Button
                                type="text"
                                size="small"
                                icon={<ListTree size={14} />}
                                onClick={() => setFilesResourceId(r.id)}
                              />
                            </Tooltip>
                            <Button
                              type="primary"
                              size="small"
                              icon={<Check size={12} />}
                              onClick={() => handleConfirm(d.id, r.id)}
                            >
                              {t('common.confirm')}
                            </Button>
                          </Space>
                        )}
                      </div>
                    );
                  })}
                </Space>
              </List.Item>
            )}
              />
                </>
              ),
            },
            {
              key: 'confirmations',
              label: `${t('dashboard.pendingConfirmations')} (${dashboard.pending_confirmations_total})`,
              children: (
                <>
                {renderTodoToolbar('confirmation', dashboard.pending_confirmations.map((item) => item.resource.id))}
                <List
                  className="dashboard-todo-list"
                  dataSource={dashboard.pending_confirmations}
                  pagination={dashboard.pending_confirmations_total > TODO_PAGE_SIZE ? {
                    current: confirmationPage,
                    pageSize: TODO_PAGE_SIZE,
                    total: dashboard.pending_confirmations_total,
                    showSizeChanger: false,
                    onChange: setConfirmationPage,
                  } : false}
                  renderItem={(item) => {
                    const r = item.resource;
                    const episodeConfirmation = item.kinds.includes('season_ambiguous')
                      || item.kinds.includes('episode_ambiguous');
                    const base = {
                      season: r.season ?? null,
                      episode: r.episode ?? null,
                      absolute_episode: r.absolute_episode ?? null,
                    };
                    const draft = { ...base, ...(episodeDrafts[r.id] ?? {}) };
                    const patchDraft = (patch: Partial<EpisodeDraft>) =>
                      setEpisodeDrafts((prev) => ({
                        ...prev,
                        [r.id]: { ...base, ...(prev[r.id] ?? {}), ...patch },
                      }));
                    return (
                      <List.Item
                        key={r.id}
                        className="dashboard-todo-item"
                        style={{ display: 'block' }}
                      >
                        <div className="pending-resource-confirmation">
                          <div className="pending-resource-header">
                            <Checkbox
                              checked={selectedTodos.confirmation.includes(r.id)}
                              onChange={(event) => toggleTodo('confirmation', r.id, event.target.checked)}
                              aria-label={t('dashboard.selectTodo')}
                            />
                            <div className="pending-resource-summary">
                              <div className="pending-resource-work-line">
                            {r.series_id || r.movie_id ? (
                              <Link to={r.series_id ? `/series/${r.series_id}` : `/movies/${r.movie_id}`}>
                                <Text strong>{item.work_title || r.title_cn || r.title_raw}</Text>
                              </Link>
                            ) : (
                              <Text strong>{item.work_title || r.title_cn || r.title_raw}</Text>
                            )}
                                <Tag color="orange" bordered={false} style={{ margin: 0 }}>
                                  {t('channels.episodeAmbiguousTag')}
                                </Tag>
                              </div>
                              <div className="pending-resource-meta">
                              {item.channel_name && (
                                <>
                                  <Link to={`/channels/${r.channel_id}`}>
                                    <Text style={{ fontSize: 12 }}>{item.channel_name}</Text>
                                  </Link>
                                  {' · '}
                                </>
                              )}
                              {timeAgo(r.created_at)}
                                {r.title_raw && (
                                  <>
                                    {' · '}
                                    <Text type="secondary" ellipsis={{ tooltip: r.title_raw }} style={{ maxWidth: 520, fontSize: 12 }}>
                                      {r.title_raw}
                                    </Text>
                                  </>
                                )}
                              </div>
                              <Space size={6} className="pending-resource-tags" wrap>
                              {item.kinds.map((kind) => (
                                <Tag color="orange" key={kind} style={{ margin: 0 }}>
                                  {t(`dashboard.confirmationKinds.${kind}`)}
                                </Tag>
                              ))}
                              {item.missing_fields.length > 0 && (
                                <Text type="secondary" style={{ fontSize: 12 }}>
                                  {t('dashboard.missingFields', { fields: item.missing_fields.join(', ') })}
                                </Text>
                              )}
                              {r.subtitle_group && <Tag style={{ margin: 0 }}>{r.subtitle_group}</Tag>}
                              {r.resolution && <Tag style={{ margin: 0 }}>{r.resolution}</Tag>}
                              {r.video_codec && <Tag style={{ margin: 0 }}>{r.video_codec}</Tag>}
                              {r.season != null && <span>S{r.season}</span>}
                              {r.episode != null && <span>EP{r.episode}</span>}
                              {r.file_size != null && <span>{formatBytes(r.file_size)}</span>}
                              </Space>
                            </div>
                            <div className="pending-resource-inspect">
                              <Button
                                type="link"
                                danger
                                size="small"
                                onClick={() => handleIgnoreTodos('confirmation', [r.id])}
                              >
                                {t('dashboard.ignore')}
                              </Button>
                              <Button
                                type="link"
                                size="small"
                                icon={<ListTree size={14} />}
                                onClick={() => setFilesResourceId(r.id)}
                              >
                                {t('resource.files')}
                              </Button>
                              <Button
                                type="link"
                                size="small"
                                icon={<PencilLine size={14} />}
                                onClick={() => setCorrectionResource(r)}
                              >
                                {t('resource.fullCorrection')}
                              </Button>
                            </div>
                          </div>
                          {episodeConfirmation && (
                          <div className="pending-resource-quick-form">
                            <div className="pending-resource-quick-heading">
                              <Text strong style={{ fontSize: 13 }}>{t('resource.quickEpisodeTitle')}</Text>
                            </div>
                            <div className="pending-resource-fields">
                              <label>
                                <span>{t('resource.seasonLabel')}</span>
                                <SeasonInput
                                  size="small"
                                  value={draft.season}
                                  onChange={(v) => patchDraft({ season: v })}
                                />
                              </label>
                              <label>
                                <span>{t('resource.episodePerSeasonLabel')}</span>
                                <InputNumber
                                  size="small"
                                  min={1}
                                  value={draft.episode}
                                  onChange={(v) => patchDraft({ episode: typeof v === 'number' ? v : null })}
                                />
                              </label>
                              <label>
                                <span>{t('resource.absoluteEpisodeShort')}</span>
                                <InputNumber
                                  size="small"
                                  min={0}
                                  value={draft.absolute_episode}
                                  onChange={(v) => patchDraft({ absolute_episode: typeof v === 'number' ? v : null })}
                                />
                              </label>
                              <Button
                                type="primary"
                                size="small"
                                loading={savingEpisodeCid === r.id}
                                disabled={draft.episode == null}
                                onClick={() => handleCorrectEpisode(r.id, draft)}
                              >
                                {t('resource.confirmEpisode')}
                              </Button>
                            </div>
                          </div>
                          )}
                        </div>
                      </List.Item>
                    );
                  }}
                />
                </>
              ),
            },
            {
              key: 'plans',
              label: `${t('dashboard.pendingPlans')} (${dashboard.pending_plans_total})`,
              children: (
                <>
                {renderTodoToolbar('plan', dashboard.pending_plans.map((item) => item.id))}
                <List
                  className="dashboard-todo-list"
                  dataSource={dashboard.pending_plans}
                  pagination={dashboard.pending_plans_total > TODO_PAGE_SIZE ? {
                    current: planPage,
                    pageSize: TODO_PAGE_SIZE,
                    total: dashboard.pending_plans_total,
                    showSizeChanger: false,
                    onChange: setPlanPage,
                  } : false}
                  renderItem={(p) => {
              const preview = p.ops_preview ?? [];
              const extra = p.ops_summary.total - preview.length;
              return (
                <List.Item
                  key={p.id}
                  className="dashboard-todo-item"
                  style={{ display: 'block' }}
                >
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'flex-start',
                      gap: 12,
                      marginBottom: 8,
                    }}
                  >
                    <Checkbox
                      checked={selectedTodos.plan.includes(p.id)}
                      onChange={(event) => toggleTodo('plan', p.id, event.target.checked)}
                      aria-label={t('dashboard.selectTodo')}
                    />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <Space size={8} wrap>
                        <StatusBadge status={p.status} />
                        {p.pending_reason === 'unclassified' && (
                          <Tag color="orange">{t('organize.uncategorizedTag')}</Tag>
                        )}
                        {p.pending_reason === 'unbound' && (
                          <Tag color="volcano">{t('organize.unboundTag')}</Tag>
                        )}
                      </Space>
                      <div style={{ fontSize: 12, color: 'var(--rr-text-muted)', marginTop: 4 }}>
                        <Link to="/media-library">
                          <Text style={{ fontSize: 12 }}>{p.rule_name ?? t('format.dash')}</Text>
                        </Link>
                        {' · '}
                        <Link to="/media-library">
                          <Text style={{ fontSize: 12 }}>{p.library_name ?? t('organize.uncategorizedTag')}</Text>
                        </Link>
                        {p.category ? ` · ${p.category}` : ''}
                        {' · '}
                        {timeAgo(p.created_at)}
                      </div>
                    </div>
                    <Space size={4} style={{ flexShrink: 0 }}>
                      <Button
                        type="text"
                        size="small"
                        icon={<Eye size={14} />}
                        title={t('organize.detail')}
                        onClick={() => setPlanDrawerId(p.id)}
                      />
                      {isPlanExecutable(p) && (
                        <Button
                          type="primary"
                          size="small"
                          icon={<Play size={14} />}
                          loading={executingPlanId === p.id}
                          onClick={() => handleExecutePlan(p.id)}
                        >
                          {t('organize.execute')}
                        </Button>
                      )}
                      {isPlanCancellable(p) && (
                        <Button
                          type="text"
                          size="small"
                          danger
                          icon={<XCircle size={14} />}
                          title={t('organize.cancelPlan')}
                          onClick={() => handleCancelPlan(p)}
                        />
                      )}
                    </Space>
                  </div>

                  {preview.length > 0 && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                      {preview.map((op) => {
                        const color =
                          op.op_type === 'move' ? 'green' : op.op_type === 'movedir' ? 'blue' : undefined;
                        const label =
                          op.op_type === 'move'
                            ? t('organize.opMove')
                            : op.op_type === 'movedir'
                              ? t('organize.opMovedir')
                              : t('organize.opKeep');
                        return (
                          <div key={op.id} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', minWidth: 0 }}>
                            <Tag color={color} style={{ margin: 0, flexShrink: 0 }}>{label}</Tag>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <OrganizeOpPaths src={op.src} dst={op.dst} />
                            </div>
                          </div>
                        );
                      })}
                      {extra > 0 && (
                        <Button
                          type="link"
                          size="small"
                          style={{ padding: 0, alignSelf: 'flex-start' }}
                          onClick={() => setPlanDrawerId(p.id)}
                        >
                          {t('organize.moreOps', { n: extra })}
                        </Button>
                      )}
                    </div>
                  )}
                </List.Item>
              );
                  }}
                />
                </>
              ),
            },
          ]}
        />
      </Card>
      ) : (
        <div style={{ marginTop: -8, marginBottom: 24 }}>
          <Text type="secondary" style={{ fontSize: 13 }}>
            <CheckCircle
              size={14}
              style={{ marginRight: 6, color: 'var(--rr-success)', verticalAlign: 'text-bottom' }}
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
                      border: '1px solid var(--rr-border-soft)',
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
                    <div style={{ fontSize: 12, color: 'var(--rr-text-muted)', marginBottom: 8 }}>
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
                              background: 'var(--rr-surface-card)',
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
                            <Space size="small" style={{ color: 'var(--rr-text-muted)', fontSize: 12, flexShrink: 0 }}>
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
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <ProgressBar progress={task.progress} />
                            </div>
                            <Text
                              type="secondary"
                              style={{ fontSize: 12, minWidth: 62, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}
                            >
                              ↓{formatSpeed(task.download_speed)}
                            </Text>
                          </div>
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

      <OrganizePlanDrawer
        planId={planDrawerId}
        libraries={libraries}
        onClose={() => setPlanDrawerId(null)}
        onChanged={fetchData}
      />

      <ResourceFilesDrawer
        resourceId={filesResourceId}
        open={!!filesResourceId}
        onClose={() => setFilesResourceId(null)}
      />

      <ResourceCorrectionModal
        resourceId={correctionResource?.id ?? null}
        open={!!correctionResource}
        onClose={() => setCorrectionResource(null)}
        onSaved={() => {
          setCorrectionResource(null);
          fetchData();
        }}
      />
    </div>
  );
}
