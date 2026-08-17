import { Tooltip } from 'antd';
import { useTranslation } from 'react-i18next';
import type { ReactNode } from 'react';
import { Clock, Download, Pause, CheckCircle, AlertTriangle, Ban } from 'lucide-react';

// DownloadTask status → icon-only mapping (colors mirror StatusBadge). The
// text label moves to a tooltip so the column can shrink to icon width.
const TASK_STATUS_ICON: Record<string, { icon: ReactNode; color: string }> = {
  pending: { icon: <Clock size={15} />, color: 'var(--rr-text-secondary)' },
  queued: { icon: <Clock size={15} />, color: 'var(--rr-primary)' },
  downloading: { icon: <Download size={15} />, color: 'var(--rr-primary)' },
  paused: { icon: <Pause size={15} />, color: 'var(--rr-warning)' },
  completed: { icon: <CheckCircle size={15} />, color: 'var(--rr-success)' },
  error: { icon: <AlertTriangle size={15} />, color: 'var(--rr-error)' },
  cancelled: { icon: <Ban size={15} />, color: 'var(--rr-error)' },
};

interface TaskStatusIconProps {
  status: string;
}

export default function TaskStatusIcon({ status }: TaskStatusIconProps) {
  const { t } = useTranslation();
  const key = (status || '').toLowerCase();
  const conf = TASK_STATUS_ICON[key] ?? TASK_STATUS_ICON.pending;
  return (
    <Tooltip title={t(`status.${key}`, { defaultValue: status })}>
      <span style={{ color: conf.color, display: 'inline-flex' }}>{conf.icon}</span>
    </Tooltip>
  );
}
