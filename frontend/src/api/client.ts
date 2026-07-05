import type { APIResponse } from '../types';

const BASE_URL = '/api/v1';

function stringifyMessage(value: unknown, fallback: string): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) {
    const messages = value.map((item) => stringifyMessage(item, '')).filter(Boolean);
    return messages.length > 0 ? messages.join('; ') : fallback;
  }
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    if (record.msg) return stringifyMessage(record.msg, fallback);
    if (record.message) return stringifyMessage(record.message, fallback);
    if (record.detail) return stringifyMessage(record.detail, fallback);
    try {
      return JSON.stringify(value);
    } catch {
      return fallback;
    }
  }
  return fallback;
}

function normalizeResponse<T>(payload: unknown, fallbackMessage: string): APIResponse<T> {
  if (payload && typeof payload === 'object' && 'success' in payload) {
    const response = payload as APIResponse<T>;
    if (response.error) {
      return {
        ...response,
        error: {
          ...response.error,
          message: stringifyMessage(response.error.message, fallbackMessage),
        },
      };
    }
    return response;
  }

  const record = payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {};
  return {
    success: false,
    data: null as T,
    error: {
      code: typeof record.code === 'string' ? record.code : 'HTTP_ERROR',
      message: stringifyMessage(record.detail ?? record.message ?? payload, fallbackMessage),
    },
  };
}

async function request<T>(url: string, options?: RequestInit): Promise<APIResponse<T>> {
  const headers = {
    'Content-Type': 'application/json',
    ...(options?.headers ?? {}),
  };
  const response = await fetch(`${BASE_URL}${url}`, {
    ...options,
    headers,
  });
  if (!response.ok) {
    try {
      return normalizeResponse<T>(await response.json(), response.statusText);
    } catch {
      return { success: false, data: null as unknown as T, error: { code: 'NETWORK_ERROR', message: response.statusText } };
    }
  }
  return normalizeResponse<T>(await response.json(), response.statusText);
}

export const api = {
  get: <T>(url: string) => request<T>(url),
  post: <T>(url: string, data?: unknown, extraHeaders?: Record<string, string>) =>
    request<T>(url, {
      method: 'POST',
      body: data ? JSON.stringify(data) : undefined,
      headers: extraHeaders,
    }),
  put: <T>(url: string, data?: unknown, extraHeaders?: Record<string, string>) =>
    request<T>(url, {
      method: 'PUT',
      body: data ? JSON.stringify(data) : undefined,
      headers: extraHeaders,
    }),
  patch: <T>(url: string, data?: unknown, extraHeaders?: Record<string, string>) =>
    request<T>(url, {
      method: 'PATCH',
      body: data ? JSON.stringify(data) : undefined,
      headers: extraHeaders,
    }),
  delete: <T>(url: string) => request<T>(url, { method: 'DELETE' }),
};
