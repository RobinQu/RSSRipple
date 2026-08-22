import { api } from './client';
import type {
  Library,
  LibraryListItem,
  LibraryUpdate,
  OrganizeAuditEntry,
  OrganizePlanDetail,
  OrganizePlanListItem,
  OrganizePreviewRequest,
  OrganizePreviewResponse,
  OrganizeRule,
  OrganizeRuleCreate,
  OrganizeRuleUpdate,
} from '../types';

export const organizeApi = {
  // Libraries (unpaginated, small set; list carries pending plan counts).
  // Scan-derived (R2): no create endpoint; update is partial-only.
  listLibraries: () => api.get<LibraryListItem[]>('/libraries'),
  updateLibrary: (id: string, data: LibraryUpdate) =>
    api.put<Library>(`/libraries/${id}`, data),
  deleteLibrary: (id: string) => api.delete<{ deleted: boolean }>(`/libraries/${id}`),

  // Organize rules (priority-ascending, unpaginated)
  listRules: () => api.get<OrganizeRule[]>('/organize-rules'),
  createRule: (data: OrganizeRuleCreate) => api.post<OrganizeRule>('/organize-rules', data),
  updateRule: (id: string, data: OrganizeRuleUpdate) =>
    api.put<OrganizeRule>(`/organize-rules/${id}`, data),
  deleteRule: (id: string) => api.delete<{ deleted: boolean }>(`/organize-rules/${id}`),
  // dry-run: render per-file src→dst for a draft rule (or the current rule
  // list when `rule` is omitted); nothing is persisted or touched on disk.
  preview: (body: OrganizePreviewRequest) =>
    api.post<OrganizePreviewResponse>('/organize-rules/preview', body),

  // Plans
  listPlans: (page = 1, pageSize = 20, status?: string, libraryId?: string) => {
    const qs = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (status) qs.set('status', status);
    if (libraryId) qs.set('library_id', libraryId);
    return api.get<OrganizePlanListItem[]>(`/organize/plans?${qs.toString()}`);
  },
  getPlan: (id: string) => api.get<OrganizePlanDetail>(`/organize/plans/${id}`),
  executePlan: (id: string) =>
    api.post<{ id: string; status: string }>(`/organize/plans/${id}/execute`),
  executeBatch: (planIds: string[]) =>
    api.post<{ results: { plan_id: string; status: string }[] }>(
      '/organize/plans/execute-batch',
      { plan_ids: planIds },
    ),
  classifyPlan: (id: string, body: { library_id: string; category?: string | null }) =>
    api.post<OrganizePlanListItem>(`/organize/plans/${id}/classify`, body),
  cancelPlan: (id: string, opts?: { delete_task?: boolean; delete_data?: boolean }) =>
    api.post<{ id: string; status: string; task_cleaned: boolean | null }>(
      `/organize/plans/${id}/cancel`,
      opts,
    ),

  // Audit
  listAudit: (page = 1, pageSize = 20, planId?: string) => {
    const qs = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (planId) qs.set('plan_id', planId);
    return api.get<OrganizeAuditEntry[]>(`/organize/audit?${qs.toString()}`);
  },
};
