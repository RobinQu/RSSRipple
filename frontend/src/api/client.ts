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

const DEFAULT_TIMEOUT_MS = 30_000;

const RETRYABLE_MIN_STATUS = 500;

export function isRetryableStatus(status: number): boolean {
  return status >= RETRYABLE_MIN_STATUS && status <= 599;
}

/** Exponential backoff between attempts: 300ms, 900ms, 2700ms, ... */
export function retryBackoffMs(attempt: number, baseMs = 300): number {
  return baseMs * 3 ** attempt;
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new DOMException('Aborted', 'AbortError'));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal?.reason ?? new DOMException('Aborted', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

export interface RetryConfig {
  /** Extra attempts after the first one. */
  retries: number;
  /** Per-attempt timeout in ms; null disables the timeout. */
  timeout: number | null;
}

export type FetchOutcome =
  | { kind: 'response'; response: Response }
  | { kind: 'timeout' };

/**
 * Runs fetch with per-attempt timeouts and bounded retries for transient
 * failures (network errors, per-attempt timeouts, 5xx responses). Each attempt
 * gets its own timeout budget. The caller's own abort signal is never
 * retried — it aborts the whole operation immediately, including backoff
 * sleeps. A network error on the final attempt is rethrown (matching the
 * pre-retry behavior of request()).
 */
export async function fetchWithRetry(
  fetchFn: typeof fetch,
  input: string,
  init: RequestInit,
  config: RetryConfig,
): Promise<FetchOutcome> {
  for (let attempt = 0; ; attempt++) {
    const timeoutSignal = config.timeout == null ? null : AbortSignal.timeout(config.timeout);
    const signal = timeoutSignal
      ? init.signal
        ? AbortSignal.any([init.signal, timeoutSignal])
        : timeoutSignal
      : init.signal ?? undefined;
    try {
      const response = await fetchFn(input, { ...init, signal });
      if (isRetryableStatus(response.status) && attempt < config.retries) {
        await sleep(retryBackoffMs(attempt), init.signal ?? undefined);
        continue;
      }
      return { kind: 'response', response };
    } catch (e) {
      if (init.signal?.aborted) throw e;
      const canRetry = attempt < config.retries;
      if (!canRetry) {
        if (timeoutSignal?.aborted) return { kind: 'timeout' };
        throw e;
      }
      await sleep(retryBackoffMs(attempt), init.signal ?? undefined);
    }
  }
}

export interface RequestOptions extends RequestInit {
  /** Per-request timeout override in ms; null disables the timeout. */
  timeout?: number | null;
  /**
   * Extra attempts on transient failure (network error / 5xx / timeout).
   * Defaults to 2 for GET (idempotent) and 0 for other methods; pass a number
   * to override or false to disable retries entirely.
   */
  retry?: number | false;
}

async function request<T>(url: string, options?: RequestOptions): Promise<APIResponse<T>> {
  const { timeout = DEFAULT_TIMEOUT_MS, retry, ...init } = options ?? {};
  const method = (init.method ?? 'GET').toUpperCase();
  const retries =
    retry === false ? 0 : typeof retry === 'number' ? Math.max(0, Math.floor(retry)) : method === 'GET' ? 2 : 0;
  const headers = {
    'Content-Type': 'application/json',
    ...(init.headers ?? {}),
  };
  const outcome = await fetchWithRetry(fetch, `${BASE_URL}${url}`, { ...init, headers }, { retries, timeout });
  if (outcome.kind === 'timeout') {
    return {
      success: false,
      data: null as unknown as T,
      error: { code: 'TIMEOUT', message: `Request timed out after ${timeout}ms` },
    };
  }
  const response = outcome.response;
  if (!response.ok) {
    if (response.status === 401 && location.pathname !== '/login') {
      // Session expired or never established — send the user to login,
      // preserving the current location so it can be restored afterwards.
      // The login page itself is excluded so its failed-OTP 401 stays local.
      const target = location.pathname + location.search;
      location.href = `/login?redirect=${encodeURIComponent(target)}`;
    }
    try {
      return normalizeResponse<T>(await response.json(), response.statusText);
    } catch {
      return { success: false, data: null as unknown as T, error: { code: 'NETWORK_ERROR', message: response.statusText } };
    }
  }
  return normalizeResponse<T>(await response.json(), response.statusText);
}

export const api = {
  get: <T>(url: string, options?: RequestOptions) => request<T>(url, options),
  post: <T>(url: string, data?: unknown, extraHeaders?: Record<string, string>, options?: RequestOptions) =>
    request<T>(url, {
      ...options,
      method: 'POST',
      body: data ? JSON.stringify(data) : undefined,
      headers: { ...options?.headers, ...extraHeaders },
    }),
  put: <T>(url: string, data?: unknown, extraHeaders?: Record<string, string>, options?: RequestOptions) =>
    request<T>(url, {
      ...options,
      method: 'PUT',
      body: data ? JSON.stringify(data) : undefined,
      headers: { ...options?.headers, ...extraHeaders },
    }),
  patch: <T>(url: string, data?: unknown, extraHeaders?: Record<string, string>, options?: RequestOptions) =>
    request<T>(url, {
      ...options,
      method: 'PATCH',
      body: data ? JSON.stringify(data) : undefined,
      headers: { ...options?.headers, ...extraHeaders },
    }),
  delete: <T>(url: string, options?: RequestOptions) => request<T>(url, { ...options, method: 'DELETE' }),
};
