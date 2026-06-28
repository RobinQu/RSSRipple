import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  Modal,
  Button,
  Space,
  Typography,
  Spin,
  Empty,
  Segmented,
  Select,
  App,
  Form,
  Input,
} from 'antd';
import { Wand2, PlusCircle, ListFilter } from 'lucide-react';
import { channelsApi } from '../api/channels';
import { agentsApi } from '../api/agents';
import FilterBuilder from './FilterBuilder';
import type { Agent, BoolCondition } from '../types';

interface Props {
  open: boolean;
  channelId: string;
  selectedIds: string[];
  onClose: () => void;
  onAgentCreated?: (agent: Agent) => void;
}

export default function FilterSummaryModal({
  open,
  channelId,
  selectedIds,
  onClose,
  onAgentCreated: _onAgentCreated,
}: Props) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [filterConfig, setFilterConfig] = useState<BoolCondition | null>(null);
  const [explanation, setExplanation] = useState<string>('');
  const [mode, setMode] = useState<'create' | 'apply'>('create');
  const [channelAgents, setChannelAgents] = useState<Agent[]>([]);
  const [applyAgentId, setApplyAgentId] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const [form] = Form.useForm();

  useEffect(() => {
    if (!open || selectedIds.length === 0) return;
    setLoading(true);
    setFilterConfig(null);
    setExplanation('');
    setApplyAgentId(null);
    setMode('create');
    form.resetFields();

    Promise.all([
      channelsApi.summarizeFilters(channelId, selectedIds),
      agentsApi.list(1, 100),
    ])
      .then(([filterRes, agentRes]) => {
        if (filterRes.success) {
          setFilterConfig(filterRes.data.filter_config);
          setExplanation(filterRes.data.explanation || '');
        } else {
          message.error(filterRes.error?.message || t('filter.generateFailed'));
        }
        if (agentRes.success) {
          setChannelAgents(agentRes.data.filter((a) => a.channel_id === channelId));
        }
      })
      .finally(() => setLoading(false));
  }, [open, channelId, selectedIds, form, message, t]);

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

  const handleCreateFromHere = async () => {
    // Navigate to new agent form with prefilled filter and channel.
    // We pass state through sessionStorage since react-router state is reset on page load.
    try {
      const values = await form.validateFields();
      sessionStorage.setItem(
        'rssripple:prefill:agent',
        JSON.stringify({
          name: values.name,
          channel_id: channelId,
          filter_config: filterConfig,
        }),
      );
      onClose();
      navigate('/agents/new');
    } catch {
      // validation failure
    }
  };

  const handleApply = async () => {
    if (!applyAgentId || !filterConfig) return;
    const target = channelAgents.find((a) => a.id === applyAgentId);
    if (!target) return;
    setApplying(true);
    try {
      const merged = mergeFilters(target.filter_config, filterConfig);
      const res = await agentsApi.update(applyAgentId, {
        name: target.name,
        channel_id: target.channel_id,
        downloader_id: target.downloader_id,
        filter_config: merged,
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
      width={680}
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
      ) : !filterConfig ? (
        <Empty description={t('filter.noCommonFeatures')} />
      ) : (
        <div>
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
              {t('filter.suggestedRules')}
            </Typography.Text>
            <FilterBuilder value={filterConfig} onChange={setFilterConfig} />
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
                  <Button onClick={onClose}>{t('common.cancel')}</Button>
                  <Button type="primary" onClick={handleCreateFromHere}>
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
                      <Button onClick={onClose}>{t('common.cancel')}</Button>
                      <Button
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
