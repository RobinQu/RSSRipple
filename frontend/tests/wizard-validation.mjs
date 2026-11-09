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
  missingReleaseMetadataFields,
  missingWorkMetadataFields,
  workMetadataFieldsUpdate,
  workMetadataLabelKey,
} from '../src/components/wizard/types.ts';
import {
  attachSubtitleToEpisode,
  changePlacementRole,
  defaultRoleWork,
  eligibleSubtitleParents,
  placementsFromAssignments,
  reconcileSubtitleAttachments,
  seasonForWork,
  shouldApplySuggestedEpisode,
  sharedEpisodeEntries,
  updateSharedEpisodes,
} from '../src/components/wizard/placementState.ts';
import en from '../src/i18n/locales/en-US.json' with { type: 'json' };
import zh from '../src/i18n/locales/zh-CN.json' with { type: 'json' };

// --- workKeyOf ---
assert.equal(workKeyOf('series', 'abc'), 'series:abc');

// --- validateStep / placement state ---
const S1 = { workType: 'series', workId: 's1' };
const S2 = { workType: 'series', workId: 's2' };
const W1 = { work_type: 'series', work_id: 's1' };
const W2 = { work_type: 'series', work_id: 's2' };
const makePlacement = (overrides = {}) => ({
  workType: 'series', workId: 's1', fileRole: 'episode', attachedToPath: null,
  season: 1, epStart: 1, epEnd: 1, ...overrides,
});

// Non-batch same-episode variants share resource-level season/episode edits.
const singleState = {
  'a.mkv': makePlacement(),
  'a.ass': makePlacement({ fileRole: 'subtitle', season: null, epStart: null, epEnd: null }),
  'notes.nfo': makePlacement({ fileRole: 'extra', season: 0, epStart: null, epEnd: null }),
};
assert.deepEqual(sharedEpisodeEntries(singleState).map(([path]) => path), ['a.mkv']);
const variantState = { ...singleState, 'b.mkv': makePlacement() };
assert.deepEqual(sharedEpisodeEntries(variantState).map(([path]) => path), ['a.mkv', 'b.mkv']);
const seasonSynced = updateSharedEpisodes(variantState, 'season', 2);
assert.equal(seasonSynced['a.mkv'].season, 2);
assert.equal(seasonSynced['b.mkv'].season, 2);
assert.equal(seasonSynced['a.ass'].season, null);
const episodeSynced = updateSharedEpisodes(seasonSynced, 'episode', 7);
for (const path of ['a.mkv', 'b.mkv']) {
  assert.equal(episodeSynced[path].epStart, 7);
  assert.equal(episodeSynced[path].epEnd, 7);
}
const multipleEpisodes = { ...singleState, 'b.mkv': makePlacement({ epStart: 2, epEnd: 2 }) };
assert.deepEqual(sharedEpisodeEntries(multipleEpisodes), []);
assert.equal(updateSharedEpisodes(multipleEpisodes, 'season', 3), multipleEpisodes);

// Loading preserves S0 on ordinary-season works and never rewrites the snapshot value.
const loadedPlacements = placementsFromAssignments([{
  file_path: 'sp.mkv', file_role: 'episode', series_id: 's1', movie_id: null,
  season: 0, episode_start: 1, episode_end: 1, attached_to_path: null,
}]);
assert.equal(loadedPlacements['sp.mkv'].season, 0);
assert.equal(loadedPlacements['sp.mkv'].epStart, 1);

// Existing extras are true no-ops, preserving both season 0 and null.
for (const season of [0, null]) {
  const state = { 'extra.mkv': makePlacement({ fileRole: 'extra', season, epStart: null, epEnd: null }) };
  assert.equal(changePlacementRole(state, 'extra.mkv', 'extra'), state);
  assert.equal(state['extra.mkv'].season, season);
}
assert.equal(seasonForWork(3, 0, 1), 0);
assert.equal(seasonForWork(3, null, 1), 3);
assert.equal(seasonForWork(0, 2, 1), 0);
assert.equal(seasonForWork(null, null, 1), 1);

