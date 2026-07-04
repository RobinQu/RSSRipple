import { api } from './client';
import type { DownloaderInstance, DownloadTask, TorrentInfo } from '../types';

export interface DownloaderCreate {
  name: string;
  type: 'transmission' | 'mock';
  url: string;
  username?: string;
  password?: string;
  download_dir: string;
}

export interface DownloaderUpdate {
  name?: string;
  type?: 'transmission' | 'mock';
  url?: string;
  username?: string;
  password?: string;
  download_dir?: string;
}

export const downloadersApi = {
  list: (page = 1, pageSize = 50) =>
    api.get<DownloaderInstance[]>(`/downloaders?page=${page}&page_size=${pageSize}`),
  get: (id: string) => api.get<DownloaderInstance>(`/downloaders/${id}`),
  create: (data: DownloaderCreate) =>
    api.post<DownloaderInstance>('/downloaders', data),
  update: (id: string, data: DownloaderUpdate) =>
    api.put<DownloaderInstance>(`/downloaders/${id}`, data),
  delete: (id: string) => api.delete<null>(`/downloaders/${id}`),
  test: (id: string) =>
    api.post<{ success: boolean; message: string; version?: string | null; free_space?: number | null }>(
      `/downloaders/${id}/test`,
    ),
  listTorrents: (id: string) =>
    api.get<TorrentInfo[]>(`/downloaders/${id}/torrents`),
  listTasks: (id: string, page = 1, pageSize = 20) =>
    api.get<DownloadTask[]>(
      `/downloaders/${id}/tasks?page=${page}&page_size=${pageSize}`,
    ),
};
