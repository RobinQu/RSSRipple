import { api } from './client';
import type { MetadataSourceOption } from './channels';
import type { Work } from '../types';

export interface MetadataConfigResponse {
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
  // Source is required: there is no global default source anymore.
  refreshMetadata: (
    id: string,
    content_type: RefreshItem['content_type'],
    source: string,
    overrideManualEdits?: boolean,
  ) =>
    api.post<RefreshResult>('/works/refresh-metadata', {
      id,
      content_type,
      source,
      override_manual_edits: overrideManualEdits ?? false,
    }),
  batchRefreshMetadata: (items: RefreshItem[], source: string) =>
    api.post<BatchRefreshResponse>('/works/batch-refresh-metadata', {
      items,
      source,
    }),
};
