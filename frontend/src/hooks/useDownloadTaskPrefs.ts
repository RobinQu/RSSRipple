import { useCallback, useEffect, useState } from 'react';
import type { SortEntry } from '../components/SortSettings';
import {
  loadColumnConfig,
  saveColumnConfig,
  type ChannelColumnConfig,
} from '../utils/requiredFields';
import {
  loadSortEntries,
  saveSortEntries,
  serializeSortEntries,
} from '../utils/sortSettings';

// Default task-table ordering: unfinished tasks first, then the most
// recently enqueued.
export const DEFAULT_TASK_SORTS: SortEntry[] = [
  { key: 'status', dir: 'asc' },
  { key: 'created_at', dir: 'desc' },
];

/** Sort + column preferences for a DownloadTaskTable instance, persisted in
 * localStorage under the given keys (per downloader / per agent). */
export function useDownloadTaskPrefs(sortStorageKey?: string, columnStorageKey?: string) {
  const [columnCfg, setColumnCfg] = useState<ChannelColumnConfig | null>(null);
  const [sorts, setSorts] = useState<SortEntry[]>(DEFAULT_TASK_SORTS);

  useEffect(() => {
    setColumnCfg(columnStorageKey ? loadColumnConfig(columnStorageKey) : null);
    setSorts((sortStorageKey && loadSortEntries(sortStorageKey)) || DEFAULT_TASK_SORTS);
  }, [sortStorageKey, columnStorageKey]);

  const handleColumnsChange = useCallback(
    (next: ChannelColumnConfig | null) => {
      setColumnCfg(next);
      if (columnStorageKey) saveColumnConfig(columnStorageKey, next);
    },
    [columnStorageKey],
  );
  const handleSortsChange = useCallback(
    (next: SortEntry[] | null) => {
      setSorts(next ?? DEFAULT_TASK_SORTS);
      if (sortStorageKey) saveSortEntries(sortStorageKey, next);
    },
    [sortStorageKey],
  );

  return {
    columnCfg,
    sorts,
    sortParam: serializeSortEntries(sorts),
    handleColumnsChange,
    handleSortsChange,
  };
}
