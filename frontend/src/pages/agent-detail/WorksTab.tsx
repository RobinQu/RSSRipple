import { useState } from 'react';
import { Alert, Button, Card, Divider, Space, Tag, Typography, App } from 'antd';
import { useTranslation } from 'react-i18next';
import { agentsApi } from '../../api/agents';
import WorkSelector from '../../components/WorkSelector';
import BackfillPreviewModal from '../../components/BackfillPreviewModal';
import {
  collectFieldConditions,
  describeCondition,
  findInvalidConditions,
  isFilterEmpty,
  nullIfEmptyFilter,
} from '../../components/filterUtils';
import type { Agent, AgentWork, RulesPreviewResponse } from '../../types';

const { Text } = Typography;

/** Works tab: buffered subscription editing (local state until Save), with
 * the rules-preview backfill selection modal before committing — the same
 * flow as AgentForm. */
export default function WorksTab({
  agent,
  works,
  worksDirty,
  loadingWorks,
  onWorksChange,
  onSaved,
}: {
  agent: Agent;
  works: AgentWork[];
  worksDirty: boolean;
  loadingWorks: boolean;
  onWorksChange: (works: AgentWork[]) => void;
  /** Save settled successfully: the parent adopts the returned agent (works,
   * counts) and refreshes the tasks list. */
  onSaved: (updated: Agent) => void;
}) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [savingWorks, setSavingWorks] = useState(false);
  // Works-tab rule-change preview (scenario ②): when the works list changes,
  // show the backfill selection modal before committing — same flow as
  // AgentForm, so editing works from the detail page also surfaces the
  // resource match diff instead of silently saving.
  const [worksPreview, setWorksPreview] = useState<RulesPreviewResponse | null>(null);
  const [worksPreviewSelected, setWorksPreviewSelected] = useState<Record<string, boolean>>({});
  const [pendingWorksSave, setPendingWorksSave] = useState<AgentWork[] | null>(null);
  const [worksPreviewSaving, setWorksPreviewSaving] = useState(false);

  const _serializeWorks = (worksList: AgentWork[]) =>
    worksList.map((w) => ({
      content_type: w.content_type,
      series_id: w.series_id,
      movie_id: w.movie_id,
      enable_episode_dedup: w.enable_episode_dedup,
      filter_overrides: nullIfEmptyFilter(w.filter_overrides),
      display_name_override: w.display_name_override,
    }));

  const doSaveWorks = async (worksList: AgentWork[], dispatchIds: string[]) => {
    setWorksPreviewSaving(true);
    const r = await agentsApi.update(agent.id, {
      name: agent.name,
      channel_id: agent.channel_id,
      downloader_id: agent.downloader_id,
      works: _serializeWorks(worksList),
      dispatch_resource_ids: dispatchIds,
    });
    setWorksPreviewSaving(false);
    if (r.success) {
      message.success(t('agents.worksSaved'));
      setWorksPreview(null);
      setPendingWorksSave(null);
      onSaved(r.data);
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };

  const handleSaveWorks = async () => {
    // Backend rejects value-taking operators with empty values (422).
    if (works.some((w) => findInvalidConditions(w.filter_overrides).length > 0)) {
      message.error(t('filter.emptyValueNotAllowed'));
      return;
    }
    setSavingWorks(true);
    try {
      // Preview the rule diff before committing. The works tab only changes
      // works, so scope_channel_wide + filter_config come from the current
      // agent (unchanged).
      const pv = await agentsApi.rulesPreview({
        agent_id: agent.id,
        scope_channel_wide: agent.scope_channel_wide,
        filter_config: agent.filter_config,
        works: _serializeWorks(works),
      });
      if (!pv.success) {
        message.error(pv.error?.message || t('agents.previewFailed'));
        return;
      }
      const newly = pv.data.newly_matching;
      const noLonger = pv.data.no_longer_matching;
      if (newly.length > 0 || noLonger.length > 0) {
        const initSel: Record<string, boolean> = {};
        newly.forEach((r) => { initSel[r.id] = true; });
        setWorksPreview(pv.data);
        setWorksPreviewSelected(initSel);
        setPendingWorksSave(works);
        return;
      }
      // No match impact: save directly with empty backfill (still advances
      // the watermark).
      await doSaveWorks(works, []);
    } finally {
      setSavingWorks(false);
    }
  };

  const handleWorksPreviewConfirm = (dispatchIds: string[]) => {
    if (!pendingWorksSave) return;
    doSaveWorks(pendingWorksSave, dispatchIds);
  };

  return (
    <Card loading={loadingWorks}>
      {/* Global filter shown read-only here so the works tab shows
          the effective rules: global conditions AND work overrides. */}
      <div style={{ marginBottom: 16 }}>
        <Text strong style={{ fontSize: 13, display: 'block', marginBottom: 6 }}>
          {t('agents.globalFilter')}
        </Text>
        {isFilterEmpty(agent.filter_config) ? (
          <Text type="secondary" style={{ fontSize: 12 }}>{t('format.dash')}</Text>
        ) : (
          <Space wrap size={[4, 4]}>
            {collectFieldConditions(agent.filter_config).map((c, i) => (
              <Tag key={i} style={{ margin: 0 }}>{describeCondition(c, t)}</Tag>
            ))}
          </Space>
        )}
      </div>
      <WorkSelector
        value={works}
        onChange={onWorksChange}
        maxWorks={10}
        channelId={agent.channel_id}
        globalFilter={agent.filter_config}
      />
      <Divider />
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 12,
        }}
      >
        <Alert
          type="info"
          showIcon
          message={t('agents.worksEditNote')}
          style={{ flex: '1 1 260px' }}
        />
        <Button
          type="primary"
          loading={savingWorks}
          disabled={!worksDirty}
          onClick={handleSaveWorks}
        >
          {t('common.save')}
        </Button>
      </div>
      <BackfillPreviewModal
        open={!!worksPreview}
        data={worksPreview}
        selected={worksPreviewSelected}
        onSelectedChange={setWorksPreviewSelected}
        onCancel={() => { setWorksPreview(null); setPendingWorksSave(null); }}
        onConfirm={handleWorksPreviewConfirm}
        onSkip={() => handleWorksPreviewConfirm([])}
        saving={worksPreviewSaving}
      />
    </Card>
  );
}
