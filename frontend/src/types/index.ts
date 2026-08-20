// Common
export interface APIResponse<T> {
  success: boolean;
  data: T;
  error: { code: string; message: string; stack?: string; details?: unknown } | null;
  meta?: { page: number; page_size: number; total: number };
}

// Channel
export type ChannelStatus = 'active' | 'inactive' | 'error';
// Channel primary metadata source (two-source architecture). The wider
// MetadataSource union remains for legacy manual-search paths.
export type ChannelMetadataSource = 'wikipedia' | 'tmdb';
export type MetadataSource = 'exa' | 'jina' | 'wikipedia' | 'tmdb' | 'local';
export interface Channel {
  id: string;
  name: string;
  type: 'rss_feed';
  url: string;
  fetch_interval: number;
  status: ChannelStatus;
  field_mapping: FieldMapping;
  metadata_agent_enabled: boolean;
  metadata_source: ChannelMetadataSource | null;
  // Ordered Exa-fallback site whitelist; null = default order, [] = disabled.
  metadata_fallback_sources: string[] | null;
  auto_cleanup_unresolved_enabled: boolean;
  auto_cleanup_unresolved_days: number;
  // Default is_anime flag for works matched from this channel; immutable after creation.
  default_is_anime: boolean;
  last_fetched_at: string | null;
  last_fetch_status: string | null;
  last_fetch_error: string | null;
  created_at: string;
  updated_at: string;
}

// External-identity display link on work detail pages (computed server-side
// by the site registry).
export interface SourceLink {
  source: string;
  label: string;
  url: string;
}

export interface FieldMapping {
  list_locator?: { source: string };
  field_mappings?: Record<string, FieldMappingRule>;
}

export interface FieldMappingRule {
  source?: string;
  regex?: string;
  group?: number;
  transform?: string;
}

export interface ChannelDetail extends Channel {
  recent_resources?: FileResource[];
  resource_count?: number;
  agent_count?: number;
}

// Linked work summary embedded in FileResource payloads (flat resources API
// serializes the loaded series/movie relationships).
export interface ResourceWorkRef {
  id: string;
  title_cn?: string | null;
  title_en?: string | null;
  original_title?: string | null;
  poster_url?: string | null;
  genre?: string[] | null;
  status?: string | null;
  is_anime?: boolean | null;
  rating?: number | null;
  number_of_seasons?: number | null;
  number_of_episodes?: number | null;
  start_date?: string | null;
  release_date?: string | null;
  description?: string | null;
}

// FileResource
export interface FileResource {
  id: string;
  channel_id: string;
  guid: string;
  title_raw: string;
  title_cn: string | null;
  title_en: string | null;
  search_title: string | null;
  subtitle_group: string | null;
  episode: number | null;
  season: number | null;
  is_batch: boolean;
  // Torrent content detection (P1): batch scope sub-classification
  // (null = single-episode resource or a legacy pre-scope batch row) and the
  // franchise-pack collection link.
  batch_scope: 'season' | 'multi_season' | 'franchise' | null;
  collection_id: string | null;
  collection_name: string | null;
  episode_start: number | null;
  episode_end: number | null;
  absolute_episode: number | null;
  episode_confidence: 'raw' | 'reconciled' | 'ambiguous' | 'manual' | null;
  resolution: string | null;
  source: string | null;
  video_codec: string | null;
  audio_codec: string | null;
  subtitle_type: string | null;
  subtitle_langs: string[] | null;
  container: string | null;
  file_size: number | null;
  torrent_url: string;
  detail_url: string | null;
  published_at: string | null;
  parsed_at: string | null;
  series_id: string | null;
  movie_id: string | null;
  series?: ResourceWorkRef | null;
  movie?: ResourceWorkRef | null;
  metadata_matched_at: string | null;
  created_at: string;
}

export interface GroupedResource {
  type: 'series' | 'movie' | 'unknown';
  id: string | null;
  title: string;
  poster_url: string | null;
  resources: FileResource[];
  episode_count?: number;
  last_update?: string | null;
}

