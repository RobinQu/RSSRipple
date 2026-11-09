import type {
  AssociationWorkRef,
  FileAssignmentRole,
  ResourceFileAssignmentItem,
  WorkRefType,
} from '../../types';
import { isEpisodePlacement, placementWorkKey } from '../wizardValidation.ts';
import type { Placement } from './types';

export type PlacementMap = Record<string, Placement>;

export function placementsFromAssignments(
  assignments: ResourceFileAssignmentItem[],
): PlacementMap {
  const placements: PlacementMap = {};
  for (const assignment of assignments) {
    const fileRole = assignment.file_role ?? 'episode';
    placements[assignment.file_path] = {
      workType: assignment.series_id ? 'series' : assignment.movie_id ? 'movie' : null,
      workId: assignment.series_id || assignment.movie_id || null,
      fileRole,
      attachedToPath: fileRole === 'subtitle' ? assignment.attached_to_path ?? null : null,
      season: fileRole === 'episode' || fileRole === 'extra' ? assignment.season : null,
      epStart: fileRole === 'episode' ? assignment.episode_start : null,
      epEnd: fileRole === 'episode' ? assignment.episode_end : null,
    };
  }
  return placements;
}

export function seasonForWork(
  workSeason: number | null | undefined,
  parsedSeason: number | null | undefined,
  fallbackSeason: number | null = null,
): number | null {
  if (workSeason === 0 || parsedSeason === 0) return 0;
  return workSeason ?? parsedSeason ?? fallbackSeason;
}

export function defaultRoleWork(
  works: AssociationWorkRef[],
  selectedWorkKey: string | null,
  selectionExplicit: boolean,
): AssociationWorkRef | null {
  if (works.length === 1) return works[0];
  if (!selectionExplicit || !selectedWorkKey) return null;
  return works.find((work) => `${work.work_type}:${work.work_id}` === selectedWorkKey) ?? null;
}

export function eligibleSubtitleParents(
  placements: PlacementMap,
  subtitlePath: string,
): string[] {
  const subtitle = placements[subtitlePath];
  const workKey = subtitle ? placementWorkKey(subtitle) : null;
  return Object.entries(placements)
    .filter(([path, placement]) =>
      path !== subtitlePath &&
      isEpisodePlacement(placement) &&
      (workKey == null || placementWorkKey(placement) === workKey),
    )
    .map(([path]) => path);
}

export function reconcileSubtitleAttachments(placements: PlacementMap): PlacementMap {
  let next: PlacementMap | null = null;
  for (const [path, placement] of Object.entries(placements)) {
    if (placement.fileRole !== 'subtitle' || !placement.attachedToPath) continue;
    if (eligibleSubtitleParents(placements, path).includes(placement.attachedToPath)) continue;
    next ??= { ...placements };
    next[path] = { ...placement, attachedToPath: null };
  }
  return next ?? placements;
}

export function attachSubtitleToEpisode(
  placements: PlacementMap,
  subtitlePath: string,
  parentPath: string | null,
): PlacementMap {
  const subtitle = placements[subtitlePath];
  if (subtitle?.fileRole !== 'subtitle') return placements;
  if (parentPath == null) {
    return {
      ...placements,
      [subtitlePath]: { ...subtitle, attachedToPath: null },
    };
  }
  const parent = placements[parentPath];
  if (!parent || !isEpisodePlacement(parent)) return placements;
  const subtitleWorkKey = placementWorkKey(subtitle);
  const parentWorkKey = placementWorkKey(parent);
  if (subtitleWorkKey != null && subtitleWorkKey !== parentWorkKey) return placements;
  return {
    ...placements,
    [subtitlePath]: {
      ...subtitle,
      workType: subtitle.workType ?? parent.workType,
      workId: subtitle.workId ?? parent.workId,
      attachedToPath: parentPath,
    },
  };
}

export function shouldApplySuggestedEpisode(
  existing: Placement | undefined,
  suggestedRole: FileAssignmentRole | undefined,
): boolean {
  return (suggestedRole ?? 'episode') === 'episode' &&
    (!existing || existing.fileRole === 'episode');
}

export function sharedEpisodeEntries(placements: PlacementMap): [string, Placement][] {
  const entries = Object.entries(placements).filter(([, placement]) => isEpisodePlacement(placement));
  const first = entries[0]?.[1];
  if (!first) return [];
  return entries.every(([, placement]) =>
    placement.season === first.season &&
    placement.epStart === first.epStart &&
    placement.epEnd === first.epEnd,
  ) ? entries : [];
}

export function updateSharedEpisodes(
  placements: PlacementMap,
  field: 'season' | 'episode',
  value: number | null,
): PlacementMap {
  const entries = sharedEpisodeEntries(placements);
  if (!entries.length) return placements;
  const next = { ...placements };
  for (const [path, placement] of entries) {
    next[path] = field === 'season'
      ? { ...placement, season: value }
      : { ...placement, epStart: value, epEnd: value };
  }
  return next;
}

export function changePlacementRole(
  placements: PlacementMap,
  path: string,
  fileRole: FileAssignmentRole,
  options: {
    work?: AssociationWorkRef | null;
    workSeason?: number | null;
    parsedSeason?: number | null;
    parsedEpisode?: number | null;
    fallbackSeason?: number | null;
  } = {},
): PlacementMap {
  const existing = placements[path];
  if (
    existing?.fileRole === fileRole &&
    (fileRole === 'excluded' || placementWorkKey(existing) != null)
  ) {
    return placements;
  }

  let workType: WorkRefType | null = existing?.workType ?? null;
  let workId: string | null = existing?.workId ?? null;
  if (fileRole !== 'excluded' && (!workType || !workId)) {
    workType = options.work?.work_type ?? null;
    workId = options.work?.work_id ?? null;
  }
  if (fileRole !== 'excluded' && (!workType || !workId)) return placements;
  if (fileRole === 'excluded') {
    workType = null;
    workId = null;
  }

  const season = fileRole === 'episode'
    ? workType === 'series'
      ? seasonForWork(
          options.workSeason,
          existing?.season ?? options.parsedSeason,
          options.fallbackSeason ?? null,
        )
      : null
    : fileRole === 'extra'
      ? existing?.fileRole === 'extra' ? existing.season : existing?.season === 0 ? 0 : null
      : null;
  const next = {
    ...placements,
    [path]: {
      workType,
      workId,
      fileRole,
      attachedToPath: fileRole === 'subtitle' ? existing?.attachedToPath ?? null : null,
      season,
      epStart: fileRole === 'episode'
        ? existing?.epStart ?? options.parsedEpisode ?? null
        : null,
      epEnd: fileRole === 'episode'
        ? existing?.epEnd ?? options.parsedEpisode ?? null
        : null,
    },
  };
  if (fileRole === 'subtitle' && !next[path].attachedToPath) {
    const parents = eligibleSubtitleParents(next, path);
    if (parents.length === 1) {
      next[path] = { ...next[path], attachedToPath: parents[0] };
    }
  }
  return reconcileSubtitleAttachments(next);
}
