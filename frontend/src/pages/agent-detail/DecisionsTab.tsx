import { useState } from 'react';
import {
  Button,
  Card,
  Checkbox,
  Empty,
  InputNumber,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
  App,
} from 'antd';
import { CheckCircle, ListTree, SkipForward } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { decisionsApi } from '../../api/tasks';
import { resourcesApi } from '../../api/channels';
import SeasonInput from '../../components/SeasonInput';
import { formatBytes, timeAgo } from '../../utils/format';
import type { FileResource, PendingDecision } from '../../types';

const { Text } = Typography;

// Per-candidate draft for the ambiguous-episode correction action (season +
// episode + absolute_episode, all optional; the backend PATCH preserves any
// field the user left out).
interface EpisodeDraft {
  season: number | null;
  episode: number | null;
  absolute_episode: number | null;
}

/** Decisions tab: pending-decision cards with confirm/skip, AI auto-handle,
 * batch actions and the ambiguous-episode correction form. */
export default function DecisionsTab({
  agentId,
  decisions,
  candidateCache,
  loading,
  onDecisionsChange,
  onTasksChange,
  onShowFiles,
}: {
  agentId: string;
  decisions: PendingDecision[];
  candidateCache: Record<string, FileResource>;
  loading: boolean;
  onDecisionsChange: () => void;
  onTasksChange: () => void;
  /** File-listing drawer for decision candidates. */
  onShowFiles: (resourceId: string) => void;
}) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  // Ambiguous-episode decisions: per-candidate draft (season / episode /
  // absolute_episode) + in-flight flag for the "correct episode" action.
  const [episodeDrafts, setEpisodeDrafts] = useState<Record<string, EpisodeDraft>>({});
  const [savingEpisodeCid, setSavingEpisodeCid] = useState<string | null>(null);
  // Batch selection + AI auto-handle loading state for the decisions tab.
  const [selectedDecisionIds, setSelectedDecisionIds] = useState<string[]>([]);
  const [aiPickLoading, setAiPickLoading] = useState<string | null>(null);
  const [batchLoading, setBatchLoading] = useState(false);

  const handleConfirm = async (did: string, rid: string) => {
    const r = await decisionsApi.confirm(did, rid);
    if (r.success) {
      message.success(t('dashboard.confirmed'));
      onDecisionsChange();
      onTasksChange();
    } else message.error(r.error?.message || t('dashboard.failed'));
  };
  const handleSkip = async (did: string) => {
    await decisionsApi.skip(did);
    onDecisionsChange();
  };

  const handleAiPick = async (did: string) => {
    setAiPickLoading(did);
    const r = await decisionsApi.aiPick(did);
    setAiPickLoading(null);
    if (r.success) {
      message.success(t('agents.aiHandled'));
      onDecisionsChange();
      onTasksChange();
    } else {
      message.error(r.error?.message || t('agents.aiHandleFailed'));
    }
  };

  const handleBatch = async (action: 'skip' | 'ai') => {
    if (selectedDecisionIds.length === 0) return;
    setBatchLoading(true);
    const r = await decisionsApi.batch(agentId, selectedDecisionIds, action);
    setBatchLoading(false);
    if (r.success) {
      const { dispatched, skipped, failed } = r.data;
      message.success(t('agents.batchDone', { dispatched, skipped, failed }));
      setSelectedDecisionIds([]);
      onDecisionsChange();
      onTasksChange();
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };

  const handleCorrectEpisode = async (cid: string, displayedDraft?: EpisodeDraft) => {
    // A prefilled form has no local draft until the first edit.  Fall back to
    // the displayed values so confirming them still performs the correction.
    const draft = episodeDrafts[cid] ?? displayedDraft;
    if (!draft || draft.episode == null) return;
    setSavingEpisodeCid(cid);
    const r = await resourcesApi.correctEpisode(cid, {
      episode: draft.episode,
      ...(draft.season != null ? { season: draft.season } : {}),
      ...(draft.absolute_episode != null ? { absolute_episode: draft.absolute_episode } : {}),
    });
    setSavingEpisodeCid(null);
    if (r.success) {
      message.success(t('agents.episodeSaved'));
      setEpisodeDrafts((prev) => {
        const next = { ...prev };
        delete next[cid];
        return next;
      });
      onDecisionsChange();
      onTasksChange();
    } else {
      message.error(r.error?.message || t('agents.saveFailed'));
    }
  };

  const isAmbiguousDecision = (d: PendingDecision): boolean => {
    const cands = d.candidate_resources;
    return !!cands && cands.length > 0 && cands.every((r) => r.episode_confidence === 'ambiguous');
  };

  // Show the raw release title as a secondary line whenever it differs from
  // the parsed/formatted title — the raw title carries the subtitle group,
  // SxxExx markers and release tags a human needs to disambiguate candidates.
  const renderRawTitle = (r: FileResource | undefined) => {
    const raw = r?.title_raw;
    if (!raw) return null;
    const formatted = r?.title_cn || r?.title_en;
    if (!formatted || raw === formatted) return null;
    return (
      <div
        style={{
          fontSize: 12,
          color: 'var(--rr-text-muted)',
          marginTop: 2,
          display: 'flex',
          alignItems: 'center',
          gap: 4,
          minWidth: 0,
        }}
      >
        <span style={{ flexShrink: 0 }}>{t('channels.rawTitle')}:</span>
        <Tooltip title={raw}>
          <span
            style={{
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              flex: 1,
              minWidth: 0,
            }}
          >
            {raw}
          </span>
        </Tooltip>
      </div>
    );
  };

  return (
    <Card>
      <Spin spinning={loading}>
        {decisions.length === 0 ? (
          <Empty description={t('dashboard.noPendingDecisions')} />
        ) : (
          <>
            <Space style={{ marginBottom: 12 }}>
              <Button
                size="small"
                loading={batchLoading}
                disabled={selectedDecisionIds.length === 0}
                onClick={() => handleBatch('skip')}
              >
                {t('agents.batchSkip', { n: selectedDecisionIds.length })}
              </Button>
              <Button
                size="small"
                type="primary"
                loading={batchLoading}
                disabled={selectedDecisionIds.length === 0}
                onClick={() => handleBatch('ai')}
              >
                {t('agents.batchAi', { n: selectedDecisionIds.length })}
              </Button>
            </Space>
            <Space direction="vertical" style={{ width: '100%' }} size={12}>
              {decisions.map((d) => {
                const ambiguous = isAmbiguousDecision(d);
                const checked = selectedDecisionIds.includes(d.id);
                return (
                <Card key={d.id} size="small">
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'flex-start',
                      marginBottom: 12,
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                      <Checkbox
                        checked={checked}
                        onChange={(e) => {
                          setSelectedDecisionIds((prev) =>
                            e.target.checked
                              ? [...prev, d.id]
                              : prev.filter((x) => x !== d.id),
                          );
                        }}
                      />
                      <div>
                        <Text strong>{d.reason}</Text>
                        <div style={{ fontSize: 12, color: 'var(--rr-text-muted)', marginTop: 4 }}>
                          {t('agents.candidateCount', { n: d.candidates.length })} · {timeAgo(d.created_at)}
                        </div>
                        {ambiguous && (
                          <div style={{ fontSize: 12, color: 'var(--rr-warning)', marginTop: 4 }}>
                            {t('agents.ambiguousHint')}
                          </div>
                        )}
                        {d.llm_suggestion && (
                          <div
                            style={{
                              marginTop: 8,
                              padding: 8,
                              borderRadius: 6,
                              background: 'var(--rr-primary-soft)',
                              border: '1px solid var(--rr-info-border)',
                              fontSize: 12,
                              color: 'var(--rr-primary)',
                            }}
                          >
                            <strong>{t('dashboard.aiSuggestion')}</strong>
                            {d.llm_suggestion}
                          </div>
                        )}
                      </div>
                    </div>
                    <Space size={6}>
                      {!ambiguous && (
                        <Button
                          size="small"
                          type="primary"
                          loading={aiPickLoading === d.id}
                          onClick={() => handleAiPick(d.id)}
                        >
                          {t('agents.aiHandle')}
                        </Button>
                      )}
                      <Button size="small" onClick={() => handleSkip(d.id)}>
                        <SkipForward size={12} /> {t('common.skip')}
                      </Button>
                    </Space>
                  </div>
                  <Space direction="vertical" style={{ width: '100%' }} size={6}>
                    {d.candidates.map((cid) => {
                      const r = candidateCache[cid] ?? d.candidate_resources?.find((x) => x.id === cid);
                      const isAiPick = !ambiguous && cid === d.llm_picked_resource_id;
                      if (ambiguous) {
                        const base = {
                          season: r?.season ?? null,
                          episode: r?.episode ?? null,
                          absolute_episode: r?.absolute_episode ?? null,
                        };
                        const draft = { ...base, ...(episodeDrafts[cid] ?? {}) };
                        const patchDraft = (patch: Partial<EpisodeDraft>) =>
                          setEpisodeDrafts((prev) => ({
                            ...prev,
                            [cid]: { ...base, ...(prev[cid] ?? {}), ...patch },
                          }));
                        return (
                          <div
                            key={cid}
                            style={{
                              display: 'flex',
                              justifyContent: 'space-between',
                              alignItems: 'center',
                              padding: '8px 12px',
                              borderRadius: 6,
                              border: '1px solid var(--rr-border-soft)',
                              gap: 12,
                            }}
                          >
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <Text ellipsis style={{ fontSize: 13 }}>
                                {r?.title_cn || r?.title_raw || cid.slice(0, 8)}
                              </Text>
                              {renderRawTitle(r)}
                              <Space size={4} wrap style={{ fontSize: 11, color: 'var(--rr-text-muted)', marginTop: 2 }}>
                                {r?.subtitle_group && <Tag style={{ margin: 0 }}>{r.subtitle_group}</Tag>}
                                {r?.resolution && <Tag style={{ margin: 0 }}>{r.resolution}</Tag>}
                                {r?.season != null && <span>S{r.season}</span>}
                                {r?.episode != null && (
                                  <span>{t('agents.rawEpisode', { n: r.episode })}</span>
                                )}
                              </Space>
                            </div>
                            <Space size={6} align="center" wrap>
                              {r && (
                                <Tooltip title={t('resource.files')}>
                                  <Button
                                    type="text"
                                    size="small"
                                    icon={<ListTree size={14} />}
                                    aria-label={t('resource.files')}
                                    onClick={() => onShowFiles(r.id)}
                                  />
                                </Tooltip>
                              )}
                              <SeasonInput
                                size="small"
                                value={draft.season}
                                placeholder={t('resource.seasonLabel')}
                                onChange={(v) => patchDraft({ season: v })}
                                style={{ width: 72 }}
                              />
                              <InputNumber
                                size="small"
                                min={1}
                                value={draft.episode}
                                placeholder={t('agents.correctEpisodePlaceholder')}
                                onChange={(v) =>
                                  patchDraft({ episode: typeof v === 'number' ? v : null })
                                }
                                style={{ width: 72 }}
                              />
                              <InputNumber
                                size="small"
                                min={0}
                                value={draft.absolute_episode}
                                placeholder={t('resource.absoluteEpisodePlaceholder')}
                                onChange={(v) =>
                                  patchDraft({ absolute_episode: typeof v === 'number' ? v : null })
                                }
                                style={{ width: 130 }}
                              />
                              <Button
                                type="primary"
                                size="small"
                                loading={savingEpisodeCid === cid}
                                disabled={draft.episode == null}
                                onClick={() => handleCorrectEpisode(cid, draft)}
                              >
                                {t('agents.correctEpisode')}
                              </Button>
                            </Space>
                          </div>
                        );
                      }
                      return (
                        <div
                          key={cid}
                          style={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            padding: '8px 12px',
                            borderRadius: 6,
                            border: `1px solid ${isAiPick ? 'var(--rr-primary)' : 'var(--rr-border-soft)'}`,
                            background: isAiPick ? 'var(--rr-primary-soft)' : 'transparent',
                            gap: 12,
                          }}
                        >
                          {r ? (
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <Space size={6} align="center" style={{ marginBottom: 2 }}>
                                <Text ellipsis style={{ fontSize: 13 }}>
                                  {r.title_cn || r.title_raw}
                                </Text>
                                {isAiPick && (
                                  <Tag color="blue" style={{ margin: 0 }}>{t('agents.aiPickTag')}</Tag>
                                )}
                              </Space>
                              {renderRawTitle(r)}
                              <Space size={4} wrap style={{ fontSize: 11, color: 'var(--rr-text-muted)', marginTop: 2 }}>
                                {r.subtitle_group && <Tag style={{ margin: 0 }}>{r.subtitle_group}</Tag>}
                                {r.resolution && <Tag style={{ margin: 0 }}>{r.resolution}</Tag>}
                                {r.video_codec && <Tag style={{ margin: 0 }}>{r.video_codec}</Tag>}
                                {r.file_size != null && <span>{formatBytes(r.file_size)}</span>}
                              </Space>
                            </div>
                          ) : (
                            <Text type="secondary" style={{ fontSize: 12 }}>{t('common.loading')}</Text>
                          )}
                          <Space size={6} align="center" wrap>
                            {r && (
                              <Tooltip title={t('resource.files')}>
                                <Button
                                  type="text"
                                  size="small"
                                  icon={<ListTree size={14} />}
                                  aria-label={t('resource.files')}
                                  onClick={() => onShowFiles(r.id)}
                                />
                              </Tooltip>
                            )}
                            <Button
                              type="primary"
                              size="small"
                              icon={<CheckCircle size={12} />}
                              onClick={() => handleConfirm(d.id, cid)}
                            >
                              {t('common.confirm')}
                            </Button>
                          </Space>
                        </div>
                      );
                    })}
                  </Space>
                </Card>
                );
              })}
            </Space>
          </>
        )}
      </Spin>
    </Card>
  );
}
