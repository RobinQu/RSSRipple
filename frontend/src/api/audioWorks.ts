import { api } from './client';
import type { AudioWork } from '../types';

export const audioWorksApi = {
  list: (
    page = 1,
    pageSize = 20,
    search?: string,
    content_type?: string,
  ) => {
    const qs = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (search) qs.set('search', search);
    if (content_type) qs.set('content_type', content_type);
    return api.get<AudioWork[]>(`/audio-works?${qs.toString()}`);
  },
  get: (id: string) => api.get<AudioWork>(`/audio-works/${id}`),
  update: (id: string, data: Partial<AudioWork>) =>
    api.put<AudioWork>(`/audio-works/${id}`, data),
  delete: (id: string) => api.delete<null>(`/audio-works/${id}`),
};
