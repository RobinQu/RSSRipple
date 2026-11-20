import { useEffect, useState } from 'react';
import { Modal, Select, App, Tag, Empty, Typography, Switch } from 'antd';
import { useTranslation } from 'react-i18next';
import { tasksApi } from '../api/tasks';
import { downloadersApi } from '../api/downloaders';
import type { DownloaderInstance } from '../types';

const { Text } = Typography;

interface Props {
  resourceId: string | null;
  open: boolean;
  onClose: () => void;
  onCreated?: () => void;
}

export default function CreateTaskModal({ resourceId, open, onClose, onCreated }: Props) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [downloaders, setDownloaders] = useState<DownloaderInstance[]>([]);
  const [loading, setLoading] = useState(false);
  const [downloaderId, setDownloaderId] = useState<string | null>(null);
  const [autoDeleteOnCompletion, setAutoDeleteOnCompletion] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setDownloaderId(null);
    setAutoDeleteOnCompletion(true);
    setLoading(true);
    downloadersApi
      .list(1, 100)
      .then((res) => {
        if (res.success) setDownloaders(res.data || []);
      })
      .finally(() => setLoading(false));
  }, [open]);

  const handleDownloaderChange = (id: string) => {
    setDownloaderId(id);
    const downloader = downloaders.find((d) => d.id === id);
    setAutoDeleteOnCompletion(downloader?.auto_delete_on_completion ?? true);
  };

  const handleOk = async () => {
    if (!resourceId || !downloaderId) {
      message.warning(t('tasks.selectDownloaderPlaceholder'));
      return;
    }
    setSubmitting(true);
    try {
      const res = await tasksApi.create({
        resource_id: resourceId,
        downloader_id: downloaderId,
        auto_delete_on_completion: autoDeleteOnCompletion,
      });
      if (res.success) {
        message.success(t('tasks.createSuccess'));
        onCreated?.();
        onClose();
      } else {
        message.error(res.error?.message || t('tasks.createFailed'));
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      onOk={handleOk}
      title={t('tasks.createTask')}
      okText={t('common.create')}
      cancelText={t('common.cancel')}
      okButtonProps={{ disabled: !downloaderId }}
      confirmLoading={submitting}
      destroyOnClose
      width={480}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t('tasks.selectDownloader')}
        </Text>
        <Select
          value={downloaderId}
          onChange={handleDownloaderChange}
          loading={loading}
          placeholder={t('tasks.selectDownloaderPlaceholder')}
          notFoundContent={<Empty description={t('tasks.noDownloaders')} />}
          style={{ width: '100%' }}
          options={downloaders.map((d) => ({
            value: d.id,
            label: (
              <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span>{d.name}</span>
                <Tag style={{ marginInlineEnd: 0 }}>{d.type}</Tag>
                <Tag
                  color={d.status === 'connected' ? 'green' : 'default'}
                  style={{ marginInlineEnd: 0 }}
                >
                  {d.status}
                </Tag>
              </span>
            ),
          }))}
        />
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 12 }}>
          <Switch
            checked={autoDeleteOnCompletion}
            onChange={setAutoDeleteOnCompletion}
            disabled={!downloaderId}
          />
          <Text>{t('tasks.autoDeleteOnCompletion')}</Text>
        </div>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t('tasks.autoDeleteExtra')}
        </Text>
      </div>
    </Modal>
  );
}