// GET /resources/{id}/files — torrent/download file listing for a resource.
// source="none" means no listing exists (magnet without metadata, not
// downloaded); files is empty in that case.
export interface ResourceFileItem {
  name: string;
  size: number;
}

export type ResourceFilesSource =
  | 'torrent_cache'
  | 'torrent_fetch'
  | 'downloader'
  | 'notification'
  | 'none';

export interface ResourceFilesResponse {
  files: ResourceFileItem[];
  source: ResourceFilesSource;
}

// PATCH /resources/{id} — manual correction of the parsed episode/batch
// fields. Every key is optional; only explicitly sent keys are updated.
export interface ResourceCorrectionBody {
  episode?: number | null;
  season?: number | null;
  absolute_episode?: number | null;
  episode_start?: number | null;
  episode_end?: number | null;
  is_batch?: boolean;
  batch_scope?: 'season' | 'multi_season' | 'franchise' | null;
}

// TV Series
export interface CollectionSummary {
  id: string;
  name: string | null;
}

export interface CollectionSibling {
  id: string;
  title: string | null;
  year: number | null;
  type: 'series' | 'movie';
}

// WorkCollection — franchise grouping (browse/manage in the /works 合集 view)
export interface WorkCollection {
  id: string;
  title_cn: string;
  title_en: string | null;
  external_id: string | null;
  external_source: string | null;
  poster_url: string | null;
  description: string | null;
  created_at: string;
  updated_at: string;
  // List-only field
  work_count?: number;
  // Detail-only fields
  works?: CollectionWork[];
  untracked_parts?: CollectionPart[];
}

export interface CollectionWork {
  id: string;
  title: string | null;
  year: number | null;
  type: 'series' | 'movie';
}

export interface CollectionPart {
  tmdb_id: string;
  title: string | null;
  year: number | null;
  poster_url: string | null;
}

export interface TVSeries {
  id: string;
  title_cn: string | null;
  title_en: string | null;
  original_title: string | null;
  aliases: string[] | null;
  external_id: string | null;
  external_source: string | null;
  canonical_name?: string | null;
  wikipedia_url?: string | null;
  description: string | null;
  poster_url: string | null;
  rating: number | null;
  genre: string[] | null;
  status: string | null;
  number_of_episodes: number | null;
  number_of_seasons: number | null;
  // Per-season episode counts — only present where the backend injects them
  // (resource metadata endpoint), used to prefill season from an absolute
  // episode number in the correction popover.
  seasons?: { season_number: number; episode_count: number }[] | null;
  start_date: string | null;
  end_date: string | null;
  content_type: string | null;
  // Tri-state: true = Japanese animation, false = confirmed live-action, null = undetermined
  is_anime?: boolean | null;
  // Fields the user edited manually via the edit form; auto scans skip these.
  manually_edited_fields?: string[] | null;
  collection_id?: string | null;
  created_at: string;
  updated_at: string;
  // Detail-only fields
  episodes?: Episode[];
  resources?: FileResource[];
  resource_count?: number;
  task_count?: number;
  agent_work_count?: number;
  collection?: CollectionSummary | null;
  collection_siblings?: CollectionSibling[];
  source_links?: SourceLink[];
}

// Movie
export interface Movie {
  id: string;
  title_cn: string | null;
  title_en: string | null;
  original_title: string | null;
  aliases: string[] | null;
  external_id: string | null;
  external_source: string | null;
  canonical_name?: string | null;
  wikipedia_url?: string | null;
  description: string | null;
  poster_url: string | null;
  rating: number | null;
  genre: string[] | null;
  status: string | null;
  release_date: string | null;
  runtime: number | null;
  content_type: string | null;
  // Tri-state: true = Japanese animation, false = confirmed live-action, null = undetermined
  is_anime?: boolean | null;
  // Fields the user edited manually via the edit form; auto scans skip these.
  manually_edited_fields?: string[] | null;
  collection_id?: string | null;
  created_at: string;
  updated_at: string;
  // Detail-only fields
  resources?: FileResource[];
  resource_count?: number;
  task_count?: number;
  agent_work_count?: number;
  collection?: CollectionSummary | null;
  collection_siblings?: CollectionSibling[];
  source_links?: SourceLink[];
}