// Sole-work inheritance is safe even for an existing unbound subtitle. Multi-work
// packs require an explicit selection rather than the default expanded work.
assert.equal(defaultRoleWork([W1], null, false), W1);
assert.equal(defaultRoleWork([W1, W2], 'series:s1', false), null);
assert.equal(defaultRoleWork([W1, W2], 'series:s2', true), W2);
const unboundSubtitle = {
  'a.mkv': makePlacement(),
  'a.ass': makePlacement({ workType: null, workId: null, fileRole: 'subtitle', season: null, epStart: null, epEnd: null }),
};
const inheritedSubtitle = changePlacementRole(unboundSubtitle, 'a.ass', 'subtitle', { work: W1 });
assert.equal(inheritedSubtitle['a.ass'].workId, 's1');
assert.equal(inheritedSubtitle['a.ass'].attachedToPath, 'a.mkv');
const noAmbiguousInheritance = changePlacementRole(unboundSubtitle, 'a.ass', 'subtitle');
assert.equal(noAmbiguousInheritance, unboundSubtitle);
const validWorklessSubtitle = {
  ...unboundSubtitle,
  'a.ass': { ...unboundSubtitle['a.ass'], attachedToPath: 'a.mkv' },
};
assert.equal(reconcileSubtitleAttachments(validWorklessSubtitle), validWorklessSubtitle);
const inheritedFromParent = attachSubtitleToEpisode(validWorklessSubtitle, 'a.ass', 'a.mkv');
assert.equal(inheritedFromParent['a.ass'].workType, 'series');
assert.equal(inheritedFromParent['a.ass'].workId, 's1');

// Existing-work subtitles use same-work parents; workless subtitles may choose
// any episode explicitly. Changing or removing the episode clears existing-work links.
const mixedState = {
  's1.mkv': makePlacement(),
  's2.mkv': makePlacement({ workId: 's2', season: 2 }),
  's1.ass': makePlacement({ fileRole: 'subtitle', attachedToPath: 's1.mkv', season: null, epStart: null, epEnd: null }),
};
assert.deepEqual(eligibleSubtitleParents(mixedState, 's1.ass'), ['s1.mkv']);
const movedEpisode = {
  ...mixedState,
  's1.mkv': makePlacement({ workId: 's2', season: 2 }),
};
assert.equal(reconcileSubtitleAttachments(movedEpisode)['s1.ass'].attachedToPath, null);
const withoutEpisode = Object.fromEntries(
  Object.entries(mixedState).filter(([path]) => path !== 's1.mkv'),
);
assert.equal(reconcileSubtitleAttachments(withoutEpisode)['s1.ass'].attachedToPath, null);
const worklessMixed = {
  ...mixedState,
  'orphan.ass': makePlacement({ workType: null, workId: null, fileRole: 'subtitle', season: null, epStart: null, epEnd: null }),
};
assert.deepEqual(eligibleSubtitleParents(worklessMixed, 'orphan.ass'), ['s1.mkv', 's2.mkv']);
const explicitParentInheritance = attachSubtitleToEpisode(worklessMixed, 'orphan.ass', 's2.mkv');
assert.equal(explicitParentInheritance['orphan.ass'].workId, 's2');
const knownSeasonRole = changePlacementRole({
  'sp.mkv': makePlacement({ fileRole: 'extra', season: 0, epStart: null, epEnd: null }),
}, 'sp.mkv', 'episode', { workSeason: 3, parsedSeason: 0 });
assert.equal(knownSeasonRole['sp.mkv'].season, 0);

// Analysis only auto-places episodes and never overwrites an existing non-episode role.
assert.equal(shouldApplySuggestedEpisode(undefined, 'episode'), true);
assert.equal(shouldApplySuggestedEpisode(undefined, undefined), true);
assert.equal(shouldApplySuggestedEpisode(undefined, 'subtitle'), false);
for (const fileRole of ['subtitle', 'extra', 'excluded']) {
  assert.equal(shouldApplySuggestedEpisode(makePlacement({ fileRole }), 'episode'), false);
}
assert.equal(shouldApplySuggestedEpisode(makePlacement(), 'episode'), true);

// Non-batch file assignments now use the same episode-season rule; audio is exempt.
assert.equal(validateStep(1, {
  isBatch: false, audioLinked: false, works: [S1],
  placements: { 'a.mkv': { workType: 'series', workId: 's1', season: 1 } },
}), null);
assert.deepEqual(validateStep(1, {
  isBatch: false, audioLinked: false, works: [S1],
  placements: { 'a.mkv': { workType: 'series', workId: 's1', season: null } },
}), { code: 'tv_season_required' });
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

// Subtitle, extra and excluded rows need no season and do not satisfy episode coverage.
assert.deepEqual(validateStep(1, {
  isBatch: true, audioLinked: false, works: [S1, S2],
  placements: {
    'a.ass': { workType: 'series', workId: 's1', fileRole: 'subtitle', season: null },
    'b.nfo': { workType: 'series', workId: 's2', fileRole: 'extra', season: null },
    'c.txt': { workType: null, workId: null, fileRole: 'excluded', season: null },
  },
}), { code: 'works_unassigned', workKeys: ['series:s1', 'series:s2'] });
assert.equal(validateStep(1, {
  isBatch: true, audioLinked: false, works: [S1, S2],
  placements: {
    'a.mkv': { workType: 'series', workId: 's1', fileRole: 'episode', season: 1 },
    'b.mkv': { workType: 'series', workId: 's2', fileRole: 'episode', season: 2 },
    'a.ass': { workType: 'series', workId: 's1', fileRole: 'subtitle', season: null },
    'b.nfo': { workType: 'series', workId: 's2', fileRole: 'extra', season: null },
  },
}), null);

