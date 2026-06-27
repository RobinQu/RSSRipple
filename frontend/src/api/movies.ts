import { api } from './client';
import type { Movie } from '../types';

export const moviesApi = {
  list: (page = 1, pageSize = 20, search?: string) => {
    const qs = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (search) qs.set('search', search);
    return api.get<Movie[]>(`/movies?${qs.toString()}`);
  },
  get: (id: string) => api.get<Movie>(`/movies/${id}`),
  create: (data: Partial<Movie>) => api.post<Movie>('/movies', data),
  update: (id: string, data: Partial<Movie>) =>
    api.put<Movie>(`/movies/${id}`, data),
  delete: (id: string) => api.delete<null>(`/movies/${id}`),
};
