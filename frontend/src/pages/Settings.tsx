import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Typography, Card, Spin, Input, Switch, Button, Select, Tag, Space, App, Alert } from 'antd';
import { Sparkles, Database, Save, RotateCcw } from 'lucide-react';
import { settingsApi, type SystemSettings, type SystemSettingsUpdate } from '../api/settings';

const { Title, Text } = Typography;

// Secret keys never come back from the server as plaintext; we track them
// locally and only submit when the user edits (or clears) them.
const SECRET_KEYS = ['llm_api_key', 'tmdb_api_key', 'jina_api_key', 'exa_api_key'] as const;
type SecretKey = (typeof SECRET_KEYS)[number];

function isSecretKey(key: string): key is SecretKey {
  return (SECRET_KEYS as readonly string[]).includes(key);
}

export default function SettingsPage() {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const [data, setData] = useState<SystemSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  // Non-secret editable values (model, base_url, effort level, switches).
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  // Secret input values (always start empty; never pre-filled with the secret).
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  // Which secret fields the user has touched (so we know to submit them).
  const [dirtySecrets, setDirtySecrets] = useState<Set<string>>(new Set());

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await settingsApi.get();
      if (r.success && r.data) {
        setData(r.data);
        const nextValues: Record<string, string | boolean> = {};
        for (const [key, field] of Object.entries(r.data.settings)) {
          if (!field.secret) nextValues[key] = field.value;
        }
        setValues(nextValues);
        setSecrets({});
        setDirtySecrets(new Set());
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const setNonSecret = (key: string, v: string | boolean) => {
    setValues((prev) => ({ ...prev, [key]: v }));
  };

  const setSecret = (key: string, v: string) => {
    setSecrets((prev) => ({ ...prev, [key]: v }));
    setDirtySecrets((prev) => new Set(prev).add(key));
  };

  const clearSecret = (key: string) => {
    setSecrets((prev) => ({ ...prev, [key]: '' }));
    setDirtySecrets((prev) => new Set(prev).add(key));
  };

  // Has any non-secret value diverged from the server snapshot?
  const nonSecretDirty = (() => {
    if (!data) return false;
    for (const [key, field] of Object.entries(data.settings)) {
      if (field.secret) continue;
      if ((values[key] ?? '') !== field.value) return true;
    }
    return false;
  })();

  const hasChanges = nonSecretDirty || dirtySecrets.size > 0;

  const handleSave = async () => {
    if (!data) return;
    const payload: SystemSettingsUpdate = {};
    // Non-secret fields: send only changed ones.
    for (const [key, field] of Object.entries(data.settings)) {
      if (field.secret) continue;
      if ((values[key] ?? '') !== field.value) {
        payload[key as keyof SystemSettingsUpdate] = values[key] as never;
      }
    }
    // Secret fields: send only touched ones (value replaces; empty clears).
    for (const key of dirtySecrets) {
      if (isSecretKey(key)) {
        payload[key] = secrets[key] ?? '';
      }
    }
    if (Object.keys(payload).length === 0) {
      message.info(t('settings.noChanges'));
      return;
    }
    setSaving(true);
    try {
      const r = await settingsApi.update(payload);
      if (r.success) {
        message.success(t('settings.saved'));
        await load();
      } else {
        message.error(r.error?.message || t('settings.saveFailed'));
      }
    } finally {
      setSaving(false);
    }
  };

  const field = (key: string) => data?.settings[key];

  const configuredTag = (key: string) => {
    const f = field(key);
    if (!f?.secret) return null;
    return f.configured ? (
      <Tag color="green" style={{ margin: 0 }}>{t('settings.configured')}</Tag>
    ) : (
      <Tag color="default" style={{ margin: 0 }}>{t('settings.notConfigured')}</Tag>
    );
  };

  const labelStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    marginBottom: 4,
  };

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 80 }}>
        <Spin size="large" />
      </div>
    );
  }

  return (
    <div style={{ maxWidth: 880, margin: '0 auto' }}>
      <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
        <div>
          <Title level={3} style={{ margin: 0 }}>{t('settings.title')}</Title>
          <Text type="secondary" style={{ fontSize: 13 }}>{t('settings.desc')}</Text>
        </div>
        <Space>
          <Button icon={<RotateCcw size={14} />} onClick={load} disabled={saving}>
            {t('settings.reset')}
          </Button>
          <Button
            type="primary"
            icon={<Save size={14} />}
            onClick={handleSave}
            loading={saving}
            disabled={!hasChanges}
          >
            {t('common.saveChanges')}
          </Button>
        </Space>
      </div>

      {/* LLM API */}
      <Card
        size="small"
        style={{ marginBottom: 16 }}
        title={
          <Space>
            <Sparkles size={16} style={{ color: '#1863dc' }} />
            <span>{t('settings.llm.title')}</span>
          </Space>
        }
      >
        <Text type="secondary" style={{ display: 'block', marginBottom: 16, fontSize: 12 }}>
          {t('settings.llm.desc')}
        </Text>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div>
            <div style={labelStyle}>
              <Text strong>{t('settings.llm.apiKey')}</Text>
              {configuredTag('llm_api_key')}
            </div>
            <Input.Password
              value={secrets.llm_api_key ?? ''}
              onChange={(e) => setSecret('llm_api_key', e.target.value)}
              placeholder={t('settings.secretPlaceholder')}
            />
          </div>

          <div>
            <div style={labelStyle}><Text strong>{t('settings.llm.model')}</Text></div>
            <Input
              value={String(values.llm_model ?? '')}
              onChange={(e) => setNonSecret('llm_model', e.target.value)}
              placeholder={t('settings.llm.modelPlaceholder')}
            />
          </div>

          <div>
            <div style={labelStyle}><Text strong>{t('settings.llm.baseUrl')}</Text></div>
            <Input
              value={String(values.llm_base_url ?? '')}
              onChange={(e) => setNonSecret('llm_base_url', e.target.value)}
              placeholder={t('settings.llm.baseUrlPlaceholder')}
            />
          </div>

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16 }}>
            <div>
              <Text strong>{t('settings.llm.enableThinking')}</Text>
              <br />
              <Text type="secondary" style={{ fontSize: 12 }}>{t('settings.llm.enableThinkingDesc')}</Text>
            </div>
            <Switch
              checked={Boolean(values.llm_enable_thinking)}
              onChange={(v) => setNonSecret('llm_enable_thinking', v)}
            />
          </div>
        </div>
      </Card>

      {/* External search data sources */}
      <Card
        size="small"
        title={
          <Space>
            <Database size={16} style={{ color: '#1863dc' }} />
            <span>{t('settings.sources.title')}</span>
          </Space>
        }
      >
        <Text type="secondary" style={{ display: 'block', marginBottom: 16, fontSize: 12 }}>
          {t('settings.sources.desc')}
        </Text>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
          {/* TMDB */}
          <SourceRow
            name={t('settings.sources.tmdb')}
            enabled={Boolean(values.tmdb_enabled)}
            onEnabled={(v) => setNonSecret('tmdb_enabled', v)}
            configuredTag={configuredTag('tmdb_api_key')}
            secretValue={secrets.tmdb_api_key ?? ''}
            onSecret={(v) => setSecret('tmdb_api_key', v)}
            onClear={() => clearSecret('tmdb_api_key')}
            placeholder={t('settings.secretPlaceholder')}
            t={t}
          />

          {/* Jina */}
          <SourceRow
            name={t('settings.sources.jina')}
            enabled={Boolean(values.jina_enabled)}
            onEnabled={(v) => setNonSecret('jina_enabled', v)}
            configuredTag={configuredTag('jina_api_key')}
            secretValue={secrets.jina_api_key ?? ''}
            onSecret={(v) => setSecret('jina_api_key', v)}
            onClear={() => clearSecret('jina_api_key')}
            placeholder={t('settings.secretPlaceholder')}
            t={t}
          />

          {/* Exa */}
          <div>
            <SourceRow
              name={t('settings.sources.exa')}
              enabled={Boolean(values.exa_enabled)}
              onEnabled={(v) => setNonSecret('exa_enabled', v)}
              configuredTag={configuredTag('exa_api_key')}
              secretValue={secrets.exa_api_key ?? ''}
              onSecret={(v) => setSecret('exa_api_key', v)}
              onClear={() => clearSecret('exa_api_key')}
              placeholder={t('settings.secretPlaceholder')}
              t={t}
            />
            <div style={{ marginTop: 10, marginLeft: 4 }}>
              <div style={labelStyle}><Text strong>{t('settings.sources.effortLevel')}</Text></div>
              <Select
                value={String(values.exa_effort_level ?? 'low')}
                onChange={(v) => setNonSecret('exa_effort_level', v)}
                options={(data?.exa_effort_levels ?? []).map((lvl) => ({ value: lvl, label: lvl }))}
                style={{ width: 180 }}
              />
            </div>
          </div>

          {/* Wikipedia */}
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16 }}>
            <div>
              <Text strong>{t('settings.sources.wikipedia')}</Text>
              <br />
              <Text type="secondary" style={{ fontSize: 12 }}>{t('settings.sources.wikipediaHint')}</Text>
            </div>
            <Switch
              checked={Boolean(values.wikipedia_enabled)}
              onChange={(v) => setNonSecret('wikipedia_enabled', v)}
            />
          </div>
        </div>

        <Alert
          type="info"
          showIcon
          style={{ marginTop: 16 }}
          message={t('settings.envNote')}
        />
      </Card>
    </div>
  );
}

interface SourceRowProps {
  name: string;
  enabled: boolean;
  onEnabled: (v: boolean) => void;
  configuredTag: React.ReactNode;
  secretValue: string;
  onSecret: (v: string) => void;
  onClear: () => void;
  placeholder: string;
  t: (k: string) => string;
}

function SourceRow({ name, enabled, onEnabled, configuredTag, secretValue, onSecret, onClear, placeholder, t }: SourceRowProps) {
  return (
    <div style={{ padding: '12px 0', borderTop: '1px solid var(--rr-border-soft)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <Space>
          <Text strong>{name}</Text>
          {configuredTag}
        </Space>
        <Space size="small">
          <Text type="secondary" style={{ fontSize: 12 }}>{t('settings.sources.enable')}</Text>
          <Switch checked={enabled} onChange={onEnabled} />
        </Space>
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <Input.Password
          value={secretValue}
          onChange={(e) => onSecret(e.target.value)}
          placeholder={placeholder}
          style={{ flex: 1 }}
        />
        <Button onClick={onClear}>{t('settings.clear')}</Button>
      </div>
    </div>
  );
}
