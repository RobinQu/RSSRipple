// Run with Node 24: node tests/api-retry.mjs. No browser/server or network —
// fetch is injected/stubbed everywhere.
import assert from 'node:assert/strict';

import { api, fetchWithRetry, isRetryableStatus, retryBackoffMs } from '../src/api/client.ts';

const okJson = (data) =>
  new Response(JSON.stringify({ success: true, data }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
const errJson = (status) =>
  new Response(JSON.stringify({ success: false, error: { code: 'X', message: 'm' } }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

// isRetryableStatus: only 5xx.
{
  assert.equal(isRetryableStatus(500), true);
  assert.equal(isRetryableStatus(503), true);
  assert.equal(isRetryableStatus(599), true);
  assert.equal(isRetryableStatus(200), false);
  assert.equal(isRetryableStatus(404), false);
  assert.equal(isRetryableStatus(429), false);
}

// retryBackoffMs: 300 / 900 / 2700 exponential.
{
  assert.equal(retryBackoffMs(0), 300);
  assert.equal(retryBackoffMs(1), 900);
  assert.equal(retryBackoffMs(2), 2700);
}

// fetchWithRetry: first-try success performs exactly one fetch.
{
  let calls = 0;
  const fetchFn = async () => {
    calls++;
    return okJson(1);
  };
  const outcome = await fetchWithRetry(fetchFn, '/x', {}, { retries: 2, timeout: null });
  assert.equal(outcome.kind, 'response');
  assert.equal(outcome.response.status, 200);
  assert.equal(calls, 1);
}

// fetchWithRetry: 5xx then success retries and returns the good response.
{
  let calls = 0;
  const fetchFn = async () => {
    calls++;
    return calls === 1 ? errJson(503) : okJson(2);
  };
  const started = Date.now();
  const outcome = await fetchWithRetry(fetchFn, '/x', {}, { retries: 2, timeout: null });
  assert.equal(outcome.kind, 'response');
  assert.equal(outcome.response.status, 200);
  assert.equal(calls, 2);
  // First backoff is 300ms.
  assert.ok(Date.now() - started >= 280, 'expected a backoff delay before the retry');
}

// fetchWithRetry: persistent 5xx exhausts retries and returns the last response.
{
  let calls = 0;
  const fetchFn = async () => {
    calls++;
    return errJson(500);
  };
  const outcome = await fetchWithRetry(fetchFn, '/x', {}, { retries: 2, timeout: null });
  assert.equal(outcome.kind, 'response');
  assert.equal(outcome.response.status, 500);
  assert.equal(calls, 3);
}

// fetchWithRetry: 4xx is never retried.
{
  let calls = 0;
  const fetchFn = async () => {
    calls++;
    return errJson(404);
  };
  const outcome = await fetchWithRetry(fetchFn, '/x', {}, { retries: 2, timeout: null });
  assert.equal(outcome.kind, 'response');
  assert.equal(outcome.response.status, 404);
  assert.equal(calls, 1);
}

// fetchWithRetry: network error then success retries.
{
  let calls = 0;
  const fetchFn = async () => {
    calls++;
    if (calls === 1) throw new TypeError('fetch failed');
    return okJson(3);
  };
  const outcome = await fetchWithRetry(fetchFn, '/x', {}, { retries: 2, timeout: null });
  assert.equal(outcome.kind, 'response');
  assert.equal(calls, 2);
}

// fetchWithRetry: persistent network error rethrows after exhausting retries.
{
  let calls = 0;
  const fetchFn = async () => {
    calls++;
    throw new TypeError('fetch failed');
  };
  await assert.rejects(fetchWithRetry(fetchFn, '/x', {}, { retries: 2, timeout: null }), /fetch failed/);
  assert.equal(calls, 3);
}

// fetchWithRetry: per-attempt timeout fires independently on each attempt,
// and the final timeout surfaces as { kind: 'timeout' }.
// (Node's AbortSignal.timeout timer is unref'd — the watchdog below is a
// ref'd timer that keeps the event loop alive and fails loudly if the abort
// signal never fires.)
{
  let calls = 0;
  const fetchFn = (_input, init) =>
    new Promise((_, reject) => {
      calls++;
      const watchdog = setTimeout(() => reject(new Error('abort signal never fired')), 500);
      init.signal.addEventListener('abort', () => {
        clearTimeout(watchdog);
        reject(init.signal.reason);
      }, { once: true });
    });
  const outcome = await fetchWithRetry(fetchFn, '/x', {}, { retries: 1, timeout: 40 });
  assert.equal(outcome.kind, 'timeout');
  assert.equal(calls, 2);
}

// fetchWithRetry: caller abort during backoff stops everything, no more fetches.
{
  let calls = 0;
  const fetchFn = async () => {
    calls++;
    return errJson(500);
  };
  const controller = new AbortController();
  setTimeout(() => controller.abort(), 50);
  await assert.rejects(
    fetchWithRetry(fetchFn, '/x', { signal: controller.signal }, { retries: 5, timeout: null }),
    (e) => e?.name === 'AbortError' || controller.signal.aborted,
  );
  assert.equal(calls, 1);
}

// fetchWithRetry: caller-aborted fetch is never retried.
{
  let calls = 0;
  const controller = new AbortController();
  const fetchFn = async () => {
    calls++;
    controller.abort();
    throw new DOMException('Aborted', 'AbortError');
  };
  await assert.rejects(
    fetchWithRetry(fetchFn, '/x', { signal: controller.signal }, { retries: 2, timeout: null }),
    (e) => e?.name === 'AbortError',
  );
  assert.equal(calls, 1);
}

// api.get: retries GET on 5xx by default (stub the global fetch).
{
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return calls < 3 ? errJson(502) : okJson({ id: 7 });
  };
  try {
    const res = await api.get('/things');
    assert.equal(res.success, true);
    assert.deepEqual(res.data, { id: 7 });
    assert.equal(calls, 3);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

// api.get: retry: false disables retries.
{
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return errJson(500);
  };
  try {
    const res = await api.get('/things', { retry: false });
    assert.equal(res.success, false);
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

// api.get: retry: 0 disables retries; retry: 1 allows exactly one extra attempt.
{
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return errJson(500);
  };
  try {
    await api.get('/things', { retry: 0 });
    assert.equal(calls, 1);
    await api.get('/things', { retry: 1 });
    assert.equal(calls, 3);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

// api.post: non-idempotent methods do not retry by default.
{
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return errJson(500);
  };
  try {
    const res = await api.post('/things', { a: 1 });
    assert.equal(res.success, false);
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

// api.get: timeout error shape is preserved after retries are exhausted.
{
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (_input, init) =>
    new Promise((_, reject) => {
      calls++;
      const watchdog = setTimeout(() => reject(new Error('abort signal never fired')), 500);
      init.signal.addEventListener('abort', () => {
        clearTimeout(watchdog);
        reject(init.signal.reason);
      }, { once: true });
    });
  try {
    const res = await api.get('/slow', { timeout: 40, retry: 1 });
    assert.equal(res.success, false);
    assert.equal(res.error.code, 'TIMEOUT');
    assert.equal(calls, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

console.log('api-retry: ok');
