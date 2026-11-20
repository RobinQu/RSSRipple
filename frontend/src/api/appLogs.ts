import { api } from './client';

export type AppLogCategory = 'metadata' | 'torrent' | 'magnet' | 'listing';
export type AppLogStatus = 'failed' | 'retrying' | 'resolved';

export interface AppLogResource {
  id: string;
  title_raw: string;
  title_cn: string | null;
  title_en: string | null;
  search_title: string | null;
  guid: string;
  detail_url: string | null;
  work_title: string | null;
  series_id: string | null;
  movie_id: string | null;
  collection_id: string | null;
  season: number | null;
  episode: number | null;
  absolute_episode: number | null;
  is_batch: boolean;
  batch_scope: string | null;
  metadata_attempts: number;
  metadata_failure_type: string | null;
  torrent_fetch_status: string | null;
  torrent_fetch_error: string | null;
  magnet_resolve_status: string | null;
  magnet_resolve_error: string | null;
  listing_reconcile_status: string | null;
  listing_reconcile_error: string | null;
}

export interface AppLogItem {
  id: string;
  category: AppLogCategory;
  severity: string;
  message: string;
  resource_id: string | null;
  channel_id: string | null;
  resource_title: string | null;
  resource: AppLogResource | null;
  channel_name: string | null;
  attempts: number;
  status: AppLogStatus;
  retry_count: number;
  last_retried_at: string | null;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
}

export const appLogsApi = {
  list: (params: { page: number; pageSize: number; category?: string; status?: string }) => {
    const query = new URLSearchParams({
      page: String(params.page),
      page_size: String(params.pageSize),
    });
    if (params.category) query.set('category', params.category);
    if (params.status) query.set('status', params.status);
    return api.get<AppLogItem[]>(`/app-logs?${query.toString()}`);
  },
  retry: (id: string) => api.post<{ retry: { status: string; log_id: string } }>(`/app-logs/${id}/retry`),
};
