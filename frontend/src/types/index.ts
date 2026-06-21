// Common
export interface APIResponse<T> {
  success: boolean;
  data: T;
  error: { code: string; message: string } | null;
  meta?: { page: number; page_size: number; total: number };
}

// Channel
export type ChannelType = 'rss_feed';
export type ChannelStatus = 'active' | 'inactive' | 'error';
export type ParserType = 'auto' | 'mikanani' | 'custom';
export interface Channel {
  id: string;
  name: string;
  type: ChannelType;
  url: string;
  fetch_interval: number;
  status: ChannelStatus;
  field_mapping: Record<string, unknown> | null;
  parser_type: ParserType;
  last_fetched_at: string | null;
  created_at: string;
  updated_at: string;
}
export interface ChannelCreate {
  name: string;
  type: ChannelType;
  url: string;
  fetch_interval?: number;
}
export interface ChannelUpdate {
  name?: string;
  url?: string;
  fetch_interval?: number;
  status?: ChannelStatus;
}

// FileResource
export interface FileResource {
  id: string;
  channel_id: string;
  guid: string;
  title_raw: string;
  title_cn: string | null;
  title_en: string | null;
  subtitle_group: string | null;
  episode: number | null;
  resolution: string | null;
  source: string | null;
  video_codec: string | null;
  audio_codec: string | null;
  subtitle_type: string | null;
  container: string | null;
  file_size: number | null;
  torrent_url: string;
  detail_url: string;
  published_at: string;
  created_at: string;
}

// ResourceFilter
export type FilterField = 'subtitle_group' | 'resolution' | 'container' | 'video_codec' | 'audio_codec' | 'subtitle_type' | 'source' | 'title_cn' | 'title_en';
export type FilterOperator = 'eq' | 'contains' | 'fuzzy' | 'in' | 'regex';
export interface ResourceFilter {
  id: string;
  agent_id: string;
  field: FilterField;
  operator: FilterOperator;
  value: string;
  priority: number;
  is_required: boolean;
  created_at: string;
}
export interface FilterCreate {
  field: FilterField;
  operator: FilterOperator;
  value: string;
  priority: number;
  is_required: boolean;
}

// Agent
export type AgentStatus = 'active' | 'paused' | 'error';
export interface Agent {
  id: string;
  name: string;
  channel_id: string;
  downloader_id: string | null;
  task_expire_days: number;
  llm_enabled: boolean;
  metadata_source: string | null;
  content_type: string;
  status: AgentStatus;
  last_run_at: string | null;
  created_at: string;
  updated_at: string;
  filters?: ResourceFilter[];
}
export interface AgentCreate {
  name: string;
  channel_id: string;
  downloader_id: string;
  task_expire_days?: number;
  llm_enabled?: boolean;
  metadata_source?: string;
  content_type?: string;
  filters?: FilterCreate[];
}
export interface AgentUpdate {
  name?: string;
  channel_id?: string;
  downloader_id?: string;
  task_expire_days?: number;
  llm_enabled?: boolean;
  metadata_source?: string;
  content_type?: string;
  status?: AgentStatus;
}

// Filter test result
export interface FilterTestResult {
  field: string;
  operator: string;
  filter_value: string;
  resource_value: string;
  passed: boolean;
  is_required: boolean;
}
export interface ResourceTestResult {
  resource_id: string;
  title_raw: string;
  filters: FilterTestResult[];
  all_required_passed: boolean;
}
export interface FilterTestResponse {
  total_resources: number;
  matched: number;
  failed: number;
  results: ResourceTestResult[];
  message?: string;
}

// Downloader
export type DownloaderType = 'transmission';
export type DownloaderStatus = 'connected' | 'disconnected' | 'error';
export interface DownloaderInstance {
  id: string;
  name: string;
  type: DownloaderType;
  url: string;
  username: string | null;
  download_dir: string | null;
  status: DownloaderStatus;
  last_checked_at: string | null;
  created_at: string;
  updated_at: string;
}
export interface DownloaderCreate {
  name: string;
  type: DownloaderType;
  url: string;
  username?: string;
  password?: string;
  download_dir?: string;
}
export interface DownloaderUpdate {
  name?: string;
  url?: string;
  username?: string;
  password?: string;
  download_dir?: string;
}

// DownloadTask
export type TaskStatus = 'pending' | 'queued' | 'downloading' | 'paused' | 'completed' | 'error' | 'cancelled';
export interface DownloadTask {
  id: string;
  agent_id: string;
  file_resource_id: string;
  downloader_id: string;
  transmission_torrent_id: number | null;
  status: TaskStatus;
  progress: number;
  download_speed: number;
  eta: number | null;
  error_message: string | null;
  retry_count: number;
  created_at: string;
  updated_at: string;
  file_resource?: FileResource;
}

// PendingDecision
export type DecisionStatus = 'pending' | 'decided' | 'expired' | 'skipped';
export interface PendingDecision {
  id: string;
  agent_id: string;
  episode_id: string | null;
  movie_id: string | null;
  candidates: string[];
  reason: string;
  llm_suggestion: string | null;
  decided_resource_id: string | null;
  status: DecisionStatus;
  created_at: string;
  decided_at: string | null;
}

// Dashboard
export interface DashboardData {
  active_agents: number;
  active_downloads: DownloadTask[];
  pending_decisions: PendingDecision[];
}
