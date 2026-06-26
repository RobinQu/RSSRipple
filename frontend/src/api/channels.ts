import { api } from './client';
import type { Channel, ChannelCreate, ChannelUpdate, FileResource, MetadataResponse, PreviewFeedData, FetchJobState } from '../types';

export const channelsApi = {
  list: (page = 1, pageSize = 20) =>
    api.get<Channel[]>(`/channels?page=${page}&page_size=${pageSize}`),
  get: (id: string) =>
    api.get<Channel>(`/channels/${id}`),
  getFormToken: () =>
    api.get<{ token: string }>('/channels/form-token'),
  create: (data: ChannelCreate, formToken?: string) =>
    api.post<Channel>('/channels', data, formToken ? { 'X-Form-Token': formToken } : undefined),
  update: (id: string, data: ChannelUpdate, formToken?: string) =>
    api.put<Channel>(`/channels/${id}`, data, formToken ? { 'X-Form-Token': formToken } : undefined),
  delete: (id: string) =>
    api.delete<null>(`/channels/${id}`),
  fetch: (id: string) =>
    api.post<FetchJobState>(`/channels/${id}/fetch`),
  fetchStatus: (id: string) =>
    api.get<FetchJobState | null>(`/channels/${id}/fetch-status`),
  resources: (channelId: string, page = 1, pageSize = 20) =>
    api.get<FileResource[]>(`/channels/${channelId}/resources?page=${page}&page_size=${pageSize}`),
  analyze: (id: string) =>
    api.post<{ field_mapping: Record<string, unknown>; confidence: string }>(`/channels/${id}/analyze`),
  /** Open a streaming SSE connection for live LLM analysis output. Returns the raw Response for ReadableStream consumption. */
  analyzeStream: (id: string): Promise<Response> =>
    fetch(`/api/v1/channels/${id}/analyze-stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    }),
  /** Analyze a feed URL directly (no channel needed — used on Create Channel). */
  analyzeUrlStream: (url: string): Promise<Response> =>
    fetch('/api/v1/channels/analyze-url-stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    }),
  validateUrl: (url: string) =>
    api.post<{ valid: boolean; message: string; item_count: number; downloadable_count: number }>('/channels/validate-url', { url }),
  previewFeed: (url: string, fieldMapping?: Record<string, unknown> | null) =>
    api.post<PreviewFeedData>('/channels/preview-feed', { url, field_mapping: fieldMapping ?? null }),
  generateTitleRegex: (id: string) =>
    api.post<{ regex: string }>(`/channels/${id}/generate-title-regex`),
};

export const resourcesApi = {
  getMetadata: (resourceId: string, source?: string) =>
    api.get<MetadataResponse | null>(`/resources/${resourceId}/metadata${source ? `?source=${source}` : ''}`),
};
