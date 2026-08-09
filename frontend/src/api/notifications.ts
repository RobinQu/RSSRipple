import { api } from './client';
import type {
  AgentWebhook,
  DownloadNotification,
  DownloadNotificationDetail,
} from '../types';

export type RetryMode = 'failed' | 'all';

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
  // Reset one notification's deliveries for redelivery — either only the
  // failed ones or all of them.
  retry: (id: string, mode: RetryMode) =>
    api.post<{ reset: number }>(`/notifications/${id}/retry`, { mode }),
  // Bulk retry across notifications; `since`/`agent_id` narrow the scope.
  retryBulk: (body: { mode: RetryMode; since?: string; agent_id?: string }) =>
    api.post<{ reset: number }>('/notifications/retry', body),
  // since=null means "start from the earliest completed task".
  backfill: (agentId: string, since: string | null) =>
    api.post<{ created: number }>(`/agents/${agentId}/notifications/backfill`, { since }),

  listWebhooks: (agentId: string) => api.get<AgentWebhook[]>(`/agents/${agentId}/webhooks`),
  createWebhook: (agentId: string, body: { url: string; mock?: boolean; enabled?: boolean }) =>
    api.post<AgentWebhook>(`/agents/${agentId}/webhooks`, body),
  updateWebhook: (
    agentId: string,
    webhookId: string,
    body: { url?: string; mock?: boolean; enabled?: boolean },
  ) => api.put<AgentWebhook>(`/agents/${agentId}/webhooks/${webhookId}`, body),
  deleteWebhook: (agentId: string, webhookId: string) =>
    api.delete<{ deleted: boolean }>(`/agents/${agentId}/webhooks/${webhookId}`),
};
