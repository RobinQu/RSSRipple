import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import {
  App,
  Button,
  Card,
  Checkbox,
  DatePicker,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { TableColumnsType } from 'antd';
import type { Dayjs } from 'dayjs';
import { Eye, RefreshCw, RotateCcw } from 'lucide-react';
import { notificationsApi } from '../api/notifications';
import StatusBadge from './StatusBadge';
import EllipsisText from './EllipsisText';
import { timeAgo } from '../utils/format';
import { withMobileLabels } from '../utils/table';
import type {
  AgentWebhook,
  DownloadNotification,
  DownloadNotificationDetail,
} from '../types';

const { Text } = Typography;

const PAGE_SIZE = 20;

interface WebhookFormValues {
  url: string | null;
  mock: boolean;
}

/** Notifications tab of the agent detail page: webhook registration,
 * backfill ("regenerate") and the paginated notification log. Mounted lazily
 * by AgentDetail's Tabs, so all fetching happens on mount. */
export default function NotificationsPanel({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const { message } = App.useApp();

  // Webhook
  const [webhook, setWebhook] = useState<AgentWebhook | null>(null);
  const [webhookModalOpen, setWebhookModalOpen] = useState(false);
  const [savingWebhook, setSavingWebhook] = useState(false);
  const [webhookForm] = Form.useForm<WebhookFormValues>();

  // Backfill ("regenerate")
  const [backfillModalOpen, setBackfillModalOpen] = useState(false);
  const [backfillSince, setBackfillSince] = useState<Dayjs | null>(null);
  const [backfilling, setBackfilling] = useState(false);

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

  useEffect(() => {
    let cancelled = false;
    notificationsApi.getWebhook(agentId).then((r) => {
      if (!cancelled && r.success) setWebhook(r.data);
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

  const openWebhookModal = () => {
    webhookForm.setFieldsValue({
      url: webhook?.url ?? null,
      mock: webhook?.mock ?? false,
    });
    setWebhookModalOpen(true);
  };

  const handleSaveWebhook = async () => {
    const values = await webhookForm.validateFields();
    setSavingWebhook(true);
    const r = await notificationsApi.putWebhook(agentId, {
      mock: values.mock,
      url: values.mock ? null : values.url,
    });
    setSavingWebhook(false);
    if (r.success) {
      message.success(t('agents.webhookSaved'));
      setWebhookModalOpen(false);
      setWebhook(r.data);
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };

  const handleUnregisterWebhook = async () => {
    const r = await notificationsApi.deleteWebhook(agentId);
    if (r.success) {
      message.success(t('agents.webhookUnregistered'));
      setWebhook({ registered: false, url: null, mock: false, token: null });
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };

  const handleBackfill = async () => {
    setBackfilling(true);
    const r = await notificationsApi.backfill(
      agentId,
      backfillSince ? backfillSince.toISOString() : null,
    );
    setBackfilling(false);
    if (r.success) {
      message.success(t('agents.notifBackfilled', { n: r.data.created }));
      setBackfillModalOpen(false);
      setBackfillSince(null);
      setPage(1);
      reloadNotifications();
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
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

  const handleRetry = async (record: DownloadNotification) => {
    setRetryingId(record.id);
    const r = await notificationsApi.retry(record.id);
    setRetryingId(null);
    if (r.success) {
      message.success(t('agents.notifRetried'));
      reloadNotifications();
    } else {
      message.error(r.error?.message || t('agents.notifRetryFailed'));
    }
  };

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
      title: t('agents.notifColNotifiedAt'),
      dataIndex: 'notified_at',
      key: 'notified_at',
      width: 160,
      render: (v: string | null) =>
        v ? (
          <Text type="secondary" style={{ fontSize: 12 }}>{timeAgo(v)}</Text>
        ) : (
          <Text type="secondary">—</Text>
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
      title: t('agents.notifColError'),
      dataIndex: 'error_message',
      key: 'error_message',
      render: (v: string | null) =>
        v ? <EllipsisText text={v} danger /> : <Text type="secondary">—</Text>,
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
          <Button
            type="text"
            size="small"
            icon={<RotateCcw size={14} />}
            disabled={record.status === 'done'}
            loading={retryingId === record.id}
            onClick={() => handleRetry(record)}
          >
            {t('common.retry')}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Card size="small" style={{ marginBottom: 16 }}>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            flexWrap: 'wrap',
            gap: 12,
          }}
        >
          <Space size={8} wrap>
            <Text strong>{t('agents.webhookTitle')}</Text>
            {webhook?.registered ? (
              webhook.mock ? (
                <Tag color="purple" style={{ margin: 0 }}>{t('agents.webhookMockTag')}</Tag>
              ) : (
                <Text code style={{ fontSize: 12 }}>{webhook.url}</Text>
              )
            ) : (
              <Text type="secondary">{t('agents.webhookNotRegistered')}</Text>
            )}
          </Space>
          <Space size={8}>
            <Button size="small" onClick={openWebhookModal}>
              {webhook?.registered ? t('common.edit') : t('agents.webhookRegister')}
            </Button>
            {webhook?.registered && (
              <Popconfirm
                title={t('agents.webhookUnregisterConfirm')}
                okText={t('common.confirm')}
                cancelText={t('common.cancel')}
                okButtonProps={{ danger: true }}
                onConfirm={handleUnregisterWebhook}
              >
                <Button size="small" danger>
                  {t('agents.webhookUnregister')}
                </Button>
              </Popconfirm>
            )}
          </Space>
        </div>
        {webhook?.registered && webhook.token && (
          <div style={{ marginTop: 8 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t('agents.webhookTokenLabel')}
            </Text>
            <Text code copyable={{ text: webhook.token }} style={{ fontSize: 12 }}>
              {webhook.token}
            </Text>
          </div>
        )}
      </Card>

      <Card>
        <Space style={{ marginBottom: 12 }}>
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
            onClick={() => setBackfillModalOpen(true)}
          >
            {t('agents.notifRegenerate')}
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
        title={webhook?.registered ? t('agents.webhookEditTitle') : t('agents.webhookModalTitle')}
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
        title={t('agents.notifRegenerateTitle')}
        open={backfillModalOpen}
        onOk={handleBackfill}
        onCancel={() => setBackfillModalOpen(false)}
        okText={t('common.confirm')}
        cancelText={t('common.cancel')}
        confirmLoading={backfilling}
        destroyOnHidden
      >
        <DatePicker
          showTime
          allowClear
          value={backfillSince}
          onChange={(v) => setBackfillSince(v)}
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
                {t('agents.notifAttempts', { n: detail.attempt_count })}
              </Text>
              {detail.error_message && (
                <Text type="danger" style={{ fontSize: 12 }}>{detail.error_message}</Text>
              )}
            </Space>
            <pre
              style={{
                margin: 0,
                padding: 12,
                borderRadius: 8,
                background: '#f5f5f5',
                fontSize: 12,
                maxHeight: '70vh',
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
