import { api } from './client';
import type { TVSeries } from '../types';

export const seriesApi = {
  list: (page = 1, pageSize = 20, search?: string) => {
    const qs = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (search) qs.set('search', search);
    return api.get<TVSeries[]>(`/series?${qs.toString()}`);
  },
  get: (id: string) => api.get<TVSeries>(`/series/${id}`),
  create: (data: Partial<TVSeries>) => api.post<TVSeries>('/series', data),
  update: (id: string, data: Partial<TVSeries>) =>
    api.put<TVSeries>(`/series/${id}`, data),
  delete: (id: string) => api.delete<null>(`/series/${id}`),
};
