import { api } from './client';
import type { DownloadTask, PendingDecision, DashboardData } from '../types';

export const tasksApi = {
  listByAgent: (agentId: string, page = 1, pageSize = 20) =>
    api.get<DownloadTask[]>(`/agents/${agentId}/tasks?page=${page}&page_size=${pageSize}`),
  get: (id: string) =>
    api.get<DownloadTask>(`/tasks/${id}`),
  pause: (id: string) =>
    api.post<{ id: string; status: string; message: string }>(`/tasks/${id}/pause`),
  resume: (id: string) =>
    api.post<{ id: string; status: string; message: string }>(`/tasks/${id}/resume`),
  retry: (id: string) =>
    api.post<{ id: string; status: string; message: string }>(`/tasks/${id}/retry`),
  delete: (id: string) =>
    api.delete<null>(`/tasks/${id}`),
};

export const decisionsApi = {
  listByAgent: (agentId: string, page = 1, pageSize = 20) =>
    api.get<PendingDecision[]>(`/agents/${agentId}/decisions?page=${page}&page_size=${pageSize}`),
  confirm: (id: string, resourceId: string) =>
    api.post<PendingDecision>(`/decisions/${id}/confirm`, { resource_id: resourceId }),
  skip: (id: string) =>
    api.post<PendingDecision>(`/decisions/${id}/skip`),
};

export const dashboardApi = {
  get: () => api.get<DashboardData>('/dashboard'),
};
