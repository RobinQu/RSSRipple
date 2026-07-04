import { useState, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  Form,
  Input,
  Button,
  Card,
  Space,
  Typography,
  App,
  Spin,
  Select,
  Alert,
} from 'antd';
import { Folder, Zap } from 'lucide-react';
import { downloadersApi } from '../api/downloaders';

const { Title } = Typography;

type DownloaderType = 'transmission' | 'mock';

export default function DownloaderForm() {
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();
  const mode = id ? 'edit' : 'create';
  const [form] = Form.useForm();
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(mode === 'edit');
  const [testing, setTesting] = useState(false);
  const [type, setType] = useState<DownloaderType>('transmission');

  useEffect(() => {
    if (mode !== 'edit' || !id) return;
    (async () => {
      const res = await downloadersApi.get(id);
      if (res.success) {
        const t = (res.data.type || 'transmission') as DownloaderType;
        setType(t);
        form.setFieldsValue({
          type: t,
          name: res.data.name,
          url: res.data.url,
          download_dir: res.data.download_dir,
          username: res.data.username ?? '',
          password: '',
        });
      } else {
        message.error(t('downloaders.loadFailed'));
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
    if (res.success) message.success(res.data.message || t('downloaders.connectionSuccess'));
    else message.error(res.error?.message || t('downloaders.connectionFailed'));
  };

  const handleSubmit = async (values: {
    type?: DownloaderType;
    name: string;
    url?: string;
    username?: string;
    password?: string;
    download_dir?: string;
  }) => {
    setSaving(true);
    const activeType = (values.type || type) as DownloaderType;
    const payload = {
      name: values.name,
      type: activeType,
      url: values.url || (activeType === 'mock' ? 'mock://local' : ''),
      download_dir: values.download_dir || (activeType === 'mock' ? '/tmp/mock-downloads' : ''),
      username: values.username || undefined,
      password: values.password || undefined,
    };
    try {
      let res;
      if (mode === 'edit' && id) {
        res = await downloadersApi.update(id, payload);
      } else {
        res = await downloadersApi.create(payload);
      }
      if (res.success) {
        message.success(t('downloaders.saved'));
        navigate(`/downloaders/${res.data.id}`);
      } else {
        message.error(res.error?.message || t('downloaders.saveFailed'));
      }
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Spin />;

  const isMock = type === 'mock';

  return (
    <div style={{ maxWidth: 560 }}>
      <Title level={3} style={{ marginBottom: 24 }}>
        {mode === 'edit' ? t('downloaders.editDownloader') : t('downloaders.addDownloader')}
      </Title>
      <Card>
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSubmit}
          initialValues={{ type: 'transmission' }}
        >
          <Form.Item name="type" label={t('downloaders.type')}>
            <Select
              disabled={mode === 'edit'}
              onChange={(v: DownloaderType) => setType(v)}
              options={[
                { value: 'transmission', label: t('downloaders.typeTransmission') },
                { value: 'mock', label: t('downloaders.typeMock') },
              ]}
            />
          </Form.Item>

          {isMock && (
            <Alert
              type="info"
              showIcon
              message={t('downloaders.mockDescription')}
              style={{ marginBottom: 16 }}
            />
          )}

          <Form.Item
            name="name"
            label={t('common.name')}
            rules={[{ required: true, message: t('downloaders.pleaseEnterName') }]}
          >
            <Input placeholder={t('downloaders.nameExample')} />
          </Form.Item>

          {!isMock && (
            <Form.Item
              name="url"
              label={t('downloaders.rpcUrl')}
              rules={[{ required: true, message: t('downloaders.enterRpcUrl') }]}
            >
              <Input placeholder="http://127.0.0.1:9091/transmission/rpc" />
            </Form.Item>
          )}

          <Form.Item
            name="download_dir"
            label={t('downloaders.defaultDir')}
            rules={isMock ? [] : [{ required: true, message: t('downloaders.enterDefaultDir') }]}
          >
            <Input
              prefix={<Folder size={14} />}
              placeholder={isMock ? '/tmp/mock-downloads' : '/volume1/downloads/rssripple'}
            />
          </Form.Item>

          {!isMock && (
            <Space style={{ width: '100%' }} size={16}>
              <Form.Item name="username" label={t('downloaders.username')} style={{ flex: 1 }}>
                <Input autoComplete="off" />
              </Form.Item>
              <Form.Item name="password" label={t('downloaders.password')} style={{ flex: 1 }}>
                <Input.Password
                  placeholder={mode === 'edit' ? t('downloaders.passwordHint') : undefined}
                  autoComplete="new-password"
                />
              </Form.Item>
            </Space>
          )}

          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={saving}>
                {mode === 'edit' ? t('common.saveChanges') : t('downloaders.addDownloader')}
              </Button>
              {mode === 'edit' && id && (
                <Button htmlType="button" icon={<Zap size={14} />} onClick={handleTest} loading={testing}>
                  {t('downloaders.testConnection')}
                </Button>
              )}
              <Button htmlType="button" onClick={() => navigate(mode === 'edit' ? `/downloaders/${id}` : '/downloaders')}>
                {t('common.cancel')}
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
}
