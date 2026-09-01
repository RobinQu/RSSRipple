/** crypto.randomUUID requires a secure context; this app is often served over
 * plain http on a LAN host, so fall back to a manual id there. The id is only
 * a client-side temp key for not-yet-persisted rows. */
export const clientId = (): string =>
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `tmp-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
