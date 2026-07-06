import { Tag } from 'antd';
import { useTranslation } from 'react-i18next';

// Explicit (background, text, border) triples per status group. The app theme
// derives colorSuccess/colorError from very dark anchors (#003c33 / #b30000),
// and antd's preset Tag tints for those come out muddy — e.g. the "已完成"
// success tag had a text colour too close to its background. Pinning explicit
// soft backgrounds + strong foregrounds keeps every status readable.
type Pair = { bg: string; fg: string; border: string };

const SUCCESS: Pair = { bg: '#edfce9', fg: '#003c33', border: '#bfe3d4' };
const ERROR: Pair = { bg: '#fff1f0', fg: '#b30000', border: '#f2b8b8' };
const INFO: Pair = { bg: '#f1f5ff', fg: '#1863dc', border: '#b8cdf7' };
const WARN: Pair = { bg: '#fff1ea', fg: '#c4502a', border: '#ffbfa6' };
const NEUTRAL: Pair = { bg: '#eeece7', fg: '#616161', border: '#d9d9dd' };

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
  cancelled: ERROR,
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
};

const statusKeySet = new Set([
  'active', 'inactive', 'error', 'pending', 'queued', 'downloading',
  'paused', 'completed', 'cancelled', 'connected', 'disconnected',
  'success', 'failed', 'running', 'pending_decisions',
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
