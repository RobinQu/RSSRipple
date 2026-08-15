import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Eye,
  Link,
  Pencil,
  Play,
  PlugZap,
  Plus,
  RefreshCw,
  ScanSearch,
  Settings,
  Trash2,
  XCircle,
} from 'lucide-react';
import { App, Button, Empty, Select, Space, Switch, Table, Tabs, Tag, Typography } from 'antd';
import type { TableColumnsType } from 'antd';
import useDocumentTitle from '../hooks/useDocumentTitle';
import { organizeApi } from '../api/organize';
import { mediaServersApi } from '../api/mediaServers';
import { volumesApi } from '../api/volumes';
import MediaServerFormModal from '../components/MediaServerFormModal';
import LibraryBindModal from '../components/LibraryBindModal';
import LibrarySettingsDrawer from '../components/LibrarySettingsDrawer';
import OrganizeOpPaths from '../components/OrganizeOpPaths';
import OrganizePlanDrawer from '../components/OrganizePlanDrawer';
import StatusBadge from '../components/StatusBadge';
import { formatDate, timeAgo } from '../utils/format';
import { withMobileLabels } from '../utils/table';
import type {
  Library,
  LibraryListItem,
  MediaServerListItem,
  MediaServerType,
  OrganizeAuditEntry,
  OrganizePlanListItem,
  OrganizePlanStatus,
  OrganizeRule,
  StorageVolume,
} from '../types';

const { Title, Text } = Typography;

const TYPE_TAG_COLORS: Record<MediaServerType, string> = {
  plex: 'gold',
  emby: 'green',
  jellyfin: 'purple',
};

const STATUS_FILTERS: (OrganizePlanStatus | 'all')[] = [
  'all',
  'pending',
  'running',
  'done',
  'failed',
  'cancelled',
];

type ConfigRow =
  | { key: string; rowType: 'server'; server: MediaServerListItem | null; children: ConfigRow[] }
  | { key: string; rowType: 'library'; library: LibraryListItem };

