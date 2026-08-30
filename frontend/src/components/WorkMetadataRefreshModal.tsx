import { useEffect, useState } from 'react';
import { Alert, App, Button, Checkbox, Empty, Input, Modal, Select, Space, Spin, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { metadataApi } from '../api/metadata';
import type { MetadataChange, MetadataSourceOption } from '../api/metadata';
import type { MetadataCandidate, MetadataSource } from '../types';

interface Props {
  open: boolean;
  workId: string;
  contentType: 'tv' | 'movie';
  initialQuery: string;
  existingSource?: string | null;
  onClose: () => void;
  onApplied: () => Promise<void> | void;
}

export default function WorkMetadataRefreshModal({
  open,
  workId,
  contentType,
  initialQuery,
  existingSource,
  onClose,
  onApplied,
}: Props) {
  const { message } = App.useApp();
  const [query, setQuery] = useState(initialQuery);
  const [source, setSource] = useState<MetadataSource | null>(null);
  const [sources, setSources] = useState<MetadataSourceOption[]>([]);
  const [trustedSites, setTrustedSites] = useState<string[]>([]);
  const [trustedOptions, setTrustedOptions] = useState<{ value: string; label: string }[]>([]);
  const [candidates, setCandidates] = useState<MetadataCandidate[]>([]);
  const [selected, setSelected] = useState<MetadataCandidate | null>(null);
  const [changes, setChanges] = useState<MetadataChange[]>([]);
  const [overrideManual, setOverrideManual] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    setQuery(initialQuery);
    setCandidates([]);
    setSelected(null);
    setChanges([]);
    setOverrideManual(false);
    void metadataApi.sources().then((result) => {
      if (!result.success) return;
      const available = result.data.primary_sources.filter((item) => item.available);
      setSources(result.data.primary_sources);
      const preferred = available.find((item) => item.value === existingSource)?.value;
      setSource(preferred ?? available[0]?.value ?? null);
      setTrustedSites(result.data.default_trusted_sites);
      setTrustedOptions(result.data.trusted_sites.map((site) => ({
        value: site.value,
        label: `${site.value} (${site.domains.join(', ')})`,
      })));
    });
  }, [existingSource, initialQuery, open]);

  const search = async () => {
    if (!query.trim() || !source) return;
    setLoading(true);
    try {
      const result = await metadataApi.search({
        query: query.trim(), content_type: contentType, mode: 'online', source,
        trusted_sites: trustedSites,
      });
      if (!result.success) {
        message.error(result.error?.message || '元数据搜索失败');
        return;
      }
      setCandidates(result.data.candidates);
      setSelected(null);
      setChanges([]);
    } finally {
      setLoading(false);
    }
  };

  const choose = async (candidate: MetadataCandidate) => {
    setSelected(candidate);
    setLoading(true);
    try {
      const result = await metadataApi.preview({
        id: workId, content_type: contentType, candidate,
        override_manual_edits: overrideManual,
      });
      if (result.success) setChanges(result.data.changes);
      else message.error(result.error?.message || '无法预览元数据差异');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (selected) void choose(selected);
    // Recompute authoritative protection state when the option changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overrideManual]);

  const apply = async () => {
    if (!selected) return;
    setLoading(true);
    try {
      const result = await metadataApi.apply({
        id: workId, content_type: contentType, candidate: selected,
        override_manual_edits: overrideManual,
      });
      if (!result.success) {
        message.error(result.error?.message || '元数据更新失败');
        return;
      }
      message.success(`已更新 ${result.data.applied.length} 个字段`);
      await onApplied();
      onClose();
    } finally {
      setLoading(false);
    }
  };

  const columns: ColumnsType<MetadataChange> = [
    { title: '字段', dataIndex: 'field', width: 150 },
    { title: '当前值', dataIndex: 'current', render: (v) => String(v ?? '—') },
    { title: '候选值', dataIndex: 'incoming', render: (v) => String(v ?? '—') },
    { title: '操作', dataIndex: 'action', width: 100, render: (_, row) => (
      <Tag color={row.action === 'update' ? 'blue' : 'gold'}>
        {row.action === 'update' ? '更新' : '人工保护'}
      </Tag>
    ) },
  ];

  return (
    <Modal open={open} title="搜索并刷新元数据" width={860} onCancel={onClose}
      footer={selected ? [
        <Button key="back" onClick={() => { setSelected(null); setChanges([]); }}>返回候选</Button>,
        <Button key="apply" type="primary" loading={loading} onClick={() => void apply()}>确认应用</Button>,
      ] : null}>
      <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
        <Input value={query} onChange={(event) => setQuery(event.target.value)} onPressEnter={() => void search()} />
        <Select value={source} onChange={setSource} style={{ width: 160 }} options={sources.map((item) => ({
          value: item.value, label: item.label, disabled: !item.available,
        }))} />
        <Button type="primary" loading={loading} disabled={!source} onClick={() => void search()}>搜索</Button>
      </Space.Compact>
      <Select mode="multiple" value={trustedSites} onChange={setTrustedSites} options={trustedOptions}
        placeholder="可信站点（清空即禁用全网回退）" style={{ width: '100%', marginBottom: 12 }} />
      {selected ? (
        <>
          <Alert type="info" showIcon message={`候选：${selected.title_cn || selected.original_title || selected.title_en}`}
            description={`身份：${selected.identity_source}:${selected.external_id}`} style={{ marginBottom: 12 }} />
          <Checkbox checked={overrideManual} onChange={(event) => setOverrideManual(event.target.checked)}>
            覆盖人工编辑字段
          </Checkbox>
          <Table rowKey="field" size="small" pagination={false} columns={columns} dataSource={changes} style={{ marginTop: 12 }} />
        </>
      ) : loading ? <Spin /> : candidates.length ? (
        <Space direction="vertical" style={{ width: '100%' }}>
          {candidates.map((candidate, index) => (
            <Alert key={`${candidate.external_id}-${index}`} type={candidate.selectable ? 'info' : 'warning'}
              message={candidate.title_cn || candidate.original_title || candidate.title_en || candidate.external_id}
              description={`${candidate.year ?? ''} · ${candidate.identity_source ?? '无可信身份'} · ${candidate.match_path}`}
              action={<Button disabled={!candidate.selectable} onClick={() => void choose(candidate)}>选择</Button>} />
          ))}
        </Space>
      ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="搜索后选择一个候选" />}
    </Modal>
  );
}
