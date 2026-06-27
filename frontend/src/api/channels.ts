import { api } from './client';
import type {
  Channel,
  ChannelDetail,
  ChannelStatus,
  FieldMapping,
  FileResource,
  FilterSuggestionResponse,
  GroupedResource,
  MetadataSearchResult,
  PreviewEntry,
} from '../types';

export interface PreviewFeedData {
  entries: PreviewEntry[];
  parsed: Record<string, unknown>[];
}

export interface ChannelCreate {
  name: string;
  type: 'rss_feed';
  url: string;
  fetch_interval?: number;
  field_mapping: FieldMapping;
  title_extraction_method?: 'none' | 'regex' | 'llm';
  title_extraction_regex?: string | null;
  metadata_source?: 'llm' | 'none';
}

export interface ChannelUpdate {
  name?: string;
  url?: string;
  fetch_interval?: number;
  status?: ChannelStatus;
  field_mapping?: FieldMapping;
  title_extraction_method?: 'none' | 'regex' | 'llm';
  title_extraction_regex?: string | null;
  metadata_source?: 'llm' | 'none';
}

export const channelsApi = {
  list: (page = 1, pageSize = 20) =>
    api.get<Channel[]>(`/channels?page=${page}&page_size=${pageSize}`),
  get: (id: string) => api.get<ChannelDetail>(`/channels/${id}`),
  getFormToken: () => api.get<{ token: string }>('/channels/form-token'),
  create: (data: ChannelCreate, formToken?: string) =>
    api.post<Channel>('/channels', data, formToken ? { 'X-Form-Token': formToken } : undefined),
  update: (id: string, data: ChannelUpdate, formToken?: string) =>
    api.put<Channel>(`/channels/${id}`, data, formToken ? { 'X-Form-Token': formToken } : undefined),
  delete: (id: string) => api.delete<null>(`/channels/${id}`),
  fetch: (id: string) => api.post<{ task_id: string }>(`/channels/${id}/fetch`),
  fetchStatus: (id: string) =>
    api.get<{ status: string; message?: string; progress?: number }>(
      `/channels/${id}/fetch-status`,
    ),
  resources: (channelId: string, page = 1, pageSize = 20, grouped = false) =>
    api.get<FileResource[] | GroupedResource[]>(
      `/channels/${channelId}/resources?page=${page}&page_size=${pageSize}${grouped ? '&grouped=true' : ''}`,
    ),
  analyze: (id: string) =>
    api.post<{ field_mapping: FieldMapping }>(`/channels/${id}/analyze`),
  analyzeStream: (id: string): Promise<Response> =>
    fetch(`/api/v1/channels/${id}/analyze-stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    }),
  analyzeUrlStream: (url: string): Promise<Response> =>
    fetch('/api/v1/channels/analyze-url-stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    }),
  validateUrl: (url: string) =>
    api.post<{ valid: boolean; message: string; item_count: number; downloadable_count: number }>(
      '/channels/validate-url',
      { url },
    ),
  previewFeed: (url: string, fieldMapping?: FieldMapping | null) =>
    api.post<PreviewFeedData>('/channels/preview-feed', {
      url,
      field_mapping: fieldMapping ?? null,
    }),
  generateTitleRegex: (id: string) =>
    api.post<{ regex: string; explanation?: string }>(
      `/channels/${id}/generate-title-regex`,
    ),
  summarizeFilters: (channelId: string, resourceIds: string[]) =>
    api.post<FilterSuggestionResponse>(
      `/channels/${channelId}/summarize-filters`,
      { resource_ids: resourceIds },
    ),
};

export const resourcesApi = {
  get: (id: string) => api.get<FileResource>(`/resources/${id}`),
  getMetadata: (id: string) =>
    api.get<{
      status: string;
      series_id?: string | null;
      movie_id?: string | null;
      series?: { id: string; title_cn?: string | null; title_en?: string | null; poster_url?: string | null };
      movie?: { id: string; title_cn?: string | null; title_en?: string | null; poster_url?: string | null };
    }>(`/resources/${id}/metadata`),
  searchMetadata: (
    id: string,
    body: { search_title: string; content_type: 'tv' | 'movie' },
  ) =>
    api.post<{ results: MetadataSearchResult[] }>(
      `/resources/${id}/metadata/search`,
      body,
    ),
  linkMetadata: (
    id: string,
    body: { selected_result: MetadataSearchResult & { content_type: 'tv' | 'movie' } },
  ) => api.post<FileResource>(`/resources/${id}/metadata/link`, body),
};
