import { api } from './client';
import type { WorkCollection } from '../types';

export const collectionsApi = {
  list: (page = 1, pageSize = 20, search?: string) => {
    const qs = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (search) qs.set('search', search);
    return api.get<WorkCollection[]>(`/collections?${qs.toString()}`);
  },
  create: (body: { title_cn: string; title_en?: string | null; description?: string | null }) =>
    api.post<WorkCollection>('/collections', body),
  get: (id: string, includeParts = false) =>
    api.get<WorkCollection>(`/collections/${id}${includeParts ? '?include_parts=true' : ''}`),
  update: (id: string, body: { title_cn?: string; title_en?: string | null; description?: string | null }) =>
    api.patch<WorkCollection>(`/collections/${id}`, body),
  remove: (id: string) => api.delete<{ deleted: boolean }>(`/collections/${id}`),
  attachWork: (id: string, workType: 'series' | 'movie', workId: string) =>
    api.post<{ attached: boolean }>(`/collections/${id}/works`, {
      work_type: workType,
      work_id: workId,
    }),
  detachWork: (id: string, workType: 'series' | 'movie', workId: string) =>
    api.delete<{ detached: boolean }>(
      `/collections/${id}/works/${workId}?work_type=${workType}`,
    ),
};
