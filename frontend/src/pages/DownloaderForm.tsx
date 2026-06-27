import { useState, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Form,
  Input,
  Button,
  Card,
  Space,
  Typography,
  App,
  Spin,
} from 'antd';
import { Zap } from 'lucide-react';
import { downloadersApi } from '../api/downloaders';

const { Title } = Typography;

export default function DownloaderForm() {
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();
  const mode = id ? 'edit' : 'create';
  const [form] = Form.useForm();
  const { message } = App.useApp();
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(mode === 'edit');
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    if (mode !== 'edit' || !id) return;
    (async () => {
      const res = await downloadersApi.get(id);
      if (res.success) {
        form.setFieldsValue({
          name: res.data.name,
          url: res.data.url,
          username: res.data.username ?? '',
          password: '',
          download_dir: res.data.download_dir ?? '',
        });
      } else {
        message.error('加载下载器失败');
        navigate('/downloaders');
      }
      setLoading(false);
    })();
  }, [id, mode, form, message, navigate]);

  const handleTest = async () => {
    if (mode !== 'edit' || !id) return;
    setTesting(true);
    const res = await downloadersApi.test(id);
    setTesting(false);
    if (res.success) message.success(res.data.message || '连接成功');
    else message.error(res.error?.message || '连接失败');
  };

  const handleSubmit = async (values: {
    name: string;
    url: string;
    username?: string;
    password?: string;
    download_dir?: string;
  }) => {
    setSaving(true);
    const payload = {
      name: values.name,
      type: 'transmission' as const,
      url: values.url,
      username: values.username || undefined,
      password: values.password || undefined,
      download_dir: values.download_dir || undefined,
    };
    try {
      let res;
      if (mode === 'edit' && id) {
        res = await downloadersApi.update(id, payload);
      } else {
        res = await downloadersApi.create(payload);
      }
      if (res.success) {
        message.success(mode === 'edit' ? '已更新' : '已添加');
        navigate(`/downloaders/${res.data.id}`);
      } else {
        message.error(res.error?.message || '保存失败');
      }
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Spin />;

  return (
    <div style={{ maxWidth: 560 }}>
      <Title level={3} style={{ marginBottom: 24 }}>
        {mode === 'edit' ? '编辑下载器' : '添加下载器'}
      </Title>
      <Card>
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSubmit}
          initialValues={{ type: 'transmission' }}
        >
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: '请输入名称' }]}
          >
            <Input placeholder="我的 Transmission" />
          </Form.Item>

          <Form.Item
            name="url"
            label="RPC URL"
            rules={[{ required: true, message: '请输入 RPC URL' }]}
          >
            <Input placeholder="http://127.0.0.1:9091/transmission/rpc" />
          </Form.Item>

          <Form.Item name="download_dir" label="默认下载目录">
            <Input placeholder="/downloads/complete" />
          </Form.Item>

          <Space style={{ width: '100%' }} size={16}>
            <Form.Item name="username" label="用户名" style={{ flex: 1 }}>
              <Input autoComplete="off" />
            </Form.Item>
            <Form.Item name="password" label="密码" style={{ flex: 1 }}>
              <Input.Password
                placeholder={mode === 'edit' ? '留空保持原密码' : undefined}
                autoComplete="new-password"
              />
            </Form.Item>
          </Space>

          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={saving}>
                {mode === 'edit' ? '保存更改' : '添加下载器'}
              </Button>
              {mode === 'edit' && id && (
                <Button icon={<Zap size={14} />} onClick={handleTest} loading={testing}>
                  测试连接
                </Button>
              )}
              <Button onClick={() => navigate(mode === 'edit' ? `/downloaders/${id}` : '/downloaders')}>
                取消
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
}
