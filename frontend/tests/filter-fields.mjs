// Run with Node 24: node tests/filter-fields.mjs. No browser/server or network.
// Pins the single-source filter-field catalog (constants/filterFields.ts):
// every consumer-visible derivation (types map, resource-level list,
// required-field DSL map, gating function) must match the historical
// hand-maintained values.
import assert from 'node:assert/strict';

import {
  FILTER_FIELD_META,
  FILTER_FIELD_TYPES,
  REQUIRED_FIELD_DSL_MAP,
  RESOURCE_LEVEL_FIELDS,
  allowedAgentFilterFields,
} from '../src/constants/filterFields.ts';

// The historical FilterField union (types/index.ts) — order-independent.
const EXPECTED_FIELDS = [
  'subtitle_groups', 'subtitle_group', 'resolution', 'source', 'video_codec',
  'audio_codec', 'subtitle_type', 'container', 'file_size', 'episode',
  'season', 'episode_start', 'episode_end', 'absolute_episode', 'is_batch',
  'subtitle_langs', 'episode_confidence', 'content_type', 'title_cn',
  'title_en', 'search_title', 'movie.rating', 'movie.year', 'movie.genre',
  'movie.collection', 'movie.is_anime', 'series.rating', 'series.year',
  'series.genre', 'series.collection', 'series.is_anime', 'collection',
];
assert.deepEqual(
  [...FILTER_FIELD_META.map((m) => m.name)].sort(),
  [...EXPECTED_FIELDS].sort(),
);
assert.equal(new Set(FILTER_FIELD_META.map((m) => m.name)).size, EXPECTED_FIELDS.length);

// The historical FilterBuilder FIELD_TYPES map.
const EXPECTED_TYPES = {
  subtitle_group: 'string', subtitle_groups: 'list', resolution: 'string',
  source: 'string', video_codec: 'string', audio_codec: 'string',
  subtitle_type: 'string', container: 'string', episode_confidence: 'string',
  content_type: 'string', title_cn: 'string', title_en: 'string',
  search_title: 'string', file_size: 'number', episode: 'number',
  season: 'number', episode_start: 'number', episode_end: 'number',
  absolute_episode: 'number', is_batch: 'bool', subtitle_langs: 'list',
  'movie.rating': 'number', 'movie.year': 'number', 'series.rating': 'number',
  'series.year': 'number', 'movie.genre': 'list', 'series.genre': 'list',
  'movie.collection': 'string', 'series.collection': 'string',
  collection: 'string', 'series.is_anime': 'bool', 'movie.is_anime': 'bool',
};
assert.deepEqual(FILTER_FIELD_TYPES, EXPECTED_TYPES);

// The historical RESOURCE_LEVEL_FIELDS (filterUtils.ts).
const EXPECTED_RESOURCE_LEVEL = [
  'subtitle_groups', 'subtitle_group', 'resolution', 'source', 'video_codec',
  'audio_codec', 'subtitle_type', 'subtitle_langs', 'container', 'file_size',
  'episode', 'season', 'episode_start', 'episode_end', 'absolute_episode',
  'is_batch', 'episode_confidence', 'title_cn', 'title_en', 'search_title',
  'content_type', 'collection',
];
assert.deepEqual(
  [...RESOURCE_LEVEL_FIELDS].sort(),
  [...EXPECTED_RESOURCE_LEVEL].sort(),
);

// The historical REQUIRED_FIELD_DSL_MAP (values compared as sets — the
// derived order differs from the old hand-written literals).
assert.deepEqual(
  Object.fromEntries(
    Object.entries(REQUIRED_FIELD_DSL_MAP).map(([k, v]) => [k, [...v].sort()]),
  ),
  {
    rating: ['movie.rating', 'series.rating'],
    year: ['movie.year', 'series.year'],
    genre: ['movie.genre', 'series.genre'],
    is_anime: ['movie.is_anime', 'series.is_anime'],
    collection: ['movie.collection', 'series.collection'],
  },
);

// Table hygiene: resource-level entries never carry a requiredKey;
// work-namespaced entries always do.
for (const m of FILTER_FIELD_META) {
  assert.equal('requiredKey' in m, !m.resourceLevel, m.name);
}

// allowedAgentFilterFields gating semantics.
assert.equal(allowedAgentFilterFields(null), null);
assert.equal(allowedAgentFilterFields(undefined), null);
{
  // Empty declaration: resource-level fields only.
  const allowed = allowedAgentFilterFields([]);
  assert.deepEqual([...allowed].sort(), [...EXPECTED_RESOURCE_LEVEL].sort());
}
{
  const allowed = allowedAgentFilterFields(['rating']);
  assert.ok(allowed.includes('series.rating') && allowed.includes('movie.rating'));
  assert.ok(!allowed.includes('series.year') && !allowed.includes('movie.year'));
  assert.ok(allowed.includes('collection')); // resource-level, always allowed
}
{
  // Full declaration unlocks every field; unknown keys are ignored.
  const allowed = allowedAgentFilterFields(
    ['rating', 'year', 'genre', 'is_anime', 'collection', 'bogus'],
  );
  assert.deepEqual([...allowed].sort(), [...EXPECTED_FIELDS].sort());
}

console.log('filter-fields: ok');
