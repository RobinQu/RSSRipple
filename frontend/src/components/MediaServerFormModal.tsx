import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Minus, Plus } from 'lucide-react';
import { App, Button, Form, Input, Modal, Select, Switch } from 'antd';
import { mediaServersApi } from '../api/mediaServers';
import { isValidRelativeSubpath } from '../utils/paths';
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

  return (
    <Modal
      open={open}
      title={t(server ? 'mediaServers.editServer' : 'mediaServers.newServer')}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      onOk={submit}
      onCancel={onClose}
      width={760}
      destroyOnHidden
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
                      <Input maxLength={512} placeholder={t('mediaServers.subpath')} />
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
  );
}
