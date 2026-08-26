import { Alert, Form, Select, Switch } from 'antd';
import { useTranslation } from 'react-i18next';
import type { MetadataSourceOption } from '../../api/channels';
import type { MetadataSource } from '../../types';
import RequiredFieldsInput from '../RequiredFieldsInput';
import { DEFAULT_FALLBACK_SOURCES } from './constants';

interface Props {
  mode: 'create' | 'edit';
  form: ReturnType<typeof Form.useForm>[0];
  metadataSources: MetadataSourceOption[];
  savedRequiredFields: string[] | null;
}

/** Tab 2 — everything about metadata matching: the master switch, the
 * external data source, the ordered web-fallback site whitelist, the
 * add-only required-fields picker, and the create-time default-is-anime
 * flag. */
export default function MetadataSearchTab({
  mode,
  form,
  metadataSources,
  savedRequiredFields,
}: Props) {
  const { t } = useTranslation();
  const agentEnabled = Form.useWatch('metadata_agent_enabled', form);

  /** Build Select options from the source catalog. Available sources are
   * selectable; a currently-selected source that is no longer available is
   * kept (with a marker) so the user can see and change it. */
  const buildSourceOptions = () => {
    const current = form.getFieldValue('metadata_source') as MetadataSource | null | undefined;
    const seen = new Set<MetadataSource>();
    const opts: { value: MetadataSource; label: string; disabled?: boolean }[] = [];
    for (const s of metadataSources) {
      if (!s.available && s.value !== current) continue;
      seen.add(s.value);
      const label = t(`channels.sources.${s.value}`, { defaultValue: s.label });
      opts.push({
        value: s.value,
        label: s.available ? label : `${label} (${t('channels.sourceUnavailable')})`,
        disabled: !s.available,
      });
    }
    // Edge case: the stored source is unknown to the catalog (e.g. backend
    // downgraded). Surface it so the value isn't silently dropped.
    if (current && !seen.has(current)) {
      opts.push({ value: current, label: `${current} (${t('channels.sourceUnavailable')})` });
    }
    return opts;
  };

  return (
    <>
      <Form.Item
        name="metadata_agent_enabled"
        label={t('channels.autoMetadataLLM')}
        valuePropName="checked"
        tooltip={t('channels.metadataLLMDesc')}
      >
        <Switch checkedChildren={t('common.on')} unCheckedChildren={t('common.off')} />
      </Form.Item>

      {agentEnabled && (
        <Form.Item
          name="metadata_source"
          label={t('channels.metadataSourceLabel')}
          tooltip={t('channels.metadataSourceDesc')}
        >
          <Select
            placeholder={t('channels.metadataSourcePlaceholder')}
            allowClear
            style={{ maxWidth: 320 }}
            options={buildSourceOptions()}
            notFoundContent={t('channels.metadataSourceNone')}
          />
        </Form.Item>
      )}

      {agentEnabled && (
        <Form.Item
          name="metadata_fallback_sources"
          label={t('channels.metadataFallbackLabel')}
          tooltip={t('channels.metadataFallbackDesc')}
          extra={t('channels.metadataFallbackHelper')}
        >
          <Select
            mode="multiple"
            placeholder={t('channels.metadataFallbackPlaceholder')}
            style={{ maxWidth: 480 }}
            options={DEFAULT_FALLBACK_SOURCES.map((s) => ({
              value: s,
              label: t(`channels.sources.${s}`, { defaultValue: s }),
            }))}
          />
        </Form.Item>
      )}

      {agentEnabled && (
        <Form.Item
          name="required_metadata_fields"
          label={t('channels.requiredFieldsLabel')}
          tooltip={t('channels.requiredFieldsDesc')}
        >
          <RequiredFieldsInput saved={savedRequiredFields} />
        </Form.Item>
      )}

      {agentEnabled && metadataSources.length > 0 && metadataSources.every((s) => !s.available) && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={t('channels.metadataSourceNone')}
          description={t('channels.metadataSourceNoneDesc')}
        />
      )}

      <Form.Item
        name="default_is_anime"
        label={t('channels.defaultIsAnime')}
        valuePropName="checked"
        tooltip={
          mode === 'create'
            ? t('channels.defaultIsAnimeDesc')
            : t('channels.defaultIsAnimeLocked')
        }
      >
        <Switch
          checkedChildren={t('common.on')}
          unCheckedChildren={t('common.off')}
          disabled={mode !== 'create'}
        />
      </Form.Item>
    </>
  );
}
