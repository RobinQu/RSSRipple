import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  App,
  Button,
  Descriptions,
  Drawer,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Timeline,
  Typography,
} from 'antd';
import type { TableColumnsType } from 'antd';
import { organizeApi } from '../api/organize';
import StatusBadge from './StatusBadge';
import { formatBytes, formatDate, timeAgo } from '../utils/format';
import { withMobileLabels } from '../utils/table';
import type { Library, OrganizePlanDetail, OrganizePlanOp } from '../types';

const { Text } = Typography;

// The frozen notification snapshot is loosely typed (extra keys allowed);
// pull the display fields out defensively.
function payloadWorkTitle(payload: Record<string, unknown>): string | null {
  const work = payload.work as Record<string, unknown> | undefined;
  if (!work) return null;
  return (
    (work.title_cn as string) ||
    (work.title_en as string) ||
    (work.original_title as string) ||
    null
  );
}

function payloadResourceTitle(payload: Record<string, unknown>): string | null {
  const resource = payload.resource as Record<string, unknown> | undefined;
  return (resource?.title_raw as string) || null;
}

function payloadFilesCount(payload: Record<string, unknown>): number | null {
  return Array.isArray(payload.files) ? payload.files.length : null;
}

function opTypeTag(opType: string, t: (k: string) => string) {
  if (opType === 'move') return <Tag color="green">{t('organize.opMove')}</Tag>;
  if (opType === 'movedir') return <Tag color="blue">{t('organize.opMovedir')}</Tag>;
  return <Tag>{t('organize.opKeep')}</Tag>;
}

function opStatusBadge(status: string, t: (k: string) => string) {
  if (status === 'kept') return <Tag>{t('organize.opStatusKept')}</Tag>;
  return <StatusBadge status={status} />;
}

