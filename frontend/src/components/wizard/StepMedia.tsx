import { AutoComplete, Input, Select, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  DIRECT_METADATA_FIELD_KEYS,
  LANG_PRESETS,
  MEDIA_TEXT_KEYS,
  mediaLabelKey,
} from './types';
import type { ResourceEditWizardState } from './useResourceEditWizard';

const { Text } = Typography;

/** Step ③ — generic media fields (dropdowns fed by system-observed values). */
export default function StepMedia({
  wiz,
  alert,
  compact,
}: {
  wiz: ResourceEditWizardState;
  alert: React.ReactNode;
  compact: boolean;
}) {
  const { t } = useTranslation();
  const {
    detail,
    directMetadata,
    setDirectMetadata,
    media,
    setMedia,
    mediaOptions,
    subtitleLangs,
    setSubtitleLangs,
  } = wiz;

  if (!detail) return null;

  return (
    <>
      {alert}
      {DIRECT_METADATA_FIELD_KEYS.filter((k) =>
        (detail.missing_fields ?? []).includes(k),
      ).map((k) => (
        <div key={k} style={{ marginBottom: 12 }}>
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
            {t(`filters.${k}`)}
          </Text>
          <Input
            value={directMetadata[k]}
            onChange={(event) => setDirectMetadata((prev) => ({
              ...prev, [k]: event.target.value,
            }))}
            status={directMetadata[k].trim() ? undefined : 'error'}
          />
        </div>
      ))}
      <div style={{ display: 'grid', gridTemplateColumns: compact ? '1fr' : '1fr 1fr', gap: 12 }}>
        {MEDIA_TEXT_KEYS.map((k) => (
          <div key={k}>
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
              {t(`resource.${mediaLabelKey(k)}`)}
            </Text>
            <AutoComplete
              style={{ width: '100%' }}
              value={media[k]}
              onChange={(v) => setMedia((prev) => ({ ...prev, [k]: v }))}
              options={(mediaOptions[k] ?? []).map((v) => ({ value: v }))}
              filterOption={(input, opt) =>
                String(opt?.value ?? '')
                  .toLowerCase()
                  .includes(input.toLowerCase())
              }
              allowClear
            />
          </div>
        ))}
        <div>
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
            {t('resource.subtitleLangs')}
          </Text>
          <Select
            mode="tags"
            style={{ width: '100%' }}
            value={subtitleLangs}
            onChange={setSubtitleLangs}
            options={LANG_PRESETS.map((l) => ({ value: l, label: l === 'multi' ? t('channels.langMulti') : l }))}
            tokenSeparators={[',']}
          />
        </div>
      </div>
    </>
  );
}
