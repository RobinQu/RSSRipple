// Run with Node 24: node tests/clipboard.mjs. No browser/server or network —
// window/navigator/document are stubbed per case and removed afterwards.
import assert from 'node:assert/strict';

import { copyToClipboard } from '../src/utils/clipboard.ts';

const stubbed = [];
function setGlobal(name, value) {
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  stubbed.push(name);
}
function clearGlobals() {
  while (stubbed.length) delete globalThis[stubbed.pop()];
}

// 1. Secure context: clipboard API succeeds and no legacy fallback is needed.
{
  setGlobal('window', { isSecureContext: true });
  let written = null;
  setGlobal('navigator', {
    clipboard: { writeText: async (text) => { written = text; } },
  });
  assert.equal(await copyToClipboard('hello'), true);
  assert.equal(written, 'hello');
  clearGlobals();
}

// 2. Clipboard API rejects and no legacy document exists: reports failure
//    instead of claiming success.
{
  setGlobal('window', { isSecureContext: true });
  setGlobal('navigator', {
    clipboard: { writeText: async () => { throw new Error('denied'); } },
  });
  assert.equal(await copyToClipboard('x'), false);
  clearGlobals();
}

function fakeDocument({ execResult, fireCopyEvent = true }) {
  const listeners = {};
  let copiedPayload = null;
  const textarea = {
    value: '',
    readOnly: false,
    style: {},
    removed: false,
    addEventListener: (type, fn) => { listeners[type] = fn; },
    removeEventListener: (type) => { delete listeners[type]; },
    focus() {},
    select() {},
    remove() { this.removed = true; },
  };
  const document = {
    activeElement: null,
    createElement: (tag) => {
      assert.equal(tag, 'textarea');
      return textarea;
    },
    body: { appendChild: (el) => { assert.equal(el, textarea); } },
    execCommand: (command) => {
      assert.equal(command, 'copy');
      if (fireCopyEvent && listeners.copy) {
        listeners.copy({
          clipboardData: { setData: (mime, text) => { copiedPayload = text; } },
          preventDefault() {},
        });
      }
      return execResult;
    },
  };
  return { document, textarea, getCopiedPayload: () => copiedPayload };
}

// 3. Insecure context (plain HTTP): falls back to execCommand and succeeds.
{
  setGlobal('window', { isSecureContext: false });
  setGlobal('navigator', {});
  setGlobal('HTMLElement', class {});
  const { document, textarea, getCopiedPayload } = fakeDocument({ execResult: true });
  setGlobal('document', document);
  assert.equal(await copyToClipboard('raw title'), true);
  assert.equal(getCopiedPayload(), 'raw title');
  assert.equal(textarea.removed, true);
  clearGlobals();
}

// 4. Legacy fallback where execCommand fails: reports failure.
{
  setGlobal('window', { isSecureContext: false });
  setGlobal('navigator', {});
  setGlobal('HTMLElement', class {});
  const { document } = fakeDocument({ execResult: false });
  setGlobal('document', document);
  assert.equal(await copyToClipboard('raw title'), false);
  clearGlobals();
}

// 5. execCommand returns true but no copy event delivered the payload:
//    treated as failure (guards against browsers lying about success).
{
  setGlobal('window', { isSecureContext: false });
  setGlobal('navigator', {});
  setGlobal('HTMLElement', class {});
  const { document } = fakeDocument({ execResult: true, fireCopyEvent: false });
  setGlobal('document', document);
  assert.equal(await copyToClipboard('raw title'), false);
  clearGlobals();
}

console.log('clipboard: ok');
