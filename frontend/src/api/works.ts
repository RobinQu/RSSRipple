import { api } from './client';
import type { MetadataSourceOption } from './channels';
import type { Work } from '../types';

export interface MetadataConfigResponse {
  default_source: string | null;
  sources: MetadataSourceOption[];
  default: string;
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
  content_type: 'tv' | 'movie';
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
  setMetadataConfig: (default_source: string | null) =>
    api.put<{ default_source: string | null }>('/works/metadata-config', { default_source }),
  refreshMetadata: (id: string, content_type: 'tv' | 'movie', source?: string | null) =>
    api.post<RefreshResult>('/works/refresh-metadata', { id, content_type, source: source ?? null }),
  batchRefreshMetadata: (items: RefreshItem[], source?: string | null) =>
    api.post<BatchRefreshResponse>('/works/batch-refresh-metadata', {
      items,
      source: source ?? null,
    }),
};
