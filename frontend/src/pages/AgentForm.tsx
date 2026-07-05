import { useState, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  Form,
  Input,
  Select,
  InputNumber,
  Button,
  Switch,
  Card,
  Space,
  Row,
  Col,
  Typography,
  App,
  Radio,
  Spin,
  Divider,
} from 'antd';
import { agentsApi } from '../api/agents';
import { channelsApi } from '../api/channels';
import { downloadersApi } from '../api/downloaders';
import FilterBuilder from '../components/FilterBuilder';
import WorkSelector from '../components/WorkSelector';
import type { Agent, AgentWork, BoolCondition, Channel, DownloaderInstance } from '../types';

const { Title, Text } = Typography;

interface FormValues {
  name: string;
  channel_id: string;
  downloader_id: string;
  download_subdir?: string;
  task_expire_days: number;
  llm_enabled: boolean;
  scope_channel_wide: boolean;
  conflict_resolution: 'ask' | 'auto';
}

export default function AgentForm() {
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();
  const mode: 'create' | 'edit' = id ? 'edit' : 'create';
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [form] = Form.useForm<FormValues>();
  const [channels, setChannels] = useState<Channel[]>([]);
  const [downloaders, setDownloaders] = useState<DownloaderInstance[]>([]);
  const [works, setWorks] = useState<AgentWork[]>([]);
  const [filterConfig, setFilterConfig] = useState<BoolCondition | null>(null);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(mode === 'edit');
  const [channelWide, setChannelWide] = useState(false);
  // Watch the selected channel id so the Filter DSL editor (which does
  // channel-scoped autocomplete) always sees the current value even before
  // the form is submitted.
  const channelId = Form.useWatch('channel_id', form) as string | undefined;

  useEffect(() => {
    Promise.all([channelsApi.list(1, 100), downloadersApi.list(1, 100)]).then(
      ([c, d]) => {
        if (c.success) setChannels(c.data);
        if (d.success) setDownloaders(d.data);
      },
    );
  }, []);

  // Load agent for edit
  useEffect(() => {
    if (mode === 'edit' && id) {
      agentsApi.get(id).then((r) => {
        if (r.success) {
          const a: Agent = r.data;
          form.setFieldsValue({
            name: a.name,
            channel_id: a.channel_id,
            downloader_id: a.downloader_id,
            download_subdir: a.download_subdir ?? '',
            task_expire_days: a.task_expire_days,
            llm_enabled: a.llm_enabled,
            scope_channel_wide: a.scope_channel_wide,
            conflict_resolution: a.conflict_resolution,
          });
          setChannelWide(a.scope_channel_wide);
          setFilterConfig(a.filter_config);
          if (a.works) setWorks(a.works);
        } else {
          message.error(t('agents.loadFailed'));
          navigate('/agents');
        }
        setLoading(false);
      });
    }
  }, [mode, id, form, message, navigate, t]);

  // Check for prefill (from FilterSummaryModal)
  useEffect(() => {
    if (mode !== 'create') return;
    try {
      const raw = sessionStorage.getItem('rssripple:prefill:agent');
      if (raw) {
        const data = JSON.parse(raw);
        sessionStorage.removeItem('rssripple:prefill:agent');
        form.setFieldsValue({
          name: data.name || '',
          channel_id: data.channel_id,
        });
        if (data.filter_config) setFilterConfig(data.filter_config);
      }
    } catch {
      /* ignore */
    }
  }, [mode, form]);

  const handleSubmit = async (values: FormValues) => {
    if (!values.scope_channel_wide && works.length === 0) {
      message.error(t('agents.worksRequired'));
      return;
    }
    setSaving(true);
    const payload = {
      name: values.name,
      channel_id: values.channel_id,
      downloader_id: values.downloader_id,
      download_subdir: values.download_subdir?.trim() || null,
      task_expire_days: values.task_expire_days,
      llm_enabled: values.llm_enabled,
      scope_channel_wide: values.scope_channel_wide,
      conflict_resolution: values.conflict_resolution,
      filter_config: filterConfig,
      works: values.scope_channel_wide
        ? []
        : works.map((w) => ({
            content_type: w.content_type,
            series_id: w.series_id,
            movie_id: w.movie_id,
            enable_episode_dedup: w.enable_episode_dedup,
            filter_overrides: w.filter_overrides,
            display_name_override: w.display_name_override,
          })),
    };
    try {
      let res;
      if (mode === 'edit' && id) {
        res = await agentsApi.update(id, payload);
      } else {
        res = await agentsApi.create(payload);
      }
      if (res.success) {
        message.success(t('agents.saved'));
        navigate(`/agents/${res.data.id}`);
      } else {
        message.error(res.error?.message || t('agents.saveFailed'));
      }
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Spin />;

  return (
    <div style={{ maxWidth: 820 }}>
      <Title level={3} style={{ marginBottom: 24 }}>
        {mode === 'create' ? t('agents.newAgent') : t('agents.editAgent')}
      </Title>
      <Card>
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSubmit}
          initialValues={{
            task_expire_days: 30,
            llm_enabled: true,
            scope_channel_wide: false,
            conflict_resolution: 'ask' as const,
          }}
          onValuesChange={(changed) => {
            if (changed.scope_channel_wide !== undefined) {
              setChannelWide(changed.scope_channel_wide);
            }
          }}
        >
          <Form.Item name="name" label={t('common.name')} rules={[{ required: true, message: t('agents.pleaseEnterName') }]}>
            <Input placeholder={t('agents.nameExample')} />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="channel_id"
                label={t('agents.channel')}
                rules={[{ required: true, message: t('agents.selectChannel') }]}
              >
                <Select
                  placeholder={t('agents.selectChannel')}
                  options={channels.map((c) => ({ label: c.name, value: c.id }))}
                  disabled={mode === 'edit'}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="downloader_id"
                label={t('agents.downloader')}
                rules={[{ required: true, message: t('agents.selectDownloader') }]}
              >
                <Select
                  placeholder={t('agents.selectDownloader')}
                  options={downloaders.map((d) => ({ label: d.name, value: d.id }))}
                />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="download_subdir"
            label={t('agents.downloadSubdir')}
            rules={[
              {
                pattern: /^(?![\\/])(?![A-Za-z]:[\\/])(?!~)(?!.*(?:^|[\\/])\.\.(?:[\\/]|$))(?!.*[\\/]$).*$/,
                message: t('agents.subdirHint'),
              },
            ]}
          >
            <Input placeholder={t('agents.subdirExample')} allowClear />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="task_expire_days" label={t('agents.taskRetention')}>
                <InputNumber min={1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="conflict_resolution" label={t('agents.conflictResolution')}>
                <Radio.Group>
                  <Radio value="ask">{t('agents.ask')}</Radio>
                  <Radio value="auto">{t('agents.auto')}</Radio>
                </Radio.Group>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="llm_enabled"
                label={t('agents.llmDecision')}
                valuePropName="checked"
              >
                <Switch checkedChildren={t('agents.on')} unCheckedChildren={t('agents.off')} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="scope_channel_wide"
                label={t('agents.subscribeScope')}
                valuePropName="checked"
              >
                <Switch
                  checkedChildren={t('agents.channelWide')}
                  unCheckedChildren={t('agents.selectedWorks')}
                />
              </Form.Item>
            </Col>
          </Row>

          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: -8, marginBottom: 16 }}>
            {channelWide
              ? t('agents.scopeChannelWideDesc')
              : t('agents.scopeWorksDesc')}
          </Text>

          <Divider style={{ margin: '12px 0' }} />

          {!channelWide && (
            <div style={{ marginBottom: 20 }}>
              <WorkSelector value={works} onChange={setWorks} maxWorks={10} channelId={channelId} />
            </div>
          )}

          <div style={{ marginBottom: 16 }}>
            <Text strong style={{ display: 'block', marginBottom: 8 }}>
              {t('agents.globalFilter')}
            </Text>
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
              {t('agents.globalFilterDesc')}
            </Text>
            <FilterBuilder value={filterConfig} onChange={setFilterConfig} channelId={channelId} />
          </div>

          <Form.Item style={{ marginTop: 24, marginBottom: 0 }}>
            <Space>
              <Button type="primary" htmlType="submit" loading={saving}>
                {mode === 'edit' ? t('agents.saveChanges') : t('agents.createAgent')}
              </Button>
              <Button htmlType="button" onClick={() => navigate('/agents')}>{t('common.cancel')}</Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
}
