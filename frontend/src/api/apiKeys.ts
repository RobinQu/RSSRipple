import { api } from './client';
import type { ApiKey, ApiKeyCreated } from '../types';

export const apiKeysApi = {
  list: () => api.get<ApiKey[]>('/api-keys'),
  // The full key is returned only here, once — the caller must display it.
  create: (name: string, expiresAt?: string | null) =>
    api.post<ApiKeyCreated>('/api-keys', { name, expires_at: expiresAt ?? null }),
  // Atomically replaces the key: the old one stops working immediately and the
  // new plaintext is returned only in this response. No expiresAt = inherit.
  rotate: (id: string, expiresAt?: string | null) =>
    api.post<ApiKeyCreated>(`/api-keys/${id}/rotate`, { expires_at: expiresAt ?? null }),
  remove: (id: string) => api.delete<{ deleted: boolean }>(`/api-keys/${id}`),
};
