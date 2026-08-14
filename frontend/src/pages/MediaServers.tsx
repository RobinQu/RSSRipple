import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  ArrowDown,
  ArrowUp,
  Link,
  Pencil,
  PlugZap,
  Plus,
  ScanSearch,
  Trash2,
} from 'lucide-react';
import { App, Button, Empty, Space, Switch, Table, Tag, Typography } from 'antd';
import type { TableColumnsType } from 'antd';
import useDocumentTitle from '../hooks/useDocumentTitle';
import { mediaServersApi } from '../api/mediaServers';
import { organizeApi } from '../api/organize';
import { volumesApi } from '../api/volumes';
import MediaServerFormModal from '../components/MediaServerFormModal';
import LibraryBindModal from '../components/LibraryBindModal';
import LibraryEditModal from '../components/LibraryEditModal';
import OrganizeRuleFormModal from '../components/OrganizeRuleFormModal';
import { describeFilter } from '../components/filterUtils';
import { withMobileLabels } from '../utils/table';
import type {
  Library,
  LibraryListItem,
  MediaServerListItem,
  MediaServerType,
  OrganizeRule,
  StorageVolume,
} from '../types';

const { Title, Text } = Typography;

const TYPE_TAG_COLORS: Record<MediaServerType, string> = {
  plex: 'gold',
  emby: 'green',
  jellyfin: 'purple',
};

