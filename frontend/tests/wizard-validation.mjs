// Run with Node 24: node tests/wizard-validation.mjs. No browser/server or network.
import assert from 'node:assert/strict';

import {
  stepFromSaveError,
  validateStep,
  workKeyOf,
} from '../src/components/wizardValidation.ts';

// --- workKeyOf ---
assert.equal(workKeyOf('series', 'abc'), 'series:abc');

// --- validateStep ---
const S1 = { workType: 'series', workId: 's1' };
const S2 = { workType: 'series', workId: 's2' };

// Non-batch and audio-linked resources have no step-1 file mapping.
assert.equal(validateStep(1, {
  isBatch: false, audioLinked: false, works: [S1],
  placements: { 'a.mkv': { workType: 'series', workId: 's1', season: null } },
}), null);
assert.equal(validateStep(1, {
  isBatch: true, audioLinked: true, works: [S1, S2], placements: {},
}), null);

// Other steps are never gated.
for (const step of [0, 2, 3]) {
  assert.equal(validateStep(step, {
    isBatch: true, audioLinked: false, works: [S1],
    placements: { 'a.mkv': { workType: 'series', workId: 's1', season: null } },
  }), null);
}

// Batch TV placement without a season blocks leaving step 1.
assert.deepEqual(validateStep(1, {
  isBatch: true, audioLinked: false, works: [S1],
  placements: { 'a.mkv': { workType: 'series', workId: 's1', season: null } },
}), { code: 'tv_season_required' });

// Movie placements need no season; single-work packs skip the completeness
// check (the server only enforces it for >1 work).
assert.equal(validateStep(1, {
  isBatch: true, audioLinked: false, works: [{ workType: 'movie', workId: 'm1' }],
  placements: { 'a.mkv': { workType: 'movie', workId: 'm1', season: null } },
}), null);
assert.equal(validateStep(1, {
  isBatch: true, audioLinked: false, works: [S1], placements: {},
}), null);

// Multi-work pack: every associated work needs at least one assignment.
assert.deepEqual(validateStep(1, {
  isBatch: true, audioLinked: false, works: [S1, S2],
  placements: { 'a.mkv': { workType: 'series', workId: 's1', season: 1 } },
}), { code: 'works_unassigned', workKeys: ['series:s2'] });
assert.equal(validateStep(1, {
  isBatch: true, audioLinked: false, works: [S1, S2],
  placements: {
    'a.mkv': { workType: 'series', workId: 's1', season: 1 },
    'b.mkv': { workType: 'series', workId: 's2', season: 2 },
  },
}), null);

// The season check wins over the completeness check (save-path order).
assert.deepEqual(validateStep(1, {
  isBatch: true, audioLinked: false, works: [S1, S2],
  placements: { 'a.mkv': { workType: 'series', workId: 's1', season: null } },
}), { code: 'tv_season_required' });

// --- stepFromSaveError ---
// Machine-readable meta.step wins over any message text.
assert.equal(stepFromSaveError('collection', '随便什么'), 0);
assert.equal(stepFromSaveError('assignments', '合集不一致'), 1);
// Fallback for responses without meta.step (older server / other errors).
assert.equal(stepFromSaveError(undefined, '非合集资源至多关联一个作品'), 0);
assert.equal(stepFromSaveError(undefined, '剧集作品必须属于所选合集（collection_id 不一致）：X'), 0);
assert.equal(stepFromSaveError(undefined, '多作品合集的每个关联作品都必须有文件指派，缺少：X'), 0);
assert.equal(stepFromSaveError(undefined, 'TV 文件必须指定季：A.mkv'), 1);
assert.equal(stepFromSaveError(undefined, '第 1 季集号区间重叠：A 与 B'), 1);
assert.equal(stepFromSaveError(undefined, '作品不存在：series missing'), 3);
assert.equal(stepFromSaveError(undefined, 'boom'), 3);

console.log('wizard-validation: ok');
