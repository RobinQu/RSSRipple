import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import {
  App,
  Button,
  Descriptions,
  Drawer,
  Input,
  Select,
  Space,
  Tag,
  Timeline,
  Typography,
} from 'antd';
import { organizeApi } from '../api/organize';
import StatusBadge from './StatusBadge';
import OrganizeOpPaths from './OrganizeOpPaths';
import ResourceCorrectionModal from './ResourceCorrectionModal';
import { confirmCancelPlan } from './cancelPlanConfirm';
import { formatBytes, formatDate, timeAgo } from '../utils/format';
import type { Library, OrganizePlanDetail } from '../types';

const { Text } = Typography;

// The frozen notification snapshot is loosely typed (extra keys allowed);
// pull the display fields out defensively.
function payloadWorkLinks(payload: Record<string, unknown>) {
  const work = payload.work as Record<string, unknown> | undefined;
  const works = payload.works as Record<string, Record<string, unknown>> | undefined;
  const source = works && Object.keys(works).length > 0
    ? Object.entries(works)
    : work ? [['legacy', work] as const] : [];
  return source.flatMap(([key, item]) => {
    const title = (item.title_cn || item.title_en || item.original_title) as string | undefined;
    const seriesId = item.series_id as string | undefined;
    const movieId = item.movie_id as string | undefined;
    if (!title) return [];
    return [{
      key,
      title,
      href: seriesId ? `/series/${seriesId}` : movieId ? `/movies/${movieId}` : null,
    }];
  });
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
  const [associationEditorOpen, setAssociationEditorOpen] = useState(false);

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

  useEffect(() => {
    if (!detail) return;
    setClassifyLibraryId(detail.library_id ?? undefined);
    const work = detail.payload.work as { genre?: string[] | null } | undefined;
    setClassifyCategory(detail.category ?? work?.genre?.[0] ?? '');
  }, [detail]);

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

  // Stale `running` plans (crash leftovers) are replayable/cancellable too —
  // the backend guards against plans this process is actively executing.
  const actionable =
    detail?.status === 'pending' ||
    detail?.status === 'failed' ||
    detail?.status === 'running';
  // classify 端点仍只接受 pending/failed（running 需先重放或取消）。
  const classifiable = detail?.status === 'pending' || detail?.status === 'failed';

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
    confirmCancelPlan({
      modal,
      message,
      t,
      planId: detail.id,
      onDone: () => {
        fetchDetail();
        onChanged();
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

  const filesCount = detail ? payloadFilesCount(detail.payload) : null;
  const workLinks = detail ? payloadWorkLinks(detail.payload) : [];

  return (
    <Drawer
      open={planId !== null}
      onClose={onClose}
      width={760}
      title={t('organize.detail')}
      loading={loading && !detail}
      footer={actionable && detail ? (
        <Space size={8} style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <Button danger onClick={handleCancel} disabled={acting}>
            {t('organize.cancelAssociation')}
          </Button>
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
        </Space>
      ) : null}
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
              {workLinks.length > 0 ? (
                <Space size={[8, 4]} wrap>
                  {workLinks.map((item) => (
                    item.href
                      ? <Link key={item.key} to={item.href}>{item.title}</Link>
                      : <Text key={item.key}>{item.title}</Text>
                  ))}
                </Space>
              ) : t('common.unknown')}
            </Descriptions.Item>
            <Descriptions.Item label={t('organize.resource')}>
              {payloadResourceTitle(detail.payload) ?? t('format.dash')}
            </Descriptions.Item>
            <Descriptions.Item label={t('organize.filesCount')}>
              {filesCount ?? t('format.dash')}
            </Descriptions.Item>
          </Descriptions>

          {classifiable && detail.resource_id && (
            <Button onClick={() => setAssociationEditorOpen(true)}>
              {t('organize.editFileAssociations')}
            </Button>
          )}

          {classifiable && (
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
            <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 8 }}>
              {detail.ops.map((op) => (
                <div
                  key={op.id}
                  style={{
                    border: '1px solid var(--rr-border-soft)',
                    borderRadius: 8,
                    padding: '8px 12px',
                  }}
                >
                  <Space size={8} wrap style={{ marginBottom: 4 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>#{op.seq}</Text>
                    {opTypeTag(op.op_type, t)}
                    <Text type="secondary" style={{ fontSize: 12 }}>{formatBytes(op.size)}</Text>
                    {opStatusBadge(op.status, t)}
                  </Space>
                  <OrganizeOpPaths
                    src={op.src}
                    dst={op.dst}
                    srcRelocated={
                      detail.status === 'done' &&
                      (op.op_type === 'move' || op.op_type === 'movedir')
                    }
                  />
                  {op.error_message && (
                    <Text type="danger" style={{ fontSize: 12 }}>{op.error_message}</Text>
                  )}
                </div>
              ))}
            </div>
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
      <ResourceCorrectionModal
        resourceId={detail?.resource_id ?? null}
        open={associationEditorOpen}
        initialStep={1}
        onClose={() => setAssociationEditorOpen(false)}
        onSaved={() => {
          message.success(t('organize.associationsRefreshQueued'));
          setAssociationEditorOpen(false);
          refreshLater();
        }}
      />
    </Drawer>
  );
}
