import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import {
  App,
  Button,
  Card,
  Checkbox,
  DatePicker,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Radio,
  Space,
  Switch,
  Table,
  Tag,
  theme,
  Typography,
} from 'antd';
import type { TableColumnsType } from 'antd';
import type { Dayjs } from 'dayjs';
import { Eye, History, Plus, RefreshCw, RotateCcw, Trash2 } from 'lucide-react';
import { notificationsApi, type RetryMode } from '../api/notifications';
import { usePolling } from '../hooks/usePolling';
import StatusBadge from './StatusBadge';
import EllipsisText from './EllipsisText';
import { timeAgo } from '../utils/format';
import { withMobileLabels } from '../utils/table';
import type {
  AgentWebhook,
  DownloadNotification,
  DownloadNotificationDetail,
  WebhookDelivery,
} from '../types';

const { Text } = Typography;

const PAGE_SIZE = 20;

interface WebhookFormValues {
  url: string;
  mock: boolean;
}

/** Notifications tab of the agent detail page: webhook registrations
 * (multi-webhook), regenerate, retries and the paginated
 * notification log. Mounted lazily by AgentDetail's Tabs, so all fetching
 * happens on mount. */
export default function NotificationsPanel({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { token } = theme.useToken();

  // Webhooks
  const [webhooks, setWebhooks] = useState<AgentWebhook[]>([]);
  const [loadingWebhooks, setLoadingWebhooks] = useState(true);
  const [webhookModalOpen, setWebhookModalOpen] = useState(false);
  const [editingWebhook, setEditingWebhook] = useState<AgentWebhook | null>(null);
  const [savingWebhook, setSavingWebhook] = useState(false);
  const [webhookForm] = Form.useForm<WebhookFormValues>();

  // Regenerate
  const [regenerateModalOpen, setRegenerateModalOpen] = useState(false);
  const [regenerateSince, setRegenerateSince] = useState<Dayjs | null>(null);
  const [regenerating, setRegenerating] = useState(false);

  // Bulk retry
  const [bulkModalOpen, setBulkModalOpen] = useState(false);
  const [bulkMode, setBulkMode] = useState<RetryMode>('failed');
  const [bulkSince, setBulkSince] = useState<Dayjs | null>(null);
  const [bulkRetrying, setBulkRetrying] = useState(false);

  // Notification list. `loading` starts true: the mount effect fetches
  // immediately, and later fetches are triggered by bumping `reloadKey`
  // (event handlers), so no setState runs synchronously inside an effect.
  const [notifications, setNotifications] = useState<DownloadNotification[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);

  // Detail drawer
  const [detail, setDetail] = useState<DownloadNotificationDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  // Per-row retry in-flight flag
  const [retryingId, setRetryingId] = useState<string | null>(null);

  const reloadNotifications = useCallback(() => {
    setLoading(true);
    setReloadKey((k) => k + 1);
  }, []);

  const loadWebhooks = useCallback(async () => {
    setLoadingWebhooks(true);
    try {
      const r = await notificationsApi.listWebhooks(agentId);
      if (r.success) setWebhooks(r.data);
    } finally {
      setLoadingWebhooks(false);
    }
  }, [agentId]);

  // Mount fetch only — `loadingWebhooks` already starts true, so the effect
  // itself runs no synchronous setState; event handlers use `loadWebhooks`.
  useEffect(() => {
    let cancelled = false;
    notificationsApi
      .listWebhooks(agentId)
      .then((r) => {
        if (!cancelled && r.success) setWebhooks(r.data);
      })
      .finally(() => {
        if (!cancelled) setLoadingWebhooks(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  useEffect(() => {
    let cancelled = false;
    notificationsApi
      .listByAgent(agentId, page, PAGE_SIZE)
      .then((r) => {
        if (cancelled) return;
        if (r.success) {
          setNotifications(r.data);
          if (r.meta) setTotal(r.meta.total);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, page, reloadKey]);

  // Poll while any row is still pending: deliveries are driven by the
  // per-minute scheduler tick, so a fresh notification can show "pending"
  // for up to ~1 minute. Silent — the fetch effect never flashes the
  // loading spinner on reloadKey bumps.
  const hasPending = notifications.some((n) => n.status === 'pending');
  usePolling(() => setReloadKey((k) => k + 1), 10000, hasPending);

  const openWebhookModal = (webhook: AgentWebhook | null) => {
    setEditingWebhook(webhook);
    webhookForm.setFieldsValue({
      url: webhook?.url ?? '',
      mock: webhook?.mock ?? false,
    });
    setWebhookModalOpen(true);
  };

  const handleSaveWebhook = async () => {
    const values = await webhookForm.validateFields();
    setSavingWebhook(true);
    const r = editingWebhook
      ? await notificationsApi.updateWebhook(agentId, editingWebhook.id, {
          url: values.url,
          mock: values.mock,
        })
      : await notificationsApi.createWebhook(agentId, {
          url: values.url,
          mock: values.mock,
        });
    setSavingWebhook(false);
    if (r.success) {
      message.success(t('agents.webhookSaved'));
      setWebhookModalOpen(false);
      await loadWebhooks();
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };

  const handleToggleWebhook = async (webhook: AgentWebhook, enabled: boolean) => {
    const r = await notificationsApi.updateWebhook(agentId, webhook.id, { enabled });
    if (r.success) {
      await loadWebhooks();
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };

  const handleDeleteWebhook = async (webhook: AgentWebhook) => {
    const r = await notificationsApi.deleteWebhook(agentId, webhook.id);
    if (r.success) {
      message.success(t('agents.webhookDeleted'));
      await loadWebhooks();
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };

  const handleRegenerate = async () => {
    setRegenerating(true);
    const r = await notificationsApi.regenerate(
      agentId,
      regenerateSince ? regenerateSince.toISOString() : null,
    );
    setRegenerating(false);
    if (r.success) {
      message.success(
        t('agents.notifRegenerated', {
          created: r.data.created,
          regenerated: r.data.regenerated,
        }),
      );
      setRegenerateModalOpen(false);
      setRegenerateSince(null);
      setPage(1);
      reloadNotifications();
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };

  const handleBulkRetry = async () => {
    setBulkRetrying(true);
    const r = await notificationsApi.retryBulk({
      mode: bulkMode,
      agent_id: agentId,
      ...(bulkSince ? { since: bulkSince.toISOString() } : {}),
    });
    setBulkRetrying(false);
    if (r.success) {
      message.success(t('agents.notifBulkRetried', { n: r.data.reset }));
      setBulkModalOpen(false);
      setBulkSince(null);
      reloadNotifications();
    } else {
      message.error(r.error?.message || t('agents.notifRetryFailed'));
    }
  };

  const handleOpenDetail = async (id: string) => {
    setLoadingDetail(true);
    const r = await notificationsApi.get(id);
    setLoadingDetail(false);
    if (r.success) {
      setDetail(r.data);
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };

  const handleRetry = async (record: DownloadNotification, mode: RetryMode) => {
    setRetryingId(record.id);
    const r = await notificationsApi.retry(record.id, mode);
    setRetryingId(null);
    if (r.success) {
      message.success(t('agents.notifBulkRetried', { n: r.data.reset }));
      reloadNotifications();
    } else {
      message.error(r.error?.message || t('agents.notifRetryFailed'));
    }
  };

  const webhookColumns: TableColumnsType<AgentWebhook> = [
    {
      title: t('agents.webhookUrlLabel'),
      dataIndex: 'url',
      key: 'url',
      render: (v: string) => <EllipsisText text={v} />,
    },
    {
      title: t('agents.webhookMockLabel'),
      dataIndex: 'mock',
      key: 'mock',
      width: 110,
      render: (v: boolean) =>
        v ? (
          <Tag color="purple" style={{ margin: 0 }}>{t('agents.webhookMockTag')}</Tag>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: t('agents.webhookEnabled'),
      dataIndex: 'enabled',
      key: 'enabled',
      width: 100,
      render: (v: boolean, record) => (
        <Switch
          size="small"
          checked={v}
          onChange={(checked) => handleToggleWebhook(record, checked)}
        />
      ),
    },
    {
      title: t('agents.notifColCreatedAt'),
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
      width: 100,
      align: 'right',
      render: (_, record) => (
        <Space size={0}>
          <Button type="text" size="small" onClick={() => openWebhookModal(record)}>
            {t('common.edit')}
          </Button>
          <Popconfirm
            title={t('agents.webhookDeleteConfirm')}
            okText={t('common.confirm')}
            cancelText={t('common.cancel')}
            okButtonProps={{ danger: true }}
            onConfirm={() => handleDeleteWebhook(record)}
          >
            <Button type="text" size="small" danger icon={<Trash2 size={14} />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const columns: TableColumnsType<DownloadNotification> = [
    {
      title: t('agents.notifColCreatedAt'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 160,
      render: (v: string) => (
        <Text type="secondary" style={{ fontSize: 12 }}>{timeAgo(v)}</Text>
      ),
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status: string) => <StatusBadge status={status} />,
    },
    {
      title: t('agents.notifColDeliveries'),
      dataIndex: 'delivery_summary',
      key: 'delivery_summary',
      width: 130,
      render: (s: DownloadNotification['delivery_summary']) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t('agents.notifDeliverySummary', { done: s.done, total: s.total })}
        </Text>
      ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 150,
      align: 'right',
      render: (_, record) => (
        <Space size={0}>
          <Button
            type="text"
            size="small"
            icon={<Eye size={14} />}
            onClick={() => handleOpenDetail(record.id)}
          >
            {t('agents.notifDetail')}
          </Button>
          <Dropdown
            menu={{
              items: [
                { key: 'failed', label: t('agents.notifRetryFailedOnly') },
                { key: 'all', label: t('agents.notifRetryAll') },
              ],
              onClick: ({ key }) => handleRetry(record, key as RetryMode),
            }}
            trigger={['click']}
          >
            <Button
              type="text"
              size="small"
              icon={<RotateCcw size={14} />}
              disabled={record.status === 'done'}
              loading={retryingId === record.id}
            >
              {t('common.retry')}
            </Button>
          </Dropdown>
        </Space>
      ),
    },
  ];

  const deliveryColumns: TableColumnsType<WebhookDelivery> = [
    {
      title: t('agents.webhookUrlLabel'),
      dataIndex: 'webhook_url',
      key: 'webhook_url',
      width: 130,
      render: (v: string) => <EllipsisText text={v} />,
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 80,
      render: (status: string) => <StatusBadge status={status} />,
    },
    {
      title: t('agents.notifColAttempts'),
      dataIndex: 'attempt_count',
      key: 'attempt_count',
      width: 60,
      render: (v: number) => <Text type="secondary" style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      // Flex column (no width): with tableLayout="fixed" it takes whatever
      // the fixed columns leave, and EllipsisText truncates instead of
      // stretching the table past the Drawer width.
      title: t('agents.notifColError'),
      dataIndex: 'error_message',
      key: 'error_message',
      render: (v: string | null) =>
        v ? <EllipsisText text={v} danger /> : <Text type="secondary">—</Text>,
    },
    {
      title: t('agents.notifColDeliveredAt'),
      dataIndex: 'delivered_at',
      key: 'delivered_at',
      width: 110,
      render: (v: string | null) =>
        v ? (
          <Text type="secondary" style={{ fontSize: 12 }}>{timeAgo(v)}</Text>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: t('agents.notifColNextAttempt'),
      dataIndex: 'next_attempt_at',
      key: 'next_attempt_at',
      width: 110,
      render: (v: string | null) =>
        v ? (
          <Text type="secondary" style={{ fontSize: 12 }}>{timeAgo(v)}</Text>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
  ];

  return (
    <div>
      <Card
        size="small"
        style={{ marginBottom: 16 }}
        title={<Text strong>{t('agents.webhookTitle')}</Text>}
        extra={
          <Button size="small" icon={<Plus size={14} />} onClick={() => openWebhookModal(null)}>
            {t('agents.webhookAdd')}
          </Button>
        }
      >
        <Table<AgentWebhook>
          className="stack-table"
          columns={withMobileLabels(webhookColumns)}
          dataSource={webhooks}
          rowKey="id"
          loading={loadingWebhooks}
          size="small"
          pagination={false}
          locale={{ emptyText: <Empty description={t('agents.webhookListEmpty')} /> }}
        />
      </Card>

      <Card>
        <Space style={{ marginBottom: 12 }} wrap>
          <Button
            size="small"
            icon={<RotateCcw size={14} />}
            onClick={() => setBulkModalOpen(true)}
          >
            {t('agents.notifBulkRetry')}
          </Button>
          <Button
            size="small"
            icon={<History size={14} />}
            onClick={() => setRegenerateModalOpen(true)}
          >
            {t('agents.notifRegenerate')}
          </Button>
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
            onClick={reloadNotifications}
          >
            {t('common.refresh')}
          </Button>
        </Space>
        <Table<DownloadNotification>
          className="stack-table"
          columns={withMobileLabels(columns)}
          dataSource={notifications}
          rowKey="id"
          loading={loading}
          size="small"
          scroll={{ x: 720 }}
          pagination={{
            current: page,
            pageSize: PAGE_SIZE,
            total,
            onChange: (p) => {
              setLoading(true);
              setPage(p);
            },
            showSizeChanger: false,
          }}
          locale={{ emptyText: <Empty description={t('agents.notifEmpty')} /> }}
        />
      </Card>

      <Modal
        title={editingWebhook ? t('agents.webhookEditTitle') : t('agents.webhookAddTitle')}
        open={webhookModalOpen}
        onOk={handleSaveWebhook}
        onCancel={() => setWebhookModalOpen(false)}
        okText={t('common.save')}
        cancelText={t('common.cancel')}
        confirmLoading={savingWebhook}
        destroyOnHidden
      >
        <Form form={webhookForm} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item noStyle shouldUpdate>
            {({ getFieldValue }) => {
              const mock = !!getFieldValue('mock');
              return (
                <Form.Item
                  name="url"
                  label={t('agents.webhookUrlLabel')}
                  rules={
                    mock
                      ? []
                      : [
                          { required: true, message: t('agents.webhookUrlRequired') },
                          {
                            pattern: /^https?:\/\/.+/i,
                            message: t('agents.webhookUrlInvalid'),
                          },
                        ]
                  }
                >
                  <Input disabled={mock} placeholder="https://example.com/webhook" />
                </Form.Item>
              );
            }}
          </Form.Item>
          <Form.Item name="mock" valuePropName="checked" style={{ marginBottom: 0 }}>
            <Checkbox
              onChange={(e) => {
                // Mock mode disables the URL input; drop any stale validation
                // errors so the form is submittable right away.
                if (e.target.checked) {
                  webhookForm.setFields([{ name: 'url', errors: [] }]);
                }
              }}
            >
              {t('agents.webhookMockLabel')}
            </Checkbox>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('agents.notifBulkRetryTitle')}
        open={bulkModalOpen}
        onOk={handleBulkRetry}
        onCancel={() => setBulkModalOpen(false)}
        okText={t('common.confirm')}
        cancelText={t('common.cancel')}
        confirmLoading={bulkRetrying}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: '100%', marginTop: 12 }}>
          <Radio.Group
            value={bulkMode}
            onChange={(e) => setBulkMode(e.target.value as RetryMode)}
            options={[
              { value: 'failed', label: t('agents.notifRetryFailedOnly') },
              { value: 'all', label: t('agents.notifRetryAll') },
            ]}
          />
          <DatePicker
            showTime
            allowClear
            value={bulkSince}
            onChange={(v) => setBulkSince(v)}
            placeholder={t('agents.notifBulkRetrySince')}
            style={{ width: '100%' }}
          />
        </Space>
      </Modal>

      <Modal
        title={t('agents.notifRegenerateTitle')}
        open={regenerateModalOpen}
        onOk={handleRegenerate}
        onCancel={() => setRegenerateModalOpen(false)}
        okText={t('common.confirm')}
        cancelText={t('common.cancel')}
        confirmLoading={regenerating}
        destroyOnHidden
      >
        <DatePicker
          showTime
          allowClear
          value={regenerateSince}
          onChange={(v) => setRegenerateSince(v)}
          placeholder={t('agents.notifRegeneratePlaceholder')}
          style={{ width: '100%', marginTop: 12 }}
        />
      </Modal>

      <Drawer
        open={!!detail || loadingDetail}
        onClose={() => setDetail(null)}
        title={t('agents.notifDetailTitle')}
        width={window.innerWidth < 768 ? '100%' : 680}
        destroyOnClose
        loading={loadingDetail}
      >
        {detail && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Space size={12} wrap>
              <StatusBadge status={detail.status} />
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t('agents.notifDeliverySummary', {
                  done: detail.delivery_summary.done,
                  total: detail.delivery_summary.total,
                })}
              </Text>
            </Space>
            <Text strong style={{ fontSize: 13 }}>{t('agents.notifDeliveriesTitle')}</Text>
            <Table<WebhookDelivery>
              columns={deliveryColumns}
              dataSource={detail.deliveries}
              rowKey="id"
              size="small"
              pagination={false}
              tableLayout="fixed"
              scroll={{ x: 600 }}
            />
            <Text strong style={{ fontSize: 13 }}>{t('agents.notifPayloadTitle')}</Text>
            <pre
              style={{
                margin: 0,
                padding: 12,
                borderRadius: 8,
                background: token.colorFillQuaternary,
                color: token.colorText,
                fontSize: 12,
                maxHeight: '40vh',
                overflow: 'auto',
                wordBreak: 'break-all',
                whiteSpace: 'pre-wrap',
              }}
            >
              {JSON.stringify(detail.payload, null, 2)}
            </pre>
          </Space>
        )}
      </Drawer>
    </div>
  );
}
