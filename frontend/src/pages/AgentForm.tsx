import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
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
} from 'antd';
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons';
import { agentsApi } from '../api/agents';
import { channelsApi } from '../api/channels';
import { downloadersApi } from '../api/downloaders';
import type { Channel, DownloaderInstance, FilterField, FilterOperator } from '../types';

const FIELDS: { label: string; value: FilterField }[] = [
  { label: 'subtitle_group', value: 'subtitle_group' },
  { label: 'resolution', value: 'resolution' },
  { label: 'container', value: 'container' },
  { label: 'video_codec', value: 'video_codec' },
  { label: 'audio_codec', value: 'audio_codec' },
  { label: 'subtitle_type', value: 'subtitle_type' },
  { label: 'source', value: 'source' },
  { label: 'title_cn', value: 'title_cn' },
  { label: 'title_en', value: 'title_en' },
];

const OPERATORS: { label: string; value: FilterOperator }[] = [
  { label: 'eq', value: 'eq' },
  { label: 'contains', value: 'contains' },
  { label: 'fuzzy', value: 'fuzzy' },
  { label: 'in', value: 'in' },
  { label: 'regex', value: 'regex' },
];

const CONTENT_TYPES = [
  { label: 'Anime', value: 'anime' },
  { label: 'TV Series', value: 'tv' },
  { label: 'Movie', value: 'movie' },
  { label: 'Mixed', value: 'mixed' },
];

const METADATA_SOURCES = [
  { label: 'None', value: '' },
  { label: 'IMDB (Cinemagoer)', value: 'imdb' },
  { label: 'TVDB', value: 'tvdb' },
];

export default function AgentForm() {
  const navigate = useNavigate();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [channels, setChannels] = useState<Channel[]>([]);
  const [downloaders, setDownloaders] = useState<DownloaderInstance[]>([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    channelsApi.list(1, 100).then((r) => {
      if (r.success) setChannels(r.data);
    });
    downloadersApi.list(1, 100).then((r) => {
      if (r.success) setDownloaders(r.data);
    });
  }, []);

  const channelOptions = channels.map((c) => ({ label: c.name, value: c.id }));
  const downloaderOptions = downloaders.map((d) => ({ label: d.name, value: d.id }));

  const handleSubmit = async (values: any) => {
    setSaving(true);
    try {
      const payload = {
        name: values.name,
        channel_id: values.channel_id,
        downloader_id: values.downloader_id,
        task_expire_days: values.task_expire_days,
        llm_enabled: values.llm_enabled ?? false,
        metadata_source: values.metadata_source || undefined,
        content_type: values.content_type,
        filters: (values.filters || []).map((f: any) => ({
          field: f.field,
          operator: f.operator,
          value: f.value,
          priority: f.priority,
          is_required: f.is_required ?? false,
        })),
      };
      const res = await agentsApi.create(payload);
      if (res.success) {
        message.success('Agent created');
        navigate('/agents');
      }
    } catch {
      message.error('Failed to create agent');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ maxWidth: 720 }}>
      <Typography.Title level={3} style={{ marginBottom: 24 }}>
        Create Agent
      </Typography.Title>
      <Card>
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSubmit}
          initialValues={{
            task_expire_days: 30,
            llm_enabled: false,
            content_type: 'anime',
            metadata_source: '',
          }}
        >
          <Form.Item
            name="name"
            label="Name"
            rules={[{ required: true, message: 'Please enter a name' }]}
          >
            <Input placeholder="Agent name" />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="channel_id"
                label="Channel"
                rules={[{ required: true, message: 'Please select a channel' }]}
              >
                <Select
                  placeholder="Select channel..."
                  options={channelOptions}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="downloader_id"
                label="Downloader"
                rules={[{ required: true, message: 'Please select a downloader' }]}
              >
                <Select
                  placeholder="Select downloader..."
                  options={downloaderOptions}
                />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="task_expire_days" label="Task Expire Days">
                <InputNumber min={1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="content_type" label="Content Type">
                <Select options={CONTENT_TYPES} />
              </Form.Item>
            </Col>
          </Row>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="llm_enabled" label="Enable LLM-assisted decisions" valuePropName="checked">
                <Switch checkedChildren="On" unCheckedChildren="Off" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="metadata_source" label="Metadata Source">
                <Select options={METADATA_SOURCES} />
              </Form.Item>
            </Col>
          </Row>

          <Typography.Text strong style={{ display: 'block', marginBottom: 12 }}>
            Resource Filters
          </Typography.Text>

          <Form.List name="filters">
            {(fields, { add, remove }) => (
              <>
                {fields.map(({ key, name, ...restField }) => (
                  <Space
                    key={key}
                    align="baseline"
                    style={{ display: 'flex', marginBottom: 8, flexWrap: 'wrap' }}
                  >
                    <Form.Item
                      {...restField}
                      name={[name, 'field']}
                      rules={[{ required: true, message: 'Field' }]}
                      style={{ marginBottom: 0 }}
                    >
                      <Select options={FIELDS} placeholder="Field" style={{ width: 150 }} />
                    </Form.Item>
                    <Form.Item
                      {...restField}
                      name={[name, 'operator']}
                      rules={[{ required: true, message: 'Op' }]}
                      style={{ marginBottom: 0 }}
                    >
                      <Select options={OPERATORS} placeholder="Operator" style={{ width: 110 }} />
                    </Form.Item>
                    <Form.Item
                      {...restField}
                      name={[name, 'value']}
                      style={{ marginBottom: 0 }}
                    >
                      <Input placeholder="Value" style={{ width: 140 }} />
                    </Form.Item>
                    <Form.Item
                      {...restField}
                      name={[name, 'priority']}
                      style={{ marginBottom: 0 }}
                    >
                      <InputNumber placeholder="Priority" style={{ width: 80 }} min={0} />
                    </Form.Item>
                    <Form.Item
                      {...restField}
                      name={[name, 'is_required']}
                      valuePropName="checked"
                      style={{ marginBottom: 0 }}
                    >
                      <Switch checkedChildren="Req" unCheckedChildren="Opt" size="small" />
                    </Form.Item>
                    <Button
                      type="text"
                      danger
                      icon={<DeleteOutlined />}
                      onClick={() => remove(name)}
                    />
                  </Space>
                ))}
                <Button
                  type="dashed"
                  onClick={() =>
                    add({
                      field: 'subtitle_group',
                      operator: 'eq',
                      value: '',
                      priority: fields.length * 10,
                      is_required: false,
                    })
                  }
                  block
                  icon={<PlusOutlined />}
                >
                  Add Filter
                </Button>
              </>
            )}
          </Form.List>

          <Form.Item style={{ marginTop: 24, marginBottom: 0 }}>
            <Space>
              <Button type="primary" htmlType="submit" loading={saving}>
                Create Agent
              </Button>
              <Button onClick={() => navigate('/agents')}>Cancel</Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
}