// Unified Work (TVSeries | Movie | AudioWork) for repository view
export type AudioContentType = 'asmr' | 'music' | 'drama_cd' | 'radio' | 'other';
export type WorkContentType = 'tv' | 'movie' | AudioContentType | null;

export interface Work {
  id: string;
  title_cn: string | null;
  title_en: string | null;
  original_title: string | null;
  poster_url: string | null;
  rating: number | null;
  status: string | null;
  content_type: WorkContentType;
  number_of_seasons: number | null;
  number_of_episodes: number | null;
  release_date: string | null;
  runtime: number | null;
  year: number | null;
  genre: string[] | null;
  // Tri-state: true = Japanese animation, false = confirmed live-action, null = undetermined
  is_anime?: boolean | null;
  // Franchise grouping — present on tv/movie items, null for audio/ungrouped
  collection_id?: string | null;
  collection_name?: string | null;
  resource_count: number;
  created_at: string;
  updated_at: string;
}

// AudioWork - non-TV/non-movie works (ASMR / music / drama CD / radio)
export interface AudioWork {
  id: string;
  title_cn: string | null;
  title_en: string | null;
  original_title: string | null;
  aliases: string[] | null;
  external_id: string | null;
  external_source: string | null;
  description: string | null;
  poster_url: string | null;
  rating: number | null;
  genre: string[] | null;
  status: string | null;
  release_date: string | null;
  runtime: number | null;
  content_type: AudioContentType | null;
  wikipedia_url: string | null;
  wikipedia_page_id: number | null;
  created_at: string;
  updated_at: string;
  // Detail-only fields
  resources?: FileResource[];
  resource_count?: number;
}

// Episode
export interface Episode {
  id: string;
  series_id: string;
  season: number;
  episode: number;
  title: string | null;
  air_date: string | null;
  created_at: string;
  updated_at: string;
}

// Filter DSL
export type FilterField =
  | 'subtitle_group'
  | 'resolution'
  | 'source'
  | 'video_codec'
  | 'audio_codec'
  | 'subtitle_type'
  | 'container'
  | 'file_size'
  | 'episode'
  | 'season'
  | 'episode_start'
  | 'episode_end'
  | 'absolute_episode'
  | 'is_batch'
  | 'subtitle_langs'
  | 'episode_confidence'
  | 'content_type'
  | 'title_cn'
  | 'title_en'
  | 'search_title'
  | 'movie.rating'
  | 'movie.year'
  | 'movie.genre'
  | 'movie.collection'
  | 'movie.is_anime'
  | 'series.rating'
  | 'series.year'
  | 'series.genre'
  | 'series.collection'
  | 'series.is_anime'
  // Resource-level collection (franchise packs link a WorkCollection
  // directly, with all work FKs empty).
  | 'collection';

export type StringFilterField = Exclude<
  FilterField,
  | 'file_size' | 'episode' | 'season' | 'episode_start' | 'episode_end' | 'absolute_episode' | 'is_batch' | 'subtitle_langs'
  | 'movie.rating' | 'movie.year' | 'movie.genre' | 'series.rating' | 'series.year' | 'series.genre'
  | 'series.is_anime' | 'movie.is_anime'
>;
export type NumberFilterField = 'file_size' | 'episode' | 'season' | 'episode_start' | 'episode_end' | 'absolute_episode'
  | 'movie.rating' | 'movie.year' | 'series.rating' | 'series.year';
export type BoolFilterField = 'is_batch' | 'series.is_anime' | 'movie.is_anime';
export type ListFilterField = 'subtitle_langs' | 'movie.genre' | 'series.genre';

