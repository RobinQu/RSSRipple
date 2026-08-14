import { api } from './client';
import type {
  MediaServer,
  MediaServerCreate,
  MediaServerListItem,
  MediaServerScanResult,
  MediaServerTestResult,
  MediaServerUpdate,
} from '../types';

export const mediaServersApi = {
  // Small set; unpaginated list carries derived library counts.
  list: () => api.get<MediaServerListItem[]>('/media-servers'),
  get: (id: string) => api.get<MediaServer>(`/media-servers/${id}`),
  create: (data: MediaServerCreate) => api.post<MediaServer>('/media-servers', data),
  update: (id: string, data: MediaServerUpdate) =>
    api.put<MediaServer>(`/media-servers/${id}`, data),
  delete: (id: string) => api.delete<{ deleted: boolean }>(`/media-servers/${id}`),
  // Connectivity + credential probe → { ok, server_version?, message? }.
  test: (id: string) => api.post<MediaServerTestResult>(`/media-servers/${id}/test`),
  // Scan sections and upsert derived Libraries → { created, updated, unbound }.
  scan: (id: string) => api.post<MediaServerScanResult>(`/media-servers/${id}/scan`),
};
