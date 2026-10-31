// ---------------------------------------------------------------------------
// ResourceEditWizard — shared types, constants and pure helpers.
// ---------------------------------------------------------------------------

import type {
  AssociationUpdatePayload,
  AssociationWorkFieldsInput,
  ResourceFileItem,
  WorkRefType,
} from '../../types';

export interface Placement {
  workType: WorkRefType;
  workId: string;
  season: number | null;
  epStart: number | null;
  epEnd: number | null;
}

/** One work_title_hint cluster of the wizard's file-mapping step. */
export interface ClusterGroup {
  key: string;
  title: string | null;
  pool: ResourceFileItem[];
  assigned: string[];
  allPaths: string[];
}

/** Flat render row of the candidate pane: cluster headers interleaved with
 * the (expanded) per-file rows. */
export type PoolRow =
  | { kind: 'header'; group: ClusterGroup }
  | { kind: 'file'; file: ResourceFileItem; idx: number }
  | { kind: 'assigned'; path: string };

export type MediaFieldKey =
  | 'resolution'
  | 'subtitle_groups'
  | 'source'
  | 'video_codec'
  | 'audio_codec'
  | 'subtitle_type'
  | 'container';

export type DirectMetadataFieldKey = 'title_cn' | 'title_en' | 'search_title';
export const DIRECT_METADATA_FIELD_KEYS: DirectMetadataFieldKey[] = [
  'title_cn', 'title_en', 'search_title',
];

export type WorkMetadataFieldKey = 'year' | 'rating' | 'is_anime' | 'genre';
export const WORK_METADATA_FIELD_KEYS: WorkMetadataFieldKey[] = [
  'year', 'rating', 'is_anime', 'genre',
];

export function missingWorkMetadataFields(
  missingFields: string[],
  targetActive: boolean,
): WorkMetadataFieldKey[] {
  return targetActive
    ? WORK_METADATA_FIELD_KEYS.filter((key) => missingFields.includes(key))
    : [];
}

export function workMetadataLabelKey(key: WorkMetadataFieldKey): string {
  switch (key) {
    case 'year': return 'workYear';
    case 'rating': return 'workRating';
    case 'is_anime': return 'workAnimeStatus';
    case 'genre': return 'workGenre';
  }
}

export interface WorkMetadataDraft {
  date: string | null;
  rating: number | null;
  is_anime: boolean | null;
  genre: string[];
}

export function workMetadataFieldsUpdate(
  workType: WorkRefType,
  current: WorkMetadataDraft,
  original: WorkMetadataDraft,
): AssociationWorkFieldsInput['fields'] {
  const fields: AssociationWorkFieldsInput['fields'] = {};
  if (current.date !== original.date) {
    fields[workType === 'series' ? 'start_date' : 'release_date'] = current.date;
  }
  if (current.rating !== original.rating) fields.rating = current.rating;
  if (current.is_anime !== original.is_anime) fields.is_anime = current.is_anime;
  if (JSON.stringify(current.genre) !== JSON.stringify(original.genre)) {
    fields.genre = current.genre;
  }
  return fields;
}

export const MEDIA_TEXT_KEYS: MediaFieldKey[] = [
  'resolution',
  'source',
  'container',
  'video_codec',
  'audio_codec',
  'subtitle_type',
  'subtitle_groups',
];

export const LANG_PRESETS = ['zh-CN', 'zh-TW', 'zh-HK', 'ja', 'en', 'ko', 'multi'];

export function normTitle(s: string | null | undefined): string {
  return (s || '').toLowerCase().replace(/[\s·・]+/g, '');
}

export type ChangesShape = {
  payload: AssociationUpdatePayload;
  scopeFrom: string;
  scopeTo: string;
  worksAdded: string[];
  worksRemoved: string[];
  mappingChanged: { path: string; label: string }[];
  collectionChanged: { from: string; to: string } | null;
  singleEpChanges: { key: string; from: string; to: string }[];
  mediaChanges: { key: MediaFieldKey | DirectMetadataFieldKey | 'subtitle_langs'; from: string; to: string }[];
  workChanges: { key: WorkMetadataFieldKey; from: string; to: string }[];
};

export function worksAddedIsEmpty(c: { worksAdded: string[]; worksRemoved: string[] }): boolean {
  return c.worksAdded.length === 0 && c.worksRemoved.length === 0;
}

export function mediaLabelKey(k: MediaFieldKey | string): string {
  switch (k) {
    case 'title_cn': return 'titleCn';
    case 'title_en': return 'titleEn';
    case 'search_title': return 'searchTitle';
    case 'resolution': return 'resolution';
    case 'source': return 'source';
    case 'video_codec': return 'videoCodec';
    case 'audio_codec': return 'audioCodec';
    case 'subtitle_type': return 'subtitleType';
    case 'container': return 'container';
    case 'subtitle_groups': return 'subtitleGroup';
    default: return String(k);
  }
}

export function mediaLabelI18nKey(k: MediaFieldKey | string): string {
  const namespace = DIRECT_METADATA_FIELD_KEYS.includes(k as DirectMetadataFieldKey)
    ? 'filter'
    : 'resource';
  return `${namespace}.${mediaLabelKey(k)}`;
}

export function scopeLabelOf(t: (k: string) => string, scope: string): string {
  switch (scope) {
    case 'multi_season': return t('channels.batchMultiSeason');
    case 'franchise': return t('channels.batchFranchise');
    case 'movies': return t('channels.batchMovies');
    default: return t('channels.batch');
  }
}