export type FilterOperator =
  | 'eq'
  | 'ne'
  | 'contains'
  | 'fuzzy'
  | 'in'
  | 'regex'
  | 'gt'
  | 'gte'
  | 'lt'
  | 'lte'
  | 'is_empty'
  | 'is_not_empty';

/** Operators that take no value (the backend ignores ``value`` for these). */
export type NoValueOperator = 'is_empty' | 'is_not_empty';
export type StringOperator = 'eq' | 'ne' | 'contains' | 'fuzzy' | 'in' | 'regex' | NoValueOperator;
export type NumberOperator = 'eq' | 'ne' | 'gt' | 'gte' | 'lt' | 'lte' | 'in' | NoValueOperator;
export type BoolOperator = 'eq' | 'ne' | NoValueOperator;
export type ListOperator = 'eq' | 'ne' | 'contains' | 'in' | NoValueOperator;

export interface FieldCondition {
  field: FilterField;
  operator: FilterOperator;
  value: string | number | boolean | string[];
}

export interface BoolCondition {
  combinator: 'and' | 'or';
  conditions: Array<BoolCondition | FieldCondition>;
  is_not?: boolean;
}

export type FilterConfig = BoolCondition;

// Agent
export type AgentStatus = 'active' | 'paused' | 'error';

export interface AgentWork {
  id: string;
  agent_id: string;
  content_type: 'tv' | 'movie';
  series_id: string | null;
  movie_id: string | null;
  enable_episode_dedup: boolean;
  filter_overrides: BoolCondition | null;
  display_name_override: string | null;
  // Latest completed download position for TV works (GET /agents/{id}).
  latest_completed_season?: number | null;
  latest_completed_episode?: number | null;
  created_at: string;
  updated_at: string;
  // populated by frontend joins
  series?: TVSeries;
  movie?: Movie;
}

export interface Agent {
  id: string;
  name: string;
  channel_id: string;
  downloader_id: string;
  download_subdir: string | null;
  task_expire_days: number;
  llm_enabled: boolean;
  scope_channel_wide: boolean;
  conflict_resolution: 'ask' | 'auto';
  llm_prompt: string | null;
  filter_config: BoolCondition | null;
  status: AgentStatus;
  last_run_at: string | null;
  last_run_status: string | null;
  created_at: string;
  updated_at: string;
  works?: AgentWork[];
  channel?: Channel;
  downloader?: DownloaderInstance;
}

export interface AgentCreate {
  name: string;
  channel_id: string;
  downloader_id: string;
  download_subdir?: string | null;
  task_expire_days?: number;
  llm_enabled?: boolean;
  scope_channel_wide?: boolean;
  conflict_resolution?: 'ask' | 'auto';
  llm_prompt?: string | null;
  filter_config?: BoolCondition | null;
  works?: AgentWorkCreate[];
  /** Resource ids selected from the rules-preview diff to backfill. Present
   *  (possibly empty) when the save went through the preview flow; null for
   *  plain non-rule edits. */
  dispatch_resource_ids?: string[] | null;
  /** "立即运行": enqueue a background full-history run right after creation. */
  run_immediately?: boolean;
}

export type AgentUpdate = AgentCreate;

export interface RulesPreviewRequest {
  agent_id?: string;
  channel_id?: string;
  scope_channel_wide: boolean;
  filter_config?: BoolCondition | null;
  works?: AgentWorkCreate[];
}

export interface RulesPreviewResource {
  id: string;
  title_raw: string;
  title_cn?: string | null;
  subtitle_group?: string | null;
  resolution?: string | null;
  source?: string | null;
  video_codec?: string | null;
  audio_codec?: string | null;
  subtitle_type?: string | null;
  subtitle_langs?: string[] | null;
  container?: string | null;
  file_size?: number | null;
  episode?: number | null;
  season?: number | null;
  episode_confidence?: string | null;
  published_at?: string | null;
  series_id?: string | null;
  movie_id?: string | null;
  // Only populated on AgentRun.matched_resources entries: true while the
  // resource still has an unresolved pending decision.
  pending_decision?: boolean;
}

