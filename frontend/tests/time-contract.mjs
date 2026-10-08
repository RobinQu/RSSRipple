// Run with Node 24: node tests/time-contract.mjs. No browser/server or network.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import { formatDate } from '../src/utils/format.ts';

const cases = {
  UTC: ['2026-11-01 05:30 Z', '2026-11-01 06:30 Z'],
  'Asia/Shanghai': ['2026-11-01 13:30 +08:00', '2026-11-01 14:30 +08:00'],
  'America/New_York': ['2026-11-01 01:30 -04:00', '2026-11-01 01:30 -05:00'],
};

if (process.argv.includes('--case')) {
  const expected = cases[process.env.TZ];
  assert.ok(expected);
  const stamps = ['2026-11-01T05:30:00Z', '2026-11-01T06:30:00Z'];
  const offsets = ['2026-11-01T01:30:00-04:00', '2026-11-01T01:30:00-05:00'];
  for (let index = 0; index < stamps.length; index += 1) {
    assert.equal(formatDate(stamps[index], 'yyyy-MM-dd HH:mm XXX'), expected[index]);
    // The existing compatibility parser must keep accepting old naive UTC.
    assert.equal(formatDate(stamps[index].slice(0, -1), 'yyyy-MM-dd HH:mm XXX'), expected[index]);
    assert.equal(formatDate(offsets[index], 'yyyy-MM-dd HH:mm XXX'), expected[index]);
    // AgentDetail/QueuePanel also consume API timestamps directly with Date.
    assert.equal(new Date(stamps[index]).toISOString(), stamps[index].replace('Z', '.000Z'));
  }
  assert.equal(new Date(stamps[1]).getTime() - new Date(stamps[0]).getTime(), 3600000);
  assert.equal(new Date(offsets[1]).getTime() - new Date(offsets[0]).getTime(), 3600000);
  assert.equal(formatDate(null), '—');
  assert.equal(formatDate('not a timestamp'), 'not a timestamp');
  console.log(JSON.stringify({ timezone: process.env.TZ, passed: true, expected }));
} else {
  for (const timezone of Object.keys(cases)) {
    const result = spawnSync(process.execPath, [fileURLToPath(import.meta.url), '--case'], {
      env: { ...process.env, TZ: timezone }, encoding: 'utf8',
    });
    process.stdout.write(result.stdout);
    process.stderr.write(result.stderr);
    assert.equal(result.status, 0, `${timezone}: ${result.error ?? result.stderr}`);
    assert.equal(JSON.parse(result.stdout).timezone, timezone);
    assert.equal(JSON.parse(result.stdout).passed, true);
  }
}
