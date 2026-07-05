import { useCallback, useEffect, useRef, useState } from 'react';
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
import { channelsApi } from '../api/channels';
import type {
  BoolCondition,
  FieldCondition,
  FilterConfig,
  FilterField,
  FilterOperator,
} from '../types';
import type { TFunction } from 'i18next';

// ---------------------------------------------------------------------------
// Field & operator metadata — kept in one place so future additions only
// need to touch this section. Backed by ``filter_engine.py`` on the server.
// ---------------------------------------------------------------------------

type FieldType = 'string' | 'number' | 'bool' | 'list';

const FIELD_TYPES: Record<FilterField, FieldType> = {
  subtitle_group: 'string',
  resolution: 'string',
  source: 'string',
  video_codec: 'string',
  audio_codec: 'string',
  subtitle_type: 'string',
  container: 'string',
  // episode_confidence is stored as a plain string on the backend but the UI
  // treats it as an enum so users pick from a fixed value list.
  episode_confidence: 'string',
  title_cn: 'string',
  title_en: 'string',
  search_title: 'string',
  file_size: 'number',
  episode: 'number',
  season: 'number',
  episode_start: 'number',
  episode_end: 'number',
  absolute_episode: 'number',
  is_batch: 'bool',
  subtitle_langs: 'list',
};

// Fields with a bounded, meaningful autocomplete set. Autocomplete is only
// worth doing for eq/ne/contains/fuzzy on string columns; list/bool/number
// use their own dedicated inputs.
const AUTOCOMPLETE_FIELDS: Set<FilterField> = new Set([
  'subtitle_group',
  'resolution',
  'source',
  'video_codec',
  'audio_codec',
  'subtitle_type',
  'container',
]);
const AUTOCOMPLETE_OPERATORS = new Set<FilterOperator>([
  'eq', 'ne', 'contains', 'fuzzy',
]);

// Static tag set for subtitle_langs — BCP-47 tags used by the backend
// pre-parser + MetadataAgent. Users can still type a custom tag.
const SUBTITLE_LANG_OPTIONS = ['zh-CN', 'zh-TW', 'ja', 'en', 'multi'];

// Enum-string fields have a fixed value set and skip the free-text
// autocomplete path so users can't accidentally type an unknown value.
const ENUM_FIELDS: Record<string, string[]> = {
  episode_confidence: ['raw', 'reconciled', 'ambiguous', 'manual'],
};

const STRING_OPERATORS: FilterOperator[] = ['eq', 'ne', 'contains', 'fuzzy', 'in', 'regex'];
const NUMBER_OPERATORS: FilterOperator[] = ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in'];
const BOOL_OPERATORS: FilterOperator[] = ['eq', 'ne'];
const LIST_OPERATORS: FilterOperator[] = ['contains', 'in', 'eq', 'ne'];
const ENUM_OPERATORS: FilterOperator[] = ['eq', 'ne', 'in'];

function operatorsFor(field: FilterField): FilterOperator[] {
  if (field in ENUM_FIELDS) return ENUM_OPERATORS;
  switch (FIELD_TYPES[field]) {
    case 'string': return STRING_OPERATORS;
    case 'number': return NUMBER_OPERATORS;
    case 'bool': return BOOL_OPERATORS;
    case 'list': return LIST_OPERATORS;
  }
}

function useFieldOptions(t: TFunction) {
  const string_fields: FilterField[] = [
    'subtitle_group', 'resolution', 'source', 'video_codec', 'audio_codec',
    'subtitle_type', 'container', 'title_cn', 'title_en', 'search_title',
  ];
  const number_fields: FilterField[] = [
    'file_size', 'episode', 'season', 'episode_start', 'episode_end', 'absolute_episode',
  ];
  const bool_fields: FilterField[] = ['is_batch'];
  const list_fields: FilterField[] = ['subtitle_langs'];
  const enum_fields: FilterField[] = ['episode_confidence'];

  const toOption = (f: FilterField) => ({ value: f, label: t(`filter.${f}` as never, { defaultValue: f }) });

  const fieldOptions = [
    { label: t('filter.stringField'), options: string_fields.map(toOption) },
    { label: t('filter.numberField'), options: number_fields.map(toOption) },
    { label: t('filter.boolField'), options: bool_fields.map(toOption) },
    { label: t('filter.listField'), options: list_fields.map(toOption) },
    { label: t('filter.enumField'), options: enum_fields.map(toOption) },
  ];

  const operatorLabel = (op: FilterOperator) => t(`filter.${op}`);

  return { fieldOptions, operatorLabel };
}

// ---------------------------------------------------------------------------
// Type guards & defaults
// ---------------------------------------------------------------------------

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

function emptyBool(): BoolCondition {
  return { combinator: 'and', conditions: [] };
}

function emptyField(): FieldCondition {
  return { field: 'subtitle_group', operator: 'eq', value: '' };
}

function defaultValueFor(field: FilterField, op: FilterOperator): string | number | boolean | string[] {
  const type = FIELD_TYPES[field];
  if (op === 'in') return [];
  switch (type) {
    case 'number': return 0;
    case 'bool': return true;
    case 'list': return '';
    default: return '';
  }
}

