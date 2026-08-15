import { api } from './client';
import type {
  StorageVolume,
  StorageVolumeCheckResult,
  StorageVolumeCreate,
  StorageVolumeUpdate,
} from '../types';

export interface VolumeDirListing {
  path: string;
  parent: string;
  dirs: string[];
  exists: boolean;
}

export const volumesApi = {
  // Small set; single page is enough (mirrors libraries).
  list: () => api.get<StorageVolume[]>('/volumes?page=1&page_size=100'),
  get: (id: string) => api.get<StorageVolume>(`/volumes/${id}`),
  create: (data: StorageVolumeCreate) => api.post<StorageVolume>('/volumes', data),
  update: (id: string, data: StorageVolumeUpdate) =>
    api.put<StorageVolume>(`/volumes/${id}`, data),
  delete: (id: string) => api.delete<{ deleted: boolean }>(`/volumes/${id}`),
  // Probe mount_path existence + writability on the server side.
  check: (id: string) =>
    api.post<StorageVolumeCheckResult>(`/volumes/${id}/check`),
  // List server-side subdirectories (mount-path directory picker).
  listDirs: (path: string) =>
    api.get<VolumeDirListing>(`/volumes/dirs?path=${encodeURIComponent(path)}`),
};
