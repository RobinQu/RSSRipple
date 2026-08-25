import { api } from './client';
import type {
  APIResponse,
  AssociationUpdatePayload,
  BatchSuggestion,
  Channel,
  ChannelDetail,
  ChannelStatus,
  FieldMapping,
  FileResource,
  FileResourceDetail,
  FilterSuggestionResponse,
  GroupedResource,
  MetadataSearchResult,
  MetadataSource,
  Movie,
  PreviewEntry,
  ResourceCorrectionBody,
  ResourceFilesResponse,
  TVSeries,
} from '../types';

export interface PreviewFeedData {
  entries: PreviewEntry[];
  parsed: Record<string, unknown>[];
}

export interface MetadataSourceOption {
  value: MetadataSource;
  label: string;
  description: string;
  enabled: boolean;
  configured: boolean;
  available: boolean;
}

export interface MetadataSourcesResponse {
  sources: MetadataSourceOption[];
  default: MetadataSource;
}

export interface ChannelCreate {
  name: string;
  type: 'rss_feed';
  url: string;
  fetch_interval?: number;
  field_mapping: FieldMapping;
  metadata_agent_enabled?: boolean;
  metadata_source?: MetadataSource | null;
  metadata_fallback_sources?: string[] | null;
  required_metadata_fields?: string[] | null;
  auto_cleanup_unresolved_enabled?: boolean;
  auto_cleanup_unresolved_days?: number;
  // Periodic work-metadata refresh (per-channel; off by default).
  metadata_refresh_enabled?: boolean;
  metadata_refresh_interval_minutes?: number | null;
  metadata_refresh_full_scope?: boolean;
  // Immutable after channel creation (server returns 422 on change attempts).
  default_is_anime?: boolean;
}

export interface ChannelUpdate {
  name?: string;
  url?: string;
  fetch_interval?: number;
  status?: ChannelStatus;
  field_mapping?: FieldMapping;
  metadata_agent_enabled?: boolean;
  metadata_source?: MetadataSource | null;
  metadata_fallback_sources?: string[] | null;
  required_metadata_fields?: string[] | null;
  auto_cleanup_unresolved_enabled?: boolean;
  auto_cleanup_unresolved_days?: number;
  // Periodic work-metadata refresh (per-channel).
  metadata_refresh_enabled?: boolean;
  metadata_refresh_interval_minutes?: number | null;
  metadata_refresh_full_scope?: boolean;
}

// GET /channels/required-field-catalog — the selectable required metadata
// fields for the channel form dialog, grouped two levels deep: ``section``
// is the work-type grouping (base/tv/pack first, then cross-cutting
// release/work), ``group`` the semantic sub-group within it. ``lock`` is the
// code-enforced requirement scope ("always", or a row shape such as
// "tv_single"); locked fields are always present in a channel's list and can
// never be removed (the list itself is add-only after creation).
// ``applies_to`` lists the row shapes the field is relevant for
// (null = every shape).
export interface RequiredFieldCatalogEntry {
  key: string;
  section: string;
  group: string;
  dsl_fields: string[];
  lock: string | null;
  locked: boolean;
  applies_to: string[] | null;
}

export interface RequiredFieldCatalogResponse {
  sections: string[];
  fields: RequiredFieldCatalogEntry[];
}

type ChannelResourcesPayload =
  | FileResource[]
  | GroupedResource[]
  | {
      groups?: GroupedResource[];
      resources?: FileResource[];
    };

function normalizeResourcesPayload(
  payload: ChannelResourcesPayload,
  grouped: boolean,
): FileResource[] | GroupedResource[] {
  if (Array.isArray(payload)) return payload;
  if (grouped) return Array.isArray(payload.groups) ? payload.groups : [];
  return Array.isArray(payload.resources) ? payload.resources : [];
}

