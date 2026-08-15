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

/** Server-side absolute start path for the volume-subpath directory picker:
    the volume mount path joined with the current relative subpath (if any). */
export function subpathBrowseStart(mountPath: string, subpath: string): string {
  const mount = (mountPath || '').trim().replace(/\/+$/, '');
  if (!mount) return '/';
  const sub = (subpath || '').trim().replace(/^\/+|\/+$/g, '');
  return sub ? `${mount}/${sub}` : mount;
}

/** Convert a server-side absolute path picked in the browser back to a relative
    subpath under the volume mount path. Returns null when the path is outside
    the volume root (caller should keep the current value). */
export function toVolumeSubpath(mountPath: string, absPath: string): string | null {
  const mount = (mountPath || '').replace(/\/+$/, '');
  if (!mount) {
    return absPath === '/' ? '' : absPath.replace(/^\/+/, '');
  }
  if (absPath === mount) return '';
  if (absPath.startsWith(`${mount}/`)) return absPath.slice(mount.length + 1);
  return null;
}
