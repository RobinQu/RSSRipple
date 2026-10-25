import { useEffect, useState } from 'react';
import { Input, Radio, Select, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import { channelsApi, type RequiredFieldCatalogEntry } from '../api/channels';
import type { RequiredMetadataDefaults } from '../types';

const { Text } = Typography;

interface Props {
  requiredFields?: string[] | null;
  value?: RequiredMetadataDefaults;
  onChange?: (value: RequiredMetadataDefaults) => void;
}

function hasDefault(defaults: RequiredMetadataDefaults, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(defaults, key);
}

export default function RequiredMetadataDefaultsInput({
  requiredFields,
  value,
  onChange,
}: Props) {
  const { t } = useTranslation();
  const [catalog, setCatalog] = useState<RequiredFieldCatalogEntry[]>([]);
  const defaults = value ?? {};

  useEffect(() => {
    channelsApi.requiredFieldCatalog().then((response) => {
      if (response.success && response.data) setCatalog(response.data.fields || []);
    });
  }, []);

  const selected = new Set([
    ...(requiredFields ?? []),
    ...catalog.filter((field) => field.locked).map((field) => field.key),
  ]);
  const fields = catalog.filter((field) => field.defaultable && selected.has(field.key));

  const setMode = (field: RequiredFieldCatalogEntry, mode: 'manual' | 'default') => {
    const next = { ...defaults };
    if (mode === 'manual') {
      delete next[field.key];
    } else if (!hasDefault(defaults, field.key)) {
      next[field.key] = field.value_type === 'string_list' ? [] : '';
    }
    onChange?.(next);
  };

  if (fields.length === 0) {
    return <Text type="secondary">{t('channels.requiredMetadataDefaultsNone')}</Text>;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {fields.map((field) => {
        const usesDefault = hasDefault(defaults, field.key);
        const defaultValue = defaults[field.key];
        return (
          <div
            key={field.key}
            style={{
              display: 'flex',
              alignItems: 'center',
              flexWrap: 'wrap',
              gap: 8,
            }}
          >
            <Text style={{ width: 140 }}>
              {t(`channels.requiredField_${field.key}`, { defaultValue: field.key })}
            </Text>
            <Radio.Group
              size="small"
              optionType="button"
              value={usesDefault ? 'default' : 'manual'}
              options={[
                { value: 'manual', label: t('channels.requiredFieldManual') },
                { value: 'default', label: t('channels.requiredFieldDefault') },
              ]}
              onChange={(event) => setMode(field, event.target.value)}
            />
            {usesDefault &&
              (field.value_type === 'string_list' ? (
                <Select
                  mode="tags"
                  size="small"
                  style={{ minWidth: 220, flex: 1 }}
                  tokenSeparators={[',', '，']}
                  placeholder={t('channels.requiredFieldDefaultListPlaceholder')}
                  value={Array.isArray(defaultValue) ? defaultValue : []}
                  onChange={(items) => onChange?.({ ...defaults, [field.key]: items })}
                />
              ) : (
                <Input
                  size="small"
                  style={{ width: 220 }}
                  placeholder={t('channels.requiredFieldDefaultPlaceholder')}
                  value={typeof defaultValue === 'string' ? defaultValue : ''}
                  onChange={(event) =>
                    onChange?.({ ...defaults, [field.key]: event.target.value })
                  }
                />
              ))}
          </div>
        );
      })}
    </div>
  );
}
