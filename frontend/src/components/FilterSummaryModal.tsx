import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  Modal,
  Button,
  Space,
  Typography,
  Spin,
  Alert,
  Segmented,
  Select,
  App,
  Form,
  Input,
  Card,
} from 'antd';
import { Wand2, PlusCircle, ListFilter, Tv, Film } from 'lucide-react';
import { channelsApi } from '../api/channels';
import { agentsApi } from '../api/agents';
import FilterBuilder, { findInvalidConditions, nullIfEmptyFilter } from './FilterBuilder';
import type {
  Agent,
  AgentWork,
  AgentWorkCreate,
  BoolCondition,
  FilterSuggestionWork,
  Movie,
  TVSeries,
} from '../types';

interface Props {
  open: boolean;
  channelId: string;
  /** Channel display name — used for the default agent name. */
  channelName?: string;
  selectedIds: string[];
  onClose: () => void;
  onAgentCreated?: (agent: Agent) => void;
}

const hasCJK = (s: string) => /[㐀-鿿぀-ヿ가-힯]/.test(s);

/** crypto.randomUUID requires a secure context; this app is often served over
 * plain http on a LAN host, so fall back to a manual id there. The id is only
 * a client-side temp key for the not-yet-persisted work row. */
const tempId = (): string =>
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `tmp-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

/** Build an AgentForm-compatible temp work entry from a suggestion. The
 * embedded series/movie object carries display-only fields so WorkSelector
 * can render title/poster before the agent exists. */
function buildTempWork(w: FilterSuggestionWork): AgentWork {
  const workId = (w.content_type === 'tv' ? w.series_id : w.movie_id) ?? '';
  const display = {
    id: workId,
    title_cn: w.title && hasCJK(w.title) ? w.title : null,
    title_en: w.title && !hasCJK(w.title) ? w.title : null,
    original_title: null,
    aliases: null,
    external_id: null,
    external_source: null,
    description: null,
    poster_url: w.poster_url,
    rating: null,
    genre: null,
    status: null,
    number_of_episodes: null,
    number_of_seasons: null,
    start_date: null,
    end_date: null,
    release_date: null,
    runtime: null,
    content_type: null,
    created_at: '',
    updated_at: '',
  };
  return {
    id: tempId(),
    agent_id: '',
    content_type: w.content_type,
    series_id: w.series_id,
    movie_id: w.movie_id,
    enable_episode_dedup: true,
    filter_overrides: nullIfEmptyFilter(w.filter_overrides),
    display_name_override: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    series: w.content_type === 'tv' ? (display as TVSeries) : undefined,
    movie: w.content_type === 'movie' ? (display as Movie) : undefined,
  };
}

const workKey = (w: {
  content_type: string;
  series_id: string | null;
  movie_id: string | null;
}) => `${w.content_type}:${w.series_id ?? ''}:${w.movie_id ?? ''}`;

const serializeWork = (w: AgentWork): AgentWorkCreate => ({
  content_type: w.content_type,
  series_id: w.series_id,
  movie_id: w.movie_id,
  enable_episode_dedup: w.enable_episode_dedup,
  filter_overrides: nullIfEmptyFilter(w.filter_overrides),
  display_name_override: w.display_name_override,
});

/** Merge two filter configs with AND */
const mergeFilters = (
  base: BoolCondition | null | undefined,
  addition: BoolCondition,
): BoolCondition => {
  if (!base || !base.conditions || base.conditions.length === 0) {
    return addition;
  }
  return {
    combinator: 'and',
    conditions: [base, addition],
  };
};

export default function FilterSummaryModal({
  open,
  channelId,
  channelName,
  selectedIds,
  onClose,
  onAgentCreated: _onAgentCreated,
}: Props) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [works, setWorks] = useState<FilterSuggestionWork[]>([]);
  // Global rules are a manual-only affair in this dialog: rules generated
  // from the selected resources are folded into each work's overrides at
  // load time, so anything present here was typed by the user.
  const [globalConfig, setGlobalConfig] = useState<BoolCondition | null>(null);
  const [unlinkedCount, setUnlinkedCount] = useState(0);
  const [explanation, setExplanation] = useState<string>('');
  const [mode, setMode] = useState<'create' | 'apply'>('create');
  const [channelAgents, setChannelAgents] = useState<Agent[]>([]);
  const [applyAgentId, setApplyAgentId] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const [form] = Form.useForm();

  useEffect(() => {
    if (!open || selectedIds.length === 0) return;
    setLoading(true);
    setWorks([]);
    setGlobalConfig(null);
    setUnlinkedCount(0);
    setExplanation('');
    setApplyAgentId(null);
    setMode('create');
    form.resetFields();

    // Default agent name: agent-{channel_name}-{YYYY-MM-DD} (local date).
    const now = new Date();
    const pad = (n: number) => String(n).padStart(2, '0');
    const date = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
    form.setFieldsValue({
      name: `agent-${channelName ?? channelId.slice(0, 8)}-${date}`,
    });

    Promise.all([
      channelsApi.summarizeFilters(channelId, selectedIds),
      agentsApi.list(1, 100),
    ])
      .then(([filterRes, agentRes]) => {
        if (filterRes.success) {
          // Fold the generated "global" common conditions into each work's
          // overrides — generated rules live at the work dimension in both
          // create and apply modes; the global section stays empty for
          // manual edits only. (When nothing is linked to a work the common
          // conditions have no anchor and are dropped; the user can still
          // hand-add global rules.)
          const generated = nullIfEmptyFilter(filterRes.data.global_filter_config);
          let suggestedWorks = filterRes.data.works ?? [];
          if (generated && suggestedWorks.length > 0) {
            suggestedWorks = suggestedWorks.map((w) => ({
              ...w,
              filter_overrides: mergeFilters(w.filter_overrides, generated),
            }));
          }
          setWorks(suggestedWorks);
          setGlobalConfig(null);
          setUnlinkedCount(filterRes.data.unlinked_count ?? 0);
          setExplanation(filterRes.data.explanation || '');
        } else {
          message.error(filterRes.error?.message || t('filter.generateFailed'));
        }
        if (agentRes.success) {
          setChannelAgents(agentRes.data.filter((a) => a.channel_id === channelId));
        }
      })
      .finally(() => setLoading(false));
  }, [open, channelId, channelName, selectedIds, form, message, t]);

  const updateWorkOverrides = (index: number, v: BoolCondition | null) => {
    setWorks((prev) => prev.map((w, i) => (i === index ? { ...w, filter_overrides: v } : w)));
  };

  /** The backend rejects value-taking operators with empty values (422) —
   * block both save paths before submitting. */
  const validateFilters = (): boolean => {
    const invalid =
      findInvalidConditions(globalConfig).length > 0 ||
      works.some((w) => findInvalidConditions(w.filter_overrides).length > 0);
    if (invalid) {
      message.error(t('filter.emptyValueNotAllowed'));
      return false;
    }
    return true;
  };

  const handleCreateFromHere = async () => {
    // Navigate to new agent form with prefilled filter and channel.
    // We pass state through sessionStorage since react-router state is reset on page load.
    if (!validateFilters()) return;
    try {
      const values = await form.validateFields();
      sessionStorage.setItem(
        'rssripple:prefill:agent',
        JSON.stringify({
          name: values.name,
          channel_id: channelId,
          filter_config: nullIfEmptyFilter(globalConfig),
          works: works.map(buildTempWork),
        }),
      );
      onClose();
      navigate('/agents/new');
    } catch {
      // validation failure
    }
  };

  const handleApply = async () => {
    if (!applyAgentId) return;
    const target = channelAgents.find((a) => a.id === applyAgentId);
    if (!target) return;
    if (!validateFilters()) return;
    setApplying(true);
    try {
      // The update endpoint REPLACES the works list, so fetch the full agent
      // and submit the complete merged list.
      const detail = await agentsApi.get(applyAgentId);
      if (!detail.success) {
        message.error(detail.error?.message || t('filter.applyFailed'));
        return;
      }
      // The global section only ever holds *manual* edits (generated rules
      // were folded into the works at load time), so a non-empty global
      // config is a deliberate hand-edit and may be AND-merged into the
      // agent's global filter. Generated rules never touch it — the agent's
      // existing behavior for other works stays intact.
      const gc = nullIfEmptyFilter(globalConfig);
      const mergedFilter = gc
        ? nullIfEmptyFilter(mergeFilters(detail.data.filter_config, gc))
        : nullIfEmptyFilter(detail.data.filter_config);
      const mergedWorks: AgentWork[] = [...(detail.data.works ?? [])];
      works.forEach((sw) => {
        const override = nullIfEmptyFilter(sw.filter_overrides);
        const idx = mergedWorks.findIndex((w) => workKey(w) === workKey(sw));
        if (idx >= 0) {
          if (override) {
            mergedWorks[idx] = {
              ...mergedWorks[idx],
              filter_overrides: mergeFilters(mergedWorks[idx].filter_overrides, override),
            };
          }
        } else {
          mergedWorks.push(buildTempWork(sw));
        }
      });
      const res = await agentsApi.update(applyAgentId, {
        name: target.name,
        channel_id: target.channel_id,
        downloader_id: target.downloader_id,
        filter_config: mergedFilter,
        works: mergedWorks.map(serializeWork),
      });
      if (res.success) {
        message.success(t('filter.appendedToAgent'));
        onClose();
      } else {
        message.error(res.error?.message || t('filter.applyFailed'));
      }
    } finally {
      setApplying(false);
    }
  };

  const renderWorkPoster = (w: FilterSuggestionWork) => {
    if (w.poster_url) {
      return (
        <img
          src={w.poster_url}
          alt=""
          style={{ width: 32, height: 45, objectFit: 'cover', borderRadius: 4, flexShrink: 0 }}
        />
      );
    }
    return (
      <div
        style={{
          width: 32,
          height: 45,
          borderRadius: 4,
          background: '#f0f0f2',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: '#93939f',
          flexShrink: 0,
        }}
      >
        {w.content_type === 'tv' ? <Tv size={16} /> : <Film size={16} />}
      </div>
    );
  };

  const hasContent = works.length > 0 || globalConfig !== null;

  return (
    <Modal
      open={open}
      onCancel={onClose}
      title={
        <Space>
          <Wand2 />
          <span>{t('filter.generate')}</span>
          <Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
            {t('filter.selectedResources', { n: selectedIds.length })}
          </Typography.Text>
        </Space>
      }
      footer={null}
      width={720}
      styles={{ body: { padding: '16px 24px 24px' } }}
      destroyOnHidden
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: '48px 0' }}>
          <Spin />
          <div style={{ marginTop: 12, color: '#93939f', fontSize: 13 }}>
            {t('filter.analyzing', { n: selectedIds.length })}
          </div>
        </div>
      ) : (
        <div>
          {/* No suggestion could be derived (all resources unlinked and no
              common field) - still render the full interactive editor so the
              user can hand-build rules; only the hint changes. */}
          {!hasContent && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message={
                unlinkedCount > 0
                  ? `${t('filter.noWorksHint')} ${t('filter.unlinkedNote', { n: unlinkedCount })}`
                  : t('filter.noCommonFeatures')
              }
            />
          )}
          {explanation && (
            <Typography.Paragraph
              type="secondary"
              style={{ fontSize: 12, marginBottom: 12 }}
            >
              {explanation}
            </Typography.Paragraph>
          )}

          <div style={{ marginBottom: 16 }}>
            <Typography.Text strong style={{ fontSize: 13, display: 'block', marginBottom: 8 }}>
              {t('filter.worksSection')}
            </Typography.Text>
            {works.length === 0 ? (
              <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
                {t('filter.noWorksHint')}
              </Typography.Text>
            ) : (
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                {works.map((w, idx) => (
                  <Card key={workKey(w)} size="small">
                    <Space align="center" size={12} style={{ marginBottom: 8 }}>
                      {renderWorkPoster(w)}
                      <div>
                        <Typography.Text strong style={{ fontSize: 13 }}>
                          {w.title || (w.series_id ?? w.movie_id ?? '').slice(0, 8)}
                        </Typography.Text>
                        <Typography.Text
                          type="secondary"
                          style={{ fontSize: 12, display: 'block' }}
                        >
                          {t('filter.workResourceCount', { n: w.resource_count })}
                        </Typography.Text>
                      </div>
                    </Space>
                    {w.override_explanation && (
                      <Typography.Text
                        type="secondary"
                        style={{ fontSize: 12, display: 'block', marginBottom: 8 }}
                      >
                        {w.override_explanation}
                      </Typography.Text>
                    )}
                    <Typography.Text
                      type="secondary"
                      style={{ fontSize: 12, display: 'block', marginBottom: 4 }}
                    >
                      {t('filter.workOverrides')}
                    </Typography.Text>
                    <FilterBuilder
                      compact
                      value={w.filter_overrides}
                      onChange={(v) => updateWorkOverrides(idx, v)}
                      channelId={channelId}
                    />
                  </Card>
                ))}
              </Space>
            )}
            {unlinkedCount > 0 && (
              <Typography.Text
                type="secondary"
                style={{ fontSize: 12, display: 'block', marginTop: 8 }}
              >
                {t('filter.unlinkedNote', { n: unlinkedCount })}
              </Typography.Text>
            )}
          </div>

          <div style={{ marginBottom: 16 }}>
            <Typography.Text strong style={{ fontSize: 13, display: 'block', marginBottom: 8 }}>
              {t('filter.globalRules')}
            </Typography.Text>
            {!globalConfig && (
              <Typography.Text
                type="secondary"
                style={{ fontSize: 12, display: 'block', marginBottom: 8 }}
              >
                {t('filter.globalRulesEmptyHint')}
              </Typography.Text>
            )}
            <FilterBuilder value={globalConfig} onChange={setGlobalConfig} channelId={channelId} />
          </div>

          <Segmented
            block
            value={mode}
            onChange={(v) => setMode(v as 'create' | 'apply')}
            options={[
              {
                label: (
                  <Space size={4}>
                    <PlusCircle />
                    <span>{t('filter.newAgent')}</span>
                  </Space>
                ),
                value: 'create',
              },
              {
                label: (
                  <Space size={4}>
                    <ListFilter />
                    <span>{t('filter.applyToExisting')}</span>
                  </Space>
                ),
                value: 'apply',
              },
            ]}
            style={{ marginBottom: 16 }}
          />

          {mode === 'create' ? (
            <Form form={form} layout="vertical" size="small">
              <Form.Item
                name="name"
                label={t('filter.agentName')}
                rules={[{ required: true, message: t('filter.agentNamePlaceholder') }]}
              >
                <Input placeholder={t('filter.agentNameExample')} autoFocus />
              </Form.Item>
              <div style={{ textAlign: 'right' }}>
                <Space>
                  <Button htmlType="button" onClick={onClose}>{t('common.cancel')}</Button>
                  <Button htmlType="button" type="primary" onClick={handleCreateFromHere}>
                    {t('filter.createAgentAndConfig')}
                  </Button>
                </Space>
              </div>
              <Typography.Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 8 }}>
                {t('filter.createAgentHint')}
              </Typography.Text>
            </Form>
          ) : (
            <div>
              {channelAgents.length === 0 ? (
                <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                  {t('filter.noAgentHint')}
                </Typography.Text>
              ) : (
                <>
                  <Typography.Text style={{ fontSize: 12, color: '#93939f', display: 'block', marginBottom: 6 }}>
                    {t('filter.selectTargetAgent')}
                  </Typography.Text>
                  <Select
                    options={channelAgents.map((a) => ({ label: a.name, value: a.id }))}
                    value={applyAgentId}
                    onChange={setApplyAgentId}
                    placeholder={t('filter.selectAgentPlaceholder')}
                    style={{ width: '100%' }}
                  />
                  {applyAgentId && (
                    <Typography.Text style={{ fontSize: 12, color: '#93939f', display: 'block', marginTop: 8 }}>
                      {t('filter.mergeHint')}
                    </Typography.Text>
                  )}
                  <div style={{ textAlign: 'right', marginTop: 16 }}>
                    <Space>
                      <Button htmlType="button" onClick={onClose}>{t('common.cancel')}</Button>
                      <Button
                        htmlType="button"
                        type="primary"
                        loading={applying}
                        disabled={!applyAgentId}
                        onClick={handleApply}
                      >
                        {t('filter.applyRules')}
                      </Button>
                    </Space>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}
