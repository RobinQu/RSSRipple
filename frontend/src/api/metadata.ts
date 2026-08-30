import { api } from './client';
import type { MetadataCandidate, MetadataSource } from '../types';

export interface MetadataSourceOption {
  value: MetadataSource;
  label: string;
  description: string;
  enabled: boolean;
  configured: boolean;
  available: boolean;
}

export interface TrustedSiteOption {
  value: string;
  domains: string[];
}

export interface MetadataSources {
  primary_sources: MetadataSourceOption[];
  trusted_sites: TrustedSiteOption[];
  default_trusted_sites: string[];
}

export interface MetadataChange {
  field: string;
  current: unknown;
  incoming: unknown;
  protected: boolean;
  action: 'update' | 'skip';
}

export const metadataApi = {
  sources: () => api.get<MetadataSources>('/metadata/sources'),
  search: (body: {
    query: string;
    content_type: 'tv' | 'movie';
    mode: 'local' | 'online';
    source?: MetadataSource;
    trusted_sites?: string[] | null;
  }) => api.post<{ candidates: MetadataCandidate[] }>('/metadata/search', body),
  preview: (body: {
    id: string;
    content_type: 'tv' | 'movie';
    candidate: MetadataCandidate;
    override_manual_edits: boolean;
  }) => api.post<{ changes: MetadataChange[]; warnings: string[] }>('/works/metadata/preview', body),
  apply: (body: {
    id: string;
    content_type: 'tv' | 'movie';
    candidate: MetadataCandidate;
    override_manual_edits: boolean;
  }) => api.post<{ applied: string[]; skipped: string[] }>('/works/metadata/apply', body),
};
