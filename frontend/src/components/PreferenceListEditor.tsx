import { Button, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import { FieldConditionNode } from './FilterBuilder';
import type { FieldCondition } from '../types';

const { Text } = Typography;

// Sensible starting row for a new preference: the most common use case is a
// subtitle-language preference (e.g. subtitle_langs contains zh-CN).
const DEFAULT_CONDITION: FieldCondition = {
  field: 'subtitle_langs',
  operator: 'contains',
  value: '',
};

/** Ordered preference-rule editor for Agent pick_preferences. Row order is
    the priority: rule 1 wins first, later rows break remaining ties. Rules
    only rank candidates, they never filter the conflict set. */
export default function PreferenceListEditor({
  value,
  onChange,
  channelId,
}: {
  value: FieldCondition[];
  onChange: (v: FieldCondition[]) => void;
  channelId?: string;
}) {
  const { t } = useTranslation();

  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= value.length) return;
    const next = [...value];
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {value.map((cond, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: 4 }}>
          <Text
            type="secondary"
            style={{ fontSize: 12, lineHeight: '24px', flexShrink: 0, width: 20 }}
          >
            {i + 1}.
          </Text>
          <div style={{ flex: 1, minWidth: 0 }}>
            <FieldConditionNode
              value={cond}
              nested
              channelId={channelId}
              onChange={(c) => onChange(value.map((x, k) => (k === i ? c : x)))}
              onDelete={() => onChange(value.filter((_, k) => k !== i))}
            />
          </div>
          <Button
            htmlType="button"
            type="text"
            size="small"
            icon={<ArrowUpOutlined />}
            disabled={i === 0}
            title={t('agents.preferenceMoveUp')}
            onClick={() => move(i, -1)}
          />
          <Button
            htmlType="button"
            type="text"
            size="small"
            icon={<ArrowDownOutlined />}
            disabled={i === value.length - 1}
            title={t('agents.preferenceMoveDown')}
            onClick={() => move(i, 1)}
          />
        </div>
      ))}
      <Button
        htmlType="button"
        type="dashed"
        size="small"
        icon={<PlusOutlined />}
        style={{ alignSelf: 'flex-start', marginTop: 4 }}
        onClick={() => onChange([...value, { ...DEFAULT_CONDITION }])}
      >
        {t('agents.addPreference')}
      </Button>
    </div>
  );
}
