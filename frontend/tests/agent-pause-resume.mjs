// Run with Node 24: node tests/agent-pause-resume.mjs. This pins the
// pause/resume contract statically without requiring a browser or server.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const agentsApiSrc = readFileSync(new URL('../src/api/agents.ts', import.meta.url), 'utf8');
assert.match(agentsApiSrc, /pause:\s*\(id: string\)\s*=>\s*api\.post<Agent>\(`\/agents\/\$\{id\}\/pause`/);
assert.match(agentsApiSrc, /resume:\s*\(id: string\)\s*=>\s*api\.post<Agent>\(`\/agents\/\$\{id\}\/resume`/);

const typesSrc = readFileSync(new URL('../src/types/index.ts', import.meta.url), 'utf8');
for (const interfaceName of ['AgentCreate', 'AgentUpdate']) {
  const block = typesSrc.match(new RegExp(`export interface ${interfaceName} \\{[\\s\\S]*?\\n\\}`))[0];
  assert.match(block, /status\?: 'active' \| 'paused';/);
  assert.doesNotMatch(block, /status\?: AgentStatus/);
}

const detailSrc = readFileSync(new URL('../src/pages/AgentDetail.tsx', import.meta.url), 'utf8');
assert.match(detailSrc, /agentsApi\.(pause|resume)\(/);
assert.match(detailSrc, /agents\.pauseConfirm/);
const listSrc = readFileSync(new URL('../src/pages/Agents.tsx', import.meta.url), 'utf8');
assert.doesNotMatch(listSrc, /agentsApi\.(pause|resume)\(/);
assert.doesNotMatch(listSrc, /handleTogglePause/);

for (const tab of ['RunTab.tsx', 'DecisionsTab.tsx']) {
  const src = readFileSync(new URL(`../src/pages/agent-detail/${tab}`, import.meta.url), 'utf8');
  assert.match(src, /agentStatus: AgentStatus/);
  assert.match(src, /disabled=\{agentStatus !== 'active'\}/);
  assert.match(src, /agents\.pausedActionsHint/);
}

const dashboardSrc = readFileSync(new URL('../src/pages/Dashboard.tsx', import.meta.url), 'utf8');
assert.match(dashboardSrc, /disabled=\{d\.agent_status !== 'active'\}/);
assert.match(dashboardSrc, /agents\.pausedActionsHint/);
assert.match(typesSrc, /interface DashboardPendingItem \{[\s\S]*?agent_status: AgentStatus;/);

const KEYS = [
  'pause', 'resume',
  'pauseConfirm', 'pauseConfirmContent',
  'resumeConfirm', 'resumeConfirmContent',
  'paused', 'resumed', 'pauseFailed', 'resumeFailed',
  'pausedActionsHint', 'pausedEditHint',
];
for (const locale of ['zh-CN', 'en-US']) {
  const data = JSON.parse(
    readFileSync(new URL(`../src/i18n/locales/${locale}.json`, import.meta.url), 'utf8'),
  );
  for (const key of KEYS) {
    const value = data.agents?.[key];
    assert.ok(typeof value === 'string' && value.length > 0, `${locale}: agents.${key} missing`);
  }
}

console.log('agent-pause-resume: ok');
