import { useCallback } from 'react';
import {
  Card,
  Select,
  Input,
  InputNumber,
  Switch,
  Space,
  Button,
  Typography,
  Divider,
} from 'antd';
import {
  MinusCircleOutlined,
  PlusOutlined,
  DeleteOutlined,
  GroupOutlined,
} from '@ant-design/icons';
import type {
  BoolCondition,
  FieldCondition,
  FilterConfig,
  FilterField,
  FilterOperator,
  NumberFilterField,
  NumberOperator,
  StringFilterField,
  StringOperator,
} from '../types';

const STRING_FIELDS: { value: StringFilterField; label: string }[] = [
  { value: 'subtitle_group', label: '字幕组 (subtitle_group)' },
  { value: 'resolution', label: '分辨率 (resolution)' },
  { value: 'source', label: '来源 (source)' },
  { value: 'video_codec', label: '视频编码 (video_codec)' },
  { value: 'audio_codec', label: '音频编码 (audio_codec)' },
  { value: 'subtitle_type', label: '字幕类型 (subtitle_type)' },
  { value: 'container', label: '容器 (container)' },
  { value: 'title_cn', label: '中文标题 (title_cn)' },
  { value: 'title_en', label: '英文标题 (title_en)' },
  { value: 'search_title', label: '搜索标题 (search_title)' },
];

const NUMBER_FIELDS: { value: NumberFilterField; label: string }[] = [
  { value: 'file_size', label: '文件大小 (file_size)' },
  { value: 'episode', label: '集数 (episode)' },
  { value: 'season', label: '季数 (season)' },
];

const STRING_OPERATORS: { value: StringOperator; label: string }[] = [
  { value: 'eq', label: '等于' },
  { value: 'ne', label: '不等于' },
  { value: 'contains', label: '包含' },
  { value: 'fuzzy', label: '模糊匹配' },
  { value: 'in', label: '属于 (多值)' },
  { value: 'regex', label: '正则' },
];

const NUMBER_OPERATORS: { value: NumberOperator; label: string }[] = [
  { value: 'eq', label: '等于' },
  { value: 'ne', label: '不等于' },
  { value: 'gt', label: '大于' },
  { value: 'gte', label: '大于等于' },
  { value: 'lt', label: '小于' },
  { value: 'lte', label: '小于等于' },
  { value: 'in', label: '属于 (多值)' },
];

const isBoolCondition = (node: unknown): node is BoolCondition => {
  return (
    typeof node === 'object' &&
    node !== null &&
    'combinator' in node &&
    'conditions' in node
  );
};

const isFieldCondition = (node: unknown): node is FieldCondition => {
  return (
    typeof node === 'object' &&
    node !== null &&
    'field' in node &&
    'operator' in node
  );
};

const NUMBER_FIELD_SET = new Set<FilterField>(['file_size', 'episode', 'season']);

function isNumberField(field: FilterField | undefined): boolean {
  return !!field && NUMBER_FIELD_SET.has(field);
}

function emptyBool(): BoolCondition {
  return { combinator: 'and', conditions: [] };
}

function emptyField(): FieldCondition {
  return { field: 'subtitle_group', operator: 'eq', value: '' };
}

/** Deep clone a filter node */
function cloneFilter<T>(v: T): T {
  if (v === null || v === undefined) return v;
  return JSON.parse(JSON.stringify(v));
}

