// ---------------------------------------------------------------------------
// AgentDetail — data-loading hook: agent/tasks/decisions/works state with
// latest-request-wins guards (a stale response from an earlier page never
// overwrites newer state; cancelled on unmount), plus the lightweight
// tab-count poll. The run-history tab owns its own loading (see RunTab).
// ---------------------------------------------------------------------------

import { useCallback, useEffect, useRef, useState } from 'react';
import { agentsApi } from '../../api/agents';
import { tasksApi, decisionsApi } from '../../api/tasks';
import { resourcesApi } from '../../api/channels';
import { createRequestGuard } from '../../utils/requestGuard';
import type {
  Agent,
  AgentWork,
  DownloadTask,
  FileResource,
  FilterConfig,
  PendingDecision,
} from '../../types';

export function useAgentDetailData(id: string | undefined, taskSortParam: string | undefined) {
  const [agent, setAgent] = useState<Agent | null>(null);
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
  // Buffered works editing: add/remove/edit only touch local state; the
  // list-level "Save" button batch-replaces works via PUT /agents/{id}. This
  // mirrors AgentForm's behaviour and lets the user configure per-work
  // filter_overrides before committing — the per-work inline Save is gone.
  const [worksDirty, setWorksDirty] = useState(false);

  // Filters (owned here so a fresh agent load replaces the edited draft —
  // works saves must not clobber unsaved filter edits).
  const [filterConfig, setFilterConfig] = useState<FilterConfig | null>(null);

  // Latest-request-wins guards: one per loader so a stale response (e.g. an
  // earlier task/decision page resolving late) never overwrites newer state;
  // cancelled on unmount so no setState fires afterwards.
  const guardsRef = useRef({
    agent: createRequestGuard(),
    tasks: createRequestGuard(),
    decisions: createRequestGuard(),
    works: createRequestGuard(),
  });
  useEffect(() => {
    const guards = guardsRef.current;
    return () => Object.values(guards).forEach((g) => g.cancel());
  }, []);

  const loadAgent = useCallback(async () => {
    if (!id) return;
    const token = guardsRef.current.agent.next();
    setLoadingAgent(true);
    const r = await agentsApi.get(id);
    if (!guardsRef.current.agent.isCurrent(token)) return;
    if (r.success) {
      setAgent(r.data);
      setFilterConfig(r.data.filter_config ?? null);
      if (r.data.works) setWorks(r.data.works);
    }
    setLoadingAgent(false);
  }, [id]);

  const loadTasks = useCallback(async () => {
    if (!id) return;
    const token = guardsRef.current.tasks.next();
    setLoadingTasks(true);
    const r = await tasksApi.listByAgent(id, taskPage, 20, taskStatus, taskSortParam);
    if (!guardsRef.current.tasks.isCurrent(token)) return;
    if (r.success) {
      setTasks(r.data);
      if (r.meta) setTaskTotal(r.meta.total);
    }
    setLoadingTasks(false);
  }, [id, taskPage, taskStatus, taskSortParam]);

  const loadDecisions = useCallback(async () => {
    if (!id) return;
    const token = guardsRef.current.decisions.next();
    setLoadingDec(true);
    const r = await decisionsApi.listByAgent(id, decPage, 20, 'pending');
    if (!guardsRef.current.decisions.isCurrent(token)) return;
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
        if (!guardsRef.current.decisions.isCurrent(token)) return;
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
    const token = guardsRef.current.works.next();
    setLoadingWorks(true);
    const r = await agentsApi.listWorks(id);
    if (!guardsRef.current.works.isCurrent(token)) return;
    if (r.success) {
      setWorks(r.data);
      setWorksDirty(false);
    }
    setLoadingWorks(false);
  }, [id]);

  useEffect(() => {
    loadAgent();
  }, [loadAgent]);

  // Lightweight tab-count poll: refresh the works/tasks/decisions counts every
  // 15s so badges like "下载任务 (N)" stay current without a manual refresh.
  // Uses page_size=1 for the list endpoints to keep the payload tiny, and
  // never overwrites unsaved works edits (worksDirty guard).
  const refreshCounts = useCallback(async () => {
    if (!id) return;
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
  }, [id, worksDirty, taskStatus]);

  useEffect(() => {
    // Fetch immediately on mount - otherwise the badges show (0) for up to
    // 15s (or until the tab is first opened) after navigation.
    refreshCounts();
    const interval = setInterval(refreshCounts, 15000);
    return () => clearInterval(interval);
  }, [refreshCounts]);

  return {
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
  };
}

export type AgentDetailData = ReturnType<typeof useAgentDetailData>;
