import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FolderOpen, Minus, Plus, Zap } from 'lucide-react';
import { App, Button, Form, Input, Modal, Select, Switch } from 'antd';
import { mediaServersApi } from '../api/mediaServers';
import { isValidRelativeSubpath, subpathBrowseStart, toVolumeSubpath } from '../utils/paths';
import DirectoryBrowserModal from './DirectoryBrowserModal';
import type { MediaServer, MediaServerType, StorageVolume } from '../types';

const TYPE_OPTIONS: MediaServerType[] = ['plex', 'emby', 'jellyfin'];

/** Create / edit modal for a MediaServerInstance, with an inline bindings
    editor (server path prefix → volume + subpath; whole-array replacement). */
export default function MediaServerFormModal({
  open,
  server,
  volumes,
  onClose,
  onSaved,
}: {
  open: boolean;
  server: MediaServer | null;
  volumes: StorageVolume[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [browseIndex, setBrowseIndex] = useState<number | null>(null);
  // Watch the whole bindings array so each row's volume selection is reactive
  // (drives the browse button's disabled state and the picker's root path).
  const bindingsValues = (Form.useWatch('bindings', form) ?? []) as Array<{
    server_path_prefix?: string;
    volume_id?: string;
    subpath?: string;
  }>;
  const browseVolume =
    browseIndex != null
      ? volumes.find((v) => v.id === bindingsValues[browseIndex]?.volume_id)
      : undefined;

  useEffect(() => {
    if (open) {
      form.setFieldsValue({
        name: server?.name ?? '',
        type: server?.type ?? 'plex',
        url: server?.url ?? '',
        // Credentials are never echoed back; empty = keep the stored token.
        token: '',
        enabled: server?.enabled ?? true,
        bindings: (server?.bindings ?? []).map((b) => ({
          server_path_prefix: b.server_path_prefix,
          volume_id: b.volume_id,
          subpath: b.subpath,
        })),
      });
    }
  }, [open, server, form]);

  const submit = async () => {
    const values = await form.validateFields();
    const bindings = (values.bindings ?? []).map(
      (b: { server_path_prefix: string; volume_id: string; subpath?: string }) => ({
        server_path_prefix: b.server_path_prefix.trim(),
        volume_id: b.volume_id,
        subpath: b.subpath?.trim() ?? '',
      }),
    );
    const token = (values.token ?? '').trim();
    setSaving(true);
    const res = server
      ? await mediaServersApi.update(server.id, {
          name: values.name.trim(),
          type: values.type as MediaServerType,
          url: values.url.trim(),
          ...(token ? { token } : {}),
          enabled: values.enabled ?? true,
          bindings,
        })
      : await mediaServersApi.create({
          name: values.name.trim(),
          type: values.type as MediaServerType,
          url: values.url.trim(),
          token: token || null,
          enabled: values.enabled ?? true,
          bindings,
        });
    setSaving(false);
    if (res.success) {
      message.success(t(server ? 'mediaServers.saved' : 'mediaServers.created'));
      onSaved();
      onClose();
    } else {
      message.error(res.error?.message || t(server ? 'mediaServers.saveFailed' : 'mediaServers.createFailed'));
    }
  };

  const handleTest = async () => {
    let values: { type?: MediaServerType; url?: string; token?: string };
    try {
      values = await form.validateFields(['type', 'url']);
    } catch {
      return; // url/type validation errors are shown inline
    }
    const type = values.type as MediaServerType;
    const url = (values.url || '').trim();
    const token = (values.token || '').trim() || undefined;
    setTesting(true);
    const res = server
      ? await mediaServersApi.test(server.id, { type, url, token })
      : await mediaServersApi.testUnsaved({ type, url, token });
    setTesting(false);
    if (res.success && res.data.ok) {
      message.success(
        res.data.server_version
          ? t('mediaServers.testOk', { version: res.data.server_version })
          : t('mediaServers.testOkNoVersion'),
      );
    } else if (res.success) {
      message.error(res.data.message || t('mediaServers.testFailed'));
    } else {
      message.error(res.error?.message || t('mediaServers.testFailed'));
    }
  };

  return (
    <>
      <Modal
        open={open}
        title={t(server ? 'mediaServers.editServer' : 'mediaServers.newServer')}
        onOk={submit}
        onCancel={onClose}
        width={760}
        destroyOnHidden
        footer={[
          <Button key="test" icon={<Zap size={14} />} onClick={handleTest} loading={testing}>
            {t('mediaServers.test')}
          </Button>,
          <Button key="cancel" onClick={onClose}>
            {t('common.cancel')}
          </Button>,
          <Button key="ok" type="primary" loading={saving} onClick={submit}>
            {t('common.save')}
          </Button>,
        ]}
      >
      <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
          <Form.Item
            name="name"
            label={t('common.name')}
            style={{ flex: '1 1 240px' }}
            rules={[{ required: true, message: t('mediaServers.nameRequired') }]}
          >
            <Input maxLength={255} />
          </Form.Item>
          <Form.Item name="type" label={t('mediaServers.type')} style={{ width: 160 }}>
            <Select
              options={TYPE_OPTIONS.map((v) => ({ value: v, label: v }))}
            />
          </Form.Item>
          <Form.Item name="enabled" label={t('mediaServers.enabled')} valuePropName="checked">
            <Switch />
          </Form.Item>
        </div>
        <Form.Item
          name="url"
          label={t('mediaServers.url')}
          rules={[{ required: true, message: t('mediaServers.urlRequired') }]}
        >
          <Input maxLength={2048} placeholder="http://plex:32400" style={{ fontFamily: 'monospace' }} />
        </Form.Item>
        <Form.Item name="token" label={t('mediaServers.token')}>
          <Input.Password
            maxLength={1024}
            placeholder={t(server ? 'mediaServers.tokenPlaceholderEdit' : 'mediaServers.tokenPlaceholderCreate')}
            autoComplete="new-password"
          />
        </Form.Item>

        <Form.Item label={t('mediaServers.bindings')} extra={t('mediaServers.bindingsExtra')}>
          <Form.List name="bindings">
            {(fields, { add, remove }) => (
              <>
                {fields.map((field) => (
                  <div
                    key={field.key}
                    style={{ display: 'flex', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}
                  >
                    <Form.Item
                      name={[field.name, 'server_path_prefix']}
                      style={{ flex: '1 1 220px', marginBottom: 0 }}
                      rules={[{ required: true, message: t('mediaServers.serverPathPrefixRequired') }]}
                    >
                      <Input
                        maxLength={1024}
                        placeholder={t('mediaServers.serverPathPrefix')}
                        style={{ fontFamily: 'monospace' }}
                      />
                    </Form.Item>
                    <Form.Item
                      name={[field.name, 'volume_id']}
                      style={{ flex: '0 1 200px', marginBottom: 0 }}
                      rules={[{ required: true, message: t('mediaServers.volumeRequired') }]}
                    >
                      <Select
                        placeholder={t('mediaServers.volume')}
                        options={volumes.map((v) => ({ value: v.id, label: v.name }))}
                      />
                    </Form.Item>
                    <Form.Item
                      name={[field.name, 'subpath']}
                      style={{ flex: '0 1 180px', marginBottom: 0 }}
                      rules={[
                        {
                          validator: (_, v: string) =>
                            !v || isValidRelativeSubpath(v)
                              ? Promise.resolve()
                              : Promise.reject(new Error(t('mediaServers.subpathInvalid'))),
                        },
                      ]}
                    >
                      <Input
                        maxLength={512}
                        placeholder={t('mediaServers.subpath')}
                        suffix={
                          <Button
                            type="text"
                            size="small"
                            icon={<FolderOpen size={14} />}
                            disabled={!bindingsValues?.[field.name]?.volume_id}
                            title={t('volumes.browse')}
                            onClick={() => setBrowseIndex(field.name)}
                          />
                        }
                      />
                    </Form.Item>
                    <Button
                      type="text"
                      size="small"
                      danger
                      icon={<Minus size={14} />}
                      title={t('common.delete')}
                      onClick={() => remove(field.name)}
                    />
                  </div>
                ))}
                <Button
                  type="dashed"
                  size="small"
                  icon={<Plus size={14} />}
                  onClick={() => add({ server_path_prefix: '', volume_id: undefined, subpath: '' })}
                >
                  {t('mediaServers.addBinding')}
                </Button>
              </>
            )}
          </Form.List>
        </Form.Item>
      </Form>
      </Modal>

      <DirectoryBrowserModal
        key={browseIndex ?? 'closed'}
        open={browseIndex != null}
        title={t('mediaServers.subpath')}
        initialPath={subpathBrowseStart(
          browseVolume?.mount_path ?? '',
          browseIndex != null ? bindingsValues[browseIndex]?.subpath ?? '' : '',
        )}
        onSelect={(absPath) => {
          if (browseIndex == null) return;
          const rel = toVolumeSubpath(browseVolume?.mount_path ?? '', absPath);
          if (rel !== null) form.setFieldValue(['bindings', browseIndex, 'subpath'], rel);
        }}
        onCancel={() => setBrowseIndex(null)}
      />
    </>
  );
}
