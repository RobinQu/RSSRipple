import { useState, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useDocumentTitle from '../hooks/useDocumentTitle';
import useAgentFilterFields from '../hooks/useAgentFilterFields';
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
  Alert,
  Tabs,
} from 'antd';
import { agentsApi } from '../api/agents';
import { channelsApi } from '../api/channels';
import { downloadersApi } from '../api/downloaders';
import FilterBuilder from '../components/FilterBuilder';
import PreferenceListEditor from '../components/PreferenceListEditor';
import {
  findInvalidConditions,
  isEmptyValue,
  isNoValueOperator,
  nullIfEmptyFilter,
} from '../components/filterUtils';
import WorkSelector from '../components/WorkSelector';
import BackfillPreviewModal from '../components/BackfillPreviewModal';
import type {
  Agent,
  AgentCreate,
  AgentWork,
  BoolCondition,
  Channel,
  DownloaderInstance,
  FieldCondition,
  RulesPreviewRequest,
  RulesPreviewResponse,
} from '../types';

const { Title, Text } = Typography;

interface FormValues {
  name: string;
  channel_id: string;
  downloader_id: string;
  download_subdir?: string;
  task_expire_days: number;
  llm_enabled: boolean;
  llm_prompt?: string;
  scope_channel_wide: boolean;
  conflict_resolution: 'ask' | 'auto';
  run_immediately: boolean;
}

