import { useEffect, useState } from 'react';
import { App, Divider, Form, InputNumber, Modal, Select, Switch } from 'antd';
import { useTranslation } from 'react-i18next';
import { resourcesApi } from '../api/channels';
import { seriesApi } from '../api/series';
import { moviesApi } from '../api/movies';
import type { FileResource, Movie, ResourceCorrectionBody, TVSeries } from '../types';

type Work = TVSeries | Movie;

interface ResourceCorrectionModalProps {
  resource: FileResource | null;
  open: boolean;
  onClose: () => void;
  onSaved?: (updated: FileResource) => void;
}

/** Manual correction of a resource's parsed episode/batch fields
 * (PATCH /resources/{id}). When the resource is linked to a work, the work's
 * tri-state is_anime and content_type can be corrected in the same save
 * (PUT /series|movies/{id}, changed fields only). */
export default function ResourceCorrectionModal({
  resource,
  open,
  onClose,
  onSaved,
}: ResourceCorrectionModalProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [work, setWork] = useState<Work | null>(null);
  const isBatch = Form.useWatch('is_batch', form) ?? false;

  const workKind: 'series' | 'movie' | null = resource?.series_id
    ? 'series'
    : resource?.movie_id
      ? 'movie'
      : null;

  useEffect(() => {
    if (!open || !resource) return;
    form.setFieldsValue({
      season: resource.season ?? null,
      episode: resource.episode ?? null,
      absolute_episode: resource.absolute_episode ?? null,
      episode_start: resource.episode_start ?? null,
      episode_end: resource.episode_end ?? null,
      is_batch: resource.is_batch,
      batch_scope: resource.batch_scope ?? null,
    });
    setWork(null);
    // Prefill the linked-work section from the full work row — the resource
    // payload's embedded work ref doesn't carry content_type.
    const workId = resource.series_id || resource.movie_id;
    if (!workId) return;
    const load = resource.series_id ? seriesApi.get : moviesApi.get;
    load(workId).then((res) => {
      if (res.success) {
        setWork(res.data as Work);
        form.setFieldsValue({
          is_anime: (res.data as Work).is_anime ?? null,
          content_type: (res.data as Work).content_type ?? null,
        });
      }
    });
  }, [open, resource, form]);

  const handleBatchToggle = (checked: boolean) => {
    // Batch resources have no single episode number; single-episode resources
    // have no batch scope/range. Clear the mutually exclusive group.
    if (checked) {
      form.setFieldsValue({ episode: null, absolute_episode: null });
    } else {
      form.setFieldsValue({ batch_scope: null, episode_start: null, episode_end: null });
    }
  };

  const submit = async () => {
    if (!resource) return;
    const values = await form.validateFields();
    setSaving(true);
    try {
      // 1) Linked work fields first (only the ones that actually changed).
      let changed = false;
      if (work && workKind) {
        const workPayload: Record<string, unknown> = {};
        const nextAnime = values.is_anime ?? null;
        if (nextAnime !== (work.is_anime ?? null)) workPayload.is_anime = nextAnime;
        const nextType = values.content_type ?? null;
        if (nextType !== (work.content_type ?? null)) workPayload.content_type = nextType;
        if (Object.keys(workPayload).length > 0) {
          const res =
            workKind === 'series'
              ? await seriesApi.update(work.id, workPayload)
              : await moviesApi.update(work.id, workPayload);
          if (!res.success) {
            message.error(res.error?.message || t('resource.correctSaveFailed'));
            return;
          }
          changed = true;
        }
      }

      // 2) Resource parse fields (only the ones that actually changed).
      const payload: ResourceCorrectionBody = {};
      const numField = (
        key: 'season' | 'episode' | 'absolute_episode' | 'episode_start' | 'episode_end',
      ) => {
        const next = (values[key] ?? null) as number | null;
        if (next !== (resource[key] ?? null)) payload[key] = next;
      };
      numField('season');
      numField('episode');
      numField('absolute_episode');
      numField('episode_start');
      numField('episode_end');
      if (values.is_batch !== resource.is_batch) payload.is_batch = values.is_batch;
      const nextScope = (values.batch_scope ?? null) as ResourceCorrectionBody['batch_scope'];
      if (nextScope !== (resource.batch_scope ?? null)) payload.batch_scope = nextScope;

      let updated = resource;
      if (Object.keys(payload).length > 0) {
        const res = await resourcesApi.correctParseFields(resource.id, payload);
        if (!res.success) {
          message.error(res.error?.message || t('resource.correctSaveFailed'));
          return;
        }
        updated = res.data;
        changed = true;
      }

      if (!changed) {
        // Nothing differed from the resource's current values, so no request
        // was sent — say so explicitly; silently closing reads as "saved" and
        // leaves the user wondering why the todo item is still there.
        message.info(t('resource.noChanges'));
        onClose();
        return;
      }
      message.success(t('resource.correctSaved'));
      onSaved?.(updated);
      onClose();
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      title={t('resource.correctTitle')}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      onOk={submit}
      onCancel={onClose}
      destroyOnHidden
      width={480}
    >
      <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
        <Form.Item name="is_batch" label={t('resource.isBatch')} valuePropName="checked">
          <Switch onChange={handleBatchToggle} />
        </Form.Item>
        <Form.Item name="season" label={t('resource.seasonLabel')}>
          <InputNumber min={0} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item name="episode" label={t('resource.episodePerSeasonLabel')}>
          <InputNumber min={0} disabled={isBatch} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item name="absolute_episode" label={t('resource.absoluteEpisodePlaceholder')}>
          <InputNumber min={0} disabled={isBatch} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item name="episode_start" label={t('resource.episodeStart')}>
          <InputNumber min={0} disabled={!isBatch} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item name="episode_end" label={t('resource.episodeEnd')}>
          <InputNumber min={0} disabled={!isBatch} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item name="batch_scope" label={t('resource.batchScope')}>
          <Select
            allowClear
            disabled={!isBatch}
            options={[
              { value: 'season', label: t('channels.batch') },
              { value: 'multi_season', label: t('channels.batchMultiSeason') },
              { value: 'franchise', label: t('channels.batchFranchise') },
            ]}
          />
        </Form.Item>

        {workKind && (
          <>
            <Divider style={{ margin: '8px 0 12px' }} />
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
              {t('resource.linkedWork')}
            </div>
            <Form.Item name="is_anime" label={t('works.animeStatus')}>
              <Select
                allowClear
                placeholder={t('common.unknown')}
                options={[
                  { value: true, label: t('works.anime') },
                  { value: false, label: t('works.liveAction') },
                ]}
              />
            </Form.Item>
            <Form.Item name="content_type" label={t('resource.contentType')}>
              <Select
                allowClear
                placeholder={t('common.unknown')}
                options={[
                  { value: 'tv', label: t('works.tv') },
                  { value: 'movie', label: t('works.movie') },
                ]}
              />
            </Form.Item>
          </>
        )}
      </Form>
    </Modal>
  );
}
