import { useMemo } from 'react';
import { Button, Checkbox, Popover, Tooltip, Typography } from 'antd';
import { ArrowDown, ArrowUp, Columns3, RotateCcw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { effectiveColumnState, type ChannelColumnConfig } from '../utils/requiredFields';

const { Text } = Typography;

interface Props {
  /** Saved config for this channel (null = defaults). */
  config: ChannelColumnConfig | null;
  /** Channel-declared required field keys — drives default state. */
  declared: string[];
  onChange: (next: ChannelColumnConfig | null) => void;
}

/** Column settings popover for the channel resource tables: every catalog
 * field can be shown/hidden and reordered (作品/操作 are fixed columns
 * outside this list). The config is persisted per channel by the parent. */
export default function ColumnSettings({ config, declared, onChange }: Props) {
  const { t } = useTranslation();
  const { order, hidden } = useMemo(
    () => effectiveColumnState(config, declared),
    [config, declared],
  );
  const label = (key: string) =>
    t(`channels.requiredField_${key}`, { defaultValue: key });

  const commit = (nextOrder: string[], nextHidden: string[]) =>
    onChange({ order: nextOrder, hidden: nextHidden });

  const toggle = (key: string, checked: boolean) =>
    commit(
      order,
      checked ? [...hidden].filter((k) => k !== key) : [...hidden, key],
    );

  const move = (idx: number, delta: -1 | 1) => {
    const next = [...order];
    const [key] = next.splice(idx, 1);
    next.splice(idx + delta, 0, key);
    commit(next, [...hidden]);
  };

  return (
    <Popover
      trigger="click"
      placement="bottomRight"
      content={
        <div style={{ minWidth: 260 }}>
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
              {t('channels.columnSettingsHint')}
            </Text>
            <Tooltip title={t('channels.columnSettingsReset')}>
              <Button
                type="text"
                size="small"
                icon={<RotateCcw size={12} />}
                aria-label={t('channels.columnSettingsReset')}
                onClick={() => onChange(null)}
              />
            </Tooltip>
          </div>
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: 2,
              maxHeight: 380,
              overflowY: 'auto',
            }}
          >
            {order.map((key, idx) => (
              <div key={key} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <Checkbox
                  checked={!hidden.has(key)}
                  onChange={(e) => toggle(key, e.target.checked)}
                  style={{ flex: 1, minWidth: 0 }}
                >
                  <Text style={{ fontSize: 13 }}>{label(key)}</Text>
                </Checkbox>
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
                  disabled={idx === order.length - 1}
                  aria-label={t('channels.columnMoveDown')}
                  onClick={() => move(idx, 1)}
                />
              </div>
            ))}
          </div>
        </div>
      }
    >
      <Button size="small" icon={<Columns3 size={14} />}>
        {t('channels.columnSettings')}
      </Button>
    </Popover>
  );
}
