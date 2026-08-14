import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Activity, Pencil, Plus, Trash2 } from 'lucide-react';
import { App, Button, Empty, Space, Table, Tag, Typography } from 'antd';
import type { TableColumnsType } from 'antd';
import useDocumentTitle from '../hooks/useDocumentTitle';
import { volumesApi } from '../api/volumes';
import VolumeFormModal from '../components/VolumeFormModal';
import { withMobileLabels } from '../utils/table';
import type { StorageVolume, StorageVolumeCheckResult } from '../types';

const { Title, Text } = Typography;

export default function Volumes() {
  const { t } = useTranslation();
  useDocumentTitle(t('volumes.title'));
  const { message, modal } = App.useApp();

  const [volumes, setVolumes] = useState<StorageVolume[]>([]);
  const [loading, setLoading] = useState(true);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<StorageVolume | null>(null);
  // Per-row probe results from POST /volumes/{id}/check (ephemeral, not persisted).
  const [checks, setChecks] = useState<Record<string, StorageVolumeCheckResult>>({});
  const [checkingId, setCheckingId] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    const res = await volumesApi.list();
    if (res.success) setVolumes(res.data);
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  const handleCheck = async (record: StorageVolume) => {
    setCheckingId(record.id);
    const res = await volumesApi.check(record.id);
    setCheckingId(null);
    if (res.success) {
      setChecks((prev) => ({ ...prev, [record.id]: res.data }));
    } else {
      message.error(res.error?.message || t('volumes.probeFailed'));
    }
  };

  const handleDelete = (record: StorageVolume) => {
    modal.confirm({
      title: t('volumes.deleteConfirm'),
      content: t('volumes.deleteWarning'),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await volumesApi.delete(record.id);
        if (r.success) {
          message.success(t('volumes.deleted'));
          fetchAll();
        } else {
          // 409 DELETE_BLOCKED carries the referencing downloader names in the
          // server message — show it verbatim.
          message.error(r.error?.message || t('volumes.deleteFailed'));
        }
      },
    });
  };

  const columns: TableColumnsType<StorageVolume> = [
    { title: t('common.name'), dataIndex: 'name', key: 'name' },
    {
      title: t('volumes.mountPath'),
      dataIndex: 'mount_path',
      key: 'mount_path',
      render: (v: string) => (
        <Text code ellipsis={{ tooltip: v }} style={{ maxWidth: 320 }}>{v}</Text>
      ),
    },
    {
      title: t('volumes.remark'),
      dataIndex: 'remark',
      key: 'remark',
      render: (v: string | null) =>
        v ? (
          <Text type="secondary" ellipsis={{ tooltip: v }} style={{ maxWidth: 240 }}>{v}</Text>
        ) : (
          t('format.dash')
        ),
    },
    {
      title: t('volumes.probe'),
      key: 'check',
      width: 200,
      render: (_, record) => {
        const result = checks[record.id];
        if (!result) return <Text type="secondary">{t('volumes.notChecked')}</Text>;
        return (
          <Space size={4}>
            <Tag color={result.exists ? 'green' : 'red'}>
              {result.exists ? t('volumes.exists') : t('volumes.missing')}
            </Tag>
            {result.exists && (
              <Tag color={result.writable ? 'green' : 'orange'}>
                {result.writable ? t('volumes.writable') : t('volumes.readOnly')}
              </Tag>
            )}
          </Space>
        );
      },
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 130,
      align: 'right',
      render: (_, record) => (
        <Space size={4}>
          <Button
            type="text"
            size="small"
            icon={<Activity size={14} />}
            title={t('volumes.probe')}
            loading={checkingId === record.id}
            onClick={() => handleCheck(record)}
          />
          <Button
            type="text"
            size="small"
            icon={<Pencil size={14} />}
            title={t('common.edit')}
            onClick={() => {
              setEditing(record);
              setModalOpen(true);
            }}
          />
          <Button
            type="text"
            size="small"
            danger
            icon={<Trash2 size={14} />}
            title={t('common.delete')}
            onClick={() => handleDelete(record)}
          />
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8, marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>
          {t('volumes.title')}
        </Title>
        <Button
          type="primary"
          icon={<Plus size={14} />}
          onClick={() => {
            setEditing(null);
            setModalOpen(true);
          }}
        >
          {t('volumes.newVolume')}
        </Button>
      </div>

      <Table
        className="stack-table"
        columns={withMobileLabels(columns)}
        dataSource={volumes}
        rowKey="id"
        loading={loading}
        locale={{ emptyText: <Empty description={t('volumes.noVolumes')} /> }}
        pagination={false}
      />

      <VolumeFormModal
        open={modalOpen}
        volume={editing}
        onClose={() => setModalOpen(false)}
        onSaved={fetchAll}
      />
    </div>
  );
}