/** Field condition editor */
function FieldConditionNode({
  value,
  onChange,
  onDelete,
  nested = false,
}: {
  value: FieldCondition;
  onChange: (v: FieldCondition) => void;
  onDelete: () => void;
  nested?: boolean;
}) {
  const numField = isNumberField(value.field);

  // When switching field types, reset operator/value appropriately
  const handleFieldChange = (field: FilterField) => {
    const willBeNum = isNumberField(field);
    let op: FilterOperator = value.operator;
    let val: string | number | string[] = value.value;
    if (willBeNum) {
      if (!['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in'].includes(op)) op = 'eq';
      if (op === 'in') val = [];
      else val = 0;
    } else {
      if (!['eq', 'ne', 'contains', 'fuzzy', 'in', 'regex'].includes(op))
        op = 'contains';
      if (op === 'in') val = [];
      else val = '';
    }
    onChange({ ...value, field, operator: op, value: val });
  };

  const handleOperatorChange = (op: FilterOperator) => {
    let val: string | number | string[] = value.value;
    if (op === 'in') {
      val = Array.isArray(value.value) ? value.value : [];
    } else if (isNumberField(value.field)) {
      val = typeof value.value === 'number' ? value.value : 0;
    } else {
      val = typeof value.value === 'string' ? value.value : '';
    }
    onChange({ ...value, operator: op, value: val });
  };

  const fieldOptions = [
    { label: '字符串字段', options: STRING_FIELDS },
    { label: '数字字段', options: NUMBER_FIELDS },
  ];

  const operators: { value: FilterOperator; label: string }[] = numField
    ? (NUMBER_OPERATORS as { value: FilterOperator; label: string }[])
    : (STRING_OPERATORS as { value: FilterOperator; label: string }[]);

  return (
    <div
      style={{
        display: 'flex',
        gap: 8,
        alignItems: 'flex-start',
        flexWrap: 'wrap',
        padding: nested ? '8px 0' : '8px 12px',
        borderRadius: 8,
        background: nested ? 'rgba(255,255,255,0.02)' : 'transparent',
      }}
    >
      <Select
        value={value.field}
        onChange={handleFieldChange}
        options={fieldOptions}
        style={{ width: 180 }}
        size="small"
        popupMatchSelectWidth={false}
      />
      <Select
        value={value.operator}
        onChange={handleOperatorChange}
        options={operators}
        style={{ width: 130 }}
        size="small"
      />
      {value.operator === 'in' ? (
        <Select
          mode="tags"
          style={{ minWidth: 200, flex: 1 }}
          value={Array.isArray(value.value) ? value.value : []}
          onChange={(tags) => onChange({ ...value, value: tags })}
          placeholder="输入值后回车添加"
          size="small"
          tokenSeparators={[',']}
        />
      ) : numField ? (
        <InputNumber
          value={typeof value.value === 'number' ? value.value : 0}
          onChange={(n) => onChange({ ...value, value: n ?? 0 })}
          style={{ width: 160 }}
          size="small"
          placeholder="数值"
        />
      ) : (
        <Input
          value={typeof value.value === 'string' ? value.value : ''}
          onChange={(e) => onChange({ ...value, value: e.target.value })}
          placeholder="值"
          size="small"
          style={{ minWidth: 160, flex: 1 }}
        />
      )}
      <Button
        type="text"
        size="small"
        danger
        icon={<MinusCircleOutlined />}
        onClick={onDelete}
      />
    </div>
  );
}