export interface RulesPreviewResponse {
  newly_matching: RulesPreviewResource[];
  no_longer_matching: RulesPreviewResource[];
  in_queue_skipped: number;
}

export interface AgentRunResource {
  id: string;
  title_raw: string;
  title_cn?: string | null;
  subtitle_group?: string | null;
  resolution?: string | null;
  episode?: number | null;
  season?: number | null;
}

export interface AgentRun {
  id: string;
  agent_id: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  total_resources: number;
  matched: number;
  dispatched: number;
  pending_decisions: number;
  filter_failed: number;
  duplicates_skipped: number;
  unrecognized: number;
  matched_resource_ids: string[];
  errors: string[];
  // Scan-window lower bound for manual windowed runs; null/absent for
  // delta/targeted runs, 1970-01-01 for an explicit "no limit" full scan.
  scan_since?: string | null;
  matched_resources: RulesPreviewResource[];
}

export interface AgentWorkCreate {
  content_type: 'tv' | 'movie';
  series_id?: string | null;
  movie_id?: string | null;
  enable_episode_dedup?: boolean;
  filter_overrides?: BoolCondition | null;
  display_name_override?: string | null;
}

// DownloadTask
export type TaskStatus =
  | 'pending'
  | 'queued'
  | 'downloading'
  | 'paused'
  | 'completed'
  | 'error'
  | 'cancelled';

export interface DownloadTask {
  id: string;
  agent_id: string;
  file_resource_id: string;
  downloader_id: string;
  download_dir: string | null;
  transmission_torrent_id: number | null;
  status: TaskStatus;
  progress: number;
  download_speed: number;
  upload_speed: number;
  eta: number | null;
  error_message: string | null;
  retry_count: number;
  max_retries: number;
  confirmed_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  file_resource?: FileResource;
  agent?: Agent;
}

// PendingDecision
export type DecisionStatus = 'pending' | 'decided' | 'expired' | 'skipped';

export interface PendingDecision {
  id: string;
  agent_id: string;
  series_id: string | null;
  movie_id: string | null;
  episode: number | null;
  season: number | null;
  candidates: string[];
  reason: string;
  llm_suggestion: string | null;
  llm_picked_resource_id: string | null;
  decided_resource_id: string | null;
  status: DecisionStatus;
  expires_at: string | null;
  created_at: string;
  decided_at: string | null;
  // populated for display
  candidate_resources?: FileResource[];
  series?: TVSeries;
  movie?: Movie;
}

// DownloadNotification
// Aggregated across the notification's per-webhook deliveries: pending while
// any delivery is still deliverable, done when all are done, failed otherwise.
export type NotificationStatus = 'pending' | 'done' | 'failed';

export interface DeliverySummary {
  total: number;
  done: number;
  failed: number;
  pending: number;
}

export interface DownloadNotification {
  id: string;
  agent_id: string;
  download_task_id: string;
  title_raw: string | null;
  status: NotificationStatus;
  delivery_summary: DeliverySummary;
  created_at: string;
  updated_at: string;
}

export interface WebhookDelivery {
  id: string;
  webhook_id: string;
  webhook_url: string;
  status: NotificationStatus;
  attempt_count: number;
  error_message: string | null;
  delivered_at: string | null;
  next_attempt_at: string | null;
  created_at: string;
}

// Detail view adds the full payload snapshot and per-webhook deliveries
// (absent from list items).
export interface DownloadNotificationDetail extends DownloadNotification {
  payload: Record<string, unknown> | null;
  deliveries: WebhookDelivery[];
}

/** One of possibly several webhook registrations on an agent. */
export interface AgentWebhook {
  id: string;
  url: string;
  mock: boolean;
  enabled: boolean;
  created_at: string;
}

// Auth
export interface AuthStatus {
  authenticated: boolean;
}

// API keys — `key` is returned only once, at creation time.
export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
}

export interface ApiKeyCreated extends ApiKey {
  key: string;
}

// Downloader
export type DownloaderStatus = 'connected' | 'disconnected' | 'error';

