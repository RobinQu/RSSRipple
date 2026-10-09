// Latest-request-wins guard for async loaders (see usePolling for the
// cancelled/running style this mirrors). Each loader owns one guard: every
// invocation takes a fresh token and only applies its result while that
// token is still current, so a slow stale response (e.g. an earlier page of
// a paginated list) can never overwrite a newer one. `cancel` is called on
// unmount so no setState happens after the component is gone.
export interface RequestGuard {
  /** Start a new request; invalidates every previously issued token. */
  next(): number;
  /** True only for the most recent token of a non-cancelled guard. */
  isCurrent(token: number): boolean;
  /** Permanently reject all tokens (component unmounted). */
  cancel(): void;
}

export function createRequestGuard(): RequestGuard {
  let latest = 0;
  let cancelled = false;
  return {
    next() {
      latest += 1;
      return latest;
    },
    isCurrent(token) {
      return !cancelled && token === latest;
    },
    cancel() {
      cancelled = true;
    },
  };
}