export default function AgentForm() {
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();
  const mode: 'create' | 'edit' = id ? 'edit' : 'create';
  const { t } = useTranslation();
  useDocumentTitle(t(mode === 'edit' ? 'agents.editAgent' : 'agents.newAgent'));
  const { message } = App.useApp();
  const [form] = Form.useForm<FormValues>();
  const [channels, setChannels] = useState<Channel[]>([]);
  const [downloaders, setDownloaders] = useState<DownloaderInstance[]>([]);
  const [works, setWorks] = useState<AgentWork[]>([]);
  const [filterConfig, setFilterConfig] = useState<BoolCondition | null>(null);
  const [pickPreferences, setPickPreferences] = useState<FieldCondition[]>([]);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(mode === 'edit');
  const [channelWide, setChannelWide] = useState(false);
  // Rules-preview selection modal (scenario ②). When the proposed rules have
  // newly-matching resources, the save is deferred until the user picks which
  // to backfill; the chosen ids are sent as dispatch_resource_ids.
  const [preview, setPreview] = useState<RulesPreviewResponse | null>(null);
  const [previewSelected, setPreviewSelected] = useState<Record<string, boolean>>({});
  const [pendingPayload, setPendingPayload] = useState<AgentCreate | null>(null);
  const [previewSaving, setPreviewSaving] = useState(false);
  // Watch the selected channel id so the Filter DSL editor (which does
  // channel-scoped autocomplete) always sees the current value even before
  // the form is submitted.
  const channelId = Form.useWatch('channel_id', form) as string | undefined;
  const llmEnabled = Form.useWatch('llm_enabled', form) as boolean | undefined;
  // Channel required-fields gate for the filter DSL editors (null =
  // unrestricted; pick preferences are exempt and never receive this).
  const allowedFilterFields = useAgentFilterFields(channelId);

  useEffect(() => {
    Promise.all([channelsApi.list(1, 100), downloadersApi.list(1, 100)]).then(
      ([c, d]) => {
        if (c.success) setChannels(c.data);
        if (d.success) {
          setDownloaders(d.data);
          // Create mode: default to the first available downloader so the
          // form is submittable out of the box (edit mode sets its own).
          if (mode === 'create' && d.data.length > 0 && !form.getFieldValue('downloader_id')) {
            form.setFieldsValue({ downloader_id: d.data[0].id });
          }
        }
      },
    );
  }, [form, mode]);

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
            llm_prompt: a.llm_prompt ?? '',
            scope_channel_wide: a.scope_channel_wide,
            conflict_resolution: a.conflict_resolution,
          });
          setChannelWide(a.scope_channel_wide);
          setFilterConfig(a.filter_config);
          setPickPreferences(a.pick_preferences ?? []);
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
        // FilterSummaryModal may also prefill suggested works; keep the
        // scope on selected-works so they are visible and editable.
        if (Array.isArray(data.works) && data.works.length > 0) {
          setWorks(data.works);
          form.setFieldsValue({ scope_channel_wide: false });
          setChannelWide(false);
        }
      }
    } catch {
      /* ignore */
    }
  }, [mode, form]);

  const buildPayload = (values: FormValues, dispatchResourceIds: string[] | null): AgentCreate => ({
    name: values.name,
    channel_id: values.channel_id,
    downloader_id: values.downloader_id,
    download_subdir: values.download_subdir?.trim() || null,
    task_expire_days: values.task_expire_days,
    llm_enabled: values.llm_enabled,
    llm_prompt: values.llm_prompt?.trim() || null,
    scope_channel_wide: values.scope_channel_wide,
    conflict_resolution: values.conflict_resolution,
    pick_preferences: pickPreferences.length > 0 ? pickPreferences : null,
    filter_config: nullIfEmptyFilter(filterConfig),
    works: values.scope_channel_wide
      ? []
      : works.map((w) => ({
          content_type: w.content_type,
          series_id: w.series_id,
          movie_id: w.movie_id,
          enable_episode_dedup: w.enable_episode_dedup,
          filter_overrides: nullIfEmptyFilter(w.filter_overrides),
          display_name_override: w.display_name_override,
        })),
    dispatch_resource_ids: dispatchResourceIds,
    run_immediately: values.run_immediately,
  });

  const buildPreviewRequest = (values: FormValues): RulesPreviewRequest => ({
    agent_id: mode === 'edit' && id ? id : undefined,
    channel_id: values.channel_id,
    scope_channel_wide: values.scope_channel_wide,
    filter_config: nullIfEmptyFilter(filterConfig),
    works: values.scope_channel_wide
      ? []
      : works.map((w) => ({
          content_type: w.content_type,
          series_id: w.series_id,
          movie_id: w.movie_id,
          enable_episode_dedup: w.enable_episode_dedup,
          filter_overrides: nullIfEmptyFilter(w.filter_overrides),
        })),
  });

  const doSave = async (payload: AgentCreate) => {
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
  };

  const handleSubmit = async (values: FormValues) => {
    if (!values.scope_channel_wide && works.length === 0) {
      message.error(t('agents.worksRequired'));
      return;
    }
    // The backend rejects value-taking operators with empty values (422) —
    // block the save here so the user gets a clear inline-level message.
    if (
      findInvalidConditions(filterConfig).length > 0 ||
      works.some((w) => findInvalidConditions(w.filter_overrides).length > 0) ||
      pickPreferences.some(
        (c) => !isNoValueOperator(c.operator) && isEmptyValue(c.value),
      )
    ) {
      message.error(t('filter.emptyValueNotAllowed'));
      return;
    }
    setSaving(true);
    try {
      // "立即运行" (create only): skip the rules-preview modal and save
      // plainly — the backend enqueues a background full-history run that
      // scans every channel resource from channel creation onward.
      if (mode === 'create' && values.run_immediately) {
        await doSave(buildPayload(values, null));
        return;
      }
      // Scenario ②: preview the rule diff before committing. Show the modal
      // whenever the change has any impact (newly-matching OR no-longer-
      // matching) so the user sees the明细 and can opt into backfill — no
      // silent mass-dispatch, and no silent save when matches changed.
      const pv = await agentsApi.rulesPreview(buildPreviewRequest(values));
      if (!pv.success) {
        message.error(pv.error?.message || t('agents.previewFailed'));
        return;
      }
      const newly = pv.data.newly_matching;
      const noLonger = pv.data.no_longer_matching;
      if (newly.length > 0 || noLonger.length > 0) {
        const initSel: Record<string, boolean> = {};
        newly.forEach((r) => { initSel[r.id] = true; });
        setPreview(pv.data);
        setPreviewSelected(initSel);
        setPendingPayload(buildPayload(values, null));
        return;
      }
      // No impact on matching: commit directly with an empty backfill list
      // (still advances the watermark since rules may have changed).
      await doSave(buildPayload(values, []));
    } finally {
      setSaving(false);
    }
  };

  const handlePreviewConfirm = async (dispatchIds: string[]) => {
    if (!pendingPayload) return;
    setPreviewSaving(true);
    try {
      await doSave({ ...pendingPayload, dispatch_resource_ids: dispatchIds });
      setPreview(null);
      setPendingPayload(null);
    } finally {
      setPreviewSaving(false);
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
            llm_prompt: '',
            scope_channel_wide: false,
            conflict_resolution: 'auto' as const,
            run_immediately: false,
          }}
          onValuesChange={(changed) => {
            if (changed.scope_channel_wide !== undefined) {
              setChannelWide(changed.scope_channel_wide);
            }
          }}
        >
          <Tabs
            defaultActiveKey="basic"
            items={[
              {
                key: 'basic',
                label: t('agents.tabBasic'),
                forceRender: true,
                children: (
                  <>
                    <Form.Item name="name" label={t('common.name')} rules={[{ required: true, message: t('agents.pleaseEnterName') }]}>
                      <Input placeholder={t('agents.nameExample')} />
                    </Form.Item>

                    <Row gutter={16}>
                      <Col xs={24} sm={12}>
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
                      <Col xs={24} sm={12}>
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
                      <Col xs={24} sm={12}>
                        <Form.Item name="task_expire_days" label={t('agents.taskRetention')}>
                          <InputNumber min={1} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                      <Col xs={24} sm={12}>
                        <Form.Item name="conflict_resolution" label={t('agents.conflictResolution')}>
                          <Radio.Group>
                            <Radio value="ask">{t('agents.ask')}</Radio>
                            <Radio value="auto">{t('agents.auto')}</Radio>
                          </Radio.Group>
                        </Form.Item>
                      </Col>
                    </Row>

                    {mode === 'create' && (
                      <Form.Item
                        name="run_immediately"
                        label={t('agents.runImmediately')}
                        valuePropName="checked"
                        extra={t('agents.runImmediatelyHint')}
                      >
                        <Switch checkedChildren={t('agents.on')} unCheckedChildren={t('agents.off')} />
                      </Form.Item>
                    )}
                  </>
                ),
              },
              {
                key: 'filters',
                label: t('agents.tabFilters'),
                forceRender: true,
                children: (
                  <>
                    {/* 内置去重逻辑：信息提示高亮、默认展开，置于所有 DSL 条件表单之前 */}
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 20 }}
                      message={t('agents.dedupInfoTitle')}
                      description={
                        <Space direction="vertical" size={8} style={{ display: 'flex' }}>
                          {(['Tv', 'Movie', 'Batch'] as const).map((kind) => (
                            <div key={kind}>
                              <Text strong style={{ fontSize: 12, display: 'block' }}>
                                {t(`agents.dedup${kind}Title`)}
                              </Text>
                              <Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
                                {t(`agents.dedup${kind}Desc`)}
                              </Text>
                            </div>
                          ))}
                        </Space>
                      }
                    />

                    {/* ① 全局过滤条件 */}
                    <div style={{ marginBottom: 20 }}>
                      <Text strong style={{ display: 'block', marginBottom: 8 }}>
                        {t('agents.globalFilter')}
                      </Text>
                      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                        {t('agents.globalFilterDesc')}
                      </Text>
                      {allowedFilterFields != null && (
                        <Text type="warning" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                          {t('agents.filterFieldsGated')}
                        </Text>
                      )}
                      <FilterBuilder value={filterConfig} onChange={setFilterConfig} channelId={channelId} allowedFields={allowedFilterFields} />
                    </div>

                    <Divider style={{ margin: '16px 0' }} />

                    {/* ② 订阅作品：「订阅范围」toggle 与订阅条件编辑一体化 */}
                    <div style={{ marginBottom: 20 }}>
                      <div
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          gap: 12,
                          flexWrap: 'wrap',
                          marginBottom: 4,
                        }}
                      >
                        <Text strong>{t('agents.subscribedWorks')}</Text>
                        <Space size={8}>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {t('agents.subscribeScope')}
                          </Text>
                          <Form.Item name="scope_channel_wide" valuePropName="checked" noStyle>
                            <Switch
                              checkedChildren={t('agents.channelWide')}
                              unCheckedChildren={t('agents.selectedWorks')}
                            />
                          </Form.Item>
                        </Space>
                      </div>
                      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                        {channelWide ? t('agents.scopeChannelWideDesc') : t('agents.scopeWorksDesc')}
                      </Text>
                      {!channelWide && (
                        <WorkSelector value={works} onChange={setWorks} maxWorks={10} channelId={channelId} globalFilter={filterConfig} allowedFields={allowedFilterFields} />
                      )}
                    </div>

                    <Divider style={{ margin: '16px 0' }} />

                    {/* ③ 按条件优选（pick_preferences 不受必选字段门控） */}
                    <div style={{ marginBottom: 20 }}>
                      <Text strong style={{ display: 'block', marginBottom: 4 }}>
                        {t('agents.pickPreferences')}
                      </Text>
                      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                        {t('agents.pickPreferencesDesc')}
                      </Text>
                      <PreferenceListEditor
                        value={pickPreferences}
                        onChange={setPickPreferences}
                        channelId={channelId}
                      />
                    </div>

                    <Divider style={{ margin: '16px 0' }} />

                    {/* ④ LLM 判断优选 */}
                    <div style={{ marginBottom: 16 }}>
                      <Space size={8}>
                        <Form.Item name="llm_enabled" valuePropName="checked" noStyle>
                          <Switch checkedChildren={t('agents.on')} unCheckedChildren={t('agents.off')} />
                        </Form.Item>
                        <Text strong>{t('agents.llmDecision')}</Text>
                      </Space>
                      {llmEnabled && (
                        <Form.Item
                          name="llm_prompt"
                          label={t('agents.llmPrompt')}
                          tooltip={t('agents.llmPromptHint')}
                          style={{ marginTop: 12 }}
                        >
                          <Input.TextArea
                            placeholder={t('agents.llmPromptPlaceholder')}
                            autoSize={{ minRows: 2, maxRows: 6 }}
                            allowClear
                          />
                        </Form.Item>
                      )}
                    </div>
                  </>
                ),
              },
            ]}
          />

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

      <BackfillPreviewModal
        open={!!preview}
        data={preview}
        selected={previewSelected}
        onSelectedChange={setPreviewSelected}
        onCancel={() => { setPreview(null); setPendingPayload(null); }}
        onConfirm={(ids) => handlePreviewConfirm(ids)}
        onSkip={() => handlePreviewConfirm([])}
        saving={previewSaving}
      />
    </div>
  );
}

