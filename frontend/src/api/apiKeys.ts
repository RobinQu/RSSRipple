import { api } from './client';
import type { ApiKey, ApiKeyCreated } from '../types';

export const apiKeysApi = {
  list: () => api.get<ApiKey[]>('/api-keys'),
  // The full key is returned only here, once — the caller must display it.
  create: (name: string) => api.post<ApiKeyCreated>('/api-keys', { name }),
  remove: (id: string) => api.delete<{ deleted: boolean }>(`/api-keys/${id}`),
};
