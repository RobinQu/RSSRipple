import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Select, Spin, Tag, Typography, App, Alert } from 'antd';
import { worksApi, type MetadataConfigResponse } from '../api/works';

const { Text } = Typography;

interface Props {
  open: boolean;
  onClose: () => void;
}

/** Configurator modal for the works-page default metadata search source. */
export default function MetadataConfigModal({ open, onClose }: Props) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [config, setConfig] = useState<MetadataConfigResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [value, setValue] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await worksApi.getMetadataConfig();
      if (r.success && r.data) {
        setConfig(r.data);
        setValue(r.data.default_source ?? null);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  const available = (config?.sources ?? []).filter((s) => s.available);
  const noneAvailable = config !== null && available.length === 0;

  const buildOptions = () => {
    if (!config) return [];
    return config.sources.map((s) => ({
      value: s.value,
      label: s.available
        ? `${t(`channels.sources.${s.value}`, { defaultValue: s.label })}`
        : `${t(`channels.sources.${s.value}`, { defaultValue: s.label })} (${t('channels.sourceUnavailable')})`,
      disabled: !s.available,
    }));
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const r = await worksApi.setMetadataConfig(value);
      if (r.success) {
        message.success(t('works.configSaved'));
        onClose();
      } else {
        message.error(r.error?.message || t('works.configSaveFailed'));
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={t('works.configTitle')}
      open={open}
      onCancel={onClose}
      onOk={handleSave}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      okButtonProps={{ disabled: noneAvailable && value === null ? false : noneAvailable }}
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: 24 }}>
          <Spin />
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Text type="secondary">{t('works.configDesc')}</Text>
          <Select
            value={value}
            onChange={(v) => setValue(v ?? null)}
            options={buildOptions()}
            allowClear
            placeholder={t('works.configPlaceholder')}
            style={{ width: '100%' }}
          />
          {config && value && (
            <Tag color="blue" style={{ alignSelf: 'flex-start' }}>
              {t('works.currentDefault')}:{' '}
              {t(`channels.sources.${value}`, { defaultValue: value })}
            </Tag>
          )}
          {noneAvailable && (
            <Alert
              type="warning"
              showIcon
              message={t('channels.metadataSourceNone')}
              description={t('channels.metadataSourceNoneDesc')}
            />
          )}
        </div>
      )}
    </Modal>
  );
}
