import { Alert, Button, Form, Input, Space, Spin, Tooltip, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import { CheckCircle, Info, Loader2, RefreshCw, Wand2, XCircle } from 'lucide-react';
import type { FieldMapping } from '../../types';
import { DEFAULT_FIELD_MAPPING_TEXT } from './constants';

const { Text } = Typography;

export type UrlStatus = 'idle' | 'checking' | 'valid' | 'invalid';

interface Props {
  mode: 'create' | 'edit';
  urlStatus: UrlStatus;
  urlMessage: string;
  downloadableCount: number;
  fieldMapping: FieldMapping | null;
  fieldMappingText: string;
  setFieldMappingText: (text: string) => void;
  analyzing: boolean;
  onValidateUrl: () => void;
  /** URL input changed — container resets its validation state. */
  onUrlChange: () => void;
  onAnalyze: () => void;
  onPreviewRefresh: () => void;
}

/** Tab 1 — channel identity + RSS source + the field-mapping (parse rule)
 * editor. The AI-analysis stream sidebar and feed preview live in the
 * container's right column and are only rendered while this tab is active. */
export default function BasicInfoTab({
  mode,
  urlStatus,
  urlMessage,
  downloadableCount,
  fieldMapping,
  fieldMappingText,
  setFieldMappingText,
  analyzing,
  onValidateUrl,
  onUrlChange,
  onAnalyze,
  onPreviewRefresh,
}: Props) {
  const { t } = useTranslation();

  return (
    <>
      <Form.Item
        name="name"
        label={t('common.name')}
        rules={[{ required: true, message: t('channels.pleaseEnterName') }]}
      >
        <Input placeholder={t('channels.nameExample')} />
      </Form.Item>

      {/* The name must live on the inner control: antd's Form.Item value
          binding does not penetrate Space.Compact wrappers, which left
          the edit form showing an empty URL. */}
      <Form.Item label={t('channels.rssUrl')} required style={{ marginBottom: 16 }}>
        <Space.Compact style={{ width: '100%' }}>
          <Form.Item
            name="url"
            rules={[{ required: true, message: t('channels.enterRssUrl') }]}
            noStyle
          >
            <Input placeholder="https://mikanani.me/RSS/..." onChange={onUrlChange} />
          </Form.Item>
          <Button onClick={onValidateUrl}>{t('channels.validate')}</Button>
        </Space.Compact>
      </Form.Item>

      {urlStatus === 'checking' && (
        <div style={{ marginBottom: 16, display: 'flex', gap: 6, alignItems: 'center' }}>
          <Loader2 size={14} style={{ animation: 'spin 1s linear infinite' }} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t('channels.validating')}
          </Text>
        </div>
      )}
      {urlStatus === 'valid' && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <CheckCircle size={14} color="var(--rr-success)" />
            <Text style={{ fontSize: 12, color: 'var(--rr-success)' }}>{urlMessage}</Text>
          </div>
          {downloadableCount === 0 && (
            <Text style={{ fontSize: 12, color: 'var(--rr-accent)' }}>
              {t('channels.noTorrentWarning')}
            </Text>
          )}
        </div>
      )}
      {urlStatus === 'invalid' && (
        <div style={{ marginBottom: 16, display: 'flex', gap: 6, alignItems: 'center' }}>
          <XCircle size={14} color="var(--rr-error)" />
          <Text style={{ fontSize: 12, color: 'var(--rr-error)' }}>{urlMessage}</Text>
        </div>
      )}

      {/* Field mapping */}
      <div style={{ marginBottom: 8 }}>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            marginBottom: 8,
          }}
        >
          <Space size={6}>
            <Text strong>{t('channels.fieldMapping')}</Text>
            <Tooltip title={t('channels.fieldMappingDesc')}>
              <Info size={13} style={{ color: 'var(--rr-text-muted)' }} />
            </Tooltip>
          </Space>
          <Space size={8}>
            <Button size="small" icon={<RefreshCw size={13} />} onClick={onPreviewRefresh}>
              {t('channels.preview')}
            </Button>
            <Button
              size="small"
              icon={analyzing ? <Spin size="small" /> : <Wand2 size={13} />}
              loading={analyzing}
              onClick={onAnalyze}
              disabled={mode === 'create' && urlStatus !== 'valid'}
            >
              {fieldMapping ? t('channels.reAnalyze') : t('channels.analyze')}
            </Button>
          </Space>
        </div>

        <Input.TextArea
          value={fieldMappingText}
          onChange={(e) => setFieldMappingText(e.target.value)}
          rows={12}
          style={{
            fontFamily: 'monospace',
            fontSize: 12,
          }}
          placeholder={DEFAULT_FIELD_MAPPING_TEXT}
        />
        {!fieldMappingText && (
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 6 }}>
            {t('channels.aiPromptHint')}
          </Text>
        )}
      </div>

      {!fieldMapping && !fieldMappingText.trim() && (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 12 }}
          message={t('channels.mappingMandatory')}
          description={t('channels.mappingMandatoryDesc')}
        />
      )}
    </>
  );
}
