import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { App, Form, Input, Modal } from 'antd';
import { volumesApi } from '../api/volumes';
import type { StorageVolume } from '../types';

/** Create / edit modal for a StorageVolume (logical storage volume). */
export default function VolumeFormModal({
  open,
  volume,
  onClose,
  onSaved,
}: {
  open: boolean;
  volume: StorageVolume | null;
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
        name: volume?.name ?? '',
        mount_path: volume?.mount_path ?? '',
        remark: volume?.remark ?? '',
      });
    }
  }, [open, volume, form]);

  const submit = async () => {
    const values = await form.validateFields();
    const body = {
      name: values.name.trim(),
      mount_path: values.mount_path.trim(),
      remark: values.remark?.trim() || null,
    };
    setSaving(true);
    const res = volume
      ? await volumesApi.update(volume.id, body)
      : await volumesApi.create(body);
    setSaving(false);
    if (res.success) {
      message.success(t(volume ? 'volumes.saved' : 'volumes.created'));
      onSaved();
      onClose();
    } else {
      // 422 mount_path missing / 409 duplicate name — surface as-is.
      message.error(res.error?.message || t(volume ? 'volumes.saveFailed' : 'volumes.createFailed'));
    }
  };

  return (
    <Modal
      open={open}
      title={t(volume ? 'volumes.editVolume' : 'volumes.newVolume')}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      onOk={submit}
      onCancel={onClose}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
        <Form.Item
          name="name"
          label={t('common.name')}
          rules={[{ required: true, message: t('volumes.nameRequired') }]}
        >
          <Input maxLength={255} />
        </Form.Item>
        <Form.Item
          name="mount_path"
          label={t('volumes.mountPath')}
          rules={[{ required: true, message: t('volumes.mountPathRequired') }]}
          extra={t('volumes.mountPathExtra')}
        >
          <Input maxLength={1024} placeholder="/storage/main" style={{ fontFamily: 'monospace' }} />
        </Form.Item>
        <Form.Item name="remark" label={t('volumes.remark')}>
          <Input.TextArea rows={2} maxLength={1024} placeholder={t('volumes.remarkPlaceholder')} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