export default function MediaServers() {
  const { t } = useTranslation();
  useDocumentTitle(t('mediaServers.title'));
  const { message, modal } = App.useApp();

  const [servers, setServers] = useState<MediaServerListItem[]>([]);
  const [libraries, setLibraries] = useState<LibraryListItem[]>([]);
  const [rules, setRules] = useState<OrganizeRule[]>([]);
  const [volumes, setVolumes] = useState<StorageVolume[]>([]);
  const [loading, setLoading] = useState(true);

  const [serverModalOpen, setServerModalOpen] = useState(false);
  const [editingServer, setEditingServer] = useState<MediaServerListItem | null>(null);
  const [bindLibrary, setBindLibrary] = useState<Library | null>(null);
  const [editingLibrary, setEditingLibrary] = useState<Library | null>(null);
  const [ruleModalOpen, setRuleModalOpen] = useState(false);
  const [editingRule, setEditingRule] = useState<OrganizeRule | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [scanningId, setScanningId] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
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
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  const libraryName = (id: string) =>
    libraries.find((lib) => lib.id === id)?.name ?? id.slice(0, 8);

  // ---------------------------------------------------------------- Servers

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
      fetchAll();
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
          fetchAll();
        } else {
          message.error(r.error?.message || t('mediaServers.deleteFailed'));
        }
      },
    });
  };

  const serverColumns: TableColumnsType<MediaServerListItem> = [
    { title: t('common.name'), dataIndex: 'name', key: 'name' },
    {
      title: t('mediaServers.type'),
      dataIndex: 'type',
      key: 'type',
      width: 100,
      render: (v: MediaServerType) => <Tag color={TYPE_TAG_COLORS[v] ?? 'default'}>{v}</Tag>,
    },
    {
      title: t('mediaServers.url'),
      dataIndex: 'url',
      key: 'url',
      render: (v: string) => (
        <Text code ellipsis={{ tooltip: v }} style={{ maxWidth: 260 }}>{v}</Text>
      ),
    },
    {
      title: t('mediaServers.enabled'),
      dataIndex: 'enabled',
      key: 'enabled',
      width: 90,
      render: (v: boolean, record) => (
        <Switch size="small" checked={v} onChange={(checked) => handleToggleServer(record, checked)} />
      ),
    },
    {
      title: t('mediaServers.librariesTitle'),
      key: 'libraries',
      width: 140,
      render: (_, record) => (
        <Space size={4}>
          <Text type="secondary">{record.library_count}</Text>
          {record.unbound_library_count > 0 && (
            <Tag color="volcano">
              {t('mediaServers.unbound')} {record.unbound_library_count}
            </Tag>
          )}
        </Space>
      ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 150,
      align: 'right',
      render: (_, record) => (
        <Space size={4}>
          <Button
            type="text"
            size="small"
            icon={<PlugZap size={14} />}
            title={t('mediaServers.test')}
            loading={testingId === record.id}
            onClick={() => handleTest(record)}
          />
          <Button
            type="text"
            size="small"
            icon={<ScanSearch size={14} />}
            title={t('mediaServers.scan')}
            loading={scanningId === record.id}
            onClick={() => handleScan(record)}
          />
          <Button
            type="text"
            size="small"
            icon={<Pencil size={14} />}
            title={t('common.edit')}
            onClick={() => {
              setEditingServer(record);
              setServerModalOpen(true);
            }}
          />
          <Button
            type="text"
            size="small"
            danger
            icon={<Trash2 size={14} />}
            title={t('common.delete')}
            onClick={() => handleDeleteServer(record)}
          />
        </Space>
      ),
    },
  ];

  // ------------------------------------------------------------- Libraries

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
          fetchAll();
        } else {
          // 409 DELETE_BLOCKED etc. — surface the server's message as-is.
          message.error(r.error?.message || t('libraries.deleteFailed'));
        }
      },
    });
  };

  // Unbound libraries first — they block organize execution and need fixing.
  const sortedLibraries = [...libraries].sort(
    (a, b) => Number(a.bound) - Number(b.bound),
  );

  const libraryColumns: TableColumnsType<LibraryListItem> = [
    { title: t('common.name'), dataIndex: 'name', key: 'name' },
    {
      title: t('mediaServers.server'),
      dataIndex: 'media_server_name',
      key: 'media_server_name',
      width: 130,
      render: (v: string | null) => v ?? t('format.dash'),
    },
    {
      title: t('libraries.kind'),
      dataIndex: 'kind',
      key: 'kind',
      width: 90,
      render: (v: string) => {
        const color = v === 'tv' ? 'blue' : v === 'movie' ? 'purple' : 'default';
        const label =
          v === 'tv'
            ? t('libraries.kindTv')
            : v === 'movie'
              ? t('libraries.kindMovie')
              : t('libraries.kindMixed');
        return <Tag color={color}>{label}</Tag>;
      },
    },
    {
      title: t('mediaServers.serverPath'),
      dataIndex: 'server_path',
      key: 'server_path',
      render: (v: string | null) =>
        v ? (
          <Text code ellipsis={{ tooltip: v }} style={{ maxWidth: 220 }}>{v}</Text>
        ) : (
          t('format.dash')
        ),
    },
    {
      title: t('mediaServers.rootPath'),
      dataIndex: 'root_path',
      key: 'root_path',
      render: (v: string | null) =>
        v ? (
          <Text code ellipsis={{ tooltip: v }} style={{ maxWidth: 220 }}>{v}</Text>
        ) : (
          t('format.dash')
        ),
    },
    {
      title: t('mediaServers.binding'),
      dataIndex: 'bound',
      key: 'bound',
      width: 100,
      render: (v: boolean) =>
        v ? (
          <Tag color="green">{t('mediaServers.bound')}</Tag>
        ) : (
          <Tag color="volcano">{t('mediaServers.unbound')}</Tag>
        ),
    },
    {
      title: t('libraries.pendingPlans'),
      dataIndex: 'pending_plan_count',
      key: 'pending_plan_count',
      width: 110,
      render: (v: number) =>
        v > 0 ? <Tag color="gold">{v}</Tag> : <Text type="secondary">0</Text>,
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 130,
      align: 'right',
      render: (_, record) => (
        <Space size={4}>
          {!record.bound && (
            <Button
              type="text"
              size="small"
              icon={<Link size={14} />}
              title={t('mediaServers.bind')}
              onClick={() => setBindLibrary(record)}
            />
          )}
          <Button
            type="text"
            size="small"
            icon={<Pencil size={14} />}
            title={t('common.edit')}
            onClick={() => setEditingLibrary(record)}
          />
          <Button
            type="text"
            size="small"
            danger
            icon={<Trash2 size={14} />}
            title={t('common.delete')}
            onClick={() => handleDeleteLibrary(record)}
          />
        </Space>
      ),
    },
  ];

  // ------------------------------------------------------------------ Rules

  const handleToggleRule = async (record: OrganizeRule, enabled: boolean) => {
    const r = await organizeApi.updateRule(record.id, { enabled });
    if (r.success) {
      setRules((prev) => prev.map((x) => (x.id === record.id ? { ...x, enabled } : x)));
    } else {
      message.error(r.error?.message || t('libraries.ruleSaveFailed'));
    }
  };

  // First-match-wins ordering: move by swapping priority with the neighbour
  // (nudging ±1 when priorities are equal so the order actually changes).
  const handleMoveRule = async (record: OrganizeRule, direction: -1 | 1) => {
    const idx = rules.findIndex((x) => x.id === record.id);
    const neighbor = rules[idx + direction];
    if (!neighbor) return;
    const target =
      neighbor.priority === record.priority
        ? neighbor.priority + direction
        : neighbor.priority;
    const r = await organizeApi.updateRule(record.id, { priority: target });
    if (r.success) fetchAll();
    else message.error(r.error?.message || t('libraries.ruleSaveFailed'));
  };

  const handleDeleteRule = (record: OrganizeRule) => {
    modal.confirm({
      title: t('libraries.deleteRuleConfirm'),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await organizeApi.deleteRule(record.id);
        if (r.success) {
          message.success(t('libraries.ruleDeleted'));
          fetchAll();
        } else {
          message.error(r.error?.message || t('libraries.ruleDeleteFailed'));
        }
      },
    });
  };

  const ruleColumns: TableColumnsType<OrganizeRule> = [
    {
      title: t('libraries.priority'),
      dataIndex: 'priority',
      key: 'priority',
      width: 130,
      render: (v: number, record) => (
        <Space size={2}>
          <Text type="secondary" style={{ minWidth: 32, display: 'inline-block' }}>{v}</Text>
          <Button
            type="text"
            size="small"
            icon={<ArrowUp size={13} />}
            title={t('libraries.moveUp')}
            disabled={rules.findIndex((x) => x.id === record.id) === 0}
            onClick={() => handleMoveRule(record, -1)}
          />
          <Button
            type="text"
            size="small"
            icon={<ArrowDown size={13} />}
            title={t('libraries.moveDown')}
            disabled={rules.findIndex((x) => x.id === record.id) === rules.length - 1}
            onClick={() => handleMoveRule(record, 1)}
          />
        </Space>
      ),
    },
    { title: t('common.name'), dataIndex: 'name', key: 'name' },
    {
      title: t('libraries.enabled'),
      dataIndex: 'enabled',
      key: 'enabled',
      width: 90,
      render: (v: boolean, record) => (
        <Switch size="small" checked={v} onChange={(checked) => handleToggleRule(record, checked)} />
      ),
    },
    {
      title: t('libraries.filter'),
      key: 'filter',
      render: (_, record) => {
        const summary = describeFilter(record.filter, t);
        return (
          <Text
            type={summary ? undefined : 'secondary'}
            ellipsis={{ tooltip: summary || undefined }}
            style={{ maxWidth: 280 }}
          >
            {summary || t('libraries.filterUnlimited')}
          </Text>
        );
      },
    },
    {
      title: t('organize.library'),
      dataIndex: 'library_id',
      key: 'library_id',
      width: 140,
      render: (v: string) => libraryName(v),
    },
    {
      title: t('libraries.template'),
      dataIndex: 'path_template',
      key: 'path_template',
      render: (v: string) => (
        <Text code ellipsis={{ tooltip: v }} style={{ maxWidth: 320 }}>{v}</Text>
      ),
    },
    {
      title: t('libraries.fileOp'),
      dataIndex: 'file_op',
      key: 'file_op',
      width: 100,
      render: (v: 'move' | 'hardlink' | 'copy') => {
        const color = v === 'move' ? 'green' : v === 'hardlink' ? 'gold' : 'geekblue';
        return <Tag color={color}>{t(`libraries.fileOp_${v}`)}</Tag>;
      },
    },
    {
      title: t('libraries.autoExecute'),
      dataIndex: 'auto_execute',
      key: 'auto_execute',
      width: 110,
      render: (v: boolean) =>
        v ? <Tag color="green">{t('common.on')}</Tag> : <Tag>{t('common.off')}</Tag>,
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 110,
      align: 'right',
      render: (_, record) => (
        <Space size={4}>
          <Button
            type="text"
            size="small"
            icon={<Pencil size={14} />}
            title={t('common.edit')}
            onClick={() => {
              setEditingRule(record);
              setRuleModalOpen(true);
            }}
          />
          <Button
            type="text"
            size="small"
            danger
            icon={<Trash2 size={14} />}
            title={t('common.delete')}
            onClick={() => handleDeleteRule(record)}
          />
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8, marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>
          {t('mediaServers.title')}
        </Title>
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
        columns={withMobileLabels(serverColumns)}
        dataSource={servers}
        rowKey="id"
        loading={loading}
        locale={{ emptyText: <Empty description={t('mediaServers.noServers')} /> }}
        pagination={false}
      />

      <div style={{ margin: '32px 0 16px' }}>
        <Title level={4} style={{ margin: 0 }}>
          {t('mediaServers.librariesTitle')}
        </Title>
        <Text type="secondary">{t('mediaServers.librariesExtra')}</Text>
      </div>

      <Table
        className="stack-table"
        columns={withMobileLabels(libraryColumns)}
        dataSource={sortedLibraries}
        rowKey="id"
        loading={loading}
        locale={{ emptyText: <Empty description={t('libraries.noLibraries')} /> }}
        pagination={false}
      />

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8, margin: '32px 0 16px' }}>
        <Title level={4} style={{ margin: 0 }}>
          {t('libraries.rulesTitle')}
        </Title>
        <Button
          icon={<Plus size={14} />}
          onClick={() => {
            setEditingRule(null);
            setRuleModalOpen(true);
          }}
        >
          {t('libraries.newRule')}
        </Button>
      </div>

      <Table
        className="stack-table"
        columns={withMobileLabels(ruleColumns)}
        dataSource={rules}
        rowKey="id"
        loading={loading}
        locale={{ emptyText: <Empty description={t('libraries.noRules')} /> }}
        pagination={false}
      />

      <MediaServerFormModal
        open={serverModalOpen}
        server={editingServer}
        volumes={volumes}
        onClose={() => setServerModalOpen(false)}
        onSaved={fetchAll}
      />
      <LibraryBindModal
        open={bindLibrary !== null}
        library={bindLibrary}
        volumes={volumes}
        onClose={() => setBindLibrary(null)}
        onSaved={fetchAll}
      />
      <LibraryEditModal
        open={editingLibrary !== null}
        library={editingLibrary}
        onClose={() => setEditingLibrary(null)}
        onSaved={fetchAll}
      />
      <OrganizeRuleFormModal
        open={ruleModalOpen}
        rule={editingRule}
        libraries={libraries}
        onClose={() => setRuleModalOpen(false)}
        onSaved={fetchAll}
      />
    </div>
  );
}
