import { Tag } from 'antd';

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

interface StatusBadgeProps {
  status: string;
}

export default function StatusBadge({ status }: StatusBadgeProps) {
  const color = statusColorMap[status.toLowerCase()] || 'default';
  return <Tag color={color}>{status}</Tag>;
}
