import { api } from './client';
import type {
  MediaServer,
  MediaServerCreate,
  MediaServerListItem,
  MediaServerScanResult,
  MediaServerTestPayload,
  MediaServerTestRequest,
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
  // Overrides let the edit form probe unsaved values (blank token = stored).
  test: (id: string, overrides?: MediaServerTestRequest) =>
    api.post<MediaServerTestResult>(`/media-servers/${id}/test`, overrides),
  // Stateless probe for the create form (no saved id yet).
  testUnsaved: (payload: MediaServerTestPayload) =>
    api.post<MediaServerTestResult>('/media-servers/test', payload),
  // Scan sections and upsert derived Libraries → { created, updated, unbound }.
  scan: (id: string) => api.post<MediaServerScanResult>(`/media-servers/${id}/scan`),
};
