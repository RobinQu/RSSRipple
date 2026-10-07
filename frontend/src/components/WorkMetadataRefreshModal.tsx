import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
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
  const { t } = useTranslation();
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
        message.error(result.error?.message || t('metadataRefresh.searchFailed'));
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
      else message.error(result.error?.message || t('metadataRefresh.previewFailed'));
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
        message.error(result.error?.message || t('metadataRefresh.applyFailed'));
        return;
      }
      message.success(t('metadataRefresh.applied', { count: result.data.applied.length }));
      await onApplied();
      onClose();
    } finally {
      setLoading(false);
    }
  };

  const columns: ColumnsType<MetadataChange> = [
    { title: t('metadataRefresh.field'), dataIndex: 'field', width: 150 },
    { title: t('metadataRefresh.current'), dataIndex: 'current', render: (v) => String(v ?? '—') },
    { title: t('metadataRefresh.incoming'), dataIndex: 'incoming', render: (v) => String(v ?? '—') },
    { title: t('common.operation'), dataIndex: 'action', width: 100, render: (_, row) => (
      <Tag color={row.action === 'update' ? 'blue' : 'gold'}>
        {row.action === 'update' ? t('metadataRefresh.update') : t('metadataRefresh.protected')}
      </Tag>
    ) },
  ];

  return (
    <Modal open={open} title={t('metadataRefresh.title')} width={860} onCancel={onClose}
      footer={selected ? [
        <Button key="back" onClick={() => { setSelected(null); setChanges([]); }}>{t('metadataRefresh.back')}</Button>,
        <Button key="apply" type="primary" loading={loading} onClick={() => void apply()}>{t('metadataRefresh.apply')}</Button>,
      ] : null}>
      <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
        <Input value={query} onChange={(event) => setQuery(event.target.value)} onPressEnter={() => void search()} />
        <Select value={source} onChange={setSource} style={{ width: 160 }} options={sources.map((item) => ({
          value: item.value, label: item.label, disabled: !item.available,
        }))} />
        <Button type="primary" loading={loading} disabled={!source} onClick={() => void search()}>{t('common.search')}</Button>
      </Space.Compact>
      <Select mode="multiple" value={trustedSites} onChange={setTrustedSites} options={trustedOptions}
        placeholder={t('metadataRefresh.trustedSites')} style={{ width: '100%', marginBottom: 12 }} />
      {selected ? (
        <>
          <Alert type="info" showIcon message={t('metadataRefresh.candidate', { title: selected.title_cn || selected.original_title || selected.title_en })}
            description={t('metadataRefresh.identity', { source: selected.identity_source, id: selected.external_id })} style={{ marginBottom: 12 }} />
          <Checkbox checked={overrideManual} onChange={(event) => setOverrideManual(event.target.checked)}>
            {t('metadataRefresh.overrideManual')}
          </Checkbox>
          <Table rowKey="field" size="small" pagination={false} columns={columns} dataSource={changes} style={{ marginTop: 12 }} />
        </>
      ) : loading ? <Spin /> : candidates.length ? (
        <Space direction="vertical" style={{ width: '100%' }}>
          {candidates.map((candidate, index) => (
            <Alert key={`${candidate.external_id}-${index}`} type={candidate.selectable ? 'info' : 'warning'}
              message={candidate.title_cn || candidate.original_title || candidate.title_en || candidate.external_id}
              description={`${candidate.year ?? ''} · ${candidate.identity_source ?? t('metadataRefresh.noIdentity')} · ${candidate.match_path}`}
              action={<Button disabled={!candidate.selectable} onClick={() => void choose(candidate)}>{t('metadataRefresh.select')}</Button>} />
          ))}
        </Space>
      ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('metadataRefresh.empty')} />}
    </Modal>
  );
}
