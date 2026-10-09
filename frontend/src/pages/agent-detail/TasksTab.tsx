import { useState } from 'react';
import { Button, Card, Checkbox, Empty, Popover, Space, Typography, App } from 'antd';
import { AlertTriangle, Copy, Pause, Play, RotateCcw, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { TableColumnsType } from 'antd';
import { tasksApi } from '../../api/tasks';
import DownloadTaskTable from '../../components/DownloadTaskTable';
import { copyToClipboard } from '../../utils/clipboard';
import type { useDownloadTaskPrefs } from '../../hooks/useDownloadTaskPrefs';
import type { DownloadTask, FileResource } from '../../types';

const { Text } = Typography;

type TaskPrefs = ReturnType<typeof useDownloadTaskPrefs>;

/** Tasks tab: the shared download-task table plus agent-specific extra
 * columns (error popover, row actions) and batch retry. */
export default function TasksTab({
  agentId,
  tasks,
  loading,
  page,
  total,
  statusFilter,
  taskPrefs,
  onPageChange,
  onStatusFilterChange,
  onReload,
  onOpenResource,
}: {
  agentId: string;
  tasks: DownloadTask[];
  loading: boolean;
  page: number;
  total: number;
  statusFilter: string | undefined;
  taskPrefs: TaskPrefs;
  onPageChange: (page: number) => void;
  onStatusFilterChange: (status: string | undefined) => void;
  onReload: () => void;
  /** Read-only resource view opened by clicking a task row. */
  onOpenResource: (resource: FileResource) => void;
}) {
  const { t } = useTranslation();
  const { message, modal } = App.useApp();
  // Batch retry: selected task ids + in-flight flag for the tasks tab.
  const [selectedTaskIds, setSelectedTaskIds] = useState<string[]>([]);
  const [taskBatchLoading, setTaskBatchLoading] = useState(false);

  const handlePause = async (tid: string) => {
    const r = await tasksApi.pause(tid);
    if (r.success) onReload();
    else message.error(r.error?.message || t('agents.taskActionFailed'));
  };
  const handleResume = async (tid: string) => {
    const r = await tasksApi.resume(tid);
    if (r.success) onReload();
    else message.error(r.error?.message || t('agents.taskActionFailed'));
  };
  const handleRetry = async (tid: string) => {
    const r = await tasksApi.retry(tid);
    if (r.success) onReload();
    else message.error(r.error?.message || t('agents.taskActionFailed'));
  };
  const handleBatchRetry = async (taskIds?: string[]) => {
    setTaskBatchLoading(true);
    const r = await tasksApi.batchRetry(agentId, taskIds);
    setTaskBatchLoading(false);
    if (r.success) {
      const { retried, failed } = r.data;
      message.success(t('agents.batchRetryDone', { retried, failed }));
      setSelectedTaskIds([]);
      onReload();
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };
  const handleBatchRetryAll = () => {
    modal.confirm({
      title: t('agents.batchRetryAllConfirm'),
      onOk: () => handleBatchRetry(),
    });
  };
  const handleDeleteTask = (tid: string) => {
    let deleteData = false;
    modal.confirm({
      title: t('agents.deleteTaskConfirm'),
      content: (
        <div>
          <div>{t('agents.deleteTaskWarning')}</div>
          <Checkbox
            style={{ marginTop: 8 }}
            onChange={(e) => {
              deleteData = e.target.checked;
            }}
          >
            {t('agents.deleteTaskData')}
          </Checkbox>
        </div>
      ),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      onOk: async () => {
        await tasksApi.delete(tid, deleteData);
        onReload();
      },
    });
  };

  const copyText = async (text: string) => {
    if (await copyToClipboard(text)) {
      message.success(t('agents.errorCopied'));
    } else {
      message.error(t('agents.copyFailed'));
    }
  };

  // Extra fixed-right columns appended after the shared task table's
  // progress column: error popover + row actions (agent-tab specific). The
  // shared table supplies work/metadata/status/progress columns.
  const taskExtraColumns: TableColumnsType<DownloadTask> = [
    {
      title: t('agents.taskError'),
      dataIndex: 'error_message',
      key: 'err',
      width: 56,
      align: 'center',
      // Error details live behind a popover: hover to read, click the copy
      // button to take the full log.
      render: (v: string | null) =>
        v ? (
          <Popover
            trigger={['hover', 'click']}
            placement="left"
            content={
              <div style={{ maxWidth: 360 }}>
                <Text style={{ fontSize: 12, display: 'block', wordBreak: 'break-word' }}>{v}</Text>
                <Button
                  size="small"
                  type="text"
                  icon={<Copy size={13} />}
                  style={{ marginTop: 6, padding: 0 }}
                  onClick={(e) => {
                    e.stopPropagation();
                    copyText(v);
                  }}
                >
                  {t('agents.copyError')}
                </Button>
              </div>
            }
          >
            <Button
              type="text"
              size="small"
              danger
              icon={<AlertTriangle size={15} />}
              aria-label={t('agents.taskError')}
              onClick={(e) => e.stopPropagation()}
            />
          </Popover>
        ) : null,
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 160,
      align: 'right',
      // Pinned right so pause/resume/retry/delete stay reachable without
      // scrolling to the far end of the (wide) table.
      fixed: 'right',
      render: (_, record) => (
        <Space size={0} onClick={(e) => e.stopPropagation()}>
          {record.status === 'downloading' && (
            <Button type="text" size="small" icon={<Pause size={14} />} aria-label={t('common.pause')} onClick={() => handlePause(record.id)} />
          )}
          {record.status === 'paused' && (
            <Button type="text" size="small" icon={<Play size={14} style={{ color: 'var(--rr-success)' }} />} aria-label={t('common.resume')} onClick={() => handleResume(record.id)} />
          )}
          {(record.status === 'error' || record.status === 'paused') && (
            <Button type="text" size="small" icon={<RotateCcw size={14} style={{ color: 'var(--rr-primary)' }} />} aria-label={t('common.retry')} onClick={() => handleRetry(record.id)} />
          )}
          <Button type="text" size="small" danger icon={<Trash2 size={14} />} aria-label={t('common.delete')} onClick={() => handleDeleteTask(record.id)} />
        </Space>
      ),
    },
  ];

  return (
    <Card>
      <DownloadTaskTable
        tasks={tasks}
        loading={loading}
        page={page}
        total={total}
        onPageChange={onPageChange}
        sorts={taskPrefs.sorts}
        onSortsChange={(next) => {
          taskPrefs.handleSortsChange(next);
          onPageChange(1);
        }}
        columnCfg={taskPrefs.columnCfg}
        onColumnsChange={taskPrefs.handleColumnsChange}
        columnSettingsHint={t('agents.taskColumnSettingsHint')}
        statusFilter={statusFilter}
        onStatusFilterChange={(v) => {
          onStatusFilterChange(v);
          onPageChange(1);
        }}
        toolbarExtra={
          <>
            <Button
              size="small"
              loading={taskBatchLoading}
              onClick={handleBatchRetryAll}
            >
              {t('agents.batchRetryAll')}
            </Button>
            <Button
              size="small"
              type="primary"
              loading={taskBatchLoading}
              disabled={selectedTaskIds.length === 0}
              onClick={() => handleBatchRetry(selectedTaskIds)}
            >
              {t('agents.batchRetrySelected', { n: selectedTaskIds.length })}
            </Button>
          </>
        }
        extraColumns={taskExtraColumns}
        rowSelection={{
          selectedRowKeys: selectedTaskIds,
          onChange: (keys) => setSelectedTaskIds(keys as string[]),
          // Only error/paused tasks are retryable (same as the backend filter).
          getCheckboxProps: (record) => ({
            disabled: !['error', 'paused'].includes(record.status),
          }),
        }}
        onRowClick={(task) => {
          if (task.file_resource) onOpenResource(task.file_resource);
        }}
        emptyText={<Empty description={t('agents.noTasks')} />}
      />
    </Card>
  );
}
