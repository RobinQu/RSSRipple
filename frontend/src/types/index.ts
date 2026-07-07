// Common
export interface APIResponse<T> {
  success: boolean;
  data: T;
  error: { code: string; message: string; stack?: string; details?: unknown } | null;
  meta?: { page: number; page_size: number; total: number };
}

// Channel
export type ChannelStatus = 'active' | 'inactive' | 'error';
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
  metadata_source: MetadataSource | null;
  last_fetched_at: string | null;
  last_fetch_status: string | null;
  last_fetch_error: string | null;
  created_at: string;
  updated_at: string;
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

// TV Series
export interface TVSeries {
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
  number_of_episodes: number | null;
  number_of_seasons: number | null;
  start_date: string | null;
  end_date: string | null;
  content_type: string | null;
  created_at: string;
  updated_at: string;
  // Detail-only fields
  episodes?: Episode[];
  resources?: FileResource[];
  resource_count?: number;
  task_count?: number;
  agent_work_count?: number;
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
  description: string | null;
  poster_url: string | null;
  rating: number | null;
  genre: string[] | null;
  status: string | null;
  release_date: string | null;
  runtime: number | null;
  content_type: string | null;
  created_at: string;
  updated_at: string;
  // Detail-only fields
  resources?: FileResource[];
  resource_count?: number;
  task_count?: number;
  agent_work_count?: number;
}

// Unified Work (TVSeries | Movie) for repository view
export interface Work {
  id: string;
  title_cn: string | null;
  title_en: string | null;
  original_title: string | null;
  poster_url: string | null;
  rating: number | null;
  status: string | null;
  content_type: 'tv' | 'movie' | null;
  number_of_seasons: number | null;
  number_of_episodes: number | null;
  release_date: string | null;
  runtime: number | null;
  year: number | null;
  genre: string[] | null;
  resource_count: number;
  created_at: string;
  updated_at: string;
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
  | 'title_cn'
  | 'title_en'
  | 'search_title';

export type StringFilterField = Exclude<
  FilterField,
  'file_size' | 'episode' | 'season' | 'episode_start' | 'episode_end' | 'absolute_episode' | 'is_batch' | 'subtitle_langs'
>;
export type NumberFilterField = 'file_size' | 'episode' | 'season' | 'episode_start' | 'episode_end' | 'absolute_episode';
export type BoolFilterField = 'is_batch';
export type ListFilterField = 'subtitle_langs';

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
  | 'lte';

export type StringOperator = 'eq' | 'ne' | 'contains' | 'fuzzy' | 'in' | 'regex';
export type NumberOperator = 'eq' | 'ne' | 'gt' | 'gte' | 'lt' | 'lte' | 'in';
export type BoolOperator = 'eq' | 'ne';
export type ListOperator = 'eq' | 'ne' | 'contains' | 'in';

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
}

export interface AgentUpdate extends AgentCreate {}

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
  type: 'series' | 'movie' | 'unknown';
  id: string | null;
  title: string;
  poster_url: string | null;
  tasks: Array<{
    task_id: string;
    resource_title: string;
    progress: number;
    agent_id: string;
    agent_name: string;
    channel_id: string;
    channel_name: string;
  }>;
}

export interface DashboardPendingItem {
  id: string;
  agent_id: string;
  agent_name: string;
  reason: string;
  candidates: PendingDecision['candidates'];
  candidate_resources?: FileResource[];
  llm_suggestion: string | null;
  created_at: string;
}

export interface DashboardData {
  active_agents: number;
  active_channels: number;
  active_download_count: number;
  active_download_groups: DashboardDownloadGroup[];
  pending_decisions: DashboardPendingItem[];
}

// Filter suggestions (new BoolCondition-based)
export interface FilterSuggestionResponse {
  filter_config: BoolCondition;
  explanation: string;
}
