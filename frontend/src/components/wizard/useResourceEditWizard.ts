// ---------------------------------------------------------------------------
// ResourceEditWizard — wizard state management hook: all state, the initial
// load effect, file-mapping selection (shift/drag), batch analysis streaming
// and the payload/change-summary builders. The component and the step panes
// only render what this hook returns.
// ---------------------------------------------------------------------------

import { useEffect, useMemo, useRef, useState } from 'react';
import { App } from 'antd';
import { useTranslation } from 'react-i18next';
import { collectionsApi } from '../../api/collections';
import { channelsApi, resourcesApi } from '../../api/channels';
import { DEFAULT_FALLBACK_SOURCES } from '../channel-form/constants';
import {
  stepFromSaveError,
  validateStep,
  workKeyOf,
  type WizardStepError,
} from '../wizardValidation';
import {
  DIRECT_METADATA_FIELD_KEYS,
  MEDIA_TEXT_KEYS,
  normTitle,
  scopeLabelOf,
  worksAddedIsEmpty,
  type ChangesShape,
  type ClusterGroup,
  type DirectMetadataFieldKey,
  type MediaFieldKey,
  type Placement,
  type PoolRow,
} from './types';
import type {
  AssociationUpdatePayload,
  AssociationWorkRef,
  BatchSuggestion,
  BatchSuggestionWork,
  FileResource,
  FileResourceDetail,
  MetadataSource,
  ResourceFileItem,
  ResourceFilesResponse,
  WorkRefType,
} from '../../types';

export interface UseResourceEditWizardOptions {
  resourceId: string;
  initialStep?: number;
  /** Called once a save settles: updated resource on success, null when
   * nothing changed / closed without applicable changes. */
  onDone: (updated: FileResource | null) => void;
  /** Called after a background metadata reparse is successfully triggered —
   * hosts showing todo lists should refresh so the entry disappears. */
  onReparse?: () => void;
}