export interface DownloaderInstance {
  id: string;
  name: string;
  type: 'transmission' | 'mock';
  url: string;
  username: string | null;
  password: string | null;
  download_dir: string;
  /** Volume binding (R1): daemon-view download_dir root ==
      volume.mount_path + volume_subpath; both null = identical views. */
  volume_id: string | null;
  volume_subpath: string | null;
  status: DownloaderStatus;
  last_checked_at: string | null;
  created_at: string;
  updated_at: string;
}

// TorrentInfo — live data from Transmission RPC
export type TorrentStatus =
  | 'stopped'
  | 'check pending'
  | 'checking'
  | 'download pending'
  | 'downloading'
  | 'seed pending'
  | 'seeding';

export interface TorrentInfo {
  id: number;
  name: string;
  hash: string;
  status: TorrentStatus;
  percent_done: number;
  rate_download: number;
  rate_upload: number;
  eta_seconds: number | null;
  total_size: number;
  have_valid: number;
  is_finished: boolean;
  error: number;
  error_string: string;
  added_date: string | null;
  peers_connected: number;
}

// Background job state
export type JobStatus = 'queued' | 'running' | 'done' | 'failed';

export interface FetchJobState {
  job_id?: string;
  task_id?: string;
  status: JobStatus;
  result?: Record<string, unknown> | null;
  error?: string | null;
  message?: string;
  progress?: number;
}

// Feed preview
export interface PreviewEntry {
  title?: string;
  link?: string;
  published?: string;
  description?: string;
  enclosures?: Array<{ url?: string; length?: string; type?: string }>;
  [key: string]: unknown;
}

export interface PreviewFeedData {
  entries: PreviewEntry[];
  parsed: Record<string, unknown>[];
}

// Metadata search
export interface MetadataSearchResult {
  title_cn: string | null;
  title_en: string | null;
  original_title: string | null;
  description: string | null;
  poster_url: string | null;
  year: number | null;
  external_id: string | null;
  content_type: 'tv' | 'movie';
}

