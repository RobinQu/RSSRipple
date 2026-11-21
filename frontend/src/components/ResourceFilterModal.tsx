import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AutoComplete, Button, Input, InputNumber, Modal, Select, Space, Typography } from 'antd';
import { MinusCircleOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { channelsApi } from '../api/channels';
import {
  RESOURCE_FILTER_FIELD_GROUPS,
  RESOURCE_FILTER_FIELD_TYPES,
  SUBTITLE_LANG_OPTIONS,
  isNoValueOperator,
  resourceFilterFieldMeta,
  resourceFilterOperators,
} from '../constants/resourceFilters';
import type { ResourceFilterField } from '../constants/resourceFilters';
import type { FilterOperator, ResourceFilterCondition } from '../types';

const { Text } = Typography;
const MAX_CONDITIONS = 20;

function emptyCondition(): ResourceFilterCondition {
  return { field: 'title_raw', operator: 'contains', value: '' };
}

function defaultValue(field: ResourceFilterField, operator: FilterOperator) {
  if (isNoValueOperator(operator)) return undefined;
  if (operator === 'in') return [];
  switch (RESOURCE_FILTER_FIELD_TYPES[field]) {
    case 'number':
      return 0;
    case 'bool':
      return true;
    default:
      return '';
  }
}

function conditionValid(condition: ResourceFilterCondition): boolean {
  if (isNoValueOperator(condition.operator)) return true;
  const value = condition.value;
  if (value === undefined || value === null) return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'string') return value.trim().length > 0;
  return true;
}

interface ValueInputProps {
  channelId?: string;
  condition: ResourceFilterCondition;
  onChange: (value: ResourceFilterCondition['value']) => void;
}

/** String field with channel-value autocomplete (subtitle group, resolution…). */
function AutocompleteValue({
  channelId,
  field,
  value,
  onChange,
}: {
  channelId?: string;
  field: ResourceFilterField;
  value: string;
  onChange: (value: string) => void;
}) {
  const [options, setOptions] = useState<{ value: string; label: string }[]>([]);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const requestSeq = useRef(0);

  const load = useCallback(
    async (q: string) => {
      if (!channelId) return;
      const seq = ++requestSeq.current;
      const response = await channelsApi.fieldValues(channelId, field, q, 20);
      if (seq !== requestSeq.current || !response.success) return;
      setOptions((response.data || []).map((item) => ({ value: item, label: item })));
    },
    [channelId, field],
  );

  useEffect(() => {
    load('');
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channelId, field]);

  const handleSearch = (q: string) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => load(q), 200);
  };

  return (
    <AutoComplete
      size="small"
      allowClear
      value={value}
      options={options}
      onSearch={handleSearch}
      onChange={(v) => onChange(v ?? '')}
      placeholder={field === 'subtitle_group' ? 'e.g. LoliHouse' : undefined}
      style={{ width: '100%' }}
      filterOption={false}
    />
  );
}

