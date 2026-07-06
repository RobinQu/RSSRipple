import { useTranslation } from 'react-i18next';
import { Modal, Table, Tag, Typography, Space, Button, Tooltip } from 'antd';
import type { TableColumnsType } from 'antd';
import type { RulesPreviewResource, RulesPreviewResponse } from '../types';
import { formatBytes } from '../utils/format';

const { Text } = Typography;

const previewColumns = (t: (k: string, opts?: Record<string, unknown>) => string): TableColumnsType<RulesPreviewResource> => [
  {
    title: t('agents.previewColTitle'),
    dataIndex: 'title_raw',
    key: 'title',
    // No ellipsis: let the (often long) raw title wrap so the user can read
    // the whole release string, not a truncated single line.
    render: (v: string, r) => {
      const langs = (r.subtitle_langs && r.subtitle_langs.length > 0)
        ? r.subtitle_langs.join('/')
        : null;
      return (
        <div>
          <Text strong style={{ fontSize: 13, color: '#17171c', wordBreak: 'break-word' }}>
            {r.title_cn || v}
          </Text>
          <Space size={4} wrap style={{ fontSize: 11, color: '#616161', marginTop: 2 }}>
            {r.subtitle_group && <Tag style={{ margin: 0 }}>{r.subtitle_group}</Tag>}
            {r.resolution && <Tag style={{ margin: 0 }}>{r.resolution}</Tag>}
            {r.source && <Tag style={{ margin: 0 }}>{r.source}</Tag>}
            {r.video_codec && <Tag style={{ margin: 0 }}>{r.video_codec}</Tag>}
            {r.audio_codec && <Tag style={{ margin: 0 }}>{r.audio_codec}</Tag>}
            {r.container && <Tag style={{ margin: 0 }}>{r.container}</Tag>}
            {r.season != null && <span>S{r.season}</span>}
            {r.episode != null && <span>EP{r.episode}</span>}
            {r.subtitle_type && <Tag style={{ margin: 0 }}>{r.subtitle_type}</Tag>}
            {langs && <Tag color="blue" style={{ margin: 0 }}>{langs}</Tag>}
            {r.file_size != null && <span>{formatBytes(r.file_size)}</span>}
            {r.episode_confidence === 'ambiguous' && (
              <Tag color="warning" style={{ margin: 0 }}>{t('agents.ambiguousTag')}</Tag>
            )}
          </Space>
        </div>
      );
    },
  },
];

interface BackfillPreviewModalProps {
  open: boolean;
  data: RulesPreviewResponse | null;
  selected: Record<string, boolean>;
  onSelectedChange: (s: Record<string, boolean>) => void;
  onCancel: () => void;
  onConfirm: (ids: string[]) => void;
  onSkip: () => void;
  saving: boolean;
}

/** Shared "file resource change" preview modal shown before committing a
 * subscription rule change. Lists newly-matching resources (selectable for
 * backfill) and reports no-longer-matching / already-in-queue counts. Used by
 * both AgentForm (full edit) and AgentDetail's works-tab save. */
export default function BackfillPreviewModal({
  open, data, selected, onSelectedChange, onCancel, onConfirm, onSkip, saving,
}: BackfillPreviewModalProps) {
  const { t } = useTranslation();
  if (!data) return null;
  const newly = data.newly_matching;
  const selectedIds = newly.filter((r) => selected[r.id]).map((r) => r.id);
  const allChecked = newly.length > 0 && selectedIds.length === newly.length;
  const columns = previewColumns(t);
  return (
    <Modal
      open={open}
      onCancel={onCancel}
      title={t('agents.previewTitle')}
      width={720}
      destroyOnClose
      footer={
        <Space>
          <Button onClick={onSkip} disabled={saving}>
            {t('agents.previewSkip')}
          </Button>
          <Button
            type="primary"
            onClick={() => onConfirm(selectedIds)}
            loading={saving}
            disabled={selectedIds.length === 0}
          >
            {t('agents.previewConfirm', { n: selectedIds.length })}
          </Button>
        </Space>
      }
    >
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
        {t('agents.previewDesc')}
      </Text>
      <div style={{ marginBottom: 8 }}>
        <Space size={12}>
          {newly.length > 0 && (
            <a
              onClick={() => {
                const next: Record<string, boolean> = {};
                newly.forEach((r) => { next[r.id] = !allChecked; });
                onSelectedChange(next);
              }}
            >
              {allChecked ? t('agents.previewUnselectAll') : t('agents.previewSelectAll')}
            </a>
          )}
          {data.no_longer_matching.length > 0 && (
            <Tooltip title={t('agents.previewNoLongerHint')}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t('agents.previewNoLonger', { n: data.no_longer_matching.length })}
              </Text>
            </Tooltip>
          )}
          {data.in_queue_skipped > 0 && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t('agents.previewInQueue', { n: data.in_queue_skipped })}
            </Text>
          )}
        </Space>
      </div>
      <Table<RulesPreviewResource>
        columns={columns}
        dataSource={newly}
        rowKey="id"
        size="small"
        pagination={{ pageSize: 10, showSizeChanger: false }}
        rowSelection={{
          selectedRowKeys: selectedIds,
          onChange: (keys) => {
            const next: Record<string, boolean> = {};
            newly.forEach((r) => { next[r.id] = keys.includes(r.id); });
            onSelectedChange(next);
          },
        }}
        locale={{ emptyText: t('agents.previewEmpty') }}
      />
    </Modal>
  );
}