function cloneFilter<T>(v: T): T {
  if (v === null || v === undefined) return v;
  return JSON.parse(JSON.stringify(v));
}

// ---------------------------------------------------------------------------
// Autocomplete Select — server-side prefix search on the current channel
// ---------------------------------------------------------------------------

interface AutocompleteSelectProps {
  channelId?: string;
  field: FilterField;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}

function AutocompleteSelect({
  channelId,
  field,
  value,
  onChange,
  placeholder,
}: AutocompleteSelectProps) {
  const [options, setOptions] = useState<{ value: string; label: string }[]>([]);
  const [loading, setLoading] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const requestSeq = useRef(0);

  const load = useCallback(
    async (q: string) => {
      if (!channelId) return;
      const seq = ++requestSeq.current;
      setLoading(true);
      const r = await channelsApi.fieldValues(channelId, field, q, 10);
      // Only apply the most recent response — stale keystrokes are dropped.
      if (seq !== requestSeq.current) return;
      if (r.success) {
        setOptions((r.data || []).map((v: string) => ({ value: v, label: v })));
      }
      setLoading(false);
    },
    [channelId, field],
  );

  useEffect(() => {
    // Warm the dropdown once on mount so the user sees candidate values as
    // soon as they open the Select — before typing anything.
    load('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channelId, field]);

  const handleSearch = (q: string) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => load(q), 200);
  };

  return (
    <Select
      showSearch
      allowClear
      // ``mode="tags"`` keeps free-text entries — the user can still type
      // "1080P" (case variant) or a value that isn't in the channel yet.
      mode="tags"
      maxCount={1}
      style={{ minWidth: 200, flex: 1 }}
      size="small"
      value={value ? [value] : []}
      onChange={(tags) => onChange(Array.isArray(tags) ? (tags[tags.length - 1] ?? '') : (tags as string))}
      onSearch={handleSearch}
      options={options}
      placeholder={placeholder}
      loading={loading}
      filterOption={false}
      notFoundContent={loading ? '…' : null}
    />
  );
}

// ---------------------------------------------------------------------------
// FieldConditionNode — the leaf editor
// ---------------------------------------------------------------------------

function FieldConditionNode({
  value,
  onChange,
  onDelete,
  channelId,
  nested = false,
}: {
  value: FieldCondition;
  onChange: (v: FieldCondition) => void;
  onDelete: () => void;
  channelId?: string;
  nested?: boolean;
}) {
  const { t } = useTranslation();
  const { fieldOptions, operatorLabel } = useFieldOptions(t);
  const fieldType = FIELD_TYPES[value.field];

  const handleFieldChange = (field: FilterField) => {
    const newType = FIELD_TYPES[field];
    // Coerce operator to a legal one for the new field type.
    let op: FilterOperator = value.operator;
    if (!operatorsFor(field).includes(op)) {
      op = operatorsFor(field)[0];
    }
    onChange({ ...value, field, operator: op, value: defaultValueFor(field, op) });
    void newType; // keep types quiet
  };

  const handleOperatorChange = (op: FilterOperator) => {
    // Value type may change when switching to/from 'in' or between
    // number/string; normalize.
    let v: string | number | boolean | string[] = value.value as never;
    const type = FIELD_TYPES[value.field];
    if (op === 'in') {
      v = Array.isArray(value.value) ? (value.value as string[]) : [];
    } else if (type === 'number') {
      v = typeof value.value === 'number' ? value.value : 0;
    } else if (type === 'bool') {
      v = typeof value.value === 'boolean' ? value.value : true;
    } else {
      v = typeof value.value === 'string' ? value.value : '';
    }
    onChange({ ...value, operator: op, value: v });
  };

  const operators = operatorsFor(value.field).map((op) => ({
    value: op,
    label: operatorLabel(op),
  }));

  const showAutocomplete =
    fieldType === 'string' &&
    AUTOCOMPLETE_FIELDS.has(value.field) &&
    AUTOCOMPLETE_OPERATORS.has(value.operator) &&
    !!channelId;

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

      {/* --- Value input, varies by (fieldType, operator) --- */}
      {value.field in ENUM_FIELDS && value.operator === 'in' ? (
        <Select
          mode="multiple"
          style={{ minWidth: 200, flex: 1 }}
          value={Array.isArray(value.value) ? (value.value as string[]) : []}
          onChange={(tags) => onChange({ ...value, value: tags })}
          size="small"
          options={(ENUM_FIELDS[value.field] || []).map((v) => ({
            value: v,
            label: t(`filter.enumValue_${v}` as never, { defaultValue: v }),
          }))}
        />
      ) : value.field in ENUM_FIELDS ? (
        <Select
          style={{ minWidth: 200, flex: 1 }}
          value={typeof value.value === 'string' ? value.value : ''}
          onChange={(v) => onChange({ ...value, value: v })}
          size="small"
          allowClear
          options={(ENUM_FIELDS[value.field] || []).map((v) => ({
            value: v,
            label: t(`filter.enumValue_${v}` as never, { defaultValue: v }),
          }))}
        />
      ) : value.operator === 'in' && fieldType === 'list' ? (
        <Select
          mode="tags"
          style={{ minWidth: 200, flex: 1 }}
          value={Array.isArray(value.value) ? (value.value as string[]) : []}
          onChange={(tags) => onChange({ ...value, value: tags })}
          placeholder={t('filter.enterValue')}
          options={SUBTITLE_LANG_OPTIONS.map((v) => ({ value: v, label: v }))}
          size="small"
          tokenSeparators={[',']}
        />
      ) : value.operator === 'in' ? (
        <Select
          mode="tags"
          style={{ minWidth: 200, flex: 1 }}
          value={Array.isArray(value.value) ? (value.value as string[]) : []}
          onChange={(tags) => onChange({ ...value, value: tags })}
          placeholder={t('filter.enterValue')}
          size="small"
          tokenSeparators={[',']}
        />
      ) : fieldType === 'bool' ? (
        <Select
          value={value.value === true || value.value === 'true' ? 'true' : 'false'}
          onChange={(v) => onChange({ ...value, value: v === 'true' })}
          size="small"
          style={{ width: 130 }}
          options={[
            { value: 'true', label: t('filter.true') },
            { value: 'false', label: t('filter.false') },
          ]}
        />
      ) : fieldType === 'list' ? (
        // Single-value operators on list field: use the same tags dropdown but
        // pinned to one selection so we still get autocomplete on the fixed
        // set of language codes.
        <Select
          showSearch
          allowClear
          mode="tags"
          maxCount={1}
          style={{ minWidth: 200, flex: 1 }}
          value={typeof value.value === 'string' && value.value ? [value.value] : []}
          onChange={(tags) => onChange({ ...value, value: Array.isArray(tags) ? (tags[tags.length - 1] ?? '') : (tags as string) })}
          size="small"
          options={SUBTITLE_LANG_OPTIONS.map((v) => ({ value: v, label: v }))}
          placeholder={t('filter.value')}
        />
      ) : fieldType === 'number' ? (
        <InputNumber
          value={typeof value.value === 'number' ? value.value : 0}
          onChange={(n) => onChange({ ...value, value: n ?? 0 })}
          style={{ width: 160 }}
          size="small"
          placeholder={t('filter.numericValue')}
        />
      ) : showAutocomplete ? (
        <AutocompleteSelect
          channelId={channelId}
          field={value.field}
          value={typeof value.value === 'string' ? value.value : ''}
          onChange={(v) => onChange({ ...value, value: v })}
          placeholder={t('filter.value')}
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
        htmlType="button"
        type="text"
        size="small"
        danger
        icon={<MinusCircleOutlined />}
        onClick={onDelete}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// BoolConditionNode (recursive)
// ---------------------------------------------------------------------------

function BoolConditionNode({
  value,
  onChange,
  onDelete,
  isRoot = false,
  depth = 0,
  channelId,
}: {
  value: BoolCondition;
  onChange: (v: BoolCondition) => void;
  onDelete?: () => void;
  isRoot?: boolean;
  depth?: number;
  channelId?: string;
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
          unCheckedChildren="--"
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
            htmlType="button"
            type="text"
            size="small"
            danger
            icon={<DeleteOutlined />}
            onClick={onDelete}
          />
        )}
      </div>

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
                channelId={channelId}
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
                channelId={channelId}
              />
            );
          }
          return null;
        })}
      </div>

      <Space size={8} style={{ marginTop: 8 }}>
        <Button
          htmlType="button"
          size="small"
          icon={<PlusOutlined />}
          onClick={addField}
          type={value.conditions.length === 0 ? 'primary' : 'default'}
        >
          {t('filter.addCondition')}
        </Button>
        <Button htmlType="button" size="small" icon={<GroupOutlined />} onClick={addGroup}>
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
  /** Channel context — enables autocomplete of real values on eq/ne. */
  channelId?: string;
}

export default function FilterBuilder({
  value,
  onChange,
  compact = false,
  channelId,
}: FilterBuilderProps) {
  const root = value ?? emptyBool();

  const handleChange = useCallback(
    (v: BoolCondition) => {
      onChange(v);
    },
    [onChange],
  );

  if (compact) {
    return (
      <BoolConditionNode value={root} onChange={handleChange} isRoot channelId={channelId} />
    );
  }

  return (
    <Card
      size="small"
      styles={{ body: { padding: 16 } }}
      style={{ background: 'transparent' }}
    >
      <BoolConditionNode value={root} onChange={handleChange} isRoot channelId={channelId} />
    </Card>
  );
}

export function normalizeFilter(v: FilterConfig | null | undefined): BoolCondition {
  if (isBoolCondition(v)) return v as BoolCondition;
  return emptyBool();
}

export function isFilterEmpty(v: FilterConfig | null | undefined): boolean {
  if (!v) return true;
  if (!isBoolCondition(v)) return true;
  if (!v.conditions || v.conditions.length === 0) return true;
  return false;
}
