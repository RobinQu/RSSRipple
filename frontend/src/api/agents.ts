import { api } from './client';
import type { Agent, AgentCreate, AgentUpdate, FilterTestResponse } from '../types';

export const agentsApi = {
  list: (page = 1, pageSize = 20) =>
    api.get<Agent[]>(`/agents?page=${page}&page_size=${pageSize}`),
  get: (id: string) =>
    api.get<Agent>(`/agents/${id}`),
  create: (data: AgentCreate) =>
    api.post<Agent>('/agents', data),
  update: (id: string, data: AgentUpdate) =>
    api.put<Agent>(`/agents/${id}`, data),
  delete: (id: string) =>
    api.delete<null>(`/agents/${id}`),
  run: (id: string) =>
    api.post<{ message: string }>(`/agents/${id}/run`),
  testFilters: (id: string) =>
    api.post<FilterTestResponse>(`/agents/${id}/test-filters`),
};
