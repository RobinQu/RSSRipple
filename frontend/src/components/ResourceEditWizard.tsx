import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  App,
  AutoComplete,
  Button,
  Checkbox,
  Grid,
  Divider,
  Empty,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Select,
  Space,
  Spin,
  Steps,
  Switch,
  Tag,
  Typography,
} from 'antd';
import { Plus, RefreshCw, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { collectionsApi } from '../api/collections';
import { moviesApi } from '../api/movies';
import { metadataApi } from '../api/metadata';
import { channelsApi, resourcesApi } from '../api/channels';
import { seriesApi } from '../api/series';
import { formatBytes } from '../utils/format';
import { DEFAULT_FALLBACK_SOURCES } from './channel-form/constants';
import SeasonInput from './SeasonInput';
import type {
  AssociationUpdatePayload,
  AssociationWorkRef,
  BatchSuggestion,
  FileResource,
  FileResourceDetail,
  MetadataCandidate,
  MetadataSource,
  ResourceFileItem,
  WorkRefType,
} from '../types';

const { Text } = Typography;

interface Placement {
  workType: WorkRefType;
  workId: string;
  season: number | null;
  epStart: number | null;
  epEnd: number | null;
}

type MediaFieldKey =
  | 'resolution'
  | 'subtitle_groups'
  | 'source'
  | 'video_codec'
  | 'audio_codec'
  | 'subtitle_type'
  | 'container';

type DirectMetadataFieldKey = 'title_cn' | 'title_en' | 'search_title';
const DIRECT_METADATA_FIELD_KEYS: DirectMetadataFieldKey[] = [
  'title_cn', 'title_en', 'search_title',
];

const MEDIA_TEXT_KEYS: MediaFieldKey[] = [
  'resolution',
  'source',
  'container',
  'video_codec',
  'audio_codec',
  'subtitle_type',
  'subtitle_groups',
];

const LANG_PRESETS = ['zh-CN', 'zh-TW', 'zh-HK', 'ja', 'en', 'ko', 'multi'];

function normTitle(s: string | null | undefined): string {
  return (s || '').toLowerCase().replace(/[\s·・]+/g, '');
}

function workKeyOf(wt: WorkRefType, wid: string): string {
  return `${wt}:${wid}`;
}

type ChangesShape = {
  payload: AssociationUpdatePayload;
  scopeFrom: string;
  scopeTo: string;
  worksAdded: string[];
  worksRemoved: string[];
  mappingChanged: { path: string; label: string }[];
  collectionChanged: { from: string; to: string } | null;
  singleEpChanges: { key: string; from: string; to: string }[];
  mediaChanges: { key: MediaFieldKey | DirectMetadataFieldKey | 'subtitle_langs'; from: string; to: string }[];
};

interface ResourceEditWizardProps {
  resourceId: string;
  initialStep?: number;
  /** Called once a save settles: updated resource on success, null when
   * nothing changed / closed without applicable changes. */
  onDone: (updated: FileResource | null) => void;
}

/** Five-step unified edit flow for a file resource:
 * ① works association (batch toggle + add/remove one-or-more works),
 * ② file mapping (left: selectable work list; right: shift-range multi-select
 *    files joined into the selected work with a season parameter, S/E
 *    prefilled from the deterministic name parses),
 * ③ collection association (search existing or create in place),
 * ④ generic media fields (dropdowns fed by system-observed values),
 * ⑤ confirmation review before the single PUT save. */
export default function ResourceEditWizard({
  resourceId,
  initialStep = 0,
  onDone,
}: ResourceEditWizardProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const screens = Grid.useBreakpoint();
  const compact = !screens.lg;

  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState<FileResourceDetail | null>(null);
  const [files, setFiles] = useState<ResourceFileItem[]>([]);
  const [step, setStep] = useState(0);
  const [saving, setSaving] = useState(false);

  const [isBatch, setIsBatch] = useState(false);
  const [works, setWorks] = useState<AssociationWorkRef[]>([]);
  const [workTitles, setWorkTitles] = useState<Record<string, string>>({});
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
  const lastCheckedIdxRef = useRef<number | null>(null);
  const [joinSeason, setJoinSeason] = useState<number | null>(1);
  // Mouse drag range-selection on the candidate list: press on a row to
  // anchor, hover more rows while held to extend, release to finish.
  const draggingRef = useRef(false);
  const dragAnchorIdxRef = useRef<number | null>(null);
  const dragModeRef = useRef<'add' | 'remove'>('add');
  const dragBaseRef = useRef<string[]>([]);

  // Collection step (search + create-in-place).
  const [collSearchQ, setCollSearchQ] = useState('');
  const [collSearching, setCollSearching] = useState(false);
  const [newCollTitle, setNewCollTitle] = useState('');
  const [creatingColl, setCreatingColl] = useState(false);

  const audioLinked = !!detail?.audio_work_id;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setDetail(null);
    setFiles([]);
    setSuggestion(null);
    setCheckedFiles([]);
    setSelectedWorkKey(null);
    setStep(initialStep);
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
      setCollectionId(d.collection_id);
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

  const addWork = (ref: AssociationWorkRef, title: string) => {
    const key = workKeyOf(ref.work_type, ref.work_id);
    if (works.some((w) => workKeyOf(w.work_type, w.work_id) === key)) {
      return;
    }
    if (!ref.work_type) return;
    setWorks((ws) => [...ws, ref]);
    setWorkTitles((prev) => ({ ...prev, [key]: title }));
    if (!selectedWorkKey) setSelectedWorkKey(key);
    worksDirtyRef.current = true;
  };

  const removeWork = (key: string) => {
    const idx = works.findIndex(
      (w) => workKeyOf(w.work_type, w.work_id) === key,
    );
    if (idx < 0) return;
    setWorks((ws) => ws.filter((_, i) => i !== idx));
    setPlacements((prev) => {
      const next: Record<string, Placement> = {};
      for (const [path, p] of Object.entries(prev)) {
        if (workKeyOf(p.workType, p.workId) !== key) next[path] = p;
      }
      return next;
    });
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
        const season = parsed?.season ?? joinSeason;
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

  const applyAnalysisSuggestion = (sug: BatchSuggestion) => {
    setSuggestion(sug);
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
      const target = works.find((knownWork) => (
        w.candidate_key === workKeyOf(knownWork.work_type, knownWork.work_id)
      )) ?? works.find((knownWork) => {
        const known = normTitle(workTitles[workKeyOf(knownWork.work_type, knownWork.work_id)]);
        const want = normTitle(w.title);
        return known === want || (!!known && (known.includes(want) || want.includes(known)));
      });
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
          if (event.type === 'result') finalSuggestion = event.suggestion ?? null;
        }
        if (done) break;
      }
      if (!finalSuggestion) {
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
    if (next === 3) {
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
        ? scopeLabelOf(detail.batch_scope)
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

  function scopeLabelOf(scope: string): string {
    switch (scope) {
      case 'multi_season': return t('channels.batchMultiSeason');
      case 'franchise': return t('channels.batchFranchise');
      case 'movies': return t('channels.batchMovies');
      default: return t('channels.batch');
    }
  }

  const handleSave = async () => {
    if (!detail) return;
    if (isBatch && !audioLinked) {
      const missing = Object.values(placements).filter(
        (p) => p.workType === 'series' && p.season == null,
      ).length;
      if (missing > 0) {
        message.error(t('resource.tvSeasonRequired'));
        setStep(1);
        return;
      }
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
        message.error(res.error?.message || t('resource.correctSaveFailed'));
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

  const panelStyle = (active: boolean) => ({
    display: active ? undefined : ('none' as const),
  });

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: 48 }}>
        <Spin />
      </div>
    );
  }
  if (!detail) {
    return <Empty description={t('resource.loadFailed')} />;
  }

  const stepsItems = [
    { title: t('resource.wizardStepWorks') },
    { title: t('resource.wizardStepFiles') },
    { title: t('resource.wizardStepCollection') },
    { title: t('resource.wizardStepMedia') },
    { title: t('resource.wizardStepConfirm') },
  ];

  const changes = step === 4 ? computeChanges() : null;

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div
        style={{
          flexShrink: 0,
          margin: '0 4px 8px',
          padding: '8px 10px',
          border: '1px solid var(--rr-border-soft)',
          borderRadius: 6,
          background: 'var(--rr-surface-card)',
        }}
      >
        <Text ellipsis={{ tooltip: detail.title_raw }} style={{ display: 'block' }}>
          {detail.title_raw}
        </Text>
        <Text type="secondary" style={{ fontFamily: 'monospace', fontSize: 11 }}>
          {t('resource.resourceId')}：{detail.id}
        </Text>
      </div>
      <div style={{ flexShrink: 0, padding: '8px 4px 16px' }}>
        <Steps size="small" responsive current={step} items={stepsItems} />
      </div>
      <div style={{ flex: 1, minHeight: 0, overflow: step === 1 ? 'hidden' : 'auto', padding: '4px 4px 12px' }}>

      {/* Step 0 — works association */}
      <div style={panelStyle(step === 0)}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
          <Text>{t('resource.isBatch')}</Text>
          <Switch
            checked={isBatch}
            onChange={(v) => setIsBatch(v)}
            disabled={audioLinked}
          />
        </div>
        <div style={{ marginBottom: 6 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>{t('resource.stepWorkType')}</Text>
        </div>
        <Space wrap style={{ marginBottom: 8 }}>
          {works.map((w) => {
            const key = workKeyOf(w.work_type, w.work_id);
            return (
              <Tag
                key={key}
                color={w.work_type === 'series' ? 'blue' : 'green'}
                closable={!audioLinked}
                onClose={() => removeWork(key)}
              >
                {workTitles[key] || w.work_id}
              </Tag>
            );
          })}
          <Button
            size="small"
            icon={<Plus size={13} />}
            disabled={audioLinked}
            onClick={() => setPickerOpen(true)}
          >
            {t('resource.addWork')}
          </Button>
        </Space>
        {audioLinked && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 8 }}
            message={t('resource.audioLinkHint')}
          />
        )}
        {isBatch && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t('resource.scopeAutoPreview', { scope: deriveScopeLabel })}
          </Text>
        )}
      </div>

      {/* Step 1 — file mapping (left: works, right: files) */}
      <div style={{ ...panelStyle(step === 1), height: '100%', minHeight: 0 }}>
        {audioLinked ? (
          <Alert type="info" showIcon message={t('resource.audioLinkHint')} />
        ) : !isBatch ? (
          <>
            {(!works.length || works[0]?.work_type === 'series') ? (
              <>
                <LabeledRow label={t('resource.seasonLabel')}>
                  <SeasonInput value={epSeason} onChange={setEpSeason} style={{ width: '100%' }} />
                </LabeledRow>
                <LabeledRow label={t('resource.episodePerSeasonLabel')}>
                  <InputNumber min={0} value={epEpisode} onChange={(v) => setEpEpisode(typeof v === 'number' ? v : null)} style={{ width: '100%' }} />
                </LabeledRow>
                <LabeledRow label={t('resource.absoluteEpisodePlaceholder')}>
                  <InputNumber min={0} value={epAbsolute} onChange={(v) => setEpAbsolute(typeof v === 'number' ? v : null)} style={{ width: '100%' }} />
                </LabeledRow>
              </>
            ) : (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t('resource.noEpisodeFields')}
              </Text>
            )}
          </>
        ) : files.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('resource.noListingHint')} />
        ) : analyzing ? (
          <div style={{ minHeight: 420, display: 'flex', flexDirection: 'column', justifyContent: 'center', padding: 24 }}>
            <Spin size="large" />
            <Text strong style={{ textAlign: 'center', marginTop: 16 }}>{analysisStatus}</Text>
            <pre style={{ marginTop: 16, maxHeight: 360, overflow: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', padding: 16, borderRadius: 8, background: 'var(--rr-surface-card)', fontSize: 12 }}>
              {analysisOutput || t('resource.analysisWaiting')}
            </pre>
          </div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: compact ? '1fr' : 'minmax(300px, 38%) minmax(0, 1fr)', gap: 12, height: '100%', minHeight: 0, overflow: compact ? 'auto' : 'hidden' }}>
            {/* Left: works */}
            <div style={{ border: '1px solid var(--rr-border-soft)', borderRadius: 8, padding: 8 }}>
              <Text strong style={{ fontSize: 12 }}>{t('resource.stepWorkType')}</Text>
              <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 4 }}>
                {works.map((w) => {
                  const key = workKeyOf(w.work_type, w.work_id);
                  const entries = Object.entries(placements).filter(
                    ([, p]) => workKeyOf(p.workType, p.workId) === key,
                  );
                  const count = entries.length;
                  const active = selectedWorkKey === key;
                  return (
                    <div key={key}>
                      <div
                        onClick={() => setSelectedWorkKey(active ? null : key)}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 6,
                          padding: '5px 7px',
                          borderRadius: 6,
                          cursor: 'pointer',
                          border: `1px solid ${active ? 'var(--rr-primary)' : 'var(--rr-border-soft)'}`,
                          background: active ? 'var(--rr-primary-soft)' : 'transparent',
                        }}
                      >
                        <span style={{ flex: 1, minWidth: 0 }}>
                          <Text ellipsis style={{ fontSize: 12, display: 'block' }}>
                            {workTitles[key] || w.work_id}
                          </Text>
                          <Text type="secondary" style={{ fontSize: 11 }}>
                            {w.work_type === 'series' ? t('works.tv') : t('works.movie')} · {count}
                          </Text>
                        </span>
                        <Button
                          size="small"
                          type="text"
                          icon={<X size={12} />}
                          onClick={(e) => {
                            e.stopPropagation();
                            removeWork(key);
                          }}
                        />
                      </div>
                      {active && w.work_type === 'series' && entries.length > 0 && (
                        <div style={{ padding: '4px 2px 2px 10px' }}>
                          <div
                            style={{
                              display: 'flex',
                              gap: 4,
                              alignItems: 'center',
                              borderBottom: '1px solid var(--rr-border)',
                              paddingBottom: 2,
                              marginBottom: 2,
                            }}
                          >
                            <Text type="secondary" style={{ fontSize: 10, flex: 1 }}>{t('resource.fileColName')}</Text>
                            <Text type="secondary" style={{ fontSize: 10, width: 52, textAlign: 'center' }}>{t('resource.seasonLabel')}</Text>
                            <Text type="secondary" style={{ fontSize: 10, width: 52, textAlign: 'center' }}>{t('resource.epStartCol')}</Text>
                            <Text type="secondary" style={{ fontSize: 10, width: 52, textAlign: 'center' }}>{t('resource.epEndCol')}</Text>
                            <span style={{ width: 22 }} />
                          </div>
                          <div style={{ maxHeight: compact ? 220 : 300, overflowY: 'auto' }}>
                            {entries.map(([path, p]) => (
                              <div key={path} style={{ display: 'flex', gap: 4, alignItems: 'center', padding: '1px 0' }}>
                                <Text ellipsis title={path} style={{ fontSize: 11, flex: 1, minWidth: 0 }}>
                                  {path.split('/').pop()}
                                </Text>
                                <SeasonInput size="small" value={p.season} onChange={(v) => setPlacementField(path, { season: v })} style={{ width: 52 }} />
                                <InputNumber size="small" min={0} value={p.epStart} onChange={(v) => setPlacementField(path, { epStart: typeof v === 'number' ? v : null })} style={{ width: 52 }} controls={false} />
                                <InputNumber size="small" min={0} value={p.epEnd} onChange={(v) => setPlacementField(path, { epEnd: typeof v === 'number' ? v : null })} style={{ width: 52 }} controls={false} />
                                <Button size="small" type="text" icon={<X size={11} />} onClick={() => unassignPaths([path])} />
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
                <Button
                  size="small"
                  icon={<Plus size={13} />}
                  onClick={() => setPickerOpen(true)}
                >
                  {t('resource.addWork')}
                </Button>
              </div>
            </div>

            {/* Right: unassigned candidate files only — assigned files live
                under the selected work on the left. */}
            <div style={{ border: '1px solid var(--rr-border-soft)', borderRadius: 8, padding: 8, minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center', marginBottom: 6 }}>
                <Button
                  size="small"
                  icon={<RefreshCw size={12} />}
                  loading={analyzing}
                  onClick={() => void analyze(true)}
                >
                  {t('resource.reanalyze')}
                </Button>
                <Divider type="vertical" />
                <SeasonInput
                  size="small"
                  value={joinSeason}
                  onChange={setJoinSeason}
                  addonBefore={t('resource.seasonLabel')}
                  style={{ width: 130 }}
                />
                <Button
                  size="small"
                  type="primary"
                  disabled={!selectedWorkKey || checkedFiles.length === 0}
                  onClick={joinChecked}
                >
                  {t('resource.joinToWork')}（{checkedFiles.length}）
                </Button>
              </div>
              {suggestion && (
                <div style={{ marginBottom: 4 }}>
                  <Text type="success" style={{ fontSize: 11 }}>
                    {t('resource.analyzeSummary', {
                      parsed: suggestion.deterministic.files.filter((f) => f.episode != null).length,
                      total: suggestion.deterministic.files.length,
                    })}
                    {suggestion.works.length > 0 ? ` · ${t('resource.llmSuggestionReady')}` : ''}
                  </Text>
                </div>
              )}
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '2px 4px', borderBottom: '1px solid var(--rr-border)' }}>
                <Text type="secondary" style={{ fontSize: 11, flex: 1 }}>{t('resource.fileColName')}</Text>
                <Text type="secondary" style={{ fontSize: 11, width: 70, flexShrink: 0 }}>{t('resource.parsedSE')}</Text>
                <Text type="secondary" style={{ fontSize: 11, width: 64, flexShrink: 0, textAlign: 'right' }}>{t('resource.fileColSize')}</Text>
              </div>
              <div
                style={{ flex: compact ? undefined : 1, minHeight: 0, maxHeight: compact ? 360 : undefined, overflowY: 'auto', userSelect: 'none', touchAction: 'none' }}
                onPointerMove={extendTouchSelect}
              >
                {poolFiles.length === 0 ? (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('resource.allMappedHint')} style={{ margin: 12 }} />
                ) : (
                  poolFiles.map((f, idx) => {
                    const checked = checkedFiles.includes(f.name);
                    const parsed = detParses[f.name];
                    return (
                      <div
                        key={f.name}
                        data-file-index={idx}
                        onPointerDown={(e) => {
                          beginPointerSelect(e, f.name, idx);
                        }}
                        onPointerEnter={() => extendDragSelect(idx)}
                        onClick={(e) => {
                          // Keyboard/tap fallback without drag semantics.
                          if (e.detail === 0) toggleFileChecked(f.name, idx, e.shiftKey);
                        }}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 8,
                          padding: '3px 4px',
                          borderBottom: '1px solid var(--rr-border-soft)',
                          background: checked ? 'var(--rr-primary-soft)' : undefined,
                          cursor: 'pointer',
                        }}
                      >
                        <Checkbox checked={checked} tabIndex={-1} />
                        <span style={{ flex: 1, minWidth: 0, fontSize: 12, overflowWrap: 'anywhere' }}>
                          {f.name}
                        </span>
                        {parsed && (parsed.season != null || parsed.episode != null) && (
                          <Tag style={{ fontSize: 10, margin: 0, width: 70, textAlign: 'center' }} color="default">
                            {parsed.season != null ? `S${parsed.season}` : ''}
                            {parsed.episode != null ? ` E${parsed.episode}` : ''}
                          </Tag>
                        )}
                        <Text type="secondary" style={{ fontSize: 11, flexShrink: 0, width: 64, textAlign: 'right' }}>
                          {formatBytes(f.size)}
                        </Text>
                      </div>
                    );
                  })
                )}
              </div>
              <div style={{ marginTop: 4 }}>
                <Text type="secondary" style={{ fontSize: 11 }}>{t('resource.dragSelectHint')}</Text>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Step 2 — collection association */}
      <div style={panelStyle(step === 2)}>
        {!isBatch ? (
          <Alert type="info" showIcon message={t('resource.collectionOnlyBatch')} />
        ) : (
          <>
            <div style={{ marginBottom: 8 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>{t('resource.collectionSearchLabel')}</Text>
              <Select
                showSearch
                allowClear
                style={{ width: '100%', marginTop: 4 }}
                placeholder={t('resource.collectionLabel')}
                value={collectionId ?? undefined}
                onSearch={(q) => {
                  setCollSearchQ(q);
                  void (async () => {
                    setCollSearching(true);
                    try {
                      const res = await collectionsApi.list(1, 20, q || undefined);
                      if (res.success) {
                        setCollections(res.data.map((c) => ({ id: c.id, name: c.title_cn })));
                      }
                    } finally {
                      setCollSearching(false);
                    }
                  })();
                }}
                filterOption={false}
                loading={collSearching || collSearchQ.length > 0 === false ? collSearching : collSearching}
                notFoundContent={collSearching ? <Spin size="small" /> : null}
                onChange={(v) => setCollectionId(v ?? null)}
                options={collections.map((c) => ({ value: c.id, label: c.name }))}
              />
            </div>
            <Divider plain style={{ margin: '4px 0 12px' }}>{t('resource.or')}</Divider>
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>{t('resource.collectionCreateLabel')}</Text>
              <Space.Compact style={{ width: '100%', marginTop: 4 }}>
                <Input
                  value={newCollTitle}
                  onChange={(e) => setNewCollTitle(e.target.value)}
                  placeholder={t('resource.collectionCreatePlaceholder')}
                  onPressEnter={() => void createCollectionAndAttach()}
                />
                <Button
                  type="primary"
                  loading={creatingColl}
                  disabled={!newCollTitle.trim()}
                  onClick={() => void createCollectionAndAttach()}
                >
                  {t('resource.collectionCreateBtn')}
                </Button>
              </Space.Compact>
            </div>
          </>
        )}
      </div>

      {/* Step 3 — generic media fields */}
      <div style={panelStyle(step === 3)}>
        {DIRECT_METADATA_FIELD_KEYS.filter((k) =>
          (detail.missing_fields ?? []).includes(k),
        ).map((k) => (
          <div key={k} style={{ marginBottom: 12 }}>
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
              {t(`filters.${k}`)}
            </Text>
            <Input
              value={directMetadata[k]}
              onChange={(event) => setDirectMetadata((prev) => ({
                ...prev, [k]: event.target.value,
              }))}
              status={directMetadata[k].trim() ? undefined : 'error'}
            />
          </div>
        ))}
        <div style={{ display: 'grid', gridTemplateColumns: compact ? '1fr' : '1fr 1fr', gap: 12 }}>
          {MEDIA_TEXT_KEYS.map((k) => (
            <div key={k}>
              <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                {t(`resource.${mediaLabelKey(k)}`)}
              </Text>
              <AutoComplete
                style={{ width: '100%' }}
                value={media[k]}
                onChange={(v) => setMedia((prev) => ({ ...prev, [k]: v }))}
                options={(mediaOptions[k] ?? []).map((v) => ({ value: v }))}
                filterOption={(input, opt) =>
                  String(opt?.value ?? '')
                    .toLowerCase()
                    .includes(input.toLowerCase())
                }
                allowClear
              />
            </div>
          ))}
          <div>
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
              {t('resource.subtitleLangs')}
            </Text>
            <Select
              mode="tags"
              style={{ width: '100%' }}
              value={subtitleLangs}
              onChange={setSubtitleLangs}
              options={LANG_PRESETS.map((l) => ({ value: l, label: l === 'multi' ? t('channels.langMulti') : l }))}
              tokenSeparators={[',']}
            />
          </div>
        </div>
      </div>

      {/* Step 4 — confirmation review */}
      <div style={panelStyle(step === 4)}>
        {!changes ? (
          <Spin />
        ) : (
          <ReviewRows changes={changes} />
        )}
      </div>

      </div>

      <Space size={8} style={{ display: 'flex', justifyContent: 'flex-end', flexShrink: 0, padding: '12px 4px 4px', borderTop: '1px solid var(--rr-border-soft)' }}>
        {step > 0 && <Button disabled={analyzing} onClick={() => setStep((s) => s - 1)}>{t('resource.prevStep')}</Button>}
        {step < 4 && (
          <Button type="primary" disabled={analyzing} onClick={() => maybeAutoAnalyze(step + 1)}>
            {t('resource.nextStep')}
          </Button>
        )}
        {step === 4 && (
          <Button type="primary" loading={saving} onClick={() => void handleSave()}>
            {t('common.confirm')}
          </Button>
        )}
      </Space>

      <WorkPickerModal
        open={pickerOpen}
        existingKeys={new Set(works.map((w) => workKeyOf(w.work_type, w.work_id)))}
        defaultMetadataSource={channelMetadataSource}
        defaultFallbackSources={channelFallbackSources}
        onClose={() => setPickerOpen(false)}
        onPick={(ref, title) => {
          addWork(ref, title);
          setPickerOpen(false);
        }}
      />
    </div>
  );

  async function createCollectionAndAttach() {
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
  }
}

function worksAddedIsEmpty(c: { worksAdded: string[]; worksRemoved: string[] }): boolean {
  return c.worksAdded.length === 0 && c.worksRemoved.length === 0;
}

function LabeledRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
        {label}
      </Text>
      {children}
    </div>
  );
}

function ReviewRows({ changes }: { changes: ChangesShape }) {
  const { t } = useTranslation();
  const hasAny =
    changes.scopeFrom !== changes.scopeTo ||
    changes.worksAdded.length > 0 ||
    changes.worksRemoved.length > 0 ||
    changes.mappingChanged.length > 0 ||
    !!changes.collectionChanged ||
    changes.singleEpChanges.length > 0 ||
    changes.mediaChanges.length > 0;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {changes.scopeFrom !== changes.scopeTo && (
        <ReviewSection title={t('resource.confirmScope')}>
          <Text style={{ fontSize: 12 }}>
            {changes.scopeFrom} → {changes.scopeTo}
          </Text>
        </ReviewSection>
      )}

      {(changes.worksAdded.length > 0 || changes.worksRemoved.length > 0) && (
        <ReviewSection title={t('resource.confirmWorks')}>
          <Space direction="vertical" size={2}>
            {changes.worksAdded.map((w) => (
              <Text key={`a-${w}`} style={{ fontSize: 12 }} type="success">
                + {w}
              </Text>
            ))}
            {changes.worksRemoved.map((w) => (
              <Text key={`r-${w}`} style={{ fontSize: 12 }} type="danger">
                − {w}
              </Text>
            ))}
          </Space>
        </ReviewSection>
      )}

      {changes.collectionChanged && (
        <ReviewSection title={t('resource.collectionLabel')}>
          <Text style={{ fontSize: 12 }}>
            {changes.collectionChanged.from} → {changes.collectionChanged.to}
          </Text>
        </ReviewSection>
      )}

      {changes.mappingChanged.length > 0 && (
        <ReviewSection title={`${t('resource.confirmMappings')}（${changes.mappingChanged.length}）`}>
          <div>
            {changes.mappingChanged.map((m) => (
              <div key={m.path} style={{ fontSize: 12, padding: '2px 0', overflowWrap: 'anywhere' }}>
                <Text type="secondary">{m.path}</Text>
                <br />
                <Text strong>{m.label}</Text>
              </div>
            ))}
          </div>
        </ReviewSection>
      )}

      {changes.singleEpChanges.length > 0 && (
        <ReviewSection title={t('resource.confirmSingleEp')}>
          {changes.singleEpChanges.map((c) => (
            <div key={c.key} style={{ fontSize: 12 }}>
              {c.key}: {c.from} → {c.to}
            </div>
          ))}
        </ReviewSection>
      )}

      {changes.mediaChanges.length > 0 && (
        <ReviewSection title={t('resource.confirmMedia')}>
          {changes.mediaChanges.map((c) => (
            <div key={c.key} style={{ fontSize: 12 }}>
              {t(`resource.${mediaLabelKey(c.key as MediaFieldKey)}`)}: {c.from} → {c.to}
            </div>
          ))}
        </ReviewSection>
      )}

      {!hasAny && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t('resource.noChanges')}
        </Text>
      )}
    </div>
  );
}

function ReviewSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ border: '1px solid var(--rr-border-soft)', borderRadius: 8, padding: '8px 12px' }}>
      <Text strong style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>{title}</Text>
      {children}
    </div>
  );
}

function mediaLabelKey(k: MediaFieldKey | string): string {
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

function WorkPickerModal({
  open,
  existingKeys,
  defaultMetadataSource,
  defaultFallbackSources,
  onClose,
  onPick,
}: {
  open: boolean;
  existingKeys: Set<string>;
  defaultMetadataSource: MetadataSource;
  defaultFallbackSources: string[];
  onClose: () => void;
  onPick: (ref: AssociationWorkRef, title: string) => void;
}) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [mode, setMode] = useState<'library' | 'online'>('library');
  const [kind, setKind] = useState<'tv' | 'movie'>('tv');
  const [metaType, setMetaType] = useState<'tv' | 'movie'>('tv');
  const [metadataSource, setMetadataSource] = useState<MetadataSource>(defaultMetadataSource);
  const [fallbackSources, setFallbackSources] = useState<string[]>(defaultFallbackSources);
  const [q, setQ] = useState('');
  const [searching, setSearching] = useState(false);
  const [libResults, setLibResults] = useState<{ id: string; title: string; year: string | null }[]>([]);
  const [metaResults, setMetaResults] = useState<MetadataCandidate[]>([]);

  useEffect(() => {
    if (!open) return;
    setMetadataSource(defaultMetadataSource);
    setFallbackSources(defaultFallbackSources);
  }, [open, defaultMetadataSource, defaultFallbackSources]);

  const searchLibrary = async (query?: string) => {
    setSearching(true);
    try {
      const fn = kind === 'tv' ? seriesApi.list : moviesApi.list;
      const res = await fn(1, 10, query || undefined);
      if (res.success) {
        setLibResults(
          res.data.map((row) => ({
            id: row.id,
            title: row.original_title || row.title_cn || row.title_en || row.id,
            year:
              ((row as { start_date?: string | null }).start_date ||
                (row as { release_date?: string | null }).release_date ||
                '')?.slice(0, 4) || null,
          })),
        );
      }
    } finally {
      setSearching(false);
    }
  };

  const searchOnline = async () => {
    if (!q.trim()) {
      message.warning(t('metadata.enterSearch'));
      return;
    }
    setSearching(true);
    try {
      const res = await metadataApi.search({
        query: q.trim(),
        content_type: metaType,
        mode: 'online',
        source: metadataSource,
        trusted_sites: fallbackSources,
      });
      setMetaResults(res.success ? res.data.candidates : []);
      if (!res.success) message.error(res.error?.message || t('metadata.searchFailed'));
    } finally {
      setSearching(false);
    }
  };

  const pickOnline = async (r: MetadataCandidate) => {
    if (!r.selectable) return;
    const clientKey = `candidate:${crypto.randomUUID()}`;
    onPick(
      {
        work_type: metaType === 'tv' ? 'series' : 'movie',
        work_id: clientKey,
        client_key: clientKey,
        candidate: r,
      },
      r.title_cn || r.original_title || r.title_en || r.external_id || clientKey,
    );
  };

  return (
    <Modal
      open={open}
      title={t('resource.pickWorkTitle')}
      footer={null}
      onCancel={onClose}
      destroyOnHidden
      width={760}
    >
      <Segmented
        block
        value={mode}
        onChange={(v) => setMode(v as 'library' | 'online')}
        options={[
          { value: 'library', label: t('resource.pickFromLibrary') },
          { value: 'online', label: t('resource.pickOnline') },
        ]}
        style={{ marginBottom: 12 }}
      />
      {mode === 'library' ? (
        <>
          <Space.Compact style={{ width: '100%', marginBottom: 10 }}>
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t('works.searchPlaceholder')}
              onPressEnter={() => void searchLibrary(q)}
            />
            <Button type="primary" loading={searching} onClick={() => void searchLibrary(q)}>
              {t('common.search')}
            </Button>
          </Space.Compact>
          <Segmented
            value={kind}
            onChange={(v) => { setKind(v as 'tv' | 'movie'); setLibResults([]); }}
            options={[
              { value: 'tv', label: t('works.tv') },
              { value: 'movie', label: t('works.movie') },
            ]}
            style={{ marginBottom: 10 }}
          />
          <div style={{ maxHeight: 320, overflowY: 'auto' }}>
            {libResults.map((row) => {
              const key = workKeyOf(kind === 'tv' ? 'series' : 'movie', row.id);
              return (
                <div
                  key={row.id}
                  style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 4px', borderBottom: '1px solid var(--rr-border-soft)' }}
                >
                  <Space size={6}>
                    <Text style={{ fontSize: 13 }}>{row.title}</Text>
                    {row.year && <Text type="secondary" style={{ fontSize: 12 }}>{row.year}</Text>}
                  </Space>
                  <Button size="small" type="primary" disabled={existingKeys.has(key)} onClick={() => onPick({ work_type: kind === 'tv' ? 'series' : 'movie', work_id: row.id }, row.title)}>
                    {existingKeys.has(key) ? t('resource.workPicked') : t('works.select')}
                  </Button>
                </div>
              );
            })}
            {!searching && libResults.length === 0 && (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('common.noResults')} />
            )}
          </div>
        </>
      ) : (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(180px, 1fr) minmax(280px, 2fr)', gap: 10, marginBottom: 10 }}>
            <Select
              value={metadataSource}
              onChange={(value) => setMetadataSource(value as MetadataSource)}
              options={(['wikipedia', 'tmdb', 'bangumi'] as MetadataSource[]).map((value) => ({
                value,
                label: t(`channels.sources.${value}`),
              }))}
            />
            <Select
              mode="multiple"
              value={fallbackSources}
              onChange={setFallbackSources}
              placeholder={t('channels.metadataFallbackPlaceholder')}
              options={DEFAULT_FALLBACK_SOURCES.map((value) => ({
                value,
                label: t(`channels.sources.${value}`),
              }))}
            />
          </div>
          <Space.Compact style={{ width: '100%', marginBottom: 10 }}>
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t('metadata.searchPlaceholder')}
              onPressEnter={() => void searchOnline()}
            />
            <Select
              value={metaType}
              onChange={(v) => setMetaType(v as 'tv' | 'movie')}
              style={{ width: 100 }}
              options={[
                { value: 'tv', label: t('works.tv') },
                { value: 'movie', label: t('works.movie') },
              ]}
            />
            <Button type="primary" loading={searching} onClick={() => void searchOnline()}>
              {t('common.search')}
            </Button>
          </Space.Compact>
          <div style={{ maxHeight: 320, overflowY: 'auto' }}>
            {metaResults.map((r, idx) => (
              <div
                key={idx}
                style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, padding: '6px 4px', borderBottom: '1px solid var(--rr-border-soft)' }}
              >
                <Space size={6}>
                  <Text style={{ fontSize: 13 }}>{r.title_cn || r.original_title || r.title_en}</Text>
                  {r.year && <Text type="secondary" style={{ fontSize: 12 }}>{r.year}</Text>}
                </Space>
                <Button size="small" type="primary" disabled={!r.selectable} onClick={() => void pickOnline(r)}>
                  {t('metadata.confirmSelection')}
                </Button>
              </div>
            ))}
            {!searching && metaResults.length === 0 && (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('metadata.noResults')} />
            )}
          </div>
        </>
      )}
    </Modal>
  );
}
