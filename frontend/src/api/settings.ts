import { api } from './client';

export type SettingKind = 'str' | 'bool';

export interface SettingField {
  value: string | boolean;
  configured: boolean;
  secret: boolean;
  kind: SettingKind;
}

export interface SettingGroup {
  id: string;
  keys: string[];
}

export interface SystemSettings {
  settings: Record<string, SettingField>;
  groups: SettingGroup[];
  exa_effort_levels: string[];
}

/** Keys that may be sent on update. Omit a key to leave it unchanged. */
export interface SystemSettingsUpdate {
  llm_api_key?: string | null;
  llm_model?: string | null;
  llm_base_url?: string | null;
  llm_enable_thinking?: boolean | null;
  tmdb_api_key?: string | null;
  jina_api_key?: string | null;
  exa_api_key?: string | null;
  exa_effort_level?: string | null;
  exa_enabled?: boolean | null;
  jina_enabled?: boolean | null;
  tmdb_enabled?: boolean | null;
  wikipedia_enabled?: boolean | null;
}

export const settingsApi = {
  get: () => api.get<SystemSettings>('/system-settings'),
  update: (payload: SystemSettingsUpdate) => api.put<SystemSettings>('/system-settings', payload),
};
