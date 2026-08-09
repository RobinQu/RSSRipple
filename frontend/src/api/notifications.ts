import { api } from './client';
import type {
  AgentWebhook,
  DownloadNotification,
  DownloadNotificationDetail,
} from '../types';

export const notificationsApi = {
  listByAgent: (agentId: string, page = 1, pageSize = 20, status?: string) => {
    const qs = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (status) qs.set('status', status);
    return api.get<DownloadNotification[]>(`/agents/${agentId}/notifications?${qs.toString()}`);
  },
  get: (id: string) => api.get<DownloadNotificationDetail>(`/notifications/${id}`),
  retry: (id: string) => api.post<DownloadNotification>(`/notifications/${id}/retry`),
  // since=null means "start from the earliest completed task".
  backfill: (agentId: string, since: string | null) =>
    api.post<{ created: number }>(`/agents/${agentId}/notifications/backfill`, { since }),
  getWebhook: (agentId: string) => api.get<AgentWebhook>(`/agents/${agentId}/webhook`),
  putWebhook: (agentId: string, body: { url?: string | null; mock: boolean }) =>
    api.put<AgentWebhook>(`/agents/${agentId}/webhook`, body),
  deleteWebhook: (agentId: string) => api.delete<null>(`/agents/${agentId}/webhook`),
};
