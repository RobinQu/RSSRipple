import { api } from './client';

export type QueueJobStatus = 'queued' | 'running' | 'done' | 'failed';
export type QueueStatsScope = 'since_restart' | 'last_24h';

export interface QueueJob {
  job_id: string;
  job_type: string;
  key: string | null;
  status: QueueJobStatus;
  result: unknown;
  error: string | null;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface QueueTypeStats {
  job_type: string;
  total: number;
  queued: number;
  running: number;
  done: number;
  failed: number;
  /** null when the type has no terminal jobs in the stats window. */
  success_rate: number | null;
  avg_duration_seconds: number | null;
}

export interface QueueOverview {
  backend: 'memory' | 'redis';
  app_role: 'all' | 'web' | 'worker';
  stats_scope: QueueStatsScope;
  counts: Record<QueueJobStatus, number>;
  by_type: QueueTypeStats[];
}

export interface SchedulerJob {
  id: string;
  trigger: string;
  next_run_time: string | null;
}

export interface QueueSchedulerResponse {
  enabled: boolean;
  jobs: SchedulerJob[];
}

export const queueApi = {
  overview: () => api.get<QueueOverview>('/queue/overview'),
  jobs: (params: { status?: string; jobType?: string; page?: number; pageSize?: number } = {}) => {
    const qs = new URLSearchParams({
      page: String(params.page ?? 1),
      page_size: String(params.pageSize ?? 50),
    });
    if (params.status) qs.set('status', params.status);
    if (params.jobType) qs.set('job_type', params.jobType);
    return api.get<QueueJob[]>(`/queue/jobs?${qs.toString()}`);
  },
  scheduler: () => api.get<QueueSchedulerResponse>('/queue/scheduler'),
};
