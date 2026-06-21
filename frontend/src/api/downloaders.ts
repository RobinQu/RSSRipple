import { api } from './client';
import type { DownloaderInstance, DownloaderCreate, DownloaderUpdate } from '../types';

export const downloadersApi = {
  list: (page = 1, pageSize = 20) =>
    api.get<DownloaderInstance[]>(`/downloaders?page=${page}&page_size=${pageSize}`),
  get: (id: string) =>
    api.get<DownloaderInstance>(`/downloaders/${id}`),
  create: (data: DownloaderCreate) =>
    api.post<DownloaderInstance>('/downloaders', data),
  update: (id: string, data: DownloaderUpdate) =>
    api.put<DownloaderInstance>(`/downloaders/${id}`, data),
  delete: (id: string) =>
    api.delete<null>(`/downloaders/${id}`),
  test: (id: string) =>
    api.post<{ success: boolean; message: string; version: string | null }>(`/downloaders/${id}/test`),
};
