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

export interface MergeWorksRequest {
  survivor_type: 'series' | 'movie';
  survivor_id: string;
  duplicate_ids: string[];
  confirm: boolean;
}

// POST /works/merge — manual same-season merge (per-season works repair
// tool). confirm=false is a probe: the server replies 422 with the
// irreversibility warning; resubmit with confirm=true after the user acks.
export interface MergeWorksResponse {
  survivor_type: string;
  survivor_id: string;
  merged: number;
  notes?: string[];
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
  merge: (body: MergeWorksRequest) =>
    api.post<MergeWorksResponse>('/works/merge', body),
  batchRefreshMetadata: (items: RefreshItem[], source: MetadataSource, trustedSites: string[]) =>
    api.post<BatchRefreshResponse>('/works/batch-refresh-metadata', {
      items,
      source,
      trusted_sites: trustedSites,
    }),
};
