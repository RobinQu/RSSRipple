import { useState, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useDocumentTitle from '../hooks/useDocumentTitle';
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
  Collapse,
  Divider,
} from 'antd';
import { Folder, FolderOpen, Zap, ChevronDown } from 'lucide-react';
import { downloadersApi } from '../api/downloaders';
import { volumesApi } from '../api/volumes';
import { isValidRelativeSubpath, subpathBrowseStart, toVolumeSubpath } from '../utils/paths';
import DirectoryBrowserModal from '../components/DirectoryBrowserModal';
import type { StorageVolume } from '../types';

const { Title } = Typography;

type DownloaderType = 'transmission' | 'mock';

/** Default download root for new downloaders (Transmission daemon's view). */
const DEFAULT_DOWNLOAD_DIR = '/downloads/complete';

export default function DownloaderForm() {
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();
  const mode = id ? 'edit' : 'create';
  const [form] = Form.useForm();
  const { t } = useTranslation();
  useDocumentTitle(t(mode === 'edit' ? 'downloaders.editDownloader' : 'downloaders.addDownloader'));
  const { message } = App.useApp();
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(mode === 'edit');
  const [testing, setTesting] = useState(false);
  const [type, setType] = useState<DownloaderType>('transmission');
  const [volumes, setVolumes] = useState<StorageVolume[]>([]);
  const [subpathBrowserOpen, setSubpathBrowserOpen] = useState(false);
  const volumeId = Form.useWatch('volume_id', form);
  const selectedVolume = volumes.find((v) => v.id === volumeId);

  useEffect(() => {
    (async () => {
      const res = await volumesApi.list();
      if (res.success) setVolumes(res.data);
    })();
  }, []);

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
          download_dir: res.data.download_dir || DEFAULT_DOWNLOAD_DIR,
          username: res.data.username ?? '',
          password: '',
          volume_id: res.data.volume_id ?? undefined,
          volume_subpath: res.data.volume_subpath ?? '',
        });
      } else {
        message.error(t('downloaders.loadFailed'));
        navigate('/downloaders');
      }
      setLoading(false);
    })();
  }, [id, mode, form, message, navigate, t]);

  const handleTest = async () => {
    if (mode !== 'edit' || !id) return;
    // Probe the *unsaved* form values, not the stored config — the backend
    // falls back to stored values for anything left blank (e.g. password).
    let values: {
      url?: string;
      username?: string;
      password?: string;
      download_dir?: string;
      volume_id?: string;
      volume_subpath?: string;
    };
    try {
      values = await form.validateFields();
    } catch {
      return; // validation errors are already shown inline
    }
    setTesting(true);
    const res = await downloadersApi.test(id, {
      url: values.url || undefined,
      username: values.username || undefined,
      password: values.password || undefined,
      download_dir: values.download_dir || undefined,
      // Explicit null = unbind (identity); omitted is not possible here since
      // the form always reflects a concrete state.
      volume_id: values.volume_id ?? null,
      volume_subpath: values.volume_id ? values.volume_subpath ?? null : null,
    });
    setTesting(false);
    if (res.success && res.data?.success !== false) {
      message.success(res.data.message || t('downloaders.connectionSuccess'));
    } else {
      message.error(
        res.error?.message || res.data?.message || t('downloaders.connectionFailed'),
      );
    }
  };

  const handleSubmit = async (values: {
    type?: DownloaderType;
    name: string;
    url?: string;
    username?: string;
    password?: string;
    download_dir?: string;
    volume_id?: string;
    volume_subpath?: string;
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
      // Volume binding: both null = daemon and process see identical paths.
      volume_id: values.volume_id || null,
      volume_subpath: values.volume_id ? values.volume_subpath?.trim() || null : null,
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
          initialValues={{ type: 'transmission', download_dir: DEFAULT_DOWNLOAD_DIR }}
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
              placeholder={isMock ? '/tmp/mock-downloads' : DEFAULT_DOWNLOAD_DIR}
            />
          </Form.Item>

          {/* Optional settings, folded away so the common path stays short. */}
          <Collapse
            ghost
            expandIcon={({ isActive }) => (
              <ChevronDown
                size={14}
                style={{ transition: 'transform 0.2s', transform: isActive ? 'rotate(180deg)' : 'none' }}
              />
            )}
            items={[
              {
                key: 'volume',
                label: t('downloaders.volumeSection'),
                children: (
                  <>
                    <Form.Item
                      name="volume_id"
                      label={t('downloaders.volume')}
                      extra={t('downloaders.volumeExtra')}
                    >
                      <Select
                        allowClear
                        placeholder={t('downloaders.volumePlaceholder')}
                        options={volumes.map((v) => ({
                          value: v.id,
                          label: `${v.name} (${v.mount_path})`,
                        }))}
                        onChange={(v?: string) => {
                          // Subpath only makes sense attached to a volume.
                          if (!v) form.setFieldValue('volume_subpath', '');
                        }}
                      />
                    </Form.Item>
                    <Form.Item
                      name="volume_subpath"
                      label={t('downloaders.volumeSubpath')}
                      extra={t('downloaders.volumeSubpathExtra')}
                      rules={[
                        {
                          validator: (_, value: string) =>
                            isValidRelativeSubpath(value ?? '')
                              ? Promise.resolve()
                              : Promise.reject(new Error(t('downloaders.volumeSubpathInvalid'))),
                        },
                      ]}
                    >
                      <Input
                        disabled={!volumeId}
                        maxLength={1024}
                        placeholder="downloads/complete"
                        style={{ fontFamily: 'monospace' }}
                        suffix={
                          <Button
                            type="text"
                            size="small"
                            icon={<FolderOpen size={14} />}
                            disabled={!volumeId}
                            onClick={() => setSubpathBrowserOpen(true)}
                          >
                            {t('volumes.browse')}
                          </Button>
                        }
                      />
                    </Form.Item>
                  </>
                ),
              },
              ...(!isMock
                ? [
                    {
                      key: 'auth',
                      label: t('downloaders.authSection'),
                      children: (
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
                      ),
                    },
                  ]
                : []),
            ]}
          />

          <Divider style={{ margin: '12px 0 16px' }} />

          <Form.Item style={{ marginBottom: 0 }}>
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

      <DirectoryBrowserModal
        open={subpathBrowserOpen}
        title={t('downloaders.volumeSubpath')}
        initialPath={subpathBrowseStart(
          selectedVolume?.mount_path ?? '',
          form.getFieldValue('volume_subpath') ?? '',
        )}
        onSelect={(absPath) => {
          const rel = toVolumeSubpath(selectedVolume?.mount_path ?? '', absPath);
          if (rel !== null) form.setFieldValue('volume_subpath', rel);
        }}
        onCancel={() => setSubpathBrowserOpen(false)}
      />
    </div>
  );
}