export function useResourceEditWizard({
  resourceId,
  initialStep = 0,
  onDone,
  onReparse,
}: UseResourceEditWizardOptions) {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState<FileResourceDetail | null>(null);
  const [files, setFiles] = useState<ResourceFileItem[]>([]);
  const [filesSource, setFilesSource] = useState<ResourceFilesResponse['source'] | null>(null);
  const [filesRetrying, setFilesRetrying] = useState(false);
  // Batch editing state for the expanded work's assignment rows (step 1).
  const [checkedAssign, setCheckedAssign] = useState<string[]>([]);
  const [batchEpStart, setBatchEpStart] = useState<number | null>(null);
  const [step, setStep] = useState(0);
  const [saving, setSaving] = useState(false);
  const [reparsing, setReparsing] = useState(false);
  // Server 422 (VALIDATION_ERROR) surfaced in-place: the message lists the
  // per-work gaps; ``step`` is the wizard step that fixes it.
  const [saveError, setSaveError] = useState<{ step: number; message: string } | null>(null);

  const [isBatch, setIsBatch] = useState(false);
  const [works, setWorks] = useState<AssociationWorkRef[]>([]);
  const [workTitles, setWorkTitles] = useState<Record<string, string>>({});
  // Per-season works: a series work IS one season. The season applied to a
  // file placement comes from the work itself; null = unknown (legacy rows
  // and online candidates) and falls back to a per-work/manual input.
  const [workSeasons, setWorkSeasons] = useState<Record<string, number | null>>({});
  const [placements, setPlacements] = useState<Record<string, Placement>>({});
  const [originalPlacements, setOriginalPlacements] = useState<Record<string, Placement>>({});
  const [collectionId, setCollectionId] = useState<string | null>(null);
  const [collections, setCollections] = useState<
    { id: string; name: string }[]
  >([]);

  const [epSeason, setEpSeason] = useState<number | null>(null);
  const [epEpisode, setEpEpisode] = useState<number | null>(null);
  const [epAbsolute, setEpAbsolute] = useState<number | null>(null);

  const [media, setMedia] = useState<Record<MediaFieldKey, string>>({
    resolution: '',
    subtitle_groups: '',
    source: '',
    video_codec: '',
    audio_codec: '',
    subtitle_type: '',
    container: '',
  });
  const [mediaOptions, setMediaOptions] = useState<
    Partial<Record<MediaFieldKey, string[]>>
  >({});
  const [subtitleLangs, setSubtitleLangs] = useState<string[]>([]);
  const [directMetadata, setDirectMetadata] = useState<Record<DirectMetadataFieldKey, string>>({
    title_cn: '', title_en: '', search_title: '',
  });
  const [channelMetadataSource, setChannelMetadataSource] = useState<MetadataSource>('wikipedia');
  const [channelFallbackSources, setChannelFallbackSources] = useState<string[]>(DEFAULT_FALLBACK_SOURCES);

  const [pickerOpen, setPickerOpen] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [analysisStatus, setAnalysisStatus] = useState('');
  const [analysisOutput, setAnalysisOutput] = useState('');
  const [suggestion, setSuggestion] = useState<BatchSuggestion | null>(null);
  const worksDirtyRef = useRef(false);

  // File-mapping selection state (step ① right pane).
  const [selectedWorkKey, setSelectedWorkKey] = useState<string | null>(null);
  const [checkedFiles, setCheckedFiles] = useState<string[]>([]);
  const [expandedClusters, setExpandedClusters] = useState<Set<string>>(new Set());
  // Cluster key waiting on a WorkPickerModal pick (whole-cluster binding).
  const [pickerCluster, setPickerCluster] = useState<string | null>(null);
  const lastCheckedIdxRef = useRef<number | null>(null);
  const [joinSeason, setJoinSeason] = useState<number | null>(1);
  // Mouse drag range-selection on the candidate list: press on a row to
  // anchor, hover more rows while held to extend, release to finish.
  const draggingRef = useRef(false);
  const dragAnchorIdxRef = useRef<number | null>(null);
  const dragModeRef = useRef<'add' | 'remove'>('add');
  const dragBaseRef = useRef<string[]>([]);

  // Collection search + create-in-place (step 0, batch resources).
  const [collSearching, setCollSearching] = useState(false);
  const [newCollTitle, setNewCollTitle] = useState('');
  const [creatingColl, setCreatingColl] = useState(false);

  const audioLinked = !!detail?.audio_work_id;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setDetail(null);
    setFiles([]);
    setFilesSource(null);
    setCheckedAssign([]);
    setSuggestion(null);
    setCheckedFiles([]);
    setSelectedWorkKey(null);
    setPickerCluster(null);
    setExpandedClusters(new Set());
    setStep(initialStep);
    setSaveError(null);
    worksDirtyRef.current = false;
    (async () => {
      const res = await resourcesApi.get(resourceId);
      if (cancelled) return;
      if (!res.success) {
        message.error(res.error?.message || t('resource.correctLoadFailed'));
        setLoading(false);
        return;
      }
      const d = res.data as FileResourceDetail;
      setDetail(d);
      setIsBatch(d.is_batch);
      const nextWorks: AssociationWorkRef[] = [];
      const titles: Record<string, string> = {};
      for (const l of d.work_links ?? []) {
        if (l.series_id) {
          nextWorks.push({ work_type: 'series', work_id: l.series_id });
          titles[workKeyOf('series', l.series_id)] =
            l.work_title || l.series_id;
        } else if (l.movie_id) {
          nextWorks.push({ work_type: 'movie', work_id: l.movie_id });
          titles[workKeyOf('movie', l.movie_id)] = l.work_title || l.movie_id;
        }
      }
      if (!nextWorks.length && d.series_id) {
        nextWorks.push({ work_type: 'series', work_id: d.series_id });
        titles[workKeyOf('series', d.series_id)] =
          d.series?.original_title || d.series?.title_cn || d.series?.title_en || '';
      }
      if (!nextWorks.length && d.movie_id) {
        nextWorks.push({ work_type: 'movie', work_id: d.movie_id });
        titles[workKeyOf('movie', d.movie_id)] =
          d.movie?.original_title || d.movie?.title_cn || d.movie?.title_en || '';
      }
      setWorks(nextWorks);
      setWorkTitles(titles);
      // Season evidence per work: the series brief carries season_number;
      // link-table works fall back to their existing file placements.
      const nextWorkSeasons: Record<string, number | null> = {};
      if (d.series_id) {
        nextWorkSeasons[workKeyOf('series', d.series_id)] =
          d.series?.season_number ?? null;
      }
      for (const l of d.work_links ?? []) {
        if (l.series_id) {
          nextWorkSeasons[workKeyOf('series', l.series_id)] =
            l.season_number ?? null;
        }
      }
      for (const a of d.file_assignments ?? []) {
        const wt: WorkRefType | null = a.series_id ? 'series' : a.movie_id ? 'movie' : null;
        if (!wt) continue;
        const key = workKeyOf(wt, (a.series_id || a.movie_id)!);
        if (nextWorkSeasons[key] == null && a.season != null) {
          nextWorkSeasons[key] = a.season;
        }
      }
      setWorkSeasons(nextWorkSeasons);
      // Default-expand the first work so existing file associations (the
      // season's mapping list) are visible without an extra click.
      if (nextWorks.length > 0) {
        setSelectedWorkKey(workKeyOf(nextWorks[0].work_type, nextWorks[0].work_id));
      }
      const nextPlacements: Record<string, Placement> = {};
      for (const a of d.file_assignments ?? []) {
        const wt: WorkRefType | null = a.series_id ? 'series' : a.movie_id ? 'movie' : null;
        if (!wt) continue;
        nextPlacements[a.file_path] = {
          workType: wt,
          workId: (a.series_id || a.movie_id)!,
          season: a.season,
          epStart: a.episode_start,
          epEnd: a.episode_end,
        };
      }
      setPlacements(nextPlacements);
      setOriginalPlacements({ ...nextPlacements });
      // Collection preselection: season packs don't persist collection_id —
      // when it's null but every linked/FK work resolves to one and the same
      // collection, adopt it (mirrors the addWork adoption path).
      let nextCollectionId = d.collection_id ?? null;
      if (!nextCollectionId) {
        const ids = new Set<string>();
        for (const l of d.work_links ?? []) {
          if (l.collection_id) ids.add(l.collection_id);
        }
        if (d.series_id && d.series?.collection_id) {
          ids.add(d.series.collection_id);
        }
        if (d.movie_id && d.movie?.collection_id) {
          ids.add(d.movie.collection_id);
        }
        if (ids.size === 1) nextCollectionId = [...ids][0];
      }
      setCollectionId(nextCollectionId);
      setEpSeason(d.season ?? null);
      setEpEpisode(d.episode ?? null);
      setEpAbsolute(d.absolute_episode ?? null);
      setMedia({
        resolution: d.resolution || '',
        subtitle_groups: (d.subtitle_groups ?? (d.subtitle_group ? [d.subtitle_group] : [])).join(', '),
        source: d.source || '',
        video_codec: d.video_codec || '',
        audio_codec: d.audio_codec || '',
        subtitle_type: d.subtitle_type || '',
        container: d.container || '',
      });
      setSubtitleLangs([...(d.subtitle_langs ?? [])]);
      setDirectMetadata({
        title_cn: d.title_cn || '',
        title_en: d.title_en || '',
        search_title: d.search_title || '',
      });
      setLoading(false);
      const channelRes = await channelsApi.get(d.channel_id);
      if (!cancelled && channelRes.success) {
        setChannelMetadataSource(channelRes.data.metadata_source || 'wikipedia');
        setChannelFallbackSources(
          channelRes.data.metadata_fallback_sources ?? DEFAULT_FALLBACK_SOURCES,
        );
      }
      const filesRes = await resourcesApi.getFiles(resourceId);
      if (!cancelled && filesRes.success) {
        setFiles(filesRes.data.files);
        setFilesSource(filesRes.data.source);
      }
      const collRes = await collectionsApi.list(1, 50);
      if (!cancelled && collRes.success) {
        setCollections(
          collRes.data.map((c) => ({ id: c.id, name: c.title_cn })),
        );
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resourceId, initialStep]);

  /** Re-call GET /resources/{id}/files — the server live-retries the torrent
   * fetch on every call, so this is the manual recovery entry when an earlier
   * download failed (source "none" despite an http(s) torrent_url). */
  const retryFetchFiles = async () => {
    if (!resourceId) return;
    setFilesRetrying(true);
    try {
      const res = await resourcesApi.getFiles(resourceId);
      if (res.success) {
        setFiles(res.data.files);
        setFilesSource(res.data.source);
      }
    } finally {
      setFilesRetrying(false);
    }
  };

  const loadMediaOptions = async () => {
    if (!detail) return;
    const fields: MediaFieldKey[] = ['resolution', 'source', 'video_codec', 'audio_codec', 'subtitle_type', 'container', 'subtitle_groups'];
    const results = await Promise.all(
      fields.map(async (f) => {
        try {
          const r = await channelsApi.fieldValues(detail.channel_id, f, '', 10);
          return [f, r.success ? (r.data as string[]) : []] as const;
        } catch {
          return [f, [] as string[]] as const;
        }
      }),
    );
    setMediaOptions(Object.fromEntries(results));
  };

  const detParses = useMemo(() => {
    const map: Record<string, { season: number | null; episode: number | null }> = {};
    for (const f of suggestion?.deterministic.files ?? []) {
      map[f.path] = { season: f.season, episode: f.episode };
    }
    return map;
  }, [suggestion]);

  // Candidate list = files not yet mapped to any work.
  const poolFiles = useMemo(
    () => files.filter((f) => !placements[f.name]),
    [files, placements],
  );

  // Cluster title per file path: persisted assignment hints first, then the
  // analyze-batch deterministic clusters as a fallback for fresh resources.
  const hintByPath = useMemo(() => {
    const map: Record<string, string> = {};
    for (const c of suggestion?.deterministic.clusters ?? []) {
      for (const p of c.files) {
        if (c.title) map[p] = c.title;
      }
    }
    for (const a of detail?.file_assignments ?? []) {
      if (a.work_title_hint) map[a.file_path] = a.work_title_hint;
    }
    return map;
  }, [detail, suggestion]);

  // Cluster-grouped view of the candidate pool: one group per distinct
  // work_title_hint, files without a hint collected into the trailing
  // "other" group. Drag/shift selection keeps indexing into poolFiles, so
  // every row resolves its global index through poolIndex.
  const poolIndex = useMemo(
    () => new Map(poolFiles.map((f, i) => [f.name, i])),
    [poolFiles],
  );

  const clusterGroups = useMemo<ClusterGroup[]>(() => {
    const groups = new Map<string, ClusterGroup>();
    for (const f of files) {
      const hint = hintByPath[f.name] || '';
      const key = hint ? `hint:${hint.toLowerCase()}` : '__other__';
      let g = groups.get(key);
      if (!g) {
        g = { key, title: hint || null, pool: [], assigned: [], allPaths: [] };
        groups.set(key, g);
      }
      g.allPaths.push(f.name);
      if (placements[f.name]) g.assigned.push(f.name);
      else g.pool.push(f);
    }
    const hinted = [...groups.values()].filter((g) => g.title);
    hinted.sort((a, b) => (a.title ?? '').localeCompare(b.title ?? ''));
    const other = groups.get('__other__');
    return other ? [...hinted, other] : hinted;
  }, [files, hintByPath, placements]);

  // Flat render list for the candidate pane: cluster headers interleaved
  // with the (expanded) per-file rows. Kept flat so drag/shift selection
  // handlers stay in a single top-level map like the pre-cluster list.
  const poolRows = useMemo<PoolRow[]>(() => {
    const rows: PoolRow[] = [];
    for (const g of clusterGroups) {
      const showHeader = clusterGroups.length > 1 || g.title != null;
      const expanded = !showHeader || expandedClusters.has(g.key);
      if (showHeader) rows.push({ kind: 'header', group: g });
      if (!expanded) continue;
      for (const f of g.pool) {
        const idx = poolIndex.get(f.name);
        if (idx != null) rows.push({ kind: 'file', file: f, idx });
      }
      for (const path of g.assigned) {
        if (placements[path]) rows.push({ kind: 'assigned', path });
      }
    }
    return rows;
  }, [clusterGroups, expandedClusters, poolIndex, placements]);

  const toggleCluster = (key: string) => {
    setExpandedClusters((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  /** Compact S/E coverage preview of a cluster, e.g. "S1 E1-26 · S2 E1-24".
   * Parsed values win; existing placements fill the gaps. */
  const clusterRangePreview = (paths: string[]): string => {
    const bySeason = new Map<number, number[]>();
    let hasAny = false;
    for (const path of paths) {
      const parsed = detParses[path];
      const placed = placements[path];
      const season = parsed?.season ?? placed?.season ?? null;
      const eps = [
        parsed?.episode ?? null,
        placed?.epStart ?? null,
        placed?.epEnd ?? null,
      ].filter((v): v is number => v != null);
      if (season == null && eps.length === 0) continue;
      hasAny = true;
      if (season != null && eps.length > 0) {
        const list = bySeason.get(season) ?? [];
        list.push(...eps);
        bySeason.set(season, list);
      } else if (season != null) {
        bySeason.set(season, bySeason.get(season) ?? []);
      }
    }
    if (!hasAny) return '';
    if (bySeason.size === 0) return '';
    return [...bySeason.entries()]
      .sort(([a], [b]) => a - b)
      .map(([s, eps]) =>
        eps.length > 0 ? `S${s} E${Math.min(...eps)}-${Math.max(...eps)}` : `S${s}`,
      )
      .join(' · ');
  };

  /** Current binding of a cluster: the shared work key, 'mixed' when its
   * files point at different works, or null when nothing is bound yet. */
  const clusterBinding = (paths: string[]): string | 'mixed' | null => {
    const keys = new Set(
      paths
        .map((p) => placements[p])
        .filter((p): p is Placement => p != null)
        .map((p) => workKeyOf(p.workType, p.workId)),
    );
    if (keys.size === 0) return null;
    if (keys.size > 1) return 'mixed';
    return [...keys][0];
  };

  const deriveScopeLabel = useMemo(() => {
    if (!isBatch) return '';
    if (!works.length) return t('channels.batchFranchise');
    const types = new Set(works.map((w) => w.work_type));
    if (types.size === 1 && works[0].work_type === 'movie') {
      return t('channels.batchMovies');
    }
    if (types.size === 1 && works.length === 1) {
      const seasons = new Set(
        Object.values(placements)
          .filter((p) => p.workId === works[0].work_id)
          .map((p) => p.season)
          .filter((s): s is number => s != null),
      );
      for (const s of detail?.batch_seasons ?? []) seasons.add(s);
      if (detail?.season != null) seasons.add(detail.season);
      for (const sr of suggestion?.deterministic.season_ranges ?? []) seasons.add(sr.season);
      return seasons.size >= 2
        ? t('channels.batchMultiSeason')
        : t('channels.batch');
    }
    return t('channels.batchFranchise');
  }, [isBatch, works, placements, detail, suggestion, t]);

  const addWork = (
    ref: AssociationWorkRef,
    title: string,
    meta?: { season?: number | null; collectionId?: string | null },
  ) => {
    const key = workKeyOf(ref.work_type, ref.work_id);
    if (works.some((w) => workKeyOf(w.work_type, w.work_id) === key)) {
      return;
    }
    if (!ref.work_type) return;
    // Two-level association (batch TV): a series work outside the selected
    // collection is rejected server-side (422) — block it up front. Non-batch
    // resources don't carry a collection_id, so the work pick alone binds it.
    if (
      isBatch &&
      ref.work_type === 'series' &&
      collectionId &&
      meta?.collectionId &&
      meta.collectionId !== collectionId
    ) {
      message.warning(t('resource.workNotInCollection'));
      return;
    }
    setWorks((ws) => [...ws, ref]);
    setWorkTitles((prev) => ({ ...prev, [key]: title }));
    setWorkSeasons((prev) => ({ ...prev, [key]: meta?.season ?? null }));
    // Adopt the picked work's collection when none is selected yet.
    if (isBatch && ref.work_type === 'series' && !collectionId && meta?.collectionId) {
      setCollectionId(meta.collectionId);
      const name = collections.find((c) => c.id === meta.collectionId)?.name;
      message.info(t('resource.collectionAdopted', { name: name ?? meta.collectionId }));
    }
    if (!selectedWorkKey) setSelectedWorkKey(key);
    worksDirtyRef.current = true;
  };

  const removeWork = (key: string) => {
    const idx = works.findIndex(
      (w) => workKeyOf(w.work_type, w.work_id) === key,
    );
    if (idx < 0) return;
    setWorks((ws) => ws.filter((_, i) => i !== idx));
    setWorkSeasons((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
    setPlacements((prev) => {
      const next: Record<string, Placement> = {};
      for (const [path, p] of Object.entries(prev)) {
        if (workKeyOf(p.workType, p.workId) !== key) next[path] = p;
      }
      return next;
    });
    setCheckedAssign((prev) =>
      prev.filter((path) => {
        const p = placements[path];
        return p != null && workKeyOf(p.workType, p.workId) !== key;
      }),
    );
    if (selectedWorkKey === key) setSelectedWorkKey(null);
    worksDirtyRef.current = true;
  };

  const unassignPaths = (paths: string[]) => {
    setPlacements((prev) => {
      const next = { ...prev };
      for (const p of paths) delete next[p];
      return next;
    });
    setCheckedFiles((prev) => prev.filter((p) => !paths.includes(p)));
    setCheckedAssign((prev) => prev.filter((p) => !paths.includes(p)));
  };

  const setPlacementField = (
    path: string,
    patch: Partial<Placement>,
  ) => {
    setPlacements((prev) => ({
      ...prev,
      [path]: { ...prev[path], ...patch },
    }));
  };

  /** Assignment rows of one work in display order (natural path sort, so the
   * episode numbers embedded in filenames ascend) — shared by the render and
   * the batch increment fill to guarantee identical ordering. */
  const sortedEntriesFor = (key: string): [string, Placement][] =>
    Object.entries(placements)
      .filter(([, p]) => workKeyOf(p.workType, p.workId) === key)
      .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }));

  /** Fill checked rows with ascending episode numbers (start, start+1, …) in
   * display order; each file becomes a single-episode mapping. */
  const applyBatchEpisodeIncrement = () => {
    if (batchEpStart == null || !selectedWorkKey || checkedAssign.length === 0) return;
    const order = sortedEntriesFor(selectedWorkKey)
      .map(([path]) => path)
      .filter((path) => checkedAssign.includes(path));
    setPlacements((prev) => {
      const next = { ...prev };
      order.forEach((path, i) => {
        if (next[path]) {
          next[path] = { ...next[path], epStart: batchEpStart + i, epEnd: batchEpStart + i };
        }
      });
      return next;
    });
  };

  /** Season applied to files joining a work: the filename parse wins, then
   * the work's own season_number, then the manual join-season fallback. */
  const seasonForJoin = (workKey: string, parsedSeason: number | null | undefined): number | null =>
    parsedSeason ?? workSeasons[workKey] ?? joinSeason;

  const joinChecked = () => {
    if (!selectedWorkKey || checkedFiles.length === 0) return;
    const sep = selectedWorkKey.indexOf(':');
    const wt = selectedWorkKey.slice(0, sep) as WorkRefType;
    const wid = selectedWorkKey.slice(sep + 1);
    const missing: string[] = [];
    setPlacements((prev) => {
      const next = { ...prev };
      for (const path of checkedFiles) {
        const parsed = detParses[path];
        const season = seasonForJoin(selectedWorkKey, parsed?.season);
        if (wt === 'series' && season == null) {
          missing.push(path);
          continue;
        }
        const ep = parsed?.episode ?? null;
        next[path] = {
          workType: wt,
          workId: wid,
          season,
          epStart: ep,
          epEnd: ep,
        };
      }
      return next;
    });
    if (missing.length > 0) {
      message.warning(
        t('resource.seasonParamRequired', { count: missing.length }),
      );
    }
    setCheckedFiles([]);
    lastCheckedIdxRef.current = null;
  };

  /** Bind a whole cluster to one work: adds the work to the association list
   * when new, then expands the cluster choice into per-file placements
   * (parsed season wins, then the work's own season_number, then the join
   * fallback — same precedence as joinChecked). Files already placed on the
   * target work keep their episode edits. */
  const applyClusterToWork = (
    group: ClusterGroup,
    ref: AssociationWorkRef,
    title: string,
    season: number | null,
  ) => {
    const targetKey = workKeyOf(ref.work_type, ref.work_id);
    if (!works.some((w) => workKeyOf(w.work_type, w.work_id) === targetKey)) {
      addWork(ref, title, { season });
    }
    const missing: string[] = [];
    let applied = 0;
    setPlacements((prev) => {
      const next = { ...prev };
      for (const path of group.allPaths) {
        const existing = next[path];
        if (existing && workKeyOf(existing.workType, existing.workId) === targetKey) {
          continue;
        }
        const parsed = detParses[path];
        const s =
          ref.work_type === 'series'
            ? seasonForJoin(targetKey, parsed?.season ?? season)
            : null;
        if (ref.work_type === 'series' && s == null) {
          missing.push(path);
          continue;
        }
        const ep = parsed?.episode ?? null;
        next[path] = { workType: ref.work_type, workId: ref.work_id, season: s, epStart: ep, epEnd: ep };
        applied += 1;
      }
      return next;
    });
    setCheckedFiles((prev) => prev.filter((p) => !group.allPaths.includes(p)));
    if (missing.length > 0) {
      message.warning(t('resource.seasonParamRequired', { count: missing.length }));
    }
    if (applied > 0) {
      message.success(t('resource.clusterApplied', { count: applied, title }));
    }
  };

  const pickClusterWork = (group: ClusterGroup, value: string) => {
    if (value === '__new__') {
      setPickerCluster(group.key);
      setPickerOpen(true);
      return;
    }
    const sep = value.indexOf(':');
    const wt = value.slice(0, sep) as WorkRefType;
    const wid = value.slice(sep + 1);
    applyClusterToWork(
      group,
      { work_type: wt, work_id: wid },
      workTitles[value] || wid,
      workSeasons[value] ?? null,
    );
  };

  const toggleFileChecked = (
    path: string,
    index: number,
    shiftKey: boolean,
  ) => {
    setCheckedFiles((prev) => {
      if (shiftKey && lastCheckedIdxRef.current != null) {
        const lo = Math.min(lastCheckedIdxRef.current, index);
        const hi = Math.max(lastCheckedIdxRef.current, index);
        const rangePaths = poolFiles.slice(lo, hi + 1).map((f) => f.name);
        const merged = new Set(prev);
        for (const p of rangePaths) merged.add(p);
        lastCheckedIdxRef.current = index;
        return [...merged];
      }
      lastCheckedIdxRef.current = index;
      return prev.includes(path)
        ? prev.filter((p) => p !== path)
        : [...prev, path];
    });
  };

  /** Candidate rows are the UNASSIGNED files only, so ranges map 1:1 onto
   * ``poolFiles`` indices. */
  const applyDragRange = (anchorIdx: number, hoverIdx: number) => {
    const lo = Math.min(anchorIdx, hoverIdx);
    const hi = Math.max(anchorIdx, hoverIdx);
    const rangePaths = poolFiles.slice(lo, hi + 1).map((f) => f.name);
    const base = new Set(dragBaseRef.current);
    for (const p of rangePaths) {
      if (dragModeRef.current === 'add') base.add(p);
      else base.delete(p);
    }
    setCheckedFiles([...base]);
  };

  const beginDragSelect = (index: number) => {
    const path = poolFiles[index]?.name;
    if (!path) return;
    draggingRef.current = true;
    dragAnchorIdxRef.current = index;
    dragModeRef.current = checkedFiles.includes(path) ? 'remove' : 'add';
    dragBaseRef.current = [...checkedFiles];
    lastCheckedIdxRef.current = index;
    setCheckedFiles((prev) =>
      dragModeRef.current === 'add'
        ? prev.includes(path)
          ? prev
          : [...prev, path]
        : prev.filter((p) => p !== path),
    );
  };

  const beginPointerSelect = (
    event: React.PointerEvent<HTMLDivElement>,
    path: string,
    index: number,
  ) => {
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    event.preventDefault();
    if (event.shiftKey && lastCheckedIdxRef.current != null) {
      toggleFileChecked(path, index, true);
      return;
    }
    beginDragSelect(index);
  };

  const extendTouchSelect = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!draggingRef.current || event.pointerType === 'mouse') return;
    const bounds = event.currentTarget.getBoundingClientRect();
    if (event.clientY < bounds.top + 32) event.currentTarget.scrollBy(0, -12);
    if (event.clientY > bounds.bottom - 32) event.currentTarget.scrollBy(0, 12);
    const row = document
      .elementFromPoint(event.clientX, event.clientY)
      ?.closest<HTMLElement>('[data-file-index]');
    const index = Number(row?.dataset.fileIndex);
    if (Number.isInteger(index)) extendDragSelect(index);
  };

  const extendDragSelect = (index: number) => {
    if (!draggingRef.current || dragAnchorIdxRef.current == null) return;
    applyDragRange(dragAnchorIdxRef.current, index);
  };

  useEffect(() => {
    const end = () => {
      draggingRef.current = false;
    };
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
    return () => {
      window.removeEventListener('pointerup', end);
      window.removeEventListener('pointercancel', end);
    };
  }, []);

  /** Season evidence of a suggested work cluster: the most frequent non-null
   * season across its files (LLM season first, deterministic filename parse
   * as fallback). */
  const clusterSeasonOf = (
    w: BatchSuggestionWork,
    detSeason: Map<string, number | null>,
  ): number | null => {
    const counts = new Map<number, number>();
    for (const f of w.files) {
      const s = f.season ?? detSeason.get(f.path) ?? null;
      if (s == null) continue;
      counts.set(s, (counts.get(s) ?? 0) + 1);
    }
    let best: number | null = null;
    let bestCount = 0;
    for (const [s, c] of counts) {
      if (c > bestCount) {
        best = s;
        bestCount = c;
      }
    }
    return best;
  };

  /** Pick among same-title works by season evidence: an exact season_number
   * match wins (an SP cluster, hint 0, prefers the season-0 special work); a
   * TV cluster (hint >= 1) never lands on a season-0 special; anything
   * ambiguous keeps the first match (legacy behaviour). */
  const pickWorkMatch = (
    candidates: AssociationWorkRef[],
    seasonHint: number | null,
  ): AssociationWorkRef => {
    if (candidates.length === 1 || seasonHint == null) return candidates[0];
    const seasonOf = (w: AssociationWorkRef) =>
      workSeasons[workKeyOf(w.work_type, w.work_id)] ?? null;
    const exact = candidates.filter((w) => seasonOf(w) === seasonHint);
    if (exact.length > 0) return exact[0];
    if (seasonHint >= 1) {
      const nonSpecial = candidates.filter((w) => seasonOf(w) !== 0);
      if (nonSpecial.length > 0) return nonSpecial[0];
    }
    return candidates[0];
  };

  const applyAnalysisSuggestion = (sug: BatchSuggestion) => {
    setSuggestion(sug);
    const detSeason = new Map<string, number | null>(
      sug.deterministic.files.map((f) => [f.path, f.season]),
    );
    const soleSeries = works.length === 1 && works[0].work_type === 'series'
      ? works[0]
      : null;
    let applied = 0;
    const next = { ...placements };
    if (soleSeries) {
      for (const f of sug.deterministic.files) {
        if (next[f.path] || f.season == null) continue;
        next[f.path] = {
          workType: 'series', workId: soleSeries.work_id,
          season: f.season, epStart: f.episode, epEnd: f.episode,
        };
        applied += 1;
      }
    }
    for (const w of sug.works) {
      const target =
        works.find((knownWork) => (
          w.candidate_key === workKeyOf(knownWork.work_type, knownWork.work_id)
        )) ??
        (() => {
          const titleMatches = works.filter((knownWork) => {
            const known = normTitle(workTitles[workKeyOf(knownWork.work_type, knownWork.work_id)]);
            const want = normTitle(w.title);
            return known === want || (!!known && (known.includes(want) || want.includes(known)));
          });
          return titleMatches.length > 0
            ? pickWorkMatch(titleMatches, clusterSeasonOf(w, detSeason))
            : undefined;
        })();
      if (!target) continue;
      for (const f of w.files) {
        next[f.path] = {
          workType: target.work_type, workId: target.work_id,
          season: f.season, epStart: f.episode_start, epEnd: f.episode_end,
        };
        applied += 1;
      }
    }
    setPlacements(next);
    return applied;
  };

  const analyze = async (force = false) => {
    setAnalyzing(true);
    setAnalysisStatus(t('resource.analysisPreparing'));
    setAnalysisOutput('');
    try {
      const response = await resourcesApi.analyzeBatchStream(resourceId, force);
      if (!response.ok || !response.body) throw new Error(response.statusText);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let finalSuggestion: BatchSuggestion | null = null;
      let streamError: string | null = null;
      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        const frames = buffer.split('\n\n');
        buffer = frames.pop() ?? '';
        for (const frame of frames) {
          const line = frame.split('\n').find((part) => part.startsWith('data: '));
          if (!line) continue;
          const event = JSON.parse(line.slice(6)) as {
            type: string; message?: string; content?: string; suggestion?: BatchSuggestion | null;
          };
          if (event.message) setAnalysisStatus(event.message);
          if (event.content) {
            setAnalysisOutput((prev) => `${prev}${event.content}`.slice(-50000));
          }
          // Server-side failures arrive as SSE `event: error` frames whose
          // data payload carries type 'error'.
          if (event.type === 'error') streamError = event.message || t('resource.reanalyzeFailed');
          if (event.type === 'result') finalSuggestion = event.suggestion ?? null;
        }
        if (done) break;
      }
      if (streamError) {
        message.error(streamError);
      } else if (!finalSuggestion) {
        message.info(t('resource.noListingHint'));
      } else {
        const applied = applyAnalysisSuggestion(finalSuggestion);
        const parsed = finalSuggestion.deterministic.files.filter((f) => f.episode != null).length;
        message.success(applied > 0
          ? `${t('resource.analyzeDone', { count: parsed })} · ${t('resource.suggestionApplied', { count: applied })}`
          : t('resource.analyzeDone', { count: parsed }));
      }
    } catch (error) {
      message.error(error instanceof Error && error.message ? error.message : t('resource.reanalyzeFailed'));
    } finally {
      setAnalyzing(false);
    }
  };

  const gotoStep = (next: number) => {
    if (next === 2) {
      void loadMediaOptions();
    }
    setStep(next);
  };

  const maybeAutoAnalyze = (next: number) => {
    if (
      next === 1 &&
      !suggestion &&
      Object.keys(placements).length === 0 &&
      isBatch &&
      files.length > 0 &&
      !audioLinked
    ) {
      void analyze();
    }
    gotoStep(next);
  };

  /** Snapshot of the wizard state the pure step validator reads. */
  const stepState = () => ({
    isBatch,
    audioLinked,
    works: works.map((w) => ({ workType: w.work_type as string, workId: w.work_id })),
    placements,
  });

  /** Surface a step-validation failure: toast for the season gap (matches
   * the historical save behavior), pinned alert for the assignment gap. */
  const showStepError = (stepIdx: number, error: WizardStepError) => {
    if (error.code === 'tv_season_required') {
      message.error(t('resource.tvSeasonRequired'));
    } else {
      const names = (error.workKeys ?? []).map((k) => workTitles[k] || k);
      setSaveError({
        step: stepIdx,
        message: t('resource.multiWorkMissingAssignments', {
          names: names.join('、'),
        }),
      });
    }
    setStep(stepIdx);
  };

  /** Forward navigation is gated: the current step must pass the same
   * validation the save path runs (backward is always allowed). */
  const handleNext = () => {
    const error = validateStep(step, stepState());
    if (error) {
      showStepError(step, error);
      return;
    }
    setSaveError(null);
    maybeAutoAnalyze(step + 1);
  };

  const buildPayload = (): AssociationUpdatePayload | null => {
    if (!detail) return null;
    const payload: AssociationUpdatePayload = {
      is_batch: isBatch,
      works,
      assignments:
        isBatch && !audioLinked
          ? Object.entries(placements).map(([file_path, p]) => ({
              file_path,
              work_type: p.workType,
              work_id: p.workId,
              file_size: files.find((f) => f.name === file_path)?.size ?? null,
              season: p.season,
              episode_start: p.epStart,
              episode_end: p.epEnd,
            }))
          : [],
      ...(isBatch ? { collection_id: collectionId } : {}),
    };
    if (!isBatch && !audioLinked) {
      if ((epSeason ?? null) !== (detail.season ?? null)) payload.season = epSeason;
      if ((epEpisode ?? null) !== (detail.episode ?? null)) payload.episode = epEpisode;
      if ((epAbsolute ?? null) !== (detail.absolute_episode ?? null)) {
        payload.absolute_episode = epAbsolute;
      }
    }
    const fields: Record<string, unknown> = {};
    for (const k of MEDIA_TEXT_KEYS) {
      const cur = media[k].trim() || null;
      if (k === 'subtitle_groups') {
        const groups = cur ? cur.split(/[,，]/).map((x) => x.trim()).filter(Boolean) : [];
        const original = detail.subtitle_groups ?? (detail.subtitle_group ? [detail.subtitle_group] : []);
        if (JSON.stringify(groups) !== JSON.stringify(original)) fields.subtitle_groups = groups;
      } else if (cur !== (detail[k] || null)) fields[k] = cur;
    }
    for (const k of DIRECT_METADATA_FIELD_KEYS) {
      const cur = directMetadata[k].trim() || null;
      if (cur !== (detail[k] || null)) fields[k] = cur;
    }
    const origLangs = JSON.stringify([...(detail.subtitle_langs ?? [])].sort());
    if (JSON.stringify([...subtitleLangs].sort()) !== origLangs) {
      fields.subtitle_langs = subtitleLangs;
    }
    if (Object.keys(fields).length > 0) payload.fields = fields;
    return payload;
  };

  /** Structured change summary for the confirmation page. */
  const computeChanges = (): ChangesShape | null => {
    const payload = buildPayload();
    if (!payload || !detail) return null;

    const origKeys = new Set([
      ...(detail.work_links ?? []).map((l) =>
        l.series_id ? workKeyOf('series', l.series_id) : workKeyOf('movie', l.movie_id!),
      ),
      ...(detail.series_id ? [workKeyOf('series', detail.series_id)] : []),
      ...(detail.movie_id ? [workKeyOf('movie', detail.movie_id)] : []),
    ]);
    const newKeys = new Set(payload.works.map((w) => workKeyOf(w.work_type, w.work_id)));
    const worksAdded = [...newKeys]
      .filter((k) => !origKeys.has(k))
      .map((k) => workTitles[k] || k);
    const worksRemoved = [...origKeys]
      .filter((k) => !newKeys.has(k))
      .map((k) => workTitles[k] || k);

    const mappingChanged: { path: string; label: string }[] = [];
    for (const [path, p] of Object.entries(placements)) {
      const orig = originalPlacements[path];
      const same =
        orig &&
        orig.workType === p.workType &&
        orig.workId === p.workId &&
        orig.season === p.season &&
        orig.epStart === p.epStart &&
        orig.epEnd === p.epEnd;
      if (!same) {
        const label = workTitles[workKeyOf(p.workType, p.workId)] || p.workId;
        const se =
          p.epStart != null
            ? p.epEnd != null && p.epEnd !== p.epStart
              ? `E${p.epStart}-${p.epEnd}`
              : `E${p.epStart}`
            : '';
        mappingChanged.push({
          path,
          label: `${label}${p.season != null ? ` · S${p.season}` : ''}${se ? ` · ${se}` : ''}`,
        });
      }
    }

    const mediaChanges: { key: MediaFieldKey | DirectMetadataFieldKey | 'subtitle_langs'; from: string; to: string }[] = [];
    for (const k of MEDIA_TEXT_KEYS) {
      const cur = media[k].trim() || null;
      const prev = k === 'subtitle_groups'
        ? ((detail.subtitle_groups?.length ? detail.subtitle_groups : (detail.subtitle_group ? [detail.subtitle_group] : [])).join(', ') || null)
        : (detail[k] || null);
      if (cur !== prev) {
        mediaChanges.push({
          key: k,
          from: prev || t('common.off'),
          to: cur || t('common.off'),
        });
      }
    }
    for (const k of DIRECT_METADATA_FIELD_KEYS) {
      const cur = directMetadata[k].trim() || null;
      const prev = detail[k] || null;
      if (cur !== prev) {
        mediaChanges.push({
          key: k,
          from: prev || t('common.off'),
          to: cur || t('common.off'),
        });
      }
    }
    const origLangsStr = JSON.stringify([...(detail.subtitle_langs ?? [])].sort());
    if (JSON.stringify([...subtitleLangs].sort()) !== origLangsStr) {
      mediaChanges.push({
        key: 'subtitle_langs',
        from: (detail.subtitle_langs ?? []).join(', ') || t('common.off'),
        to: subtitleLangs.join(', ') || t('common.off'),
      });
    }

    const singleEpChanges: { key: string; from: string; to: string }[] = [];
    if (!isBatch) {
      const pairs: [string, number | null, number | null][] = [
        [t('resource.seasonLabel'), detail.season ?? null, epSeason],
        [t('resource.episodePerSeasonLabel'), detail.episode ?? null, epEpisode],
        [t('resource.absoluteEpisode'), detail.absolute_episode ?? null, epAbsolute],
      ];
      for (const [key, from, to] of pairs) {
        if (from !== to) {
          singleEpChanges.push({
            key,
            from: from == null ? t('format.dash') : String(from),
            to: to == null ? t('format.dash') : String(to),
          });
        }
      }
    }

    return {
      payload,
      scopeFrom: detail.batch_scope
        ? scopeLabelOf(t, detail.batch_scope)
        : t('format.dash'),
      scopeTo: isBatch ? deriveScopeLabel : t('format.dash'),
      worksAdded,
      worksRemoved,
      mappingChanged,
      collectionChanged:
        isBatch && collectionId !== (detail.collection_id ?? null)
          ? {
              from:
                collections.find((c) => c.id === detail.collection_id)?.name ??
                detail.collection_name ??
                detail.collection_id ??
                t('format.dash'),
              to:
                collections.find((c) => c.id === collectionId)?.name ??
                collectionId ??
                t('format.dash'),
            }
          : null,
      singleEpChanges,
      mediaChanges,
    };
  };

  const handleSave = async () => {
    if (!detail) return;
    setSaveError(null);
    // Final defense — the same checks already gate forward navigation.
    const stepError = validateStep(1, stepState());
    if (stepError) {
      showStepError(1, stepError);
      return;
    }
    const changes = computeChanges();
    if (!changes) return;
    const payload = changes.payload;
    const noChange =
      worksAddedIsEmpty(changes) &&
      changes.mappingChanged.length === 0 &&
      changes.mediaChanges.length === 0 &&
      changes.singleEpChanges.length === 0 &&
      !changes.collectionChanged &&
      changes.scopeFrom === changes.scopeTo;
    if (noChange) {
      message.info(t('resource.noChanges'));
      onDone(null);
      return;
    }
    setSaving(true);
    try {
      const res = await resourcesApi.updateAssociations(resourceId, payload);
      if (!res.success) {
        // The 422 carries a machine-readable meta.step naming the step that
        // fixes the violation (collection/work mismatch → 0, file-assignment
        // coverage → 1); the message text match is only a fallback.
        const msg = res.error?.message || t('resource.correctSaveFailed');
        const metaStep = (res.meta as { step?: string } | undefined)?.step;
        const target = stepFromSaveError(metaStep, msg);
        setSaveError({ step: target, message: msg });
        setStep(target);
        return;
      }
      const warnings = res.data.warnings ?? [];
      if (warnings.length > 0) {
        message.warning(warnings.join('；'));
      }
      message.success(t('resource.correctSaved'));
      onDone(res.data);
    } finally {
      setSaving(false);
    }
  };

  // Secondary footer action: full background metadata reparse. The server
  // hides the resource from dashboard confirmations up front and clears the
  // flag when the job finishes, so the wizard stays open.
  const handleReparse = async () => {
    setReparsing(true);
    try {
      const res = await resourcesApi.reparseMetadata(resourceId);
      if (!res.success) {
        if (res.error?.code === 'ALREADY_RUNNING') {
          message.warning(t('resource.reparseRunning'));
        } else {
          message.error(res.error?.message || t('resource.reparseFailed'));
        }
        return;
      }
      message.success(t('resource.reparseTriggered'));
      onReparse?.();
    } finally {
      setReparsing(false);
    }
  };

  const searchCollections = async (q: string) => {
    setCollSearching(true);
    try {
      const res = await collectionsApi.list(1, 20, q || undefined);
      if (res.success) {
        setCollections(res.data.map((c) => ({ id: c.id, name: c.title_cn })));
      }
    } finally {
      setCollSearching(false);
    }
  };

  const createCollectionAndAttach = async () => {
    const title = newCollTitle.trim();
    if (!title) return;
    setCreatingColl(true);
    try {
      const res = await collectionsApi.create({ title_cn: title });
      if (!res.success) {
        message.error(res.error?.message || t('collections.createTitle'));
        return;
      }
      setCollections((prev) =>
        prev.some((c) => c.id === res.data.id)
          ? prev
          : [{ id: res.data.id, name: res.data.title_cn }, ...prev],
      );
      setCollectionId(res.data.id);
      setNewCollTitle('');
      message.success(t('collections.created'));
    } finally {
      setCreatingColl(false);
    }
  };

  return {
    // load state
    loading,
    detail,
    files,
    filesSource,
    filesRetrying,
    retryFetchFiles,
    // navigation
    step,
    setStep,
    handleNext,
    saving,
    reparsing,
    saveError,
    setSaveError,
    handleSave,
    handleReparse,
    // association (step 0)
    isBatch,
    setIsBatch,
    audioLinked,
    works,
    workTitles,
    workSeasons,
    setWorkSeasons,
    collectionId,
    setCollectionId,
    collections,
    collSearching,
    searchCollections,
    newCollTitle,
    setNewCollTitle,
    creatingColl,
    createCollectionAndAttach,
    deriveScopeLabel,
    addWork,
    removeWork,
    // single-episode fields (step 1, non-batch)
    epSeason,
    setEpSeason,
    epEpisode,
    setEpEpisode,
    epAbsolute,
    setEpAbsolute,
    // file mapping (step 1)
    checkedAssign,
    setCheckedAssign,
    batchEpStart,
    setBatchEpStart,
    placements,
    setPlacementField,
    unassignPaths,
    sortedEntriesFor,
    applyBatchEpisodeIncrement,
    selectedWorkKey,
    setSelectedWorkKey,
    checkedFiles,
    expandedClusters,
    toggleCluster,
    pickerCluster,
    setPickerCluster,
    joinSeason,
    setJoinSeason,
    joinChecked,
    applyClusterToWork,
    pickClusterWork,
    toggleFileChecked,
    beginPointerSelect,
    extendTouchSelect,
    extendDragSelect,
    detParses,
    poolFiles,
    clusterGroups,
    poolRows,
    clusterRangePreview,
    clusterBinding,
    // analysis
    analyzing,
    analysisStatus,
    analysisOutput,
    suggestion,
    analyze,
    // media fields (step 2)
    media,
    setMedia,
    mediaOptions,
    subtitleLangs,
    setSubtitleLangs,
    directMetadata,
    setDirectMetadata,
    // work picker
    pickerOpen,
    setPickerOpen,
    channelMetadataSource,
    channelFallbackSources,
    // confirmation (step 3)
    computeChanges,
  };
}

export type ResourceEditWizardState = ReturnType<typeof useResourceEditWizard>;
