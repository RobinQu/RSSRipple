import type { APIResponse } from '../types';

const BASE_URL = '/api/v1';

async function request<T>(url: string, options?: RequestInit): Promise<APIResponse<T>> {
  const response = await fetch(`${BASE_URL}${url}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    try {
      return await response.json();
    } catch {
      return { success: false, data: null as unknown as T, error: { code: 'NETWORK_ERROR', message: response.statusText } };
    }
  }
  return response.json();
}

export const api = {
  get: <T>(url: string) => request<T>(url),
  post: <T>(url: string, data?: unknown) =>
    request<T>(url, { method: 'POST', body: data ? JSON.stringify(data) : undefined }),
  put: <T>(url: string, data?: unknown) =>
    request<T>(url, { method: 'PUT', body: data ? JSON.stringify(data) : undefined }),
  delete: <T>(url: string) => request<T>(url, { method: 'DELETE' }),
};
