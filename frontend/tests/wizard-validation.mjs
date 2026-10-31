// Run with Node 24: node tests/wizard-validation.mjs. No browser/server or network.
import assert from 'node:assert/strict';

import {
  admitWork,
  pickWorkMatch,
  stepFromSaveError,
  validateStep,
  workKeyOf,
} from '../src/components/wizardValidation.ts';
import {
  mediaLabelI18nKey,
  missingWorkMetadataFields,
  workMetadataFieldsUpdate,
  workMetadataLabelKey,
} from '../src/components/wizard/types.ts';
import en from '../src/i18n/locales/en-US.json' with { type: 'json' };
import zh from '../src/i18n/locales/zh-CN.json' with { type: 'json' };

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

// Association and confirmation steps have no client-side gate.
for (const step of [0, 3]) {
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

// Explicit season-0 works are extras, but unknown seasons are not exempt.
const SP = { workType: 'series', workId: 'sp', season: 0 };
assert.equal(validateStep(1, {
  isBatch: true, audioLinked: false, works: [{ ...S1, season: 1 }, SP],
  placements: { 'a.mkv': { workType: 'series', workId: 's1', season: 1 } },
}), null);
assert.deepEqual(validateStep(1, {
  isBatch: true, audioLinked: false, works: [{ ...S1, season: 1 }, S2],
  placements: { 'a.mkv': { workType: 'series', workId: 's1', season: 1 } },
}), { code: 'works_unassigned', workKeys: ['series:s2'] });

// The season check wins over the completeness check (save-path order).
assert.deepEqual(validateStep(1, {
  isBatch: true, audioLinked: false, works: [S1, S2],
  placements: { 'a.mkv': { workType: 'series', workId: 's1', season: null } },
}), { code: 'tv_season_required' });

// Displayed work-metadata gaps gate both step-2 navigation and final save.
const blankWorkDraft = { date: null, rating: null, is_anime: null, genre: [] };
const step2Base = {
  isBatch: false, audioLinked: false, works: [S1], placements: {},
};
assert.deepEqual(validateStep(2, {
  ...step2Base,
  missingWorkFields: ['year', 'rating', 'is_anime', 'genre'],
  workMetadata: blankWorkDraft,
}), {
  code: 'work_fields_required',
  fields: ['year', 'rating', 'is_anime', 'genre'],
});
assert.equal(validateStep(2, {
  ...step2Base,
  missingWorkFields: ['year', 'rating', 'is_anime', 'genre'],
  workMetadata: {
    date: '2024-04-01', rating: 0, is_anime: false, genre: ['Drama'],
  },
}), null);
assert.equal(validateStep(2, {
  ...step2Base, missingWorkFields: [], workMetadata: blankWorkDraft,
}), null);

// --- pickWorkMatch ---
const candidates = [
  { id: 's1', season: 1 },
  { id: 's2', season: 2 },
  { id: 'sp', season: 0 },
];
const candidateSeason = (candidate) => candidate.season;
assert.equal(pickWorkMatch([candidates[0]], null, candidateSeason), candidates[0]);
assert.equal(pickWorkMatch([candidates[0]], 2, candidateSeason), undefined);
assert.equal(pickWorkMatch([candidates[1]], 2, candidateSeason), candidates[1]);
assert.equal(pickWorkMatch(candidates, null, candidateSeason), undefined);
assert.equal(pickWorkMatch(candidates, 2, candidateSeason), candidates[1]);
assert.equal(pickWorkMatch(candidates, 0, candidateSeason), candidates[2]);
assert.equal(pickWorkMatch(candidates, 3, candidateSeason), undefined);
assert.equal(pickWorkMatch(
  [candidates[0], { id: 'other-s1', season: 1 }], 1, candidateSeason,
), undefined);
assert.equal(pickWorkMatch(candidates, 1, (candidate) => (
  candidate.id === 'sp' ? null : candidate.season
)), candidates[0]);

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

// --- admitWork ---
// Non-batch / audio-linked / movie picks never touch the collection.
assert.deepEqual(admitWork(
  { isBatch: false, audioLinked: false, collectionId: 'c1', works: [] },
  { workType: 'series', workId: 's1' }, { collectionId: 'c2' },
), { action: 'ok' });
assert.deepEqual(admitWork(
  { isBatch: true, audioLinked: true, collectionId: 'c1', works: [] },
  { workType: 'series', workId: 's1' }, { collectionId: 'c2' },
), { action: 'ok' });
assert.deepEqual(admitWork(
  { isBatch: true, audioLinked: false, collectionId: 'c1', works: [] },
  { workType: 'movie', workId: 'm1' }, { collectionId: 'c2' },
), { action: 'ok' });

// First series work with no collection selected adopts its collection.
assert.deepEqual(admitWork(
  { isBatch: true, audioLinked: false, collectionId: null, works: [] },
  { workType: 'series', workId: 's1' }, { collectionId: 'c1' },
), { action: 'adopt-collection', collectionId: 'c1' });
// Adoption is first-work-only: with works already present (e.g. after a
// cross-collection clear) a later pick must not re-adopt.
assert.deepEqual(admitWork(
  { isBatch: true, audioLinked: false, collectionId: null, works: [S1] },
  { workType: 'series', workId: 's2' }, { collectionId: 'c2' },
), { action: 'ok' });
// No collection anywhere: nothing to adopt or clear.
assert.deepEqual(admitWork(
  { isBatch: true, audioLinked: false, collectionId: null, works: [] },
  { workType: 'series', workId: 's1' }, { collectionId: null },
), { action: 'ok' });

// Inside the selected collection (or membership unknown): fine.
assert.deepEqual(admitWork(
  { isBatch: true, audioLinked: false, collectionId: 'c1', works: [] },
  { workType: 'series', workId: 's1' }, { collectionId: 'c1' },
), { action: 'ok' });
assert.deepEqual(admitWork(
  { isBatch: true, audioLinked: false, collectionId: 'c1', works: [] },
  { workType: 'series', workId: 's1' }, { collectionId: null },
), { action: 'ok' });
assert.deepEqual(admitWork(
  { isBatch: true, audioLinked: false, collectionId: 'c1', works: [] },
  { workType: 'series', workId: 's1' }, undefined,
), { action: 'ok' });

// Outside-collection series pick clears the collection (cross-collection
// packs are legal; the link settles to NULL server-side).
assert.deepEqual(admitWork(
  { isBatch: true, audioLinked: false, collectionId: 'c1', works: [S1] },
  { workType: 'series', workId: 's2' }, { collectionId: 'c2' },
), { action: 'clear-collection' });

// Work-field validation errors return to the media step.
assert.equal(stepFromSaveError('media', '作品字段修订目标不在作品关联列表中'), 2);

// --- missingWorkMetadataFields ---
assert.deepEqual(missingWorkMetadataFields(['year', 'rating', 'resolution'], true), [
  'year', 'rating',
]);
assert.deepEqual(missingWorkMetadataFields(['year', 'rating'], false), []);

for (const key of ['year', 'rating', 'is_anime', 'genre']) {
  const labelKey = workMetadataLabelKey(key);
  assert.equal(typeof zh.resource[labelKey], 'string');
  assert.equal(typeof en.resource[labelKey], 'string');
}
assert.equal(typeof zh.resource.workFieldsRequired, 'string');
assert.equal(typeof en.resource.workFieldsRequired, 'string');
assert.equal(mediaLabelI18nKey('title_cn'), 'filter.titleCn');
assert.equal(mediaLabelI18nKey('resolution'), 'resource.resolution');
assert.equal(typeof zh.filter.titleCn, 'string');
assert.equal(typeof en.filter.titleCn, 'string');

// --- workMetadataFieldsUpdate ---
const originalWork = {
  date: null, rating: null, is_anime: null, genre: [],
};
assert.deepEqual(workMetadataFieldsUpdate('series', originalWork, originalWork), {});
assert.deepEqual(workMetadataFieldsUpdate('series', {
  date: '2024', rating: 8.1, is_anime: null, genre: [],
}, originalWork), {
  start_date: '2024', rating: 8.1,
});
assert.deepEqual(workMetadataFieldsUpdate('movie', {
  date: '2025', rating: 0, is_anime: false, genre: ['Drama'],
}, originalWork), {
  release_date: '2025', rating: 0, is_anime: false, genre: ['Drama'],
});
assert.deepEqual(workMetadataFieldsUpdate('series', originalWork, {
  date: '2024-04-01', rating: 8.1, is_anime: true, genre: ['Drama'],
}), {
  start_date: null, rating: null, is_anime: null, genre: [],
});

console.log('wizard-validation: ok');
