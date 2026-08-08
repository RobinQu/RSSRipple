import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { App, Form, Input, Modal } from 'antd';
import { collectionsApi } from '../api/collections';
import type { WorkCollection } from '../types';

/** Create / rename modal — title_cn required, title_en/description optional. */
export default function CollectionFormModal({
  open,
  collection,
  onClose,
  onSaved,
}: {
  open: boolean;
  collection: WorkCollection | null;
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
        title_cn: collection?.title_cn ?? '',
        title_en: collection?.title_en ?? '',
        description: collection?.description ?? '',
      });
    }
  }, [open, collection, form]);

  const submit = async () => {
    const values = await form.validateFields();
    setSaving(true);
    const body = {
      title_cn: values.title_cn.trim(),
      title_en: values.title_en?.trim() || null,
      description: values.description?.trim() || null,
    };
    const res = collection
      ? await collectionsApi.update(collection.id, body)
      : await collectionsApi.create(body);
    setSaving(false);
    if (res.success) {
      message.success(t(collection ? 'collections.saved' : 'collections.created'));
      onSaved();
      onClose();
    } else {
      message.error(res.error?.message || t(collection ? 'collections.saveFailed' : 'collections.createFailed'));
    }
  };

  return (
    <Modal
      open={open}
      title={t(collection ? 'collections.editTitle' : 'collections.createTitle')}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      onOk={submit}
      onCancel={onClose}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
        <Form.Item
          name="title_cn"
          label={t('collections.nameCn')}
          rules={[{ required: true, message: t('collections.nameCnRequired') }]}
        >
          <Input maxLength={200} />
        </Form.Item>
        <Form.Item name="title_en" label={t('collections.nameEn')}>
          <Input maxLength={200} />
        </Form.Item>
        <Form.Item name="description" label={t('collections.description')}>
          <Input.TextArea rows={2} maxLength={2000} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
