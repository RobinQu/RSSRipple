import { Checkbox, Tag, Typography } from 'antd';
import { formatBytes } from '../../utils/format';

const { Text } = Typography;

/** One unassigned candidate file row in the wizard's file-mapping step.
 * Module-level so the drag/shift selection handlers arrive as props (the
 * react-hooks refs rule rejects inline handler closures calling ref-reading
 * functions inside the grouped cluster render). */
export function PoolFileRow({
  name,
  size,
  idx,
  checked,
  parsed,
  indented,
  onPointerDown,
  onPointerEnter,
  onToggle,
}: {
  name: string;
  size: number;
  idx: number;
  checked: boolean;
  parsed: { season: number | null; episode: number | null } | undefined;
  /** Align under a cluster header when the pool is cluster-grouped. */
  indented: boolean;
  onPointerDown: (e: React.PointerEvent<HTMLDivElement>, path: string, index: number) => void;
  onPointerEnter: (index: number) => void;
  onToggle: (path: string, index: number, shiftKey: boolean) => void;
}) {
  return (
    <div
      data-file-index={idx}
      role="button"
      tabIndex={0}
      aria-pressed={checked}
      onPointerDown={(e) => onPointerDown(e, name, idx)}
      onPointerEnter={() => onPointerEnter(idx)}
      onClick={(e) => {
        // Keyboard/tap fallback without drag semantics.
        if (e.detail === 0) onToggle(name, idx, e.shiftKey);
      }}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onToggle(name, idx, e.shiftKey);
        }
      }}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: `3px 4px 3px ${indented ? 26 : 4}px`,
        borderBottom: '1px solid var(--rr-border-soft)',
        background: checked ? 'var(--rr-primary-soft)' : undefined,
        cursor: 'pointer',
      }}
    >
      <Checkbox checked={checked} tabIndex={-1} />
      <span style={{ flex: 1, minWidth: 0, fontSize: 12, overflowWrap: 'anywhere', wordBreak: 'break-all', lineHeight: 1.4 }}>
        {name}
      </span>
      {parsed && (parsed.season != null || parsed.episode != null) && (
        <Tag style={{ fontSize: 10, margin: 0, width: 70, flexShrink: 0, textAlign: 'center' }} color="default">
          {parsed.season != null ? `S${parsed.season}` : ''}
          {parsed.episode != null ? ` E${parsed.episode}` : ''}
        </Tag>
      )}
      <Text type="secondary" style={{ fontSize: 11, flexShrink: 0, width: 64, textAlign: 'right' }}>
        {formatBytes(size)}
      </Text>
    </div>
  );
}

export function LabeledRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
        {label}
      </Text>
      {children}
    </div>
  );
}
