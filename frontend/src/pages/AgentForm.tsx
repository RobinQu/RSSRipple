import { useState, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
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
  task_expire_days: number;
  llm_enabled: boolean;
  scope_channel_wide: boolean;
  conflict_resolution: 'ask' | 'auto';
}

export default function AgentForm() {
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();
  const mode: 'create' | 'edit' = id ? 'edit' : 'create';
  const { message } = App.useApp();
  const [form] = Form.useForm<FormValues>();
  const [channels, setChannels] = useState<Channel[]>([]);
  const [downloaders, setDownloaders] = useState<DownloaderInstance[]>([]);
  const [works, setWorks] = useState<AgentWork[]>([]);
  const [filterConfig, setFilterConfig] = useState<BoolCondition | null>(null);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(mode === 'edit');
  const [channelWide, setChannelWide] = useState(false);

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
            task_expire_days: a.task_expire_days,
            llm_enabled: a.llm_enabled,
            scope_channel_wide: a.scope_channel_wide,
            conflict_resolution: a.conflict_resolution,
          });
          setChannelWide(a.scope_channel_wide);
          setFilterConfig(a.filter_config);
          if (a.works) setWorks(a.works);
        } else {
          message.error('加载 Agent 失败');
          navigate('/agents');
        }
        setLoading(false);
      });
    }
  }, [mode, id, form, message, navigate]);

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
      message.error('非频道范围模式下至少需要添加 1 个订阅作品');
      return;
    }
    setSaving(true);
    const payload = {
      name: values.name,
      channel_id: values.channel_id,
      downloader_id: values.downloader_id,
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
        message.success(mode === 'edit' ? '已更新' : '已创建');
        navigate(`/agents/${res.data.id}`);
      } else {
        message.error(res.error?.message || '保存失败');
      }
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Spin />;

  return (
    <div style={{ maxWidth: 820 }}>
      <Title level={3} style={{ marginBottom: 24 }}>
        {mode === 'create' ? '新建 Agent' : '编辑 Agent'}
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
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="例如：新番自动下载" />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="channel_id"
                label="频道"
                rules={[{ required: true, message: '请选择频道' }]}
              >
                <Select
                  placeholder="选择频道"
                  options={channels.map((c) => ({ label: c.name, value: c.id }))}
                  disabled={mode === 'edit'}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="downloader_id"
                label="下载器"
                rules={[{ required: true, message: '请选择下载器' }]}
              >
                <Select
                  placeholder="选择下载器"
                  options={downloaders.map((d) => ({ label: d.name, value: d.id }))}
                />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="task_expire_days" label="任务保留天数">
                <InputNumber min={1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="conflict_resolution" label="冲突处理">
                <Radio.Group>
                  <Radio value="ask">询问用户</Radio>
                  <Radio value="auto">自动选择</Radio>
                </Radio.Group>
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="llm_enabled"
                label="启用 LLM 辅助决策"
                valuePropName="checked"
              >
                <Switch checkedChildren="开" unCheckedChildren="关" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="scope_channel_wide"
                label="订阅范围"
                valuePropName="checked"
              >
                <Switch
                  checkedChildren="整个频道"
                  unCheckedChildren="选定作品"
                />
              </Form.Item>
            </Col>
          </Row>

          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: -8, marginBottom: 16 }}>
            {channelWide
              ? '整个频道模式：频道下所有已匹配元数据的资源都会进入过滤流程'
              : '选定作品模式：仅处理下方订阅列表中的作品'}
          </Text>

          <Divider style={{ margin: '12px 0' }} />

          {!channelWide && (
            <div style={{ marginBottom: 20 }}>
              <WorkSelector value={works} onChange={setWorks} maxWorks={10} />
            </div>
          )}

          <div style={{ marginBottom: 16 }}>
            <Text strong style={{ display: 'block', marginBottom: 8 }}>
              全局过滤条件
            </Text>
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
              所有资源都必须通过这些条件，作品级过滤将按 AND 合并
            </Text>
            <FilterBuilder value={filterConfig} onChange={setFilterConfig} />
          </div>

          <Form.Item style={{ marginTop: 24, marginBottom: 0 }}>
            <Space>
              <Button type="primary" htmlType="submit" loading={saving}>
                {mode === 'edit' ? '保存更改' : '创建 Agent'}
              </Button>
              <Button onClick={() => navigate('/agents')}>取消</Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
}
