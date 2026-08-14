import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { App, Form, Input, Modal } from 'antd';
import { organizeApi } from '../api/organize';
import type { Library } from '../types';

/** Edit modal for a scan-derived Library: only the subtitle language map is
    user-editable; everything else comes from the media-server scan. */
export default function LibraryEditModal({
  open,
  library,
  onClose,
  onSaved,
}: {
  open: boolean;
  library: Library | null;
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
        // Edited as raw JSON text; parsed + validated on submit.
        subtitle_lang_map: library?.subtitle_lang_map
          ? JSON.stringify(library.subtitle_lang_map, null, 2)
          : '',
      });
    }
  }, [open, library, form]);

  const submit = async () => {
    if (!library) return;
    const values = await form.validateFields();
    let langMap: Record<string, string> | null = null;
    const rawMap = (values.subtitle_lang_map ?? '').trim();
    if (rawMap) {
      try {
        const parsed: unknown = JSON.parse(rawMap);
        if (
          typeof parsed !== 'object' ||
          parsed === null ||
          Array.isArray(parsed) ||
          Object.entries(parsed).some(
            ([k, v]) => typeof k !== 'string' || typeof v !== 'string',
          )
        ) {
          throw new Error('not a string map');
        }
        langMap = parsed as Record<string, string>;
      } catch {
        message.error(t('libraries.subtitleLangMapInvalid'));
        return;
      }
    }
    setSaving(true);
    const res = await organizeApi.updateLibrary(library.id, {
      subtitle_lang_map: langMap,
    });
    setSaving(false);
    if (res.success) {
      message.success(t('mediaServers.librarySaved'));
      onSaved();
      onClose();
    } else {
      message.error(res.error?.message || t('mediaServers.librarySaveFailed'));
    }
  };

  return (
    <Modal
      open={open}
      title={t('mediaServers.editLibrary', { name: library?.name ?? '' })}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      onOk={submit}
      onCancel={onClose}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
        <Form.Item
          name="subtitle_lang_map"
          label={t('libraries.subtitleLangMap')}
          extra={t('libraries.subtitleLangMapExtra')}
        >
          <Input.TextArea
            rows={3}
            placeholder='{"zh-CN": "chs", "zh-TW": "cht"}'
            style={{ fontFamily: 'monospace' }}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
