import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Form, Input, Button, Space, Card, Row, Col, Typography, App } from 'antd';
import { downloadersApi } from '../api/downloaders';

export default function DownloaderForm() {
  const navigate = useNavigate();
  const [form] = Form.useForm();
  const { message } = App.useApp();
  const [saving, setSaving] = useState(false);

  const handleSubmit = async (values: { name: string; url: string; username?: string; password?: string; download_dir?: string }) => {
    setSaving(true);
    const res = await downloadersApi.create({
      name: values.name,
      type: 'transmission',
      url: values.url,
      username: values.username || undefined,
      password: values.password || undefined,
      download_dir: values.download_dir || undefined,
    });
    setSaving(false);
    if (res.success) {
      message.success('Downloader added');
      navigate('/downloaders');
    }
  };

  return (
    <div style={{ maxWidth: 560 }}>
      <Typography.Title level={3} style={{ marginBottom: 24 }}>Add Downloader</Typography.Title>
      <Card>
        <Form form={form} layout="vertical" onFinish={handleSubmit} initialValues={{ name: '', url: '', username: '', password: '', download_dir: '' }}>
          <Form.Item name="name" label="Name" rules={[{ required: true, message: 'Please enter a name' }]}>
            <Input placeholder="My Transmission" />
          </Form.Item>
          <Form.Item name="url" label="API URL" rules={[{ required: true, message: 'Please enter the API URL' }]}>
            <Input placeholder="http://transmission:9091/transmission/rpc" />
          </Form.Item>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="username" label="Username">
                <Input />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="password" label="Password">
                <Input.Password />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="download_dir" label="Download Directory">
            <Input placeholder="/downloads/anime" />
          </Form.Item>
          <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: -8, marginBottom: 16 }}>
            Directory on the downloader where files will be saved.
          </Typography.Text>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={saving}>Add Downloader</Button>
              <Button onClick={() => navigate('/downloaders')}>Cancel</Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
}
