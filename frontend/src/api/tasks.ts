import { api } from './client';
import type { DownloadTask, PendingDecision } from '../types';

export const tasksApi = {
  listByAgent: (agentId: string, page = 1, pageSize = 20, status?: string) => {
    const qs = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (status) qs.set('status', status);
    return api.get<DownloadTask[]>(`/agents/${agentId}/tasks?${qs.toString()}`);
  },
  get: (id: string) => api.get<DownloadTask>(`/tasks/${id}`),
  create: (body: { resource_id: string; downloader_id: string }) =>
    api.post<DownloadTask>('/tasks', body),
  pause: (id: string) =>
    api.post<{ id: string; status: string }>(`/tasks/${id}/pause`),
  resume: (id: string) =>
    api.post<{ id: string; status: string }>(`/tasks/${id}/resume`),
  retry: (id: string) =>
    api.post<{ id: string; status: string }>(`/tasks/${id}/retry`),
  // taskIds omitted/empty = retry every error/paused task of the agent.
  batchRetry: (agentId: string, taskIds?: string[]) =>
    api.post<{ processed: number; retried: number; failed: number; errors: string[] }>(
      `/agents/${agentId}/tasks/batch-retry`,
      taskIds && taskIds.length ? { task_ids: taskIds } : {},
    ),
  delete: (id: string, deleteData = false) =>
    api.delete<null>(`/tasks/${id}?delete_data=${deleteData}`),
};

export const decisionsApi = {
  listByAgent: (
    agentId: string,
    page = 1,
    pageSize = 20,
    status?: string,
  ) => {
    const qs = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (status) qs.set('status', status);
    return api.get<PendingDecision[]>(
      `/agents/${agentId}/decisions?${qs.toString()}`,
    );
  },
  confirm: (id: string, resourceId: string) =>
    api.post<PendingDecision>(`/decisions/${id}/confirm`, {
      resource_id: resourceId,
    }),
  skip: (id: string) => api.post<PendingDecision>(`/decisions/${id}/skip`),
  aiPick: (id: string) =>
    api.post<{ id: string; status: string; decided_resource_id: string | null }>(
      `/decisions/${id}/ai-pick`,
    ),
  batch: (agentId: string, decisionIds: string[], action: 'skip' | 'ai') =>
    api.post<{
      processed: number;
      dispatched: number;
      skipped: number;
      failed: number;
      errors: string[];
    }>(`/agents/${agentId}/decisions/batch`, {
      decision_ids: decisionIds,
      action,
    }),
};

export const dashboardApi = {
  get: () =>
    api.get<import('../types').DashboardData>('/dashboard'),
};
