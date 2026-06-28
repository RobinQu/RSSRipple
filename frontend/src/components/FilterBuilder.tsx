import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
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
import type { TFunction } from 'i18next';

function useFieldOptions(t: TFunction) {
  const STRING_FIELDS: { value: StringFilterField; label: string }[] = [
    { value: 'subtitle_group', label: t('filter.subtitleGroup') },
    { value: 'resolution', label: t('filter.resolution') },
    { value: 'source', label: t('filter.source') },
    { value: 'video_codec', label: t('filter.videoCodec') },
    { value: 'audio_codec', label: t('filter.audioCodec') },
    { value: 'subtitle_type', label: t('filter.subtitleType') },
    { value: 'container', label: t('filter.container') },
    { value: 'title_cn', label: t('filter.titleCn') },
    { value: 'title_en', label: t('filter.titleEn') },
    { value: 'search_title', label: t('filter.searchTitle') },
  ];

  const NUMBER_FIELDS: { value: NumberFilterField; label: string }[] = [
    { value: 'file_size', label: t('filter.fileSize') },
    { value: 'episode', label: t('filter.episode') },
    { value: 'season', label: t('filter.season') },
  ];

  const fieldOptions = [
    { label: t('filter.stringField'), options: STRING_FIELDS },
    { label: t('filter.numberField'), options: NUMBER_FIELDS },
  ];

  const STRING_OPERATORS: { value: StringOperator; label: string }[] = [
    { value: 'eq', label: t('filter.eq') },
    { value: 'ne', label: t('filter.ne') },
    { value: 'contains', label: t('filter.contains') },
    { value: 'fuzzy', label: t('filter.fuzzy') },
    { value: 'in', label: t('filter.in') },
    { value: 'regex', label: t('filter.regex') },
  ];

  const NUMBER_OPERATORS: { value: NumberOperator; label: string }[] = [
    { value: 'eq', label: t('filter.eq') },
    { value: 'ne', label: t('filter.ne') },
    { value: 'gt', label: t('filter.gt') },
    { value: 'gte', label: t('filter.gte') },
    { value: 'lt', label: t('filter.lt') },
    { value: 'lte', label: t('filter.lte') },
    { value: 'in', label: t('filter.in') },
  ];

  return { STRING_FIELDS, NUMBER_FIELDS, fieldOptions, STRING_OPERATORS, NUMBER_OPERATORS };
}

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
  const { t } = useTranslation();
  const { fieldOptions, STRING_OPERATORS, NUMBER_OPERATORS } = useFieldOptions(t);
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
        background: nested ? '#f7f7f5' : 'transparent',
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
          placeholder={t('filter.enterValue')}
          size="small"
          tokenSeparators={[',']}
        />
      ) : numField ? (
        <InputNumber
          value={typeof value.value === 'number' ? value.value : 0}
          onChange={(n) => onChange({ ...value, value: n ?? 0 })}
          style={{ width: 160 }}
          size="small"
          placeholder={t('filter.numericValue')}
        />
      ) : (
        <Input
          value={typeof value.value === 'string' ? value.value : ''}
          onChange={(e) => onChange({ ...value, value: e.target.value })}
          placeholder={t('filter.value')}
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
  const { t } = useTranslation();

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
        border: isRoot ? 'none' : '1px dashed #d9d9dd',
        borderRadius: 10,
        background: isRoot ? 'transparent' : '#f7f7f5',
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
            { value: 'and', label: t('filter.and') },
            { value: 'or', label: t('filter.or') },
          ]}
        />
        <Switch
          checked={!!value.is_not}
          onChange={(v) => onChange({ ...value, is_not: v })}
          checkedChildren={t('filter.not')}
          unCheckedChildren={t('filter.not')}
          size="small"
        />
        {!isRoot && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t('filter.subGroup')}
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
            color: '#93939f',
            fontSize: 13,
            border: '1px dashed #d9d9dd',
            borderRadius: 8,
          }}
        >
          {t('filter.noConditions')}
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
          {t('filter.addCondition')}
        </Button>
        <Button size="small" icon={<GroupOutlined />} onClick={addGroup}>
          {t('filter.addConditionGroup')}
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
