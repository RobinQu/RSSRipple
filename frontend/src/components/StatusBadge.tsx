import { Tag } from 'antd';
import { useTranslation } from 'react-i18next';

const statusColorMap: Record<string, string> = {
  active: 'green',
  inactive: 'default',
  downloading: 'processing',
  completed: 'success',
  connected: 'success',
  decided: 'success',
  paused: 'warning',
  pending: 'default',
  queued: 'processing',
  error: 'error',
  failed: 'error',
  cancelled: 'error',
  expired: 'default',
  skipped: 'default',
  seeding: 'cyan',
  stopped: 'default',
  fetching: 'processing',
  analyzing: 'processing',
  disconnected: 'default',
};

const statusKeySet = new Set([
  'active', 'inactive', 'error', 'pending', 'queued', 'downloading',
  'paused', 'completed', 'cancelled', 'connected', 'disconnected',
  'success', 'failed',
]);

interface StatusBadgeProps {
  status: string;
}

export default function StatusBadge({ status }: StatusBadgeProps) {
  const { t } = useTranslation();
  const color = statusColorMap[status.toLowerCase()] || 'default';
  const key = status.toLowerCase();
  const label = statusKeySet.has(key) ? t(`status.${key}`) : status;
  return <Tag color={color}>{label}</Tag>;
}
