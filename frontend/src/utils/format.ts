import { formatDistanceToNow, format as formatDateFns } from 'date-fns';

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return '—';
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

export function formatSpeed(bytesPerSec: number | null | undefined): string {
  if (bytesPerSec == null || bytesPerSec <= 0) return '0 B/s';
  return formatBytes(bytesPerSec) + '/s';
}

export function formatEta(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0) return '—';
  if (seconds === 0) return 'Done';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export function timeAgo(dateStr: string | null | undefined): string {
  if (!dateStr) return '—';
  try {
    return formatDistanceToNow(new Date(dateStr), { addSuffix: true });
  } catch {
    return dateStr;
  }
}

export function formatDate(dateStr: string | null | undefined, pattern = 'yyyy-MM-dd HH:mm'): string {
  if (!dateStr) return '—';
  try {
    return formatDateFns(new Date(dateStr), pattern);
  } catch {
    return dateStr;
  }
}

export function formatPercent(p: number): string {
  return `${(p * 100).toFixed(1)}%`;
}