/** Bool condition editor (recursive) */
function BoolConditionNode({
  value,
  onChange,
  onDelete,
  isRoot = false,
  depth = 0,
}: {
  value: BoolCondition;
  onChange: (v: BoolCondition) => void;
  onDelete?: () => void;
  isRoot?: boolean;
  depth?: number;
}) {
  const updateCondition = (idx: number, newVal: BoolCondition | FieldCondition) => {
    const next = cloneFilter(value);
    next.conditions[idx] = newVal;
    onChange(next);
  };

  const removeCondition = (idx: number) => {
    const next = cloneFilter(value);
    next.conditions.splice(idx, 1);
    onChange(next);
  };

  const addField = () => {
    const next = cloneFilter(value);
    next.conditions.push(emptyField());
    onChange(next);
  };

  const addGroup = () => {
    const next = cloneFilter(value);
    next.conditions.push(emptyBool());
    onChange(next);
  };

  return (
    <div
      style={{
        padding: isRoot ? 0 : '12px',
        border: isRoot ? 'none' : '1px dashed rgba(255,255,255,0.12)',
        borderRadius: 10,
        background: isRoot ? 'transparent' : 'rgba(255,255,255,0.02)',
        position: 'relative',
      }}
    >
      {/* Toolbar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginBottom: value.conditions.length > 0 ? 8 : 0,
          flexWrap: 'wrap',
        }}
      >
        <Select
          value={value.combinator}
          onChange={(c) => onChange({ ...value, combinator: c })}
          size="small"
          style={{ width: 90 }}
          options={[
            { value: 'and', label: 'AND (全部)' },
            { value: 'or', label: 'OR (任一)' },
          ]}
        />
        <Switch
          checked={!!value.is_not}
          onChange={(v) => onChange({ ...value, is_not: v })}
          checkedChildren="NOT"
          unCheckedChildren="NOT"
          size="small"
        />
        {!isRoot && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            子条件组
          </Typography.Text>
        )}
        <div style={{ flex: 1 }} />
        {!isRoot && onDelete && (
          <Button
            type="text"
            size="small"
            danger
            icon={<DeleteOutlined />}
            onClick={onDelete}
          />
        )}
      </div>

      {/* Conditions */}
      {value.conditions.length === 0 && isRoot && (
        <div
          style={{
            padding: '24px 0',
            textAlign: 'center',
            color: 'rgba(255,255,255,0.3)',
            fontSize: 13,
            border: '1px dashed rgba(255,255,255,0.1)',
            borderRadius: 8,
          }}
        >
          暂无过滤条件，点击下方按钮添加
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: depth === 0 ? 8 : 4 }}>
        {value.conditions.map((cond, idx) => {
          if (isBoolCondition(cond)) {
            return (
              <BoolConditionNode
                key={idx}
                value={cond}
                isRoot={false}
                depth={depth + 1}
                onChange={(v) => updateCondition(idx, v)}
                onDelete={() => removeCondition(idx)}
              />
            );
          }
          if (isFieldCondition(cond)) {
            return (
              <FieldConditionNode
                key={idx}
                value={cond}
                nested={!isRoot}
                onChange={(v) => updateCondition(idx, v)}
                onDelete={() => removeCondition(idx)}
              />
            );
          }
          return null;
        })}
      </div>

      {/* Add buttons */}
      <Space size={8} style={{ marginTop: 8 }}>
        <Button
          size="small"
          icon={<PlusOutlined />}
          onClick={addField}
          type={value.conditions.length === 0 ? 'primary' : 'default'}
        >
          添加条件
        </Button>
        <Button size="small" icon={<GroupOutlined />} onClick={addGroup}>
          添加条件组
        </Button>
      </Space>

      {!isRoot && <Divider style={{ margin: '8px 0', opacity: 0.1 }} />}
    </div>
  );
}

export interface FilterBuilderProps {
  value: BoolCondition | null;
  onChange: (v: BoolCondition | null) => void;
  /** Compact mode - renders inside a smaller container */
  compact?: boolean;
}

export default function FilterBuilder({
  value,
  onChange,
  compact = false,
}: FilterBuilderProps) {
  const root = value ?? emptyBool();

  const handleChange = useCallback(
    (v: BoolCondition) => {
      // If completely empty, signal null? Keep a minimum structure for usability.
      onChange(v);
    },
    [onChange],
  );

  if (compact) {
    return (
      <BoolConditionNode value={root} onChange={handleChange} isRoot />
    );
  }

  return (
    <Card
      size="small"
      styles={{ body: { padding: 16 } }}
      style={{ background: 'transparent' }}
    >
      <BoolConditionNode value={root} onChange={handleChange} isRoot />
    </Card>
  );
}

/** Helper to normalize a possibly null filter to a valid root */
export function normalizeFilter(v: FilterConfig | null | undefined): BoolCondition {
  if (isBoolCondition(v)) return v as BoolCondition;
  return emptyBool();
}

/** Helper to check if a filter is effectively empty (no conditions anywhere) */
export function isFilterEmpty(v: FilterConfig | null | undefined): boolean {
  if (!v) return true;
  if (!isBoolCondition(v)) return true;
  if (!v.conditions || v.conditions.length === 0) return true;
  return false;
}
