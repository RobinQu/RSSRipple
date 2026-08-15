import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Empty, Modal, Space, Spin, Typography } from 'antd';
import { ChevronLeft, CornerUpLeft, FolderOpen } from 'lucide-react';
import { volumesApi } from '../api/volumes';

const { Text } = Typography;

const joinPath = (base: string, name: string) =>
  base === '/' ? `/${name}` : `${base}/${name}`;

/** Server-side directory picker. Browsing is absolute-path based; callers map
    the selected path onto their own field (mount path, relative subpath, …). */
export default function DirectoryBrowserModal({
  open,
  title,
  initialPath,
  onSelect,
  onCancel,
}: {
  open: boolean;
  title: string;
  initialPath: string;
  onSelect: (path: string) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [curPath, setCurPath] = useState('/');
  const [parentPath, setParentPath] = useState('/');
  const [dirs, setDirs] = useState<string[]>([]);
  const [browsing, setBrowsing] = useState(false);
  const [browseError, setBrowseError] = useState<string | null>(null);

  const loadDir = async (path: string) => {
    setBrowsing(true);
    setBrowseError(null);
    const res = await volumesApi.listDirs(path);
    setBrowsing(false);
    if (res.success) {
      setCurPath(res.data.path);
      setParentPath(res.data.parent);
      setDirs(res.data.dirs);
    } else {
      setBrowseError(res.error?.message || t('volumes.browseFailed'));
    }
  };

  useEffect(() => {
    if (open) {
      loadDir((initialPath || '').trim() || '/');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <Modal
      open={open}
      title={title}
      onCancel={onCancel}
      footer={[
        <Button key="root" onClick={() => loadDir('/')} disabled={browsing}>
          <CornerUpLeft size={14} /> {t('volumes.rootDir')}
        </Button>,
        <Button key="cancel" onClick={onCancel}>
          {t('common.cancel')}
        </Button>,
        <Button
          key="select"
          type="primary"
          disabled={!!browseError}
          onClick={() => {
            onSelect(curPath);
            onCancel();
          }}
        >
          {t('volumes.selectDir')}
        </Button>,
      ]}
    >
      <Space direction="vertical" style={{ width: '100%' }} size={8}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Button
            size="small"
            icon={<ChevronLeft size={14} />}
            onClick={() => loadDir(parentPath)}
            disabled={browsing || parentPath === curPath}
          >
            {t('volumes.parentDir')}
          </Button>
          <Text code ellipsis={{ tooltip: curPath }} style={{ flex: 1, maxWidth: '100%' }}>
            {curPath}
          </Text>
        </div>
        {browseError && (
          <Text type="danger" style={{ fontSize: 12 }}>
            {browseError}
          </Text>
        )}
        <div
          style={{
            border: '1px solid #e5e7eb',
            borderRadius: 6,
            maxHeight: 280,
            overflow: 'auto',
          }}
        >
          {browsing ? (
            <div style={{ padding: 24, textAlign: 'center' }}>
              <Spin size="small" />
            </div>
          ) : dirs.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t('volumes.noSubdirs')}
              style={{ padding: 16 }}
            />
          ) : (
            dirs.map((d) => (
              <div
                key={d}
                style={{
                  padding: '6px 12px',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  borderBottom: '1px solid #f0f0f0',
                  fontSize: 13,
                }}
                onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.background = '#f5f5f5')}
                onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.background = 'transparent')}
                onClick={() => loadDir(joinPath(curPath, d))}
              >
                <FolderOpen size={14} style={{ color: '#d89614', flexShrink: 0 }} />
                <span>{d}</span>
              </div>
            ))
          )}
        </div>
      </Space>
    </Modal>
  );
}
