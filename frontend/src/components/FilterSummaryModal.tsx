import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
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
          message.error(filterRes.error?.message || '生成过滤规则失败');
        }
        if (agentRes.success) {
          setChannelAgents(agentRes.data.filter((a) => a.channel_id === channelId));
        }
      })
      .finally(() => setLoading(false));
  }, [open, channelId, selectedIds, form, message]);

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
        message.success('已追加过滤规则到 Agent');
        onClose();
      } else {
        message.error(res.error?.message || '应用失败');
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
          <span>生成过滤规则</span>
          <Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
            已选 {selectedIds.length} 个资源
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
            正在分析 {selectedIds.length} 个资源...
          </div>
        </div>
      ) : !filterConfig ? (
        <Empty description="未发现共同特征" />
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
              建议过滤规则（可编辑）
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
                    <span>新建 Agent</span>
                  </Space>
                ),
                value: 'create',
              },
              {
                label: (
                  <Space size={4}>
                    <ListFilter />
                    <span>应用到已有 Agent</span>
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
                label="Agent 名称"
                rules={[{ required: true, message: '请输入名称' }]}
              >
                <Input placeholder="例如：新番 1080p 自动下载" autoFocus />
              </Form.Item>
              <div style={{ textAlign: 'right' }}>
                <Space>
                  <Button onClick={onClose}>取消</Button>
                  <Button type="primary" onClick={handleCreateFromHere}>
                    创建 Agent 并配置
                  </Button>
                </Space>
              </div>
              <Typography.Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 8 }}>
                将跳转至 Agent 创建页，过滤规则已预填，选择下载器后即可完成创建。
              </Typography.Text>
            </Form>
          ) : (
            <div>
              {channelAgents.length === 0 ? (
                <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                  当前频道下暂无 Agent，请选择"新建 Agent"。
                </Typography.Text>
              ) : (
                <>
                  <Typography.Text style={{ fontSize: 12, color: '#93939f', display: 'block', marginBottom: 6 }}>
                    选择目标 Agent
                  </Typography.Text>
                  <Select
                    options={channelAgents.map((a) => ({ label: a.name, value: a.id }))}
                    value={applyAgentId}
                    onChange={setApplyAgentId}
                    placeholder="选择 Agent..."
                    style={{ width: '100%' }}
                  />
                  {applyAgentId && (
                    <Typography.Text style={{ fontSize: 12, color: '#93939f', display: 'block', marginTop: 8 }}>
                      新规则将与现有规则按 AND 合并。
                    </Typography.Text>
                  )}
                  <div style={{ textAlign: 'right', marginTop: 16 }}>
                    <Space>
                      <Button onClick={onClose}>取消</Button>
                      <Button
                        type="primary"
                        loading={applying}
                        disabled={!applyAgentId}
                        onClick={handleApply}
                      >
                        应用规则
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
