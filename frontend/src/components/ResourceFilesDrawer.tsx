import { useEffect, useState } from 'react';
import { Alert, App, Button, Drawer, Empty, Input, Spin, Tree, Typography, theme } from 'antd';
import { useTranslation } from 'react-i18next';
import { resourcesApi } from '../api/channels';
import { formatBytes } from '../utils/format';
import { buildFileTree } from '../utils/fileTree';
import type { ResourceFilesResponse } from '../types';

const { Text, Link } = Typography;

// Poll cadence while a magnet resolution is queued/running (cap ~40 polls).
const MAGNET_POLL_INTERVAL_MS = 5000;
const MAGNET_POLL_MAX = 40;

/** Client-side tracker line validation: scheme allowlist + parseable host. */
function isValidTrackerLine(line: string): boolean {
  if (!/^(udp|https?):\/\//i.test(line)) return false;
  try {
    return !!new URL(line).hostname;
  } catch {
    return false;
  }
}

/** Inline file-list view (summary + tree + empty states). Shared by the
 * standalone drawer and the resource detail drawer's files section. */
export function ResourceFilesView({ resourceId }: { resourceId: string }) {
  const { t } = useTranslation();
  const { token } = theme.useToken();
  const { message } = App.useApp();
  const [data, setData] = useState<ResourceFilesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [trackerOpen, setTrackerOpen] = useState(false);
  const [trackerText, setTrackerText] = useState('');
  // Bump to restart the fetch+polling loop (manual magnet retry).
  const [pollNonce, setPollNonce] = useState(0);

  useEffect(() => {
    let active = true;
    let polls = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setLoading(true);
    setFailed(false);
    setData(null);

    const tick = () => {
      resourcesApi
        .getFiles(resourceId)
        .then((res) => {
          if (!active) return;
          if (res.success) {
            setData(res.data);
            setFailed(false);
            // Keep polling while a magnet resolution is queued/running; stop
            // once files appear or the resolution reached a terminal state.
            const magnet = res.data.magnet_resolve;
            const waiting =
              res.data.files.length === 0 &&
              magnet != null &&
              (magnet.status == null ||
                magnet.status === 'pending' ||
                magnet.status === 'running');
            if (waiting && polls < MAGNET_POLL_MAX) {
              polls += 1;
              timer = setTimeout(tick, MAGNET_POLL_INTERVAL_MS);
            }
          } else {
            setFailed(true);
          }
        })
        .finally(() => {
          if (active) setLoading(false);
        });
    };
    tick();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [resourceId, pollNonce]);

  const onRetry = async () => {
    const magnet = data?.magnet_resolve;
    // undefined → trackers key omitted (server clears stored list).
    let trackers: string[] | null | undefined;
    if (trackerOpen) {
      const lines = trackerText
        .split('\n')
        .map((l) => l.trim())
        .filter(Boolean);
      const bad = lines.find((l) => !isValidTrackerLine(l));
      if (bad) {
        message.error(t('resource.magnetResolveInvalidTracker', { tracker: bad }));
        return;
      }
      trackers = lines.length ? lines : undefined;
    } else if (magnet?.trackers?.length) {
      // Plain retry without opening the input keeps the stored custom list.
      trackers = magnet.trackers;
    }
    setRetrying(true);
    try {
      const res = await resourcesApi.resolveMagnet(resourceId, trackers);
      if (res.success) {
        message.success(t('resource.magnetResolveRetrySuccess'));
        setPollNonce((n) => n + 1);
      } else {
        message.error(res.error?.message || t('resource.magnetResolveRetryFailed'));
      }
    } catch {
      message.error(t('resource.magnetResolveRetryFailed'));
    } finally {
      setRetrying(false);
    }
  };

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
    const magnet = data.magnet_resolve;
    if (magnet) {
      if (magnet.status === 'failed') {
        return (
          <div>
            <Alert
              type="warning"
              showIcon
              message={t('resource.magnetResolveFailed', { error: magnet.error || '' })}
              action={
                <Button size="small" loading={retrying} onClick={onRetry}>
                  {t('resource.magnetResolveRetry')}
                </Button>
              }
            />
            <div style={{ marginTop: 8 }}>
              <Link
                style={{ fontSize: 12 }}
                onClick={() => {
                  if (!trackerOpen && !trackerText && magnet.trackers?.length) {
                    setTrackerText(magnet.trackers.join('\n'));
                  }
                  setTrackerOpen(!trackerOpen);
                }}
              >
                {t('resource.magnetResolveCustomTrackers')}
              </Link>
              {trackerOpen && (
                <div style={{ marginTop: 4 }}>
                  <Input.TextArea
                    rows={3}
                    value={trackerText}
                    onChange={(e) => setTrackerText(e.target.value)}
                    placeholder="udp://tracker.example:1337/announce"
                    style={{ fontSize: 12 }}
                  />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {t('resource.magnetResolveCustomTrackersHint')}
                  </Text>
                </div>
              )}
            </div>
          </div>
        );
      }
      const running = magnet.status === 'running';
      return (
        <Alert
          type="info"
          showIcon
          icon={running ? <Spin size="small" /> : undefined}
          message={t(
            running ? 'resource.magnetResolveRunning' : 'resource.magnetResolveQueued',
          )}
        />
      );
    }
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
