import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useDocumentTitle from '../hooks/useDocumentTitle';
import useAgentFilterFields from '../hooks/useAgentFilterFields';
import useUrlTab from '../hooks/useUrlTab';
import { useDownloadTaskPrefs } from '../hooks/useDownloadTaskPrefs';
import {
  Tabs,
  Button,
  Space,
  Card,
  Typography,
  Spin,
} from 'antd';
import { ArrowLeft, Edit } from 'lucide-react';
import StatusBadge from '../components/StatusBadge';
import NotificationsPanel from '../components/NotificationsPanel';
import ResourceDetailDrawer from '../components/ResourceDetailDrawer';
import ResourceFilesDrawer from '../components/ResourceFilesDrawer';
import { timeAgo } from '../utils/format';
import { agentTaskColumnStorageKey } from '../utils/requiredFields';
import { agentTaskSortStorageKey } from '../utils/sortSettings';
import { useAgentDetailData } from './agent-detail/useAgentDetailData';
import WorksTab from './agent-detail/WorksTab';
import TasksTab from './agent-detail/TasksTab';
import DecisionsTab from './agent-detail/DecisionsTab';
import FiltersTab from './agent-detail/FiltersTab';
import RunTab from './agent-detail/RunTab';
import type { Agent, FileResource } from '../types';

const { Title, Text } = Typography;

// Page-level tabs mirrored to `?tab=` (see useUrlTab).
const AGENT_DETAIL_TABS = ['works', 'tasks', 'decisions', 'filters', 'notifications', 'run'] as const;

/** Agent detail page: header summary + per-tab panels. All data loading lives
 * in ``agent-detail/useAgentDetailData`` (request-guarded) and each tab is a
 * self-contained component under ``agent-detail/``; this file only wires tab
 * state, the shared drawers and the cross-tab refresh callbacks. */
export default function AgentDetail() {
  const { id } = useParams<{ id: string }>();
  const { t } = useTranslation();
  const [tab, setTab] = useUrlTab('works', AGENT_DETAIL_TABS);
  // Task-table sort/column prefs (shared with the downloader local-task
  // table), persisted per agent in localStorage.
  const taskPrefs = useDownloadTaskPrefs(
    id ? agentTaskSortStorageKey(id) : undefined,
    id ? agentTaskColumnStorageKey(id) : undefined,
  );
  const {
    agent,
    setAgent,
    loadingAgent,
    loadAgent,
    tasks,
    taskPage,
    setTaskPage,
    taskTotal,
    taskStatus,
    setTaskStatus,
    loadingTasks,
    loadTasks,
    decisions,
    decTotal,
    loadingDec,
    candidateCache,
    loadDecisions,
    works,
    setWorks,
    worksDirty,
    setWorksDirty,
    loadingWorks,
    loadWorks,
    filterConfig,
    setFilterConfig,
  } = useAgentDetailData(id, taskPrefs.sortParam);

  useDocumentTitle(agent?.name ?? t('agents.title'));
  // Channel required-fields gate for the filter DSL editor (null =
  // unrestricted; pick preferences are exempt and never receive this).
  const allowedFilterFields = useAgentFilterFields(agent?.channel_id);

  // Read-only resource view opened by clicking a task row.
  const [selectedTaskResource, setSelectedTaskResource] = useState<FileResource | null>(null);
  // File-listing drawer shared by the decisions tab and the run drawer.
  const [filesResourceId, setFilesResourceId] = useState<string | null>(null);

  useEffect(() => {
    if (tab === 'tasks') loadTasks();
  }, [tab, loadTasks]);

  useEffect(() => {
    if (tab === 'decisions') loadDecisions();
  }, [tab, loadDecisions]);

  useEffect(() => {
    // Reload works when entering the tab, but never overwrite unsaved edits.
    if (tab === 'works' && !worksDirty) loadWorks();
  }, [tab, loadWorks, worksDirty]);

  const handleWorksChange = (next: typeof works) => {
    setWorks(next);
    setWorksDirty(true);
  };

  /** Works save settled: adopt the returned agent (works + counts) and
   * refresh the tasks list (the save may have dispatched backfill). */
  const handleWorksSaved = (updated: Agent) => {
    if (updated.works) setWorks(updated.works);
    setAgent(updated);
    setWorksDirty(false);
    loadTasks();
  };

  if (loadingAgent || !agent) {
    return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  }

  return (
    <div>
      <Space align="center" style={{ marginBottom: 24 }}>
        <Link to="/agents">
          <Button type="text" icon={<ArrowLeft size={18} />} aria-label={t('common.back')} />
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
        onChange={(k) => setTab(k as typeof tab)}
        items={[
          {
            key: 'works',
            label: `${t('agents.subscribedWorks')} (${works.length})`,
            children: (
              <WorksTab
                agent={agent}
                works={works}
                worksDirty={worksDirty}
                loadingWorks={loadingWorks}
                onWorksChange={handleWorksChange}
                onSaved={handleWorksSaved}
              />
            ),
          },
          {
            key: 'tasks',
            label: `${t('agents.downloadTasks')} (${taskTotal})`,
            children: id ? (
              <TasksTab
                agentId={id}
                tasks={tasks}
                loading={loadingTasks}
                page={taskPage}
                total={taskTotal}
                statusFilter={taskStatus}
                taskPrefs={taskPrefs}
                onPageChange={setTaskPage}
                onStatusFilterChange={setTaskStatus}
                onReload={loadTasks}
                onOpenResource={setSelectedTaskResource}
              />
            ) : null,
          },
          {
            key: 'decisions',
            label: `${t('dashboard.pendingDecisions')} (${decTotal})`,
            children: id ? (
              <DecisionsTab
                agentId={id}
                decisions={decisions}
                candidateCache={candidateCache}
                loading={loadingDec}
                onDecisionsChange={loadDecisions}
                onTasksChange={loadTasks}
                onShowFiles={setFilesResourceId}
              />
            ) : null,
          },
          {
            key: 'filters',
            label: t('agents.filter'),
            children: (
              <FiltersTab
                agent={agent}
                filterConfig={filterConfig}
                onFilterConfigChange={setFilterConfig}
                allowedFilterFields={allowedFilterFields}
                onSaved={loadAgent}
              />
            ),
          },
          {
            key: 'notifications',
            label: t('agents.notifications'),
            // Mounted lazily by Tabs on first activation; the panel fetches
            // webhook status and the notification list on mount.
            children: id ? <NotificationsPanel agentId={id} /> : null,
          },
          {
            key: 'run',
            label: t('agents.runControl'),
            children: id ? (
              <RunTab
                agentId={id}
                active={tab === 'run'}
                onActivity={loadTasks}
                onShowFiles={setFilesResourceId}
              />
            ) : null,
          },
        ]}
      />

      <ResourceFilesDrawer
        resourceId={filesResourceId}
        open={!!filesResourceId}
        onClose={() => setFilesResourceId(null)}
      />

      {/* Read-only resource view: task rows open the shared resource drawer
          with all write actions disabled (same as the downloader page). */}
      <ResourceDetailDrawer
        resource={selectedTaskResource}
        readOnly
        onClose={() => setSelectedTaskResource(null)}
      />
    </div>
  );
}
