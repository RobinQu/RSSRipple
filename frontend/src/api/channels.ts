import { api } from './client';
import type { Channel, ChannelCreate, ChannelUpdate, FileResource } from '../types';

export const channelsApi = {
  list: (page = 1, pageSize = 20) =>
    api.get<Channel[]>(`/channels?page=${page}&page_size=${pageSize}`),
  get: (id: string) =>
    api.get<Channel>(`/channels/${id}`),
  create: (data: ChannelCreate) =>
    api.post<Channel>('/channels', data),
  update: (id: string, data: ChannelUpdate) =>
    api.put<Channel>(`/channels/${id}`, data),
  delete: (id: string) =>
    api.delete<null>(`/channels/${id}`),
  fetch: (id: string) =>
    api.post<{ message: string }>(`/channels/${id}/fetch`),
  resources: (channelId: string, page = 1, pageSize = 20) =>
    api.get<FileResource[]>(`/channels/${channelId}/resources?page=${page}&page_size=${pageSize}`),
  analyze: (id: string) =>
    api.post<{ field_mapping: Record<string, unknown>; confidence: string }>(`/channels/${id}/analyze`),
  applyMapping: (id: string, data: { field_mapping: Record<string, unknown>; parser_type: string }) =>
    api.post<Channel>(`/channels/${id}/apply-mapping`, data),
  validateUrl: (url: string) =>
    api.post<{ valid: boolean; message: string; item_count: number; downloadable_count: number }>('/channels/validate-url', { url }),
};