// Subtitle attachments must target a same-work episode; known non-zero work
// seasons are identity constraints rather than editable hints.
assert.deepEqual(validateStep(1, {
  isBatch: true, audioLinked: false, works: [{ ...S1, season: 1 }, { ...S2, season: 2 }],
  placements: {
    'a.mkv': { workType: 'series', workId: 's1', fileRole: 'episode', season: 1 },
    'b.mkv': { workType: 'series', workId: 's2', fileRole: 'episode', season: 2 },
    'a.ass': { workType: 'series', workId: 's1', fileRole: 'subtitle', attachedToPath: 'b.mkv', season: null },
  },
}), { code: 'subtitle_attachment_invalid', paths: ['a.ass'] });
assert.equal(validateStep(1, {
  isBatch: true, audioLinked: false, works: [{ ...S1, season: 1 }, { ...S2, season: 2 }],
  placements: {
    'a.mkv': { workType: 'series', workId: 's1', fileRole: 'episode', season: 1 },
    'b.mkv': { workType: 'series', workId: 's2', fileRole: 'episode', season: 2 },
    'a.ass': { workType: 'series', workId: 's1', fileRole: 'subtitle', attachedToPath: 'a.mkv', season: null },
  },
}), null);
assert.equal(validateStep(1, {
  isBatch: false, audioLinked: false, works: [{ ...S1, season: 1 }],
  placements: {
    'a.mkv': { workType: 'series', workId: 's1', fileRole: 'episode', season: 1 },
    'a.ass': { workType: null, workId: null, fileRole: 'subtitle', attachedToPath: 'a.mkv', season: null },
  },
}), null);
assert.deepEqual(validateStep(1, {
  isBatch: false, audioLinked: false, works: [{ ...S1, season: 2 }],
  placements: {
    'a.mkv': { workType: 'series', workId: 's1', fileRole: 'episode', season: 1 },
  },
}), { code: 'tv_season_mismatch' });
assert.equal(validateStep(1, {
  isBatch: false, audioLinked: false, works: [{ ...S1, season: 2 }],
  placements: {
    'a.mkv': { workType: 'series', workId: 's1', fileRole: 'episode', season: 0 },
  },
}), null);
assert.equal(validateStep(1, {
  isBatch: false, audioLinked: false, works: [{ ...S1, season: 0 }],
  placements: {
    'a.mkv': { workType: 'series', workId: 's1', fileRole: 'episode', season: 0 },
  },
}), null);
assert.deepEqual(validateStep(1, {
  isBatch: false, audioLinked: false, works: [{ ...S1, season: 0 }],
  placements: {
    'a.mkv': { workType: 'series', workId: 's1', fileRole: 'episode', season: 1 },
  },
}), { code: 'tv_season_mismatch' });

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

// Every displayed release-field gap is validated; legacy subtitle_group maps
// to the subtitle_groups control. Filled work fields do not mask release gaps.
assert.deepEqual(missingReleaseMetadataFields([
  'subtitle_group', 'subtitle_groups', 'resolution', 'title_cn', 'year', 'unknown',
]), ['title_cn', 'resolution', 'subtitle_groups']);
assert.deepEqual(validateStep(2, {
  ...step2Base,
  missingReleaseFields: ['title_cn', 'subtitle_groups', 'subtitle_langs'],
  directMetadata: { title_cn: '标题' },
  media: { subtitle_groups: '  ' },
  subtitleLangs: [],
  workMetadata: blankWorkDraft,
}), {
  code: 'release_fields_required',
  fields: ['subtitle_groups', 'subtitle_langs'],
});
assert.equal(validateStep(2, {
  ...step2Base,
  missingReleaseFields: ['title_cn', 'subtitle_groups', 'subtitle_langs'],
  directMetadata: { title_cn: '标题' },
  media: { subtitle_groups: 'Group' },
  subtitleLangs: ['zh-CN'],
  workMetadata: blankWorkDraft,
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
for (const key of [
  'releaseFieldsRequired', 'fileRole_episode', 'fileRole_subtitle',
  'fileRole_extra', 'fileRole_excluded', 'attachedEpisode',
  'tvSeasonMismatch', 'subtitleAttachmentInvalid',
]) {
  assert.equal(typeof zh.resource[key], 'string');
  assert.equal(typeof en.resource[key], 'string');
}
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
