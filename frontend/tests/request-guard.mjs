// Run with Node 24: node tests/request-guard.mjs. No browser/server or network.
import assert from 'node:assert/strict';

import { createRequestGuard } from '../src/utils/requestGuard.ts';

// Latest token wins; every earlier token is rejected.
{
  const guard = createRequestGuard();
  const first = guard.next();
  const second = guard.next();
  assert.equal(guard.isCurrent(first), false);
  assert.equal(guard.isCurrent(second), true);
}

// A single in-flight request stays current until the next one starts.
{
  const guard = createRequestGuard();
  const only = guard.next();
  assert.equal(guard.isCurrent(only), true);
  assert.equal(guard.isCurrent(only), true);
}

// cancel() (component unmounted) rejects even the latest token, permanently.
{
  const guard = createRequestGuard();
  const token = guard.next();
  guard.cancel();
  assert.equal(guard.isCurrent(token), false);
  assert.equal(guard.isCurrent(guard.next()), false);
}

// Guards are independent — one loader's refresh must not invalidate another's.
{
  const a = createRequestGuard();
  const b = createRequestGuard();
  const ta = a.next();
  const tb = b.next();
  b.next();
  assert.equal(a.isCurrent(ta), true);
  assert.equal(b.isCurrent(tb), false);
  b.cancel();
  assert.equal(a.isCurrent(ta), true);
}

console.log('request-guard: ok');
