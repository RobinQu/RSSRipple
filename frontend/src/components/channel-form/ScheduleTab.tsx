import { Form, InputNumber, Switch } from 'antd';
import { useTranslation } from 'react-i18next';

interface Props {
  form: ReturnType<typeof Form.useForm>[0];
}

/** Tab 3 — every scheduler-driven cadence: RSS fetch interval, stale
 * unresolved-resource auto-cleanup, and the periodic work-metadata refresh. */
export default function ScheduleTab({ form }: Props) {
  const { t } = useTranslation();
  const cleanupEnabled = Form.useWatch('auto_cleanup_unresolved_enabled', form);
  const refreshEnabled = Form.useWatch('metadata_refresh_enabled', form);

  return (
    <>
      <Form.Item
        name="fetch_interval"
        label={t('channels.fetchIntervalSec')}
        rules={[{ required: true }]}
      >
        <InputNumber min={60} style={{ width: 180 }} />
      </Form.Item>

      <Form.Item
        name="auto_cleanup_unresolved_enabled"
        label={t('channels.autoCleanupUnresolved')}
        valuePropName="checked"
        tooltip={t('channels.autoCleanupUnresolvedDesc')}
      >
        <Switch checkedChildren={t('common.on')} unCheckedChildren={t('common.off')} />
      </Form.Item>

      {cleanupEnabled && (
        <Form.Item
          name="auto_cleanup_unresolved_days"
          label={t('channels.cleanupThresholdDays')}
          tooltip={t('channels.cleanupThresholdDaysDesc')}
          rules={[{ required: true }]}
        >
          <InputNumber min={1} max={365} style={{ width: 180 }} />
        </Form.Item>
      )}

      <Form.Item
        name="metadata_refresh_enabled"
        label={t('channels.metadataRefreshEnabled')}
        valuePropName="checked"
        tooltip={t('channels.metadataRefreshEnabledDesc')}
      >
        <Switch checkedChildren={t('common.on')} unCheckedChildren={t('common.off')} />
      </Form.Item>

      {refreshEnabled && (
        <>
          <Form.Item
            name="metadata_refresh_interval_minutes"
            label={t('channels.metadataRefreshInterval')}
            tooltip={t('channels.metadataRefreshIntervalDesc')}
          >
            <InputNumber
              min={30}
              max={10080}
              step={30}
              style={{ width: 180 }}
              addonAfter={t('works.minutes')}
            />
          </Form.Item>
          <Form.Item
            name="metadata_refresh_full_scope"
            label={t('channels.metadataRefreshFullScope')}
            valuePropName="checked"
            tooltip={t('channels.metadataRefreshFullScopeDesc')}
          >
            <Switch checkedChildren={t('common.on')} unCheckedChildren={t('common.off')} />
          </Form.Item>
        </>
      )}
    </>
  );
}
