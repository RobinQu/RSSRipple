// ---------------------------------------------------------------------------
// Channel resource-list field filter catalog.
//
// Separate from constants/filterFields.ts on purpose: the Agent Filter DSL is
// a saved, gated contract, while this catalog is a lightweight list-page
// filter over resource-local columns only. Backed by
// app/services/resource_search.py — keep the field names in sync.
// ---------------------------------------------------------------------------

import type { FilterOperator } from '../types';

export type ResourceFilterFieldType = 'string' | 'number' | 'bool' | 'list';

export interface ResourceFilterFieldMeta {
  readonly name: string;
  readonly type: ResourceFilterFieldType;
  /** Fixed enum values (renders a Select instead of free text). */
  readonly enumValues?: readonly string[];
  /** Server-side autocomplete via /channels/{id}/field-values. */
  readonly autocomplete?: boolean;
}

export const RESOURCE_FILTER_FIELD_META = [
  // Titles
  { name: 'title_raw', type: 'string' },
  { name: 'title_cn', type: 'string' },
  { name: 'title_en', type: 'string' },
  { name: 'search_title', type: 'string' },
  // Release metadata
  { name: 'subtitle_group', type: 'string', autocomplete: true },
  { name: 'subtitle_groups', type: 'list', autocomplete: true },
  { name: 'subtitle_langs', type: 'list' },
  { name: 'subtitle_type', type: 'string', autocomplete: true },
  { name: 'resolution', type: 'string', autocomplete: true },
  { name: 'source', type: 'string', autocomplete: true },
  { name: 'video_codec', type: 'string', autocomplete: true },
  { name: 'audio_codec', type: 'string', autocomplete: true },
  { name: 'container', type: 'string', autocomplete: true },
  { name: 'file_size', type: 'number' },
  // Episode / batch
  { name: 'is_batch', type: 'bool' },
  { name: 'episode', type: 'number' },
  { name: 'season', type: 'number' },
  { name: 'episode_start', type: 'number' },
  { name: 'episode_end', type: 'number' },
  { name: 'absolute_episode', type: 'number' },
  { name: 'title_year', type: 'number' },
  { name: 'episode_confidence', type: 'string', enumValues: ['raw', 'reconciled', 'ambiguous', 'manual'] },
  // Work type (derived from the mutually-exclusive work FKs)
  { name: 'content_type', type: 'string', enumValues: ['tv', 'movie', 'audio'] },
] as const satisfies readonly ResourceFilterFieldMeta[];

export type ResourceFilterField = (typeof RESOURCE_FILTER_FIELD_META)[number]['name'];

export const RESOURCE_FILTER_FIELD_TYPES = Object.fromEntries(
  RESOURCE_FILTER_FIELD_META.map((m) => [m.name, m.type]),
) as Record<ResourceFilterField, ResourceFilterFieldType>;

const META_BY_NAME = Object.fromEntries(
  RESOURCE_FILTER_FIELD_META.map((m) => [m.name, m]),
) as Record<ResourceFilterField, ResourceFilterFieldMeta>;

export function resourceFilterFieldMeta(field: ResourceFilterField): ResourceFilterFieldMeta {
  return META_BY_NAME[field];
}

const NO_VALUE_OPERATORS: FilterOperator[] = ['is_empty', 'is_not_empty'];
const STRING_OPERATORS: FilterOperator[] = ['eq', 'ne', 'contains', 'fuzzy', ...NO_VALUE_OPERATORS];
const LIST_OPERATORS: FilterOperator[] = ['eq', 'ne', 'contains', 'in', ...NO_VALUE_OPERATORS];
const NUMBER_OPERATORS: FilterOperator[] = ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', ...NO_VALUE_OPERATORS];
const BOOL_OPERATORS: FilterOperator[] = ['eq', 'ne', ...NO_VALUE_OPERATORS];

export function resourceFilterOperators(field: ResourceFilterField): FilterOperator[] {
  switch (RESOURCE_FILTER_FIELD_TYPES[field]) {
    case 'number':
      return NUMBER_OPERATORS;
    case 'bool':
      return BOOL_OPERATORS;
    case 'list':
      return LIST_OPERATORS;
    default:
      return STRING_OPERATORS;
  }
}

export function isNoValueOperator(operator: FilterOperator | string): boolean {
  return operator === 'is_empty' || operator === 'is_not_empty';
}

/** Semantic groups used by the filter modal's field dropdown. */
export const RESOURCE_FILTER_FIELD_GROUPS: { key: string; fields: ResourceFilterField[] }[] = [
  { key: 'title', fields: ['title_raw', 'title_cn', 'title_en', 'search_title'] },
  {
    key: 'release',
    fields: [
      'subtitle_group', 'subtitle_groups', 'subtitle_langs', 'subtitle_type',
      'resolution', 'source', 'video_codec', 'audio_codec', 'container', 'file_size',
    ],
  },
  {
    key: 'episode',
    fields: [
      'is_batch', 'episode', 'season', 'episode_start', 'episode_end',
      'absolute_episode', 'title_year', 'episode_confidence',
    ],
  },
  { key: 'work', fields: ['content_type'] },
];

export const SUBTITLE_LANG_OPTIONS = ['zh-CN', 'zh-TW', 'ja', 'en', 'multi'];
