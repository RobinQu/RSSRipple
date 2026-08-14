import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { App, Form, Input, Modal, Select } from 'antd';
import { organizeApi } from '../api/organize';
import { isValidRelativeSubpath } from '../utils/paths';
import type { Library, StorageVolume } from '../types';

/** In-place binding fix for an unbound (scan-derived) Library: pick the
    storage volume + subpath that corresponds to its server-side root. */
export default function LibraryBindModal({
  open,
  library,
  volumes,
  onClose,
  onSaved,
}: {
  open: boolean;
  library: Library | null;
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
        volume_id: library?.volume_id ?? undefined,
        root_subpath: library?.root_subpath ?? '',
      });
    }
  }, [open, library, form]);

  const submit = async () => {
    if (!library) return;
    const values = await form.validateFields();
    setSaving(true);
    const res = await organizeApi.updateLibrary(library.id, {
      volume_id: values.volume_id,
      root_subpath: values.root_subpath?.trim() || null,
    });
    setSaving(false);
    if (res.success) {
      message.success(t('mediaServers.bindSuccess'));
      onSaved();
      onClose();
    } else {
      message.error(res.error?.message || t('mediaServers.bindFailed'));
    }
  };

  return (
    <Modal
      open={open}
      title={t('mediaServers.bindTitle', { name: library?.name ?? '' })}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      onOk={submit}
      onCancel={onClose}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
        <Form.Item
          name="volume_id"
          label={t('mediaServers.volume')}
          rules={[{ required: true, message: t('mediaServers.volumeRequired') }]}
        >
          <Select
            options={volumes.map((v) => ({
              value: v.id,
              label: `${v.name} (${v.mount_path})`,
            }))}
          />
        </Form.Item>
        <Form.Item
          name="root_subpath"
          label={t('mediaServers.subpath')}
          extra={t('mediaServers.bindSubpathExtra')}
          rules={[
            {
              validator: (_, v: string) =>
                !v || isValidRelativeSubpath(v)
                  ? Promise.resolve()
                  : Promise.reject(new Error(t('mediaServers.subpathInvalid'))),
            },
          ]}
        >
          <Input maxLength={512} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
