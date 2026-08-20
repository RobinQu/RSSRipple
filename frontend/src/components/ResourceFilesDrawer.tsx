import { useEffect, useState } from 'react';
import { Drawer, Empty, Spin, Tree, Typography, theme } from 'antd';
import { useTranslation } from 'react-i18next';
import { resourcesApi } from '../api/channels';
import { formatBytes } from '../utils/format';
import { buildFileTree } from '../utils/fileTree';
import type { ResourceFilesResponse } from '../types';

const { Text } = Typography;

/** Inline file-list view (summary + tree + empty states). Shared by the
 * standalone drawer and the resource detail drawer's files section. */
export function ResourceFilesView({ resourceId }: { resourceId: string }) {
  const { t } = useTranslation();
  const { token } = theme.useToken();
  const [data, setData] = useState<ResourceFilesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setFailed(false);
    setData(null);
    resourcesApi
      .getFiles(resourceId)
      .then((res) => {
        if (!active) return;
        if (res.success) setData(res.data);
        else setFailed(true);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [resourceId]);

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: '24px 0' }}>
        <Spin />
      </div>
    );
  }
  if (failed || !data) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }}>
        {t('resource.filesLoadFailed')}
      </Text>
    );
  }
  if (data.files.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={
          data.source === 'none' ? t('resource.filesEmptyNone') : t('resource.filesEmpty')
        }
      />
    );
  }

  const totalSize = data.files.reduce((sum, f) => sum + (f.size || 0), 0);
  return (
    <div>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
        {t('resource.filesSummary', {
          count: data.files.length,
          size: formatBytes(totalSize),
        })}
      </Text>
      <Tree
        treeData={buildFileTree(data.files)}
        defaultExpandAll
        selectable={false}
        style={{ fontSize: 12, color: token.colorTextSecondary }}
      />
    </div>
  );
}

interface ResourceFilesDrawerProps {
  resourceId: string | null;
  open: boolean;
  onClose: () => void;
  title?: string;
}

export default function ResourceFilesDrawer({
  resourceId,
  open,
  onClose,
  title,
}: ResourceFilesDrawerProps) {
  const { t } = useTranslation();
  return (
    <Drawer
      title={title || t('resource.files')}
      open={open}
      onClose={onClose}
      width={window.innerWidth < 768 ? '100%' : 420}
      destroyOnHidden
    >
      {resourceId && <ResourceFilesView resourceId={resourceId} />}
    </Drawer>
  );
}
