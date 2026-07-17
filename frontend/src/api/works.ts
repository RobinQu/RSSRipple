import { api } from './client';
import type { MetadataSourceOption } from './channels';
import type { Work } from '../types';

export interface MetadataConfigResponse {
  default_source: string | null;
  auto_refresh_enabled: boolean;
  auto_refresh_interval_minutes: number;
  sources: MetadataSourceOption[];
}

export interface RefreshResult {
  found: boolean;
  filled: string[];
  source: string | null;
  message?: string;
  candidate?: {
    title_cn: string | null;
    title_en: string | null;
    external_id: string | null;
    external_source: string | null;
  };
}

export interface RefreshItem {
  id: string;
  content_type: 'tv' | 'movie' | 'asmr' | 'music' | 'drama_cd' | 'radio' | 'other';
}

export interface BatchRefreshResponse {
  job: { job_id: string; status: string } | null;
  count: number;
  source: string | null;
}

export const worksApi = {
  list: (page = 1, pageSize = 20, search?: string, content_type?: string) => {
    const qs = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (search) qs.set('search', search);
    if (content_type) qs.set('content_type', content_type);
    return api.get<Work[]>(`/works?${qs.toString()}`);
  },
  getMetadataConfig: () => api.get<MetadataConfigResponse>('/works/metadata-config'),
  setMetadataConfig: (
    default_source: string,
    auto_refresh_enabled: boolean,
    auto_refresh_interval_minutes: number,
  ) =>
    api.put<MetadataConfigResponse>('/works/metadata-config', {
      default_source,
      auto_refresh_enabled,
      auto_refresh_interval_minutes,
    }),
  refreshMetadata: (id: string, content_type: RefreshItem['content_type'], source?: string | null) =>
    api.post<RefreshResult>('/works/refresh-metadata', { id, content_type, source: source ?? null }),
  batchRefreshMetadata: (items: RefreshItem[], source?: string | null) =>
    api.post<BatchRefreshResponse>('/works/batch-refresh-metadata', {
      items,
      source: source ?? null,
    }),
};
