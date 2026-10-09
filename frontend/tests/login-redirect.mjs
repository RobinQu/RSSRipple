// Run with Node 24: node tests/login-redirect.mjs. No browser/server or network.
import assert from 'node:assert/strict';

import { resolveLoginRedirect } from '../src/utils/redirect.ts';

// Missing / empty param falls back to home.
assert.equal(resolveLoginRedirect(''), '/');
assert.equal(resolveLoginRedirect('?redirect='), '/');
// Normal relative targets are preserved, including query strings.
assert.equal(resolveLoginRedirect('?redirect=%2Fagents'), '/agents');
assert.equal(
  resolveLoginRedirect(`?redirect=${encodeURIComponent('/agents/abc?tab=tasks')}`),
  '/agents/abc?tab=tasks',
);
// Open-redirect attempts are rejected: absolute URLs, protocol-relative,
// non-slash schemes, and bare strings.
assert.equal(resolveLoginRedirect(`?redirect=${encodeURIComponent('https://evil.example')}`), '/');
assert.equal(resolveLoginRedirect(`?redirect=${encodeURIComponent('//evil.example')}`), '/');
assert.equal(resolveLoginRedirect('?redirect=javascript:alert(1)'), '/');
assert.equal(resolveLoginRedirect('?redirect=agents'), '/');

console.log('login-redirect: ok');
