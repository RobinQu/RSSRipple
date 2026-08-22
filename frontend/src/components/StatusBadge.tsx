import { Tag } from 'antd';
import { useTranslation } from 'react-i18next';

// Explicit (background, text, border) triples per status group. The app theme
// derives colorSuccess/colorError from very dark anchors (var(--rr-success) / var(--rr-error)),
// and antd's preset Tag tints for those come out muddy — e.g. the "已完成"
// success tag had a text colour too close to its background. Pinning explicit
// soft backgrounds + strong foregrounds keeps every status readable.
type Pair = { bg: string; fg: string; border: string };

const SUCCESS: Pair = { bg: 'var(--rr-success-soft)', fg: 'var(--rr-success)', border: 'var(--rr-success-border)' };
const ERROR: Pair = { bg: 'var(--rr-error-soft)', fg: 'var(--rr-error)', border: 'var(--rr-error-border)' };
const INFO: Pair = { bg: 'var(--rr-primary-soft)', fg: 'var(--rr-primary)', border: 'var(--rr-info-border)' };
const WARN: Pair = { bg: 'var(--rr-warning-soft)', fg: 'var(--rr-warning)', border: 'var(--rr-warning-border)' };
const NEUTRAL: Pair = { bg: 'var(--rr-surface-card)', fg: 'var(--rr-text-secondary)', border: 'var(--rr-border)' };

const statusStyleMap: Record<string, Pair> = {
  active: SUCCESS,
  inactive: NEUTRAL,
  downloading: INFO,
  completed: SUCCESS,
  connected: SUCCESS,
  decided: SUCCESS,
  paused: WARN,
  pending: NEUTRAL,
  queued: INFO,
  error: ERROR,
  failed: ERROR,
  // 人工取消 / organize 执行后例行清理都不是失败，不给错误红。
  cancelled: NEUTRAL,
  expired: NEUTRAL,
  skipped: NEUTRAL,
  seeding: INFO,
  stopped: NEUTRAL,
  fetching: INFO,
  analyzing: INFO,
  disconnected: NEUTRAL,
  success: SUCCESS,
  running: INFO,
  pending_decisions: WARN,
  processing: INFO,
  done: SUCCESS,
};

const statusKeySet = new Set([
  'active', 'inactive', 'error', 'pending', 'queued', 'downloading',
  'paused', 'completed', 'cancelled', 'connected', 'disconnected',
  'success', 'failed', 'running', 'pending_decisions', 'processing', 'done',
]);

interface StatusBadgeProps {
  status: string;
}

export default function StatusBadge({ status }: StatusBadgeProps) {
  const { t } = useTranslation();
  const key = status.toLowerCase();
  const pair = statusStyleMap[key] || NEUTRAL;
  const label = statusKeySet.has(key) ? t(`status.${key}`) : status;
  return (
    <Tag style={{ backgroundColor: pair.bg, color: pair.fg, borderColor: pair.border, margin: 0 }}>
      {label}
    </Tag>
  );
}
