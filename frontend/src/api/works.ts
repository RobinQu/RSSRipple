import { api } from './client';
import type { MetadataSource, Work } from '../types';

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
  batchRefreshMetadata: (items: RefreshItem[], source: MetadataSource, trustedSites: string[]) =>
    api.post<BatchRefreshResponse>('/works/batch-refresh-metadata', {
      items,
      source,
      trusted_sites: trustedSites,
    }),
};
