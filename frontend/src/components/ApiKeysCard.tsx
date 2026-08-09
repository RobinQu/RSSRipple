import { useState, useEffect, useCallback } from 'react';
import { App, Button, Card, Form, Input, Modal, Popconfirm, Table, Typography } from 'antd';
import type { TableColumnsType } from 'antd';
import { KeyRound, Plus, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { apiKeysApi } from '../api/apiKeys';
import { timeAgo } from '../utils/format';
import type { ApiKey, ApiKeyCreated } from '../types';

const { Text } = Typography;

/** Settings card for personal API keys. The full key is returned only at
 * creation time, so it is shown in a one-off follow-up modal. */
export default function ApiKeysCard() {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);

  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [form] = Form.useForm<{ name: string }>();

  // The just-created key, shown exactly once.
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await apiKeysApi.list();
      if (r.success) setKeys(r.data);
    } finally {
      setLoading(false);
    }
  }, []);

  // Mount fetch only — `loading` already starts true, so the effect itself
  // runs no synchronous setState; event handlers use `load` above.
  useEffect(() => {
    let cancelled = false;
    apiKeysApi
      .list()
      .then((r) => {
        if (!cancelled && r.success) setKeys(r.data);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleCreate = async () => {
    const { name } = await form.validateFields();
    setCreating(true);
    const r = await apiKeysApi.create(name.trim());
    setCreating(false);
    if (r.success) {
      setCreateOpen(false);
      form.resetFields();
      setCreated(r.data);
      await load();
    } else {
      message.error(r.error?.message || t('settings.apiKeys.createFailed'));
    }
  };

  const handleDelete = async (record: ApiKey) => {
    const r = await apiKeysApi.remove(record.id);
    if (r.success) {
      message.success(t('settings.apiKeys.deleted'));
      await load();
    } else {
      message.error(r.error?.message || t('settings.apiKeys.deleteFailed'));
    }
  };

  const columns: TableColumnsType<ApiKey> = [
    {
      title: t('common.name'),
      dataIndex: 'name',
      key: 'name',
      render: (v: string) => <Text style={{ fontSize: 13 }}>{v}</Text>,
    },
    {
      title: t('settings.apiKeys.prefix'),
      dataIndex: 'prefix',
      key: 'prefix',
      width: 140,
      render: (v: string) => <Text code style={{ fontSize: 12 }}>{v}…</Text>,
    },
    {
      title: t('settings.apiKeys.createdAt'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 140,
      render: (v: string) => (
        <Text type="secondary" style={{ fontSize: 12 }}>{timeAgo(v)}</Text>
      ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 80,
      align: 'right',
      render: (_, record) => (
        <Popconfirm
          title={t('settings.apiKeys.deleteConfirm')}
          okText={t('common.confirm')}
          cancelText={t('common.cancel')}
          okButtonProps={{ danger: true }}
          onConfirm={() => handleDelete(record)}
        >
          <Button type="text" size="small" danger icon={<Trash2 size={14} />} />
        </Popconfirm>
      ),
    },
  ];

  return (
    <Card
      size="small"
      style={{ marginTop: 16 }}
      title={
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
          <KeyRound size={16} style={{ color: '#1863dc' }} />
          <span>{t('settings.apiKeys.title')}</span>
        </span>
      }
      extra={
        <Button size="small" icon={<Plus size={14} />} onClick={() => setCreateOpen(true)}>
          {t('settings.apiKeys.create')}
        </Button>
      }
    >
      <Text type="secondary" style={{ display: 'block', marginBottom: 12, fontSize: 12 }}>
        {t('settings.apiKeys.desc')}
      </Text>
      <Table<ApiKey>
        columns={columns}
        dataSource={keys}
        rowKey="id"
        loading={loading}
        size="small"
        pagination={false}
        locale={{ emptyText: t('settings.apiKeys.empty') }}
      />

      <Modal
        title={t('settings.apiKeys.createTitle')}
        open={createOpen}
        onOk={handleCreate}
        onCancel={() => setCreateOpen(false)}
        okText={t('common.create')}
        cancelText={t('common.cancel')}
        confirmLoading={creating}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item
            name="name"
            label={t('common.name')}
            rules={[{ required: true, whitespace: true, message: t('settings.apiKeys.nameRequired') }]}
          >
            <Input placeholder={t('settings.apiKeys.namePlaceholder')} maxLength={64} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('settings.apiKeys.createdTitle')}
        open={!!created}
        footer={
          <Button type="primary" onClick={() => setCreated(null)}>
            {t('settings.apiKeys.createdDismiss')}
          </Button>
        }
        onCancel={() => setCreated(null)}
      >
        {created && (
          <>
            <Text type="warning" style={{ display: 'block', marginBottom: 12, fontSize: 13 }}>
              {t('settings.apiKeys.shownOnce')}
            </Text>
            <Text code copyable={{ text: created.key }} style={{ fontSize: 13, wordBreak: 'break-all' }}>
              {created.key}
            </Text>
          </>
        )}
      </Modal>
    </Card>
  );
}
