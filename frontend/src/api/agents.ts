import { api } from './client';
import type {
  Agent,
  AgentCreate,
  AgentUpdate,
  AgentWork,
  AgentWorkCreate,
  AgentSuggestionGroup,
  BoolCondition,
  FilterTestResponse,
} from '../types';

export const agentsApi = {
  list: (page = 1, pageSize = 20) =>
    api.get<Agent[]>(`/agents?page=${page}&page_size=${pageSize}`),
  get: (id: string) => api.get<Agent>(`/agents/${id}`),
  create: (data: AgentCreate) => api.post<Agent>('/agents', data),
  update: (id: string, data: AgentUpdate) => api.put<Agent>(`/agents/${id}`, data),
  delete: (id: string) => api.delete<null>(`/agents/${id}`),
  run: (id: string) => api.post<{ task_id: string }>(`/agents/${id}/run`),
  runStatus: (id: string) =>
    api.get<{ status: string; message?: string; stats?: Record<string, number> }>(
      `/agents/${id}/run-status`,
    ),
  testFilters: (id: string, body?: { resource_ids?: string[] }) =>
    api.post<FilterTestResponse>(`/agents/${id}/test-filters`, body ?? {}),
  suggestions: (id: string) =>
    api.get<{ scope_channel_wide: boolean; suggestions: AgentSuggestionGroup[] }>(
      `/agents/${id}/suggestions`,
    ),

  // Works sub-resource
  listWorks: (agentId: string) =>
    api.get<AgentWork[]>(`/agents/${agentId}/works`),
  addWork: (agentId: string, data: AgentWorkCreate) =>
    api.post<AgentWork>(`/agents/${agentId}/works`, data),
  updateWork: (
    agentId: string,
    workId: string,
    data: {
      enable_episode_dedup?: boolean;
      filter_overrides?: BoolCondition | null;
      display_name_override?: string | null;
    },
  ) => api.put<AgentWork>(`/agents/${agentId}/works/${workId}`, data),
  removeWork: (agentId: string, workId: string) =>
    api.delete<null>(`/agents/${agentId}/works/${workId}`),
};
