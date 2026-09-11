import { Button, Popover, Tooltip, Typography } from 'antd';
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Plus,
  RotateCcw,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

const { Text } = Typography;

export interface SortEntry {
  key: string;
  dir: 'asc' | 'desc';
}

export interface SortField {
  key: string;
  label: string;
  /** Human-readable direction labels (e.g. 未完成优先/已完成优先 for status). */
  ascLabel: string;
  descLabel: string;
}

interface Props {
  /** All sortable fields; the active subset lives in ``value``. */
  fields: SortField[];
  /** Active sort entries in priority order (top applies first). */
  value: SortEntry[];
  /** Null resets to the consumer's default ordering. */
  onChange: (next: SortEntry[] | null) => void;
  hint?: string;
}

/** Sort-settings popover: an ordered list of sort rules (top applies first),
 * each with a direction toggle; unused fields can be appended below. The
 * config is persisted by the parent (per table instance). */
export default function SortSettings({ fields, value, onChange, hint }: Props) {
  const { t } = useTranslation();
  const fieldOf = (key: string) => fields.find((f) => f.key === key);
  const active = value.filter((e) => fieldOf(e.key));
  const inactive = fields.filter((f) => !active.some((e) => e.key === f.key));

  const commit = (next: SortEntry[]) => onChange(next);

  const move = (idx: number, delta: -1 | 1) => {
    const next = [...active];
    const [entry] = next.splice(idx, 1);
    next.splice(idx + delta, 0, entry);
    commit(next);
  };

  const toggleDir = (idx: number) => {
    const next = [...active];
    next[idx] = { ...next[idx], dir: next[idx].dir === 'asc' ? 'desc' : 'asc' };
    commit(next);
  };

  return (
    <Popover
      trigger="click"
      placement="bottomRight"
      content={
        <div style={{ minWidth: 280 }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: 6,
              gap: 12,
            }}
          >
            <Text type="secondary" style={{ fontSize: 12 }}>
              {hint ?? t('downloaders.sortSettingsHint')}
            </Text>
            <Tooltip title={t('downloaders.sortSettingsReset')}>
              <Button
                type="text"
                size="small"
                icon={<RotateCcw size={12} />}
                aria-label={t('downloaders.sortSettingsReset')}
                onClick={() => onChange(null)}
              />
            </Tooltip>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {active.map((entry, idx) => {
              const field = fieldOf(entry.key)!;
              return (
                <div key={entry.key} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <Text style={{ fontSize: 13, flex: 1, minWidth: 0 }} ellipsis>
                    {field.label}
                  </Text>
                  <Button
                    size="small"
                    style={{ fontSize: 12 }}
                    icon={entry.dir === 'asc' ? <ArrowUp size={12} /> : <ArrowDown size={12} />}
                    aria-label={t('downloaders.sortToggleDirection')}
                    onClick={() => toggleDir(idx)}
                  >
                    {entry.dir === 'asc' ? field.ascLabel : field.descLabel}
                  </Button>
                  <Button
                    type="text"
                    size="small"
                    icon={<ArrowUp size={12} />}
                    disabled={idx === 0}
                    aria-label={t('channels.columnMoveUp')}
                    onClick={() => move(idx, -1)}
                  />
                  <Button
                    type="text"
                    size="small"
                    icon={<ArrowDown size={12} />}
                    disabled={idx === active.length - 1}
                    aria-label={t('channels.columnMoveDown')}
                    onClick={() => move(idx, 1)}
                  />
                  <Button
                    type="text"
                    size="small"
                    icon={<X size={12} />}
                    aria-label={t('downloaders.sortRemove')}
                    onClick={() => commit(active.filter((_, i) => i !== idx))}
                  />
                </div>
              );
            })}
          </div>
          {inactive.length > 0 && (
            <div
              style={{
                display: 'flex',
                gap: 4,
                flexWrap: 'wrap',
                marginTop: 8,
                paddingTop: 8,
                borderTop: '1px solid var(--rr-border-soft)',
              }}
            >
              {inactive.map((f) => (
                <Button
                  key={f.key}
                  size="small"
                  type="dashed"
                  icon={<Plus size={12} />}
                  style={{ fontSize: 12 }}
                  onClick={() => commit([...active, { key: f.key, dir: 'asc' }])}
                >
                  {f.label}
                </Button>
              ))}
            </div>
          )}
        </div>
      }
    >
      <Button size="small" icon={<ArrowUpDown size={14} />}>
        {t('downloaders.sortSettings')}
      </Button>
    </Popover>
  );
}
