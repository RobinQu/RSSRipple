import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Form, Input, InputNumber, Button, Space, Card, Typography, App } from 'antd';
import { CheckCircle, XCircle, Loader2 } from 'lucide-react';
import { channelsApi } from '../api/channels';

export default function ChannelForm() {
  const navigate = useNavigate();
  const [form] = Form.useForm();
  const { message } = App.useApp();
  const [saving, setSaving] = useState(false);
  const [urlStatus, setUrlStatus] = useState<'idle' | 'checking' | 'valid' | 'invalid'>('idle');
  const [urlMessage, setUrlMessage] = useState('');
  const [downloadableCount, setDownloadableCount] = useState(0);

  const validateUrl = async () => {
    const url = form.getFieldValue('url');
    if (!url) return;
    setUrlStatus('checking');
    const res = await channelsApi.validateUrl(url);
    if (res.success && res.data.valid) {
      setUrlStatus('valid');
      setDownloadableCount(res.data.downloadable_count);
      setUrlMessage(`Valid feed: ${res.data.item_count} items, ${res.data.downloadable_count} with downloads`);
    } else {
      setUrlStatus('invalid');
      setUrlMessage(res.data?.message || 'Invalid URL');
    }
  };

  const handleSubmit = async (values: { name: string; url: string; fetch_interval: number }) => {
    setSaving(true);
    const res = await channelsApi.create({ name: values.name, url: values.url, fetch_interval: values.fetch_interval, type: 'rss_feed' });
    setSaving(false);
    if (res.success) {
      message.success('Channel created');
      navigate('/channels');
    }
  };

  return (
    <div style={{ maxWidth: 560 }}>
      <Typography.Title level={3} style={{ marginBottom: 24 }}>Create Channel</Typography.Title>
      <Card>
        <Form form={form} layout="vertical" onFinish={handleSubmit} initialValues={{ name: '', url: '', fetch_interval: 1800 }}>
          <Form.Item name="name" label="Name" rules={[{ required: true, message: 'Please enter a channel name' }]}>
            <Input placeholder="My anime feed" />
          </Form.Item>
          <Form.Item name="url" label="RSS URL" rules={[{ required: true, message: 'Please enter the RSS URL' }]}>
            <Space.Compact style={{ width: '100%' }}>
              <Input
                placeholder="https://mikanani.me/RSS/..."
                onChange={() => setUrlStatus('idle')}
              />
              <Button onClick={validateUrl}>Validate</Button>
            </Space.Compact>
          </Form.Item>
          {urlStatus === 'checking' && (
            <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 6 }}>
              <Loader2 size={14} style={{ animation: 'spin 1s linear infinite' }} />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>Checking...</Typography.Text>
            </div>
          )}
          {urlStatus === 'valid' && (
            <div style={{ marginBottom: 16 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <CheckCircle size={14} color="#52c41a" />
                <Typography.Text style={{ fontSize: 12, color: '#52c41a' }}>{urlMessage}</Typography.Text>
              </div>
              {downloadableCount === 0 && (
                <Typography.Text style={{ fontSize: 12, color: '#faad14' }}>
                  Warning: no torrent files or magnet links found in feed entries
                </Typography.Text>
              )}
            </div>
          )}
          {urlStatus === 'invalid' && (
            <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 6 }}>
              <XCircle size={14} color="#ff4d4f" />
              <Typography.Text style={{ fontSize: 12, color: '#ff4d4f' }}>{urlMessage}</Typography.Text>
            </div>
          )}
          <Form.Item name="fetch_interval" label="Fetch Interval (seconds)" rules={[{ required: true, message: 'Required' }]}>
            <InputNumber min={60} style={{ width: 160 }} />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={saving}>Create Channel</Button>
              <Button onClick={() => navigate('/channels')}>Cancel</Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
}
