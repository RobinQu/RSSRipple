import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Select, Spin, Tag, Typography, App, Alert, Switch, InputNumber } from 'antd';
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
  const [autoRefreshEnabled, setAutoRefreshEnabled] = useState(false);
  const [intervalMinutes, setIntervalMinutes] = useState(1440);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await worksApi.getMetadataConfig();
      if (r.success && r.data) {
        setConfig(r.data);
        setValue(r.data.default_source ?? null);
        setAutoRefreshEnabled(r.data.auto_refresh_enabled);
        setIntervalMinutes(r.data.auto_refresh_interval_minutes);
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
    if (!value) {
      message.warning(t('works.configSourceRequired'));
      return;
    }
    setSaving(true);
    try {
      const r = await worksApi.setMetadataConfig(value, autoRefreshEnabled, intervalMinutes);
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
      okButtonProps={{ disabled: noneAvailable || !value }}
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
            onChange={(v) => setValue(v)}
            options={buildOptions()}
            placeholder={t('works.configPlaceholder')}
            style={{ width: '100%' }}
          />
          {config && value && (
            <Tag color="blue" style={{ alignSelf: 'flex-start' }}>
              {t('works.currentDefault')}:{' '}
              {t(`channels.sources.${value}`, { defaultValue: value })}
            </Tag>
          )}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 16,
              marginTop: 8,
            }}
          >
            <div>
              <Text strong>{t('works.autoRefresh')}</Text>
              <br />
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t('works.autoRefreshDesc')}
              </Text>
            </div>
            <Switch checked={autoRefreshEnabled} onChange={setAutoRefreshEnabled} />
          </div>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 16,
            }}
          >
            <Text>{t('works.refreshInterval')}</Text>
            <InputNumber
              min={30}
              max={10080}
              step={30}
              disabled={!autoRefreshEnabled}
              value={intervalMinutes}
              addonAfter={t('works.minutes')}
              onChange={(v) => setIntervalMinutes(Number(v) || 1440)}
              style={{ width: 180 }}
            />
          </div>
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
