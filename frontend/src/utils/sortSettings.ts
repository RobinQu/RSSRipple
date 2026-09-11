import type { SortEntry } from '../components/SortSettings';

// Per-downloader task-list sort configuration (persisted in localStorage).

export function taskSortStorageKey(downloaderId: string): string {
  return `rssripple:downloader-task-sorts:${downloaderId}`;
}

function isValid(entries: unknown): entries is SortEntry[] {
  return (
    Array.isArray(entries) &&
    entries.every(
      (e) =>
        e &&
        typeof e === 'object' &&
        typeof (e as SortEntry).key === 'string' &&
        ((e as SortEntry).dir === 'asc' || (e as SortEntry).dir === 'desc'),
    )
  );
}

/** Load a saved sort config by storage key; null = never customized. */
export function loadSortEntries(storageKey: string): SortEntry[] | null {
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (isValid(parsed)) return parsed.map((e) => ({ key: e.key, dir: e.dir }));
  } catch {
    // Corrupted JSON or storage unavailable — fall back to defaults.
  }
  return null;
}

/** Persist (or clear with null) a sort config under the given storage key.
 * Failures (private mode etc.) degrade silently to session-only state. */
export function saveSortEntries(storageKey: string, entries: SortEntry[] | null): void {
  try {
    if (entries) localStorage.setItem(storageKey, JSON.stringify(entries));
    else localStorage.removeItem(storageKey);
  } catch {
    // ignore
  }
}

/** Serialize sort entries for the `sort` query param (key:dir,…). */
export function serializeSortEntries(entries: SortEntry[]): string | undefined {
  if (entries.length === 0) return undefined;
  return entries.map((e) => `${e.key}:${e.dir}`).join(',');
}