/** Plan detail drawer: ops list, payload summary, audit timeline, actions. */
export default function OrganizePlanDrawer({
  planId,
  libraries,
  onClose,
  onChanged,
}: {
  planId: string | null;
  libraries: Library[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const { message, modal } = App.useApp();
  const [detail, setDetail] = useState<OrganizePlanDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState(false);
  const [classifyLibraryId, setClassifyLibraryId] = useState<string | undefined>();
  const [classifyCategory, setClassifyCategory] = useState('');

  const fetchDetail = useCallback(async () => {
    if (!planId) return;
    setLoading(true);
    const res = await organizeApi.getPlan(planId);
    if (res.success) setDetail(res.data);
    setLoading(false);
  }, [planId]);

  useEffect(() => {
    setDetail(null);
    setClassifyLibraryId(undefined);
    setClassifyCategory('');
    fetchDetail();
  }, [fetchDetail]);

  // Execution runs in the background (202): re-poll shortly after triggering
  // so the drawer/list converge to the final status without a manual refresh.
  const refreshLater = useCallback(() => {
    window.setTimeout(() => {
      fetchDetail();
      onChanged();
    }, 2000);
    window.setTimeout(() => {
      fetchDetail();
      onChanged();
    }, 6000);
  }, [fetchDetail, onChanged]);

  const actionable = detail?.status === 'pending' || detail?.status === 'failed';

  const handleExecute = async () => {
    if (!detail) return;
    setActing(true);
    const r = await organizeApi.executePlan(detail.id);
    setActing(false);
    if (r.success) {
      message.success(t('organize.executed'));
      fetchDetail();
      onChanged();
      refreshLater();
    } else {
      message.error(r.error?.message || t('organize.executeFailed'));
    }
  };

  const handleCancel = () => {
    if (!detail) return;
    modal.confirm({
      title: t('organize.cancelConfirm'),
      okText: t('common.confirm'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await organizeApi.cancelPlan(detail.id);
        if (r.success) {
          message.success(t('organize.cancelled'));
          fetchDetail();
          onChanged();
        } else {
          message.error(r.error?.message || t('organize.cancelFailed'));
        }
      },
    });
  };

  const handleClassify = async () => {
    if (!detail || !classifyLibraryId) return;
    setActing(true);
    const r = await organizeApi.classifyPlan(detail.id, {
      library_id: classifyLibraryId,
      category: classifyCategory.trim() || null,
    });
    setActing(false);
    if (r.success) {
      message.success(t('organize.classified'));
      fetchDetail();
      onChanged();
    } else {
      message.error(r.error?.message || t('organize.classifyFailed'));
    }
  };

  const opColumns: TableColumnsType<OrganizePlanOp> = [
    { title: '#', dataIndex: 'seq', key: 'seq', width: 48 },
    {
      title: t('organize.opType'),
      dataIndex: 'op_type',
      key: 'op_type',
      width: 90,
      render: (v: string) => opTypeTag(v, t),
    },
    {
      title: t('organize.src'),
      dataIndex: 'src',
      key: 'src',
      render: (v: string) => (
        <Text ellipsis={{ tooltip: v }} style={{ maxWidth: 260 }}>{v}</Text>
      ),
    },
    {
      title: t('organize.dst'),
      dataIndex: 'dst',
      key: 'dst',
      render: (v: string | null) =>
        v ? <Text ellipsis={{ tooltip: v }} style={{ maxWidth: 260 }}>{v}</Text> : t('format.dash'),
    },
    {
      title: t('organize.size'),
      dataIndex: 'size',
      key: 'size',
      width: 90,
      render: (v: number) => formatBytes(v),
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (v: string) => opStatusBadge(v, t),
    },
    {
      title: t('common.error'),
      dataIndex: 'error_message',
      key: 'error_message',
      render: (v: string | null) =>
        v ? (
          <Text type="danger" ellipsis={{ tooltip: v }} style={{ maxWidth: 200 }}>{v}</Text>
        ) : (
          t('format.dash')
        ),
    },
  ];

  const filesCount = detail ? payloadFilesCount(detail.payload) : null;

  return (
    <Drawer
      open={planId !== null}
      onClose={onClose}
      width={760}
      title={t('organize.detail')}
      loading={loading && !detail}
    >
      {detail && (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Descriptions size="small" column={2} bordered>
            <Descriptions.Item label={t('common.status')}>
              <Space size={8}>
                <StatusBadge status={detail.status} />
                {detail.pending_reason === 'unclassified' && (
                  <Tag color="orange">{t('organize.uncategorizedTag')}</Tag>
                )}
                {detail.pending_reason === 'unbound' && (
                  <Tag color="volcano">{t('organize.unboundTag')}</Tag>
                )}
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label={t('organize.rule')}>
              {detail.rule_name ?? t('format.dash')}
            </Descriptions.Item>
            <Descriptions.Item label={t('organize.library')}>
              {detail.library_name ?? t('format.dash')}
            </Descriptions.Item>
            <Descriptions.Item label={t('organize.category')}>
              {detail.category ?? t('format.dash')}
            </Descriptions.Item>
            <Descriptions.Item label={t('organize.createdAt')}>
              {formatDate(detail.created_at)}
            </Descriptions.Item>
            <Descriptions.Item label={t('organize.executedAt')}>
              {detail.executed_at ? formatDate(detail.executed_at) : t('common.never')}
            </Descriptions.Item>
            {detail.error_message && (
              <Descriptions.Item label={t('common.error')} span={2}>
                <Text type="danger">{detail.error_message}</Text>
              </Descriptions.Item>
            )}
          </Descriptions>

          <Descriptions size="small" column={1} bordered title={t('organize.payload')}>
            <Descriptions.Item label={t('organize.work')}>
              {payloadWorkTitle(detail.payload) ?? t('common.unknown')}
            </Descriptions.Item>
            <Descriptions.Item label={t('organize.resource')}>
              {payloadResourceTitle(detail.payload) ?? t('format.dash')}
            </Descriptions.Item>
            <Descriptions.Item label={t('organize.filesCount')}>
              {filesCount ?? t('format.dash')}
            </Descriptions.Item>
          </Descriptions>

          {actionable && (
            <Space size={8} wrap>
              <Button
                type="primary"
                onClick={handleExecute}
                loading={acting}
                disabled={detail.library_id === null || detail.pending_reason === 'unbound'}
                title={
                  detail.library_id === null
                    ? t('organize.executeNeedsClassify')
                    : detail.pending_reason === 'unbound'
                      ? t('organize.executeNeedsBinding')
                      : undefined
                }
              >
                {t('organize.execute')}
              </Button>
              <Button danger onClick={handleCancel}>
                {t('organize.cancelPlan')}
              </Button>
            </Space>
          )}

          {actionable && (
            <div>
              <Text strong>{t('organize.classify')}</Text>
              <Space size={8} style={{ display: 'flex', marginTop: 8, flexWrap: 'wrap' }}>
                <Select
                  style={{ minWidth: 220 }}
                  placeholder={t('organize.selectLibrary')}
                  value={classifyLibraryId ?? detail.library_id ?? undefined}
                  onChange={setClassifyLibraryId}
                  options={libraries.map((lib) => ({ value: lib.id, label: lib.name }))}
                />
                <Input
                  style={{ width: 180 }}
                  placeholder={t('organize.categoryOptional')}
                  value={classifyCategory}
                  onChange={(e) => setClassifyCategory(e.target.value)}
                />
                <Button onClick={handleClassify} loading={acting} disabled={!classifyLibraryId}>
                  {t('organize.classifySubmit')}
                </Button>
              </Space>
            </div>
          )}

          <div>
            <Text strong>{t('organize.ops')}</Text>
            <Table
              className="stack-table"
              style={{ marginTop: 8 }}
              size="small"
              columns={withMobileLabels(opColumns)}
              dataSource={detail.ops}
              rowKey="id"
              pagination={false}
            />
          </div>

          <div>
            <Text strong>{t('organize.audit')}</Text>
            <Timeline
              style={{ marginTop: 12 }}
              items={detail.audit_entries.map((entry) => ({
                key: entry.id,
                children: (
                  <div>
                    <Space size={8} wrap>
                      <Tag>{entry.action}</Tag>
                      <Text type="secondary">{timeAgo(entry.created_at)}</Text>
                    </Space>
                    {entry.detail && Object.keys(entry.detail).length > 0 && (
                      <div>
                        <Text code style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>
                          {JSON.stringify(entry.detail)}
                        </Text>
                      </div>
                    )}
                  </div>
                ),
              }))}
            />
          </div>
        </Space>
      )}
    </Drawer>
  );
}
