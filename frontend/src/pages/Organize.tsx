import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Eye, Play, RefreshCw, XCircle } from 'lucide-react';
import {
  App,
  Button,
  Empty,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd';
import type { TableColumnsType } from 'antd';
import useDocumentTitle from '../hooks/useDocumentTitle';
import { organizeApi } from '../api/organize';
import OrganizePlanDrawer from '../components/OrganizePlanDrawer';
import StatusBadge from '../components/StatusBadge';
import { formatDate, timeAgo } from '../utils/format';
import { withMobileLabels } from '../utils/table';
import type {
  Library,
  OrganizeAuditEntry,
  OrganizePlanListItem,
  OrganizePlanStatus,
} from '../types';

const { Title, Text } = Typography;

const STATUS_FILTERS: (OrganizePlanStatus | 'all')[] = [
  'all',
  'pending',
  'running',
  'done',
  'failed',
  'cancelled',
];

export default function Organize() {
  const { t } = useTranslation();
  useDocumentTitle(t('organize.title'));
  const { message, modal } = App.useApp();

  const [tab, setTab] = useState<'plans' | 'audit'>('plans');

  // ------------------------------------------------------------------ Plans
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [plans, setPlans] = useState<OrganizePlanListItem[]>([]);
  const [plansTotal, setPlansTotal] = useState(0);
  const [plansPage, setPlansPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<OrganizePlanStatus | 'all'>('all');
  const [libraryFilter, setLibraryFilter] = useState<string | undefined>();
  const [plansLoading, setPlansLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [batchRunning, setBatchRunning] = useState(false);
  const [drawerPlanId, setDrawerPlanId] = useState<string | null>(null);

  // ------------------------------------------------------------------ Audit
  const [audit, setAudit] = useState<OrganizeAuditEntry[]>([]);
  const [auditTotal, setAuditTotal] = useState(0);
  const [auditPage, setAuditPage] = useState(1);
  const [auditLoading, setAuditLoading] = useState(false);

  const fetchLibraries = useCallback(async () => {
    const res = await organizeApi.listLibraries();
    if (res.success) setLibraries(res.data);
  }, []);

  const fetchPlans = useCallback(async () => {
    setPlansLoading(true);
    const res = await organizeApi.listPlans(
      plansPage,
      20,
      statusFilter === 'all' ? undefined : statusFilter,
      libraryFilter,
    );
    if (res.success) {
      setPlans(res.data);
      if (res.meta) setPlansTotal(res.meta.total);
    }
    setPlansLoading(false);
  }, [plansPage, statusFilter, libraryFilter]);

  const fetchAudit = useCallback(async () => {
    setAuditLoading(true);
    const res = await organizeApi.listAudit(auditPage, 20);
    if (res.success) {
      setAudit(res.data);
      if (res.meta) setAuditTotal(res.meta.total);
    }
    setAuditLoading(false);
  }, [auditPage]);

  useEffect(() => {
    fetchLibraries();
  }, [fetchLibraries]);

  useEffect(() => {
    if (tab === 'plans') fetchPlans();
    else fetchAudit();
  }, [tab, fetchPlans, fetchAudit]);

  const isExecutable = (p: OrganizePlanListItem) =>
    (p.status === 'pending' || p.status === 'failed') &&
    p.library_id !== null &&
    p.pending_reason !== 'unbound';
  const isCancellable = (p: OrganizePlanListItem) =>
    p.status === 'pending' || p.status === 'failed';

  const handleExecute = async (id: string) => {
    const r = await organizeApi.executePlan(id);
    if (r.success) {
      message.success(t('organize.executed'));
      // Background execution (202) — re-poll so the row converges to its
      // final status without a manual refresh.
      window.setTimeout(fetchPlans, 2000);
      window.setTimeout(fetchPlans, 6000);
    } else {
      message.error(r.error?.message || t('organize.executeFailed'));
    }
    fetchPlans();
  };

  const handleBatchExecute = async () => {
    if (selectedIds.length === 0) return;
    setBatchRunning(true);
    const r = await organizeApi.executeBatch(selectedIds);
    setBatchRunning(false);
    if (r.success) {
      message.success(t('organize.batchSubmitted', { count: selectedIds.length }));
      setSelectedIds([]);
      window.setTimeout(fetchPlans, 2000);
      window.setTimeout(fetchPlans, 6000);
    } else {
      message.error(r.error?.message || t('organize.executeFailed'));
    }
    fetchPlans();
  };

  const handleCancel = (record: OrganizePlanListItem) => {
    modal.confirm({
      title: t('organize.cancelConfirm'),
      okText: t('common.confirm'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await organizeApi.cancelPlan(record.id);
        if (r.success) {
          message.success(t('organize.cancelled'));
          fetchPlans();
        } else {
          message.error(r.error?.message || t('organize.cancelFailed'));
        }
      },
    });
  };

  const opsSummaryText = (p: OrganizePlanListItem) => {
    const s = p.ops_summary;
    const parts: string[] = [];
    if (s.move) parts.push(`${t('organize.opMove')} ${s.move}`);
    if (s.keep) parts.push(`${t('organize.opKeep')} ${s.keep}`);
    if (s.movedir) parts.push(`${t('organize.opMovedir')} ${s.movedir}`);
    return parts.length ? parts.join(' · ') : t('format.dash');
  };

  const planColumns: TableColumnsType<OrganizePlanListItem> = [
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 150,
      render: (v: string, record) => (
        <Space size={6}>
          <StatusBadge status={v} />
          {record.pending_reason === 'unclassified' && (
            <Tag color="orange">{t('organize.uncategorizedTag')}</Tag>
          )}
          {record.pending_reason === 'unbound' && (
            <Tag color="volcano">{t('organize.unboundTag')}</Tag>
          )}
        </Space>
      ),
    },
    {
      title: t('organize.rule'),
      dataIndex: 'rule_name',
      key: 'rule_name',
      render: (v: string | null) => v ?? t('format.dash'),
    },
    {
      title: t('organize.library'),
      key: 'library',
      render: (_, record) =>
        record.library_name ??
        (record.library_id === null ? (
          <Text type="warning">{t('organize.uncategorizedTag')}</Text>
        ) : (
          t('format.dash')
        )),
    },
    {
      title: t('organize.category'),
      dataIndex: 'category',
      key: 'category',
      width: 110,
      render: (v: string | null) => v ?? t('format.dash'),
    },
    {
      title: t('organize.ops'),
      key: 'ops_summary',
      width: 200,
      render: (_, record) => opsSummaryText(record),
    },
    {
      title: t('common.error'),
      dataIndex: 'error_message',
      key: 'error_message',
      render: (v: string | null) =>
        v ? (
          <Text type="danger" ellipsis={{ tooltip: v }} style={{ maxWidth: 220 }}>{v}</Text>
        ) : (
          t('format.dash')
        ),
    },
    {
      title: t('organize.createdAt'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 140,
      render: (v: string) => timeAgo(v),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 140,
      align: 'right',
      render: (_, record) => (
        <Space size={4}>
          <Button
            type="text"
            size="small"
            icon={<Eye size={14} />}
            title={t('organize.detail')}
            onClick={() => setDrawerPlanId(record.id)}
          />
          {isExecutable(record) && (
            <Button
              type="text"
              size="small"
              icon={<Play size={14} />}
              title={t('organize.execute')}
              onClick={() => handleExecute(record.id)}
            />
          )}
          {record.pending_reason === 'unbound' && isCancellable(record) && (
            <Button
              type="text"
              size="small"
              icon={<Play size={14} />}
              disabled
              title={t('organize.executeNeedsBinding')}
            />
          )}
          {isCancellable(record) && (
            <Button
              type="text"
              size="small"
              danger
              icon={<XCircle size={14} />}
              title={t('organize.cancelPlan')}
              onClick={() => handleCancel(record)}
            />
          )}
        </Space>
      ),
    },
  ];

  const auditColumns: TableColumnsType<OrganizeAuditEntry> = [
    {
      title: t('organize.createdAt'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: (v: string) => formatDate(v),
    },
    {
      title: t('organize.plan'),
      dataIndex: 'plan_id',
      key: 'plan_id',
      width: 140,
      render: (v: string) => (
        <Button type="link" size="small" onClick={() => setDrawerPlanId(v)}>
          {v.slice(0, 8)}
        </Button>
      ),
    },
    {
      title: t('organize.action'),
      dataIndex: 'action',
      key: 'action',
      width: 160,
      render: (v: string) => <Tag>{v}</Tag>,
    },
    {
      title: t('organize.detailJson'),
      dataIndex: 'detail',
      key: 'detail',
      render: (v: Record<string, unknown> | null) =>
        v && Object.keys(v).length > 0 ? (
          <Text code ellipsis={{ tooltip: JSON.stringify(v) }} style={{ maxWidth: 480 }}>
            {JSON.stringify(v)}
          </Text>
        ) : (
          t('format.dash')
        ),
    },
  ];

  const plansTab = (
    <>
      <Space size={8} style={{ display: 'flex', marginBottom: 16, flexWrap: 'wrap' }}>
        <Tabs
          size="small"
          activeKey={statusFilter}
          onChange={(k) => {
            setStatusFilter(k as OrganizePlanStatus | 'all');
            setPlansPage(1);
            setSelectedIds([]);
          }}
          items={STATUS_FILTERS.map((s) => ({
            key: s,
            label: s === 'all' ? t('common.all') : t(`status.${s}`),
          }))}
        />
        <Select
          allowClear
          style={{ minWidth: 180 }}
          placeholder={t('organize.libraryFilter')}
          value={libraryFilter}
          onChange={(v) => {
            setLibraryFilter(v);
            setPlansPage(1);
          }}
          options={libraries.map((lib) => ({ value: lib.id, label: lib.name }))}
        />
        <Button icon={<RefreshCw size={14} />} onClick={fetchPlans}>
          {t('common.refresh')}
        </Button>
        <Button
          type="primary"
          disabled={selectedIds.length === 0}
          loading={batchRunning}
          onClick={handleBatchExecute}
        >
          {t('organize.executeBatch', { count: selectedIds.length })}
        </Button>
      </Space>
      <Table
        className="stack-table"
        columns={withMobileLabels(planColumns)}
        dataSource={plans}
        rowKey="id"
        loading={plansLoading}
        locale={{ emptyText: <Empty description={t('organize.noPlans')} /> }}
        rowSelection={{
          selectedRowKeys: selectedIds,
          onChange: (keys) => setSelectedIds(keys as string[]),
          getCheckboxProps: (record) => ({ disabled: !isExecutable(record) }),
        }}
        pagination={{
          current: plansPage,
          pageSize: 20,
          total: plansTotal,
          onChange: setPlansPage,
          showSizeChanger: false,
        }}
      />
    </>
  );

  const auditTab = (
    <Table
      className="stack-table"
      columns={withMobileLabels(auditColumns)}
      dataSource={audit}
      rowKey="id"
      loading={auditLoading}
      locale={{ emptyText: <Empty description={t('organize.noAudit')} /> }}
      pagination={{
        current: auditPage,
        pageSize: 20,
        total: auditTotal,
        onChange: setAuditPage,
        showSizeChanger: false,
      }}
    />
  );

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8, marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>
          {t('organize.title')}
        </Title>
      </div>

      <Tabs
        activeKey={tab}
        onChange={(k) => setTab(k as 'plans' | 'audit')}
        items={[
          { key: 'plans', label: t('organize.plansTab'), children: plansTab },
          { key: 'audit', label: t('organize.auditTab'), children: auditTab },
        ]}
      />

      <OrganizePlanDrawer
        planId={drawerPlanId}
        libraries={libraries}
        onClose={() => setDrawerPlanId(null)}
        onChanged={fetchPlans}
      />
    </div>
  );
}