export default function MediaLibrary() {
  const { t } = useTranslation();
  useDocumentTitle(t('mediaLibrary.title'));
  const { message, modal } = App.useApp();

  const [tab, setTab] = useState<'plans' | 'audit' | 'config'>('plans');

  // ------------------------------------------------------------------ Config
  const [servers, setServers] = useState<MediaServerListItem[]>([]);
  const [libraries, setLibraries] = useState<LibraryListItem[]>([]);
  const [rules, setRules] = useState<OrganizeRule[]>([]);
  const [volumes, setVolumes] = useState<StorageVolume[]>([]);
  const [configLoading, setConfigLoading] = useState(true);

  const [serverModalOpen, setServerModalOpen] = useState(false);
  const [editingServer, setEditingServer] = useState<MediaServerListItem | null>(null);
  const [bindLibrary, setBindLibrary] = useState<Library | null>(null);
  const [settingsLibrary, setSettingsLibrary] = useState<Library | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [scanningId, setScanningId] = useState<string | null>(null);

  // ------------------------------------------------------------------ Plans
  const [plans, setPlans] = useState<OrganizePlanListItem[]>([]);
  const [plansTotal, setPlansTotal] = useState(0);
  const [plansPage, setPlansPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<OrganizePlanStatus | 'all'>('pending');
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

  const fetchConfig = useCallback(async () => {
    setConfigLoading(true);
    const [serverRes, libRes, ruleRes, volumeRes] = await Promise.all([
      mediaServersApi.list(),
      organizeApi.listLibraries(),
      organizeApi.listRules(),
      volumesApi.list(),
    ]);
    if (serverRes.success) setServers(serverRes.data);
    if (libRes.success) setLibraries(libRes.data);
    if (ruleRes.success) setRules(ruleRes.data);
    if (volumeRes.success) setVolumes(volumeRes.data);
    setConfigLoading(false);
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
    fetchConfig();
  }, [fetchConfig]);

  useEffect(() => {
    if (tab === 'plans') fetchPlans();
    else if (tab === 'audit') fetchAudit();
  }, [tab, fetchPlans, fetchAudit]);

  // ------------------------------------------------------------ Plans actions
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

  const opTypeTag = (opType: string): { color?: string; label: string } => {
    if (opType === 'move') return { color: 'green', label: t('organize.opMove') };
    if (opType === 'movedir') return { color: 'blue', label: t('organize.opMovedir') };
    return { color: undefined, label: t('organize.opKeep') };
  };

  // ------------------------------------------------------------ Server actions
  const handleToggleServer = async (record: MediaServerListItem, enabled: boolean) => {
    const r = await mediaServersApi.update(record.id, { enabled });
    if (r.success) {
      setServers((prev) => prev.map((x) => (x.id === record.id ? { ...x, enabled } : x)));
    } else {
      message.error(r.error?.message || t('mediaServers.saveFailed'));
    }
  };

  const handleTest = async (record: MediaServerListItem) => {
    setTestingId(record.id);
    const res = await mediaServersApi.test(record.id);
    setTestingId(null);
    if (res.success && res.data.ok) {
      message.success(
        res.data.server_version
          ? t('mediaServers.testOk', { version: res.data.server_version })
          : t('mediaServers.testOkNoVersion'),
      );
    } else if (res.success) {
      message.error(res.data.message || t('mediaServers.testFailed'));
    } else {
      message.error(res.error?.message || t('mediaServers.testFailed'));
    }
  };

  const handleScan = async (record: MediaServerListItem) => {
    setScanningId(record.id);
    const res = await mediaServersApi.scan(record.id);
    setScanningId(null);
    if (res.success) {
      const { created, updated, unbound } = res.data;
      modal.info({
        title: t('mediaServers.scanResultTitle'),
        content: (
          <div>
            <div>{t('mediaServers.scanResult', { created, updated, unbound })}</div>
            {unbound > 0 && (
              <div style={{ marginTop: 8 }}>
                <Text type="warning">
                  {t('mediaServers.scanUnboundHint', { count: unbound })}
                </Text>
              </div>
            )}
          </div>
        ),
      });
      fetchConfig();
    } else {
      message.error(res.error?.message || t('mediaServers.scanFailed'));
    }
  };

  const handleDeleteServer = (record: MediaServerListItem) => {
    modal.confirm({
      title: t('mediaServers.deleteConfirm'),
      content: t('mediaServers.deleteWarning'),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await mediaServersApi.delete(record.id);
        if (r.success) {
          message.success(t('mediaServers.deleted'));
          fetchConfig();
        } else {
          message.error(r.error?.message || t('mediaServers.deleteFailed'));
        }
      },
    });
  };

  const handleDeleteLibrary = (record: LibraryListItem) => {
    modal.confirm({
      title: t('libraries.deleteConfirm'),
      content: t('libraries.deleteWarning'),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await organizeApi.deleteLibrary(record.id);
        if (r.success) {
          message.success(t('libraries.deleted'));
          fetchConfig();
        } else {
          message.error(r.error?.message || t('libraries.deleteFailed'));
        }
      },
    });
  };

  // ---------------------------------------------------------------- Plans tab
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
      title: t('organize.target'),
      key: 'target',
      width: 180,
      render: (_, record) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          <Text ellipsis={{ tooltip: record.rule_name || undefined }}>
            {record.rule_name ?? t('format.dash')}
          </Text>
          {record.library_id === null ? (
            <Text type="warning" style={{ fontSize: 12 }}>{t('organize.uncategorizedTag')}</Text>
          ) : (
            <Text type="secondary" ellipsis={{ tooltip: record.library_name || undefined }} style={{ fontSize: 12 }}>
              {record.library_name ?? t('format.dash')}
            </Text>
          )}
          {record.category && (
            <Text type="secondary" ellipsis={{ tooltip: record.category }} style={{ fontSize: 12 }}>
              {record.category}
            </Text>
          )}
        </div>
      ),
    },
    {
      title: t('organize.ops'),
      key: 'ops',
      render: (_, record) => {
        const preview = record.ops_preview ?? [];
        const extra = record.ops_summary.total - preview.length;
        if (preview.length === 0) return t('format.dash');
        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 }}>
            {preview.map((op) => {
              const tag = opTypeTag(op.op_type);
              return (
                <div key={op.id} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', minWidth: 0 }}>
                  <Tag color={tag.color} style={{ margin: 0, flexShrink: 0 }}>{tag.label}</Tag>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <OrganizeOpPaths src={op.src} dst={op.dst} />
                  </div>
                </div>
              );
            })}
            {extra > 0 && (
              <Button
                type="link"
                size="small"
                style={{ padding: 0, alignSelf: 'flex-start' }}
                onClick={() => setDrawerPlanId(record.id)}
              >
                {t('organize.moreOps', { n: extra })}
              </Button>
            )}
          </div>
        );
      },
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

  const plansTab = (
    <>
      <Space size={8} style={{ display: 'flex', marginBottom: 16, flexWrap: 'wrap' }}>
        <Select
          style={{ minWidth: 140 }}
          value={statusFilter}
          onChange={(v) => {
            setStatusFilter(v as OrganizePlanStatus | 'all');
            setPlansPage(1);
            setSelectedIds([]);
          }}
          options={STATUS_FILTERS.map((s) => ({
            value: s,
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

  // --------------------------------------------------------------- Audit tab
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

  // -------------------------------------------------------------- Config tab
  const orphanLibraries = libraries.filter((l) => !l.media_server_id);
  const configRows: ConfigRow[] = [
    ...servers.map<ConfigRow>((s) => ({
      key: s.id,
      rowType: 'server',
      server: s,
      children: libraries
        .filter((l) => l.media_server_id === s.id)
        .sort((a, b) => Number(a.bound) - Number(b.bound))
        .map<ConfigRow>((l) => ({ key: l.id, rowType: 'library', library: l })),
    })),
    ...(orphanLibraries.length > 0
      ? [
          {
            key: '__orphans__',
            rowType: 'server' as const,
            server: null,
            children: orphanLibraries.map<ConfigRow>((l) => ({
              key: l.id,
              rowType: 'library',
              library: l,
            })),
          },
        ]
      : []),
  ];

  const configColumns: TableColumnsType<ConfigRow> = [
    {
      title: t('common.name'),
      key: 'name',
      width: 240,
      render: (_, row) =>
        row.rowType === 'server' ? (
          <Space size={6} style={{ maxWidth: '100%' }}>
            <Text
              strong
              ellipsis={{ tooltip: row.server ? row.server.name : t('mediaLibrary.orphanServers') }}
            >
              {row.server ? row.server.name : t('mediaLibrary.orphanServers')}
            </Text>
            {row.server && row.server.unbound_library_count > 0 && (
              <Tag color="volcano" style={{ flexShrink: 0 }}>
                {t('mediaServers.unbound')} {row.server.unbound_library_count}
              </Tag>
            )}
          </Space>
        ) : (
          <Text ellipsis={{ tooltip: row.library.name }}>{row.library.name}</Text>
        ),
    },
    {
      title: t('mediaServers.type'),
      key: 'type',
      width: 100,
      render: (_, row) =>
        row.rowType === 'server' ? (
          row.server ? (
            <Tag color={TYPE_TAG_COLORS[row.server.type] ?? 'default'}>{row.server.type}</Tag>
          ) : (
            t('format.dash')
          )
        ) : (
          <Tag color={row.library.kind === 'tv' ? 'blue' : row.library.kind === 'movie' ? 'purple' : 'default'}>
            {row.library.kind === 'tv'
              ? t('libraries.kindTv')
              : row.library.kind === 'movie'
                ? t('libraries.kindMovie')
                : t('libraries.kindMixed')}
          </Tag>
        ),
    },
    {
      title: t('common.url'),
      key: 'location',
      render: (_, row) =>
        row.rowType === 'server' ? (
          row.server ? (
            <Text code ellipsis={{ tooltip: row.server.url }} style={{ maxWidth: 240 }}>
              {row.server.url}
            </Text>
          ) : (
            t('format.dash')
          )
        ) : row.library.server_path ? (
          <Text code ellipsis={{ tooltip: row.library.server_path }} style={{ maxWidth: 240 }}>
            {row.library.server_path}
          </Text>
        ) : (
          t('format.dash')
        ),
    },
    {
      title: t('mediaServers.rootPath'),
      key: 'root_path',
      render: (_, row) =>
        row.rowType === 'library' && row.library.root_path ? (
          <Text code ellipsis={{ tooltip: row.library.root_path }} style={{ maxWidth: 220 }}>
            {row.library.root_path}
          </Text>
        ) : (
          t('format.dash')
        ),
    },
    {
      title: t('mediaServers.binding'),
      key: 'bound',
      width: 100,
      render: (_, row) =>
        row.rowType === 'library' ? (
          row.library.bound ? (
            <Tag color="green">{t('mediaServers.bound')}</Tag>
          ) : (
            <Tag color="volcano">{t('mediaServers.unbound')}</Tag>
          )
        ) : (
          t('format.dash')
        ),
    },
    {
      title: t('libraries.pendingPlans'),
      key: 'pending_plan_count',
      width: 110,
      render: (_, row) =>
        row.rowType === 'library' ? (
          row.library.pending_plan_count > 0 ? (
            <Tag color="gold">{row.library.pending_plan_count}</Tag>
          ) : (
            <Text type="secondary">0</Text>
          )
        ) : (
          t('format.dash')
        ),
    },
    {
      title: t('mediaServers.enabled'),
      key: 'enabled',
      width: 90,
      render: (_, row) =>
        row.rowType === 'server' && row.server ? (
          <Switch
            size="small"
            checked={row.server.enabled}
            onChange={(checked) => handleToggleServer(row.server as MediaServerListItem, checked)}
          />
        ) : (
          t('format.dash')
        ),
    },
    {
      title: t('mediaLibrary.settings'),
      key: 'settings',
      width: 90,
      render: (_, row) =>
        row.rowType === 'library' ? (
          <Button
            type="link"
            size="small"
            icon={<Settings size={14} />}
            onClick={() => setSettingsLibrary(row.library)}
          >
            {t('mediaLibrary.settings')}
          </Button>
        ) : (
          t('format.dash')
        ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 170,
      align: 'right',
      render: (_, row) => {
        if (row.rowType === 'server') {
          if (!row.server) return null;
          return (
            <Space size={4}>
              <Button
                type="text"
                size="small"
                icon={<PlugZap size={14} />}
                title={t('mediaServers.test')}
                loading={testingId === row.server.id}
                onClick={() => handleTest(row.server as MediaServerListItem)}
              />
              <Button
                type="text"
                size="small"
                icon={<ScanSearch size={14} />}
                title={t('mediaServers.scan')}
                loading={scanningId === row.server.id}
                onClick={() => handleScan(row.server as MediaServerListItem)}
              />
              <Button
                type="text"
                size="small"
                icon={<Pencil size={14} />}
                title={t('common.edit')}
                onClick={() => {
                  setEditingServer(row.server as MediaServerListItem);
                  setServerModalOpen(true);
                }}
              />
              <Button
                type="text"
                size="small"
                danger
                icon={<Trash2 size={14} />}
                title={t('common.delete')}
                onClick={() => handleDeleteServer(row.server as MediaServerListItem)}
              />
            </Space>
          );
        }
        return (
          <Space size={4}>
            {!row.library.bound && (
              <Button
                type="text"
                size="small"
                icon={<Link size={14} />}
                title={t('mediaServers.bind')}
                onClick={() => setBindLibrary(row.library)}
              />
            )}
            <Button
              type="text"
              size="small"
              danger
              icon={<Trash2 size={14} />}
              title={t('common.delete')}
              onClick={() => handleDeleteLibrary(row.library)}
            />
          </Space>
        );
      },
    },
  ];

  const configTab = (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
        <Button
          type="primary"
          icon={<Plus size={14} />}
          onClick={() => {
            setEditingServer(null);
            setServerModalOpen(true);
          }}
        >
          {t('mediaServers.newServer')}
        </Button>
      </div>
      <Table
        className="stack-table"
        columns={withMobileLabels(configColumns)}
        dataSource={configRows}
        rowKey="key"
        loading={configLoading}
        expandable={{ defaultExpandAllRows: true }}
        locale={{ emptyText: <Empty description={t('mediaServers.noServers')} /> }}
        pagination={false}
      />
    </>
  );

  return (
    <div>
      <div style={{ marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>
          {t('mediaLibrary.title')}
        </Title>
      </div>

      <Tabs
        activeKey={tab}
        onChange={(k) => setTab(k as 'plans' | 'audit' | 'config')}
        items={[
          { key: 'plans', label: t('mediaLibrary.tabPlans'), children: plansTab },
          { key: 'audit', label: t('mediaLibrary.tabAudit'), children: auditTab },
          { key: 'config', label: t('mediaLibrary.tabConfig'), children: configTab },
        ]}
      />

      <MediaServerFormModal
        open={serverModalOpen}
        server={editingServer}
        volumes={volumes}
        onClose={() => setServerModalOpen(false)}
        onSaved={fetchConfig}
      />
      <LibraryBindModal
        open={bindLibrary !== null}
        library={bindLibrary}
        volumes={volumes}
        onClose={() => setBindLibrary(null)}
        onSaved={fetchConfig}
      />
      <LibrarySettingsDrawer
        open={settingsLibrary !== null}
        library={settingsLibrary}
        libraries={libraries}
        rules={rules}
        volumes={volumes}
        onClose={() => setSettingsLibrary(null)}
        onChanged={fetchConfig}
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
