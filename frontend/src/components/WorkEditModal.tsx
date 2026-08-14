import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import type { Dayjs } from 'dayjs';
import { App, DatePicker, Form, Input, InputNumber, Modal, Select } from 'antd';
import { seriesApi } from '../api/series';
import { moviesApi } from '../api/movies';
import { GENRE_NAMES, genreSlug } from '../constants/genres';
import type { Movie, TVSeries } from '../types';

type Work = TVSeries | Movie;
type ContentType = 'tv' | 'movie';

const GENRE_OPTIONS = GENRE_NAMES.map((v) => ({
  value: v,
  label: v,
}));

function normText(v: string | null | undefined): string | null {
  return v?.trim() ? v.trim() : null;
}

function normDate(v: Dayjs | string | null | undefined): string | null {
  if (!v) return null;
  return dayjs(v).format('YYYY-MM-DD');
}

/** Work detail "编辑" entry — one form for all human-editable metadata fields.
 *
 *  Only fields whose value actually changed are sent, and the backend records
 *  those in ``manually_edited_fields`` so automatic metadata scans stop
 *  overwriting them (until the refresh dialog opts into overriding).
 */
export default function WorkEditModal({
  open,
  work,
  contentType,
  onClose,
  onSaved,
}: {
  open: boolean;
  work: Work | null;
  contentType: ContentType;
  onClose: () => void;
  onSaved: (updated: Work) => void;
}) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open || !work) return;
    form.setFieldsValue({
      title_cn: work.title_cn ?? '',
      title_en: work.title_en ?? '',
      original_title: work.original_title ?? '',
      description: work.description ?? '',
      status: work.status ?? '',
      rating: work.rating ?? null,
      genre: work.genre ?? [],
      is_anime: work.is_anime ?? null,
      ...(contentType === 'tv'
        ? {
            number_of_seasons: (work as TVSeries).number_of_seasons ?? null,
            number_of_episodes: (work as TVSeries).number_of_episodes ?? null,
            start_date: (work as TVSeries).start_date
              ? dayjs((work as TVSeries).start_date)
              : null,
            end_date: (work as TVSeries).end_date
              ? dayjs((work as TVSeries).end_date)
              : null,
          }
        : {
            release_date: (work as Movie).release_date
              ? dayjs((work as Movie).release_date)
              : null,
            runtime: (work as Movie).runtime ?? null,
          }),
    });
  }, [open, work, contentType, form]);

  const submit = async () => {
    if (!work) return;
    const values = await form.validateFields();
    const payload: Record<string, unknown> = {};
    const put = (key: string, value: unknown) => {
      payload[key] = value;
    };

    const text = (key: keyof Work) => {
      const next = normText(values[key] as string | null | undefined);
      const orig = normText(work[key] as string | null | undefined);
      if (next !== orig) put(key as string, next);
    };
    text('title_cn');
    text('title_en');
    text('original_title');
    text('description');
    text('status');

    if ((values.rating ?? null) !== (work.rating ?? null)) {
      put('rating', values.rating ?? null);
    }
    const nextGenre = (values.genre ?? []).slice().sort();
    const origGenre = (work.genre ?? []).slice().sort();
    if (JSON.stringify(nextGenre) !== JSON.stringify(origGenre)) {
      put('genre', values.genre ?? null);
    }
    const nextAnime = values.is_anime === undefined ? null : values.is_anime;
    if ((nextAnime ?? null) !== (work.is_anime ?? null)) {
      put('is_anime', nextAnime);
    }

    if (contentType === 'tv') {
      const s = work as TVSeries;
      if ((values.number_of_seasons ?? null) !== (s.number_of_seasons ?? null)) {
        put('number_of_seasons', values.number_of_seasons ?? null);
      }
      if ((values.number_of_episodes ?? null) !== (s.number_of_episodes ?? null)) {
        put('number_of_episodes', values.number_of_episodes ?? null);
      }
      const start = normDate(values.start_date);
      if (start !== (s.start_date ?? null)) put('start_date', start);
      const end = normDate(values.end_date);
      if (end !== (s.end_date ?? null)) put('end_date', end);
    } else {
      const m = work as Movie;
      const release = normDate(values.release_date);
      if (release !== (m.release_date ?? null)) put('release_date', release);
      if ((values.runtime ?? null) !== (m.runtime ?? null)) {
        put('runtime', values.runtime ?? null);
      }
    }

    if (Object.keys(payload).length === 0) {
      onClose();
      return;
    }

    setSaving(true);
    const res =
      contentType === 'tv'
        ? await seriesApi.update(work.id, payload)
        : await moviesApi.update(work.id, payload);
    setSaving(false);
    if (res.success) {
      message.success(t('works.editSaved'));
      onSaved(res.data as Work);
      onClose();
    } else {
      message.error(res.error?.message || t('works.editSaveFailed'));
    }
  };

  const titleLabel = (key: string) => t(contentType === 'tv' ? `series.${key}` : `movies.${key}`);

  return (
    <Modal
      open={open}
      title={t('works.editWork')}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      onOk={submit}
      onCancel={onClose}
      destroyOnHidden
      width={560}
    >
      <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
        <Form.Item name="title_cn" label={titleLabel('cnTitle')}>
          <Input maxLength={512} />
        </Form.Item>
        <Form.Item name="title_en" label={titleLabel('enTitle')}>
          <Input maxLength={512} />
        </Form.Item>
        <Form.Item name="original_title" label={titleLabel('originalTitle')}>
          <Input maxLength={512} />
        </Form.Item>
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
        <Form.Item name="genre" label={t('works.editGenre')}>
          <Select
            mode="multiple"
            allowClear
            placeholder={t('common.unknown')}
            options={GENRE_OPTIONS.map((g) => ({
              value: g.value,
              label: t(`genre.${genreSlug(g.value)}` as never, { defaultValue: g.value }),
            }))}
          />
        </Form.Item>
        <Form.Item name="status" label={t('common.status')}>
          <Input maxLength={100} />
        </Form.Item>
        <Form.Item name="rating" label={titleLabel('rating')}>
          <InputNumber min={0} max={10} step={0.1} style={{ width: '100%' }} />
        </Form.Item>
        {contentType === 'tv' ? (
          <>
            <Form.Item name="number_of_seasons" label={t('works.editSeasons')}>
              <InputNumber min={0} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="number_of_episodes" label={t('works.editEpisodes')}>
              <InputNumber min={0} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="start_date" label={t('works.editStartDate')}>
              <DatePicker style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="end_date" label={t('works.editEndDate')}>
              <DatePicker style={{ width: '100%' }} />
            </Form.Item>
          </>
        ) : (
          <>
            <Form.Item name="release_date" label={t('works.editReleaseDate')}>
              <DatePicker style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="runtime" label={t('works.editRuntime')}>
              <InputNumber min={0} style={{ width: '100%' }} />
            </Form.Item>
          </>
        )}
        <Form.Item name="description" label={t('works.editDescription')}>
          <Input.TextArea rows={3} maxLength={2048} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
