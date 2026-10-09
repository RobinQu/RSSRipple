// Run with Node 24: node tests/channel-pause-resume.mjs. No browser/server or
// network — src/api/channels.ts cannot be imported in Node (extensionless
// relative imports), so this pins the pause/resume contract statically plus
// the i18n key parity the UI buttons depend on.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const channelsApiSrc = readFileSync(new URL('../src/api/channels.ts', import.meta.url), 'utf8');

// channelsApi exposes pause/resume hitting the dedicated endpoints.
assert.match(channelsApiSrc, /pause:\s*\(id: string\)\s*=>\s*api\.post<Channel>\(`\/channels\/\$\{id\}\/pause`/);
assert.match(channelsApiSrc, /resume:\s*\(id: string\)\s*=>\s*api\.post<Channel>\(`\/channels\/\$\{id\}\/resume`/);

// ChannelUpdate.status is narrowed to the user-settable values; 'error' is
// system-managed (fetch write-back) and must not be accepted by the type.
const updateBlock = channelsApiSrc.match(/export interface ChannelUpdate \{[\s\S]*?\n\}/)[0];
assert.match(updateBlock, /status\?: 'active' \| 'inactive';/);
assert.doesNotMatch(updateBlock, /status\?: ChannelStatus/);

// Both pages wire the pause/resume toggle with a confirmation modal.
for (const page of ['Channels.tsx', 'ChannelDetail.tsx']) {
  const src = readFileSync(new URL(`../src/pages/${page}`, import.meta.url), 'utf8');
  assert.match(src, /channelsApi\.(pause|resume)\(/, `${page} calls pause/resume`);
  assert.match(src, /channels\.pauseConfirm/, `${page} confirms before pausing`);
}

// i18n: every pause/resume key exists in both locales with non-empty text.
const KEYS = [
  'pause', 'resume',
  'pauseConfirm', 'pauseConfirmContent',
  'resumeConfirm', 'resumeConfirmContent',
  'paused', 'resumed', 'pauseFailed', 'resumeFailed',
  'fetchPausedTip',
];
for (const locale of ['zh-CN', 'en-US']) {
  const data = JSON.parse(
    readFileSync(new URL(`../src/i18n/locales/${locale}.json`, import.meta.url), 'utf8'),
  );
  for (const key of KEYS) {
    const value = data.channels?.[key];
    assert.ok(typeof value === 'string' && value.length > 0, `${locale}: channels.${key} missing`);
  }
}

console.log('channel-pause-resume: ok');
