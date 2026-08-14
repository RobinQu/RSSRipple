/** Client-side mirror of the backend relative-path rule for volume subpaths
    (same as Agent.download_subdir): no absolute paths, no empty / '.' / '..'
    segments. The backend re-validates authoritatively. */
export function isValidRelativeSubpath(raw: string): boolean {
  const v = raw.trim();
  if (!v) return true;
  if (v.startsWith('/') || v.startsWith('\\') || v.startsWith('~')) return false;
  if (/^[a-zA-Z]:/.test(v) || v.startsWith('\\\\')) return false;
  return !v.split(/[\\/]+/).some((p) => p === '' || p === '.' || p === '..');
}