// Agent suggestions
export interface AgentSuggestionGroup {
  id: string | null;
  sample_title: string;
  resources: string[];
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

// Filter test result
export interface ConditionTestResult {
  field: string;
  operator: string;
  value: string | number | string[];
  passed: boolean;
}

export interface ResourceTestResult {
  resource_id: string;
  title: string;
  passed: boolean;
  conditions: ConditionTestResult[];
}

export interface FilterTestResponse {
  results: ResourceTestResult[];
  stats: { total: number; passed: number; failed: number };
}

// Dashboard
export interface DashboardDownloadGroup {
  type: 'series' | 'movie' | 'unknown' | 'untracked';
  id: string | null;
  title: string;
  poster_url: string | null;
  tasks: Array<{
    task_id: string;
    resource_title: string;
    progress: number;
    agent_id: string | null;
    agent_name: string | null;
    channel_id: string | null;
    channel_name: string | null;
    // Present on 'untracked' entries (torrents RSSRipple did not dispatch).
    downloader_id?: string | null;
    downloader_name?: string | null;
  }>;
}

export interface DashboardPendingItem {
  id: string;
  agent_id: string;
  agent_name: string;
  reason: string;
  candidates: string[];
  candidate_resources?: FileResource[];
  season?: number | null;
  // Linked work identity (when the decision is work-scoped) — used to link
  // the work title from the dashboard pending list.
  title?: string | null;
  series_id?: string | null;
  movie_id?: string | null;
  llm_suggestion: string | null;
  created_at: string;
}

// A file resource with an ambiguous episode/season number awaiting manual
// correction (episode_confidence="ambiguous"). Surfaced on the dashboard as an
// actionable todo independent of any single agent's PendingDecision.
export interface DashboardConfirmationItem {
  resource: FileResource;
  channel_name: string | null;
  work_title: string | null;
}

export interface DashboardData {
  active_agents: number;
  active_channels: number;
  active_download_count: number;
  active_download_groups: DashboardDownloadGroup[];
  pending_decisions: DashboardPendingItem[];
  pending_confirmations: DashboardConfirmationItem[];
  pending_plans: OrganizePlanListItem[];
}

// Filter suggestions (Agent-rules based)
export interface FilterSuggestionWork {
  content_type: 'tv' | 'movie';
  series_id: string | null;
  movie_id: string | null;
  title: string | null;
  poster_url: string | null;
  resource_count: number;
  /** Differentiating conditions for THIS work (merged AND with the agent's
   * global filter_config when the work is subscribed). */
  filter_overrides: BoolCondition | null;
  override_explanation: string;
}

export interface FilterSuggestionResponse {
  works: FilterSuggestionWork[];
  /** Conditions shared by ALL selected resources. */
  global_filter_config: BoolCondition | null;
  /** Selected resources not linked to any work. */
  unlinked_count: number;
  explanation: string;
}

// ---------------------------------------------------------------------------
// Built-in file organization (organize) — mirrors app/schemas/organize.py.

// Logical storage volumes — mirrors app/schemas/storage_volume.py.
export interface StorageVolume {
  id: string;
  name: string;
  /** Absolute path inside the RSSRipple container (mounted at compose time). */
  mount_path: string;
  remark: string | null;
  created_at: string;
  updated_at: string;
}

export interface StorageVolumeCreate {
  name: string;
  mount_path: string;
  remark?: string | null;
}

export type StorageVolumeUpdate = Partial<StorageVolumeCreate>;

export interface StorageVolumeCheckResult {
  exists: boolean;
  writable: boolean;
}

export type LibraryKind = 'tv' | 'movie' | 'mixed';

// Libraries are scan-derived from media servers (R2) — mirrors
// app/schemas/organize.py LibraryOut. `root_path` is a derived display field
// (volume mount_path + root_subpath resolution), null while unbound.
export interface Library {
  id: string;
  name: string;
  kind: LibraryKind;
  media_server_id: string | null;
  media_server_name: string | null;
  section_key: string | null;
  /** Library root as seen by the media server (raw). */
  server_path: string | null;
  volume_id: string | null;
  volume_name: string | null;
  root_subpath: string | null;
  /** Recycle dir for batch-move leftovers (volume-relative), null = keep in place. */
  recycle_subpath: string | null;
  root_path: string | null;
  /** volume_id !== null; unbound libraries need an in-place binding fix. */
  bound: boolean;
  subtitle_lang_map: Record<string, string> | null;
  created_at: string;
  updated_at: string;
}

export interface LibraryListItem extends Library {
  pending_plan_count: number;
}

/** No create endpoint — libraries come from media-server scans. */
export interface LibraryUpdate {
  subtitle_lang_map?: Record<string, string> | null;
  volume_id?: string | null;
  root_subpath?: string | null;
  recycle_subpath?: string | null;
}

// Media servers — mirrors app/schemas/media_server.py.
export type MediaServerType = 'plex' | 'emby' | 'jellyfin';

export interface MediaServerBinding {
  id: string;
  server_path_prefix: string;
  volume_id: string;
  subpath: string;
}

export type MediaServerBindingInput = Omit<MediaServerBinding, 'id'>;

export interface MediaServer {
  id: string;
  name: string;
  type: MediaServerType;
  url: string;
  enabled: boolean;
  bindings: MediaServerBinding[];
  created_at: string;
  updated_at: string;
}

export interface MediaServerListItem extends MediaServer {
  library_count: number;
  unbound_library_count: number;
}

export interface MediaServerCreate {
  name: string;
  type: MediaServerType;
  url: string;
  /** Secret; never echoed back. */
  token?: string | null;
  enabled?: boolean;
  /** Whole-array replacement semantics. */
  bindings?: MediaServerBindingInput[];
}

export interface MediaServerUpdate {
  name?: string;
  type?: MediaServerType;
  url?: string;
  /** null/omitted = keep the stored token. */
  token?: string | null;
  enabled?: boolean;
  /** omitted = untouched; array (incl. empty) = replace all. */
  bindings?: MediaServerBindingInput[];
}

export interface MediaServerTestResult {
  ok: boolean;
  server_version: string | null;
  message: string | null;
}

/** Optional overrides for `POST /media-servers/{id}/test` (edit-form probe). */
export interface MediaServerTestRequest {
  type?: MediaServerType;
  url?: string;
  /** undefined = keep the stored token. */
  token?: string;
}

/** Stateless probe body for `POST /media-servers/test` (create-form probe). */
export interface MediaServerTestPayload {
  type: MediaServerType;
  url: string;
  token?: string | null;
}

export interface MediaServerScanResult {
  created: number;
  updated: number;
  unbound: number;
}

export interface OrganizeRule {
  id: string;
  name: string;
  priority: number;
  enabled: boolean;
  /** BoolCondition DSL root; null = match all. */
  filter: BoolCondition | null;
  library_id: string;
  path_template: string;
  file_op: 'move' | 'hardlink' | 'copy';
  auto_execute: boolean;
  created_at: string;
  updated_at: string;
}

export interface OrganizeRuleCreate {
  name: string;
  priority?: number;
  enabled?: boolean;
  filter?: BoolCondition | null;
  library_id: string;
  path_template: string;
  file_op?: 'move' | 'hardlink' | 'copy';
  auto_execute?: boolean;
}

export type OrganizeRuleUpdate = Partial<OrganizeRuleCreate>;

/** Non-persisted rule draft accepted by the preview endpoint. */
export interface OrganizeRuleDraft {
  name?: string;
  priority?: number;
  enabled?: boolean;
  filter?: BoolCondition | null;
  library_id: string;
  path_template: string;
  file_op?: 'move' | 'hardlink' | 'copy';
}

export interface OrganizePreviewRequest {
  notification_id?: string;
  resource_id?: string;
  rule?: OrganizeRuleDraft | null;
  category?: string | null;
}

export interface OrganizePreviewOp {
  op_type: string; // move | keep
  src: string;
  dst: string | null;
  size: number;
  reason: string;
}

export interface OrganizePreviewResponse {
  matched_rule: { id?: string | null; name: string } | null;
  library: { id: string; name: string } | null;
  category: string | null;
  needs_category: boolean;
  uncategorized: boolean;
  ops: OrganizePreviewOp[];
}

export type OrganizePlanStatus = 'pending' | 'running' | 'done' | 'failed' | 'cancelled';

export interface OrganizeOpsSummary {
  total: number;
  move: number;
  keep: number;
  movedir: number;
}

export interface OrganizePlanListItem {
  id: string;
  notification_id: string;
  rule_id: string | null;
  rule_name: string | null;
  library_id: string | null;
  library_name: string | null;
  category: string | null;
  status: OrganizePlanStatus;
  error_message: string | null;
  executed_at: string | null;
  created_at: string;
  updated_at: string;
  ops_summary: OrganizeOpsSummary;
  /** First few ops (sorted by seq) for inline list preview. */
  ops_preview: OrganizePlanOp[];
  /** Derived reason a plan stays pending: library/category unset →
      'unclassified'; target library has no volume binding → 'unbound'. */
  pending_reason: 'unclassified' | 'unbound' | null;
}

export interface OrganizePlanOp {
  id: string;
  seq: number;
  op_type: string; // move | keep | movedir
  src: string;
  dst: string | null;
  size: number;
  status: string; // pending | done | kept | failed
  error_message: string | null;
}

export interface OrganizeAuditEntry {
  id: string;
  plan_id: string;
  action: string;
  detail: Record<string, unknown> | null;
  created_at: string;
}

export interface OrganizePlanDetail extends OrganizePlanListItem {
  /** Frozen notification snapshot (shape: app/schemas/notification.py). */
  payload: Record<string, unknown>;
  ops: OrganizePlanOp[];
  audit_entries: OrganizeAuditEntry[];
}
