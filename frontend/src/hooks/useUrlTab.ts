import { useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';

/**
 * Page-level tab state persisted in the URL (`?tab=<key>`) so a refresh or a
 * shared link restores the active tab. The param is omitted when the tab is
 * the default, keeping URLs clean; unknown values fall back to the default.
 * Other query params are preserved. Uses `replace` so tab clicks don't pile
 * up in browser history. Not for Drawer/Modal-internal tabs — those are too
 * deep to be worth routing.
 */
export default function useUrlTab<T extends string>(
  defaultKey: T,
  validKeys: readonly T[],
  param = 'tab',
): [T, (key: T) => void] {
  const [searchParams, setSearchParams] = useSearchParams();
  const raw = searchParams.get(param);
  const tab = validKeys.includes(raw as T) ? (raw as T) : defaultKey;

  const setTab = useCallback(
    (key: T) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (key === defaultKey) next.delete(param);
          else next.set(param, key);
          return next;
        },
        { replace: true },
      );
    },
    [defaultKey, param, setSearchParams],
  );

  return [tab, setTab];
}