export const channelsApi = {
  list: (page = 1, pageSize = 20) =>
    api.get<Channel[]>(`/channels?page=${page}&page_size=${pageSize}`),
  get: (id: string) => api.get<ChannelDetail>(`/channels/${id}`),
  getFormToken: () => api.get<{ token: string }>('/channels/form-token'),
  metadataSources: () => api.get<MetadataSourcesResponse>('/channels/metadata-sources'),
  requiredFieldCatalog: () =>
    api.get<RequiredFieldCatalogResponse>('/channels/required-field-catalog'),
  create: (data: ChannelCreate, formToken?: string) =>
    api.post<Channel>('/channels', data, formToken ? { 'X-Form-Token': formToken } : undefined),
  update: (id: string, data: ChannelUpdate, formToken?: string) =>
    api.put<Channel>(`/channels/${id}`, data, formToken ? { 'X-Form-Token': formToken } : undefined),
  delete: (id: string) => api.delete<null>(`/channels/${id}`),
  fetch: (id: string, force = false) =>
    api.post<{ task_id: string }>(`/channels/${id}/fetch?force=${force}`),
  fetchStatus: (id: string) =>
    api.get<{ status: string; message?: string; progress?: number }>(
      `/channels/${id}/fetch-status`,
    ),
  fieldValues: (channelId: string, field: string, q = '', limit = 10) => {
    const params = new URLSearchParams({ field, limit: String(limit) });
    if (q) params.set('q', q);
    return api.get<string[]>(`/channels/${channelId}/field-values?${params}`);
  },
  resources: async (
    channelId: string,
    page = 1,
    pageSize = 20,
    grouped = false,
    matched?: boolean,
  ) => {
    const matchedParam =
      matched === true ? '&matched=true' : matched === false ? '&matched=false' : '';
    const response = await api.get<ChannelResourcesPayload>(
      `/channels/${channelId}/resources?page=${page}&page_size=${pageSize}${grouped ? '&grouped=true' : ''}${matchedParam}`,
    );
    if (!response.success) return response as APIResponse<FileResource[] | GroupedResource[]>;
    return {
      ...response,
      data: normalizeResourcesPayload(response.data, grouped),
    } as APIResponse<FileResource[] | GroupedResource[]>;
  },
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
      resource_id?: string;
      status?: string;
      series_id?: string | null;
      movie_id?: string | null;
      series?: { id: string; title_cn?: string | null; title_en?: string | null; original_title?: string | null; poster_url?: string | null };
      movie?: { id: string; title_cn?: string | null; title_en?: string | null; original_title?: string | null; poster_url?: string | null };
      linked?: {
        type: 'series' | 'movie';
        entity: TVSeries | Movie;
      } | null;
      metadata_matched_at?: string | null;
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
  correctEpisode: (
    id: string,
    body: { episode: number | null; season?: number | null; absolute_episode?: number | null; note?: string },
  ) => api.patch<FileResource>(`/resources/${id}/episode`, body),
  getFiles: (id: string) => api.get<ResourceFilesResponse>(`/resources/${id}/files`),
  correctParseFields: (id: string, body: ResourceCorrectionBody) =>
    api.patch<FileResource>(`/resources/${id}`, body),
  // Edit wizard write path: full association state (works set, collection,
  // per-file placements) + optional generic fields in one transaction.
  updateAssociations: (
    id: string,
    body: AssociationUpdatePayload,
  ) =>
    api.put<FileResourceDetail & { warnings?: string[] }>(
      `/resources/${id}/associations`,
      body,
    ),
  // Non-persistent LLM suggestions for the file-mapping step.
  analyzeBatch: (id: string) =>
    api.post<{ suggestion: BatchSuggestion | null; listing_source: string }>(
      `/resources/${id}/analyze-batch`,
      {},
    ),
  analyzeBatchStream: (id: string, force = false): Promise<Response> =>
    fetch(`/api/v1/resources/${id}/analyze-batch-stream?force=${force}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    }),
};