function FilterValueInput({ channelId, condition, onChange }: ValueInputProps) {
  const { t } = useTranslation();
  const meta = resourceFilterFieldMeta(condition.field);
  const value = condition.value;

  if (isNoValueOperator(condition.operator)) {
    return <Text type="secondary" style={{ fontSize: 12 }}>{t('channels.fieldFilterNoValue')}</Text>;
  }

  if (meta.enumValues) {
    return (
      <Select
        size="small"
        style={{ width: '100%' }}
        value={typeof value === 'string' ? value : undefined}
        onChange={onChange}
        options={meta.enumValues.map((item) => ({
          value: item,
          label: t(`filter.enumValue_${item}` as never, { defaultValue: item }),
        }))}
      />
    );
  }

  if (meta.type === 'bool') {
    return (
      <Select
        size="small"
        style={{ width: '100%' }}
        value={typeof value === 'boolean' ? value : true}
        onChange={onChange}
        options={[
          { value: true, label: t('common.yes') },
          { value: false, label: t('common.no') },
        ]}
      />
    );
  }

  if (meta.type === 'number') {
    return (
      <InputNumber
        size="small"
        style={{ width: '100%' }}
        value={typeof value === 'number' ? value : Number(value) || 0}
        onChange={(v) => onChange(v ?? 0)}
      />
    );
  }

  if (meta.type === 'list') {
    const listOptions =
      condition.field === 'subtitle_langs'
        ? SUBTITLE_LANG_OPTIONS.map((item) => ({ value: item, label: item }))
        : [];
    if (condition.operator === 'in') {
      return (
        <Select
          mode="tags"
          size="small"
          style={{ width: '100%' }}
          value={Array.isArray(value) ? value : []}
          onChange={(items) => onChange(items)}
          options={listOptions}
          placeholder={t('filter.enterValue')}
        />
      );
    }
    if (condition.field === 'subtitle_langs') {
      return (
        <Select
          size="small"
          style={{ width: '100%' }}
          value={typeof value === 'string' ? value : undefined}
          onChange={onChange}
          options={listOptions}
          showSearch
        />
      );
    }
  }

  if (meta.autocomplete) {
    return (
      <AutocompleteValue
        channelId={channelId}
        field={condition.field}
        value={typeof value === 'string' ? value : ''}
        onChange={onChange}
      />
    );
  }

  return (
    <Input
      size="small"
      value={typeof value === 'string' ? value : ''}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

interface Props {
  open: boolean;
  value: ResourceFilterCondition[];
  channelId?: string;
  onClose: () => void;
  onApply: (value: ResourceFilterCondition[]) => void;
}

/** Modal editor for the channel resource list's resource-local field filters. */
export default function ResourceFilterModal({
  open,
  value,
  channelId,
  onClose,
  onApply,
}: Props) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<ResourceFilterCondition[]>([]);

  useEffect(() => {
    if (open) setDraft(value.map((item) => ({ ...item })));
  }, [open, value]);

  const fieldOptions = useMemo(
    () =>
      RESOURCE_FILTER_FIELD_GROUPS.map((group) => ({
        label: t(`channels.resourceFilterGroup_${group.key}`),
        options: group.fields.map((field) => ({
          value: field,
          label: t(`channels.resourceFilterField_${field}`, {
            defaultValue: t(`filter.${field}` as never, { defaultValue: field }),
          }),
        })),
      })),
    [t],
  );

  const update = (index: number, next: ResourceFilterCondition) =>
    setDraft((prev) => prev.map((item, idx) => (idx === index ? next : item)));

  const handleFieldChange = (index: number, field: ResourceFilterField) => {
    const operator = resourceFilterOperators(field)[0];
    update(index, { field, operator, value: defaultValue(field, operator) });
  };

  const handleOperatorChange = (index: number, operator: FilterOperator) => {
    const current = draft[index];
    update(index, {
      ...current,
      operator,
      value: defaultValue(current.field, operator),
    });
  };

  const applyDisabled = draft.some((item) => !conditionValid(item));

  return (
    <Modal
      open={open}
      title={t('channels.fieldFilterTitle')}
      onCancel={onClose}
      width={760}
      destroyOnHidden
      footer={
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Button onClick={() => setDraft([])} disabled={draft.length === 0}>
            {t('channels.fieldFilterClear')}
          </Button>
          <Space>
            <Button onClick={onClose}>{t('common.cancel')}</Button>
            <Button type="primary" disabled={applyDisabled} onClick={() => onApply(draft)}>
              {t('channels.fieldFilterApply')}
            </Button>
          </Space>
        </Space>
      }
    >
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t('channels.fieldFilterHint')}
        </Text>
        {draft.length === 0 ? (
          <div
            style={{
              padding: '24px 0',
              textAlign: 'center',
              color: 'var(--rr-text-muted)',
              fontSize: 13,
              border: '1px dashed var(--rr-border)',
              borderRadius: 8,
            }}
          >
            {t('channels.fieldFilterEmpty')}
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {draft.map((condition, index) => (
              <div key={index} style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
                <Select
                  size="small"
                  value={condition.field}
                  onChange={(field) => handleFieldChange(index, field as ResourceFilterField)}
                  options={fieldOptions}
                  style={{ width: 190, flexShrink: 0 }}
                  popupMatchSelectWidth={false}
                />
                <Select
                  size="small"
                  value={condition.operator}
                  onChange={(operator) => handleOperatorChange(index, operator as FilterOperator)}
                  options={resourceFilterOperators(condition.field).map((operator) => ({
                    value: operator,
                    label: t(`filter.${operator}`),
                  }))}
                  style={{ width: 120, flexShrink: 0 }}
                />
                <div style={{ flex: 1, minWidth: 180 }}>
                  <FilterValueInput
                    channelId={channelId}
                    condition={condition}
                    onChange={(next) => update(index, { ...condition, value: next })}
                  />
                </div>
                <Button
                  type="text"
                  size="small"
                  danger
                  icon={<MinusCircleOutlined />}
                  aria-label={t('common.remove')}
                  onClick={() => setDraft((prev) => prev.filter((_, idx) => idx !== index))}
                />
              </div>
            ))}
          </div>
        )}
        <Button
          size="small"
          icon={<PlusOutlined />}
          onClick={() => setDraft((prev) => [...prev, emptyCondition()])}
          disabled={draft.length >= MAX_CONDITIONS}
        >
          {t('filter.addCondition')}
        </Button>
      </Space>
    </Modal>
  );
}
