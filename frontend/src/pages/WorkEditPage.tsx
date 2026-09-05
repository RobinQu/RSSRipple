import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import dayjs from 'dayjs';
import type { Dayjs } from 'dayjs';
import {
  Alert,
  App,
  Button,
  Card,
  DatePicker,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Spin,
  Typography,
} from 'antd';
import { ArrowLeft } from 'lucide-react';
import useDocumentTitle from '../hooks/useDocumentTitle';
import { seriesApi } from '../api/series';
import { moviesApi } from '../api/movies';
import { GENRE_NAMES, genreSlug } from '../constants/genres';
import { seasonLabel } from '../utils/season';
import type { Movie, TVSeries } from '../types';

const { Title } = Typography;

type Work = TVSeries | Movie;
type ContentType = 'tv' | 'movie';

function normText(v: string | null | undefined): string | null {
  return v?.trim() ? v.trim() : null;
}

function normDate(v: Dayjs | string | null | undefined): string | null {
  if (!v) return null;
  return dayjs(v).format('YYYY-MM-DD');
}

/** Full-page work editor (series/movie shared, routed by content type).
 *  Only changed fields are submitted, so the backend records exactly those in
 *  ``manually_edited_fields`` and automatic metadata scans keep skipping them.
 *  Replaces the old WorkEditModal on the detail pages. */
export default function WorkEditPage({ contentType }: { contentType: ContentType }) {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [work, setWork] = useState<Work | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  useDocumentTitle(t('works.editWork'));

  const detailPath = `/${contentType === 'tv' ? 'series' : 'movies'}/${id}`;

  useEffect(() => {
    if (!id) return;
    let active = true;
    const load = contentType === 'tv' ? seriesApi.get : moviesApi.get;
    load(id)
      .then((res) => {
        if (active && res.success) setWork(res.data as Work);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [id, contentType]);

  if (loading) {
    return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  }
  if (!work) {
    return <Typography.Text type="danger">{t('works.loadFailed')}</Typography.Text>;
  }

  const initialValues = {
    title_cn: work.title_cn ?? '',
    title_en: work.title_en ?? '',
    original_title: work.original_title ?? '',
    aliases: work.aliases ?? [],
    description: work.description ?? '',
    poster_url: work.poster_url ?? '',
    genre: work.genre ?? [],
    status: work.status ?? '',
    rating: work.rating ?? null,
    content_type: work.content_type ?? null,
    is_anime: work.is_anime ?? null,
    external_id: work.external_id ?? '',
    external_source: work.external_source ?? '',
    ...(contentType === 'tv'
      ? {
          number_of_episodes: (work as TVSeries).number_of_episodes ?? null,
          start_date: (work as TVSeries).start_date ? dayjs((work as TVSeries).start_date) : null,
          end_date: (work as TVSeries).end_date ? dayjs((work as TVSeries).end_date) : null,
        }
      : {
          release_date: (work as Movie).release_date ? dayjs((work as Movie).release_date) : null,
          runtime: (work as Movie).runtime ?? null,
        }),
  };

  const submit = async () => {
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
    text('poster_url');
    text('status');
    text('external_id');
    text('external_source');

    const list = (key: 'aliases' | 'genre') => {
      const next = (values[key] ?? []) as string[];
      const orig = work[key] ?? [];
      if (JSON.stringify(next) !== JSON.stringify(orig)) put(key, next);
    };
    list('aliases');
    list('genre');

    if ((values.rating ?? null) !== (work.rating ?? null)) {
      put('rating', values.rating ?? null);
    }
    const nextType = values.content_type ?? null;
    if (nextType !== (work.content_type ?? null)) put('content_type', nextType);
    const nextAnime = values.is_anime === undefined ? null : values.is_anime;
    if ((nextAnime ?? null) !== (work.is_anime ?? null)) put('is_anime', nextAnime);

    if (contentType === 'tv') {
      const s = work as TVSeries;
      // season_number is an identity attribute (a work IS one season) —
      // read-only here; number_of_seasons is retired (legacy orphan column).
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
      navigate(detailPath);
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
      navigate(detailPath);
    } else {
      message.error(res.error?.message || t('works.editSaveFailed'));
    }
  };

  const titleLabel = (key: string) => t(contentType === 'tv' ? `series.${key}` : `movies.${key}`);

  return (
    <div style={{ maxWidth: 720 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 24 }}>
        <Link to={detailPath}>
          <Button type="text" icon={<ArrowLeft size={18} />} />
        </Link>
        <Title level={3} style={{ margin: 0 }}>
          {t('works.editWork')}
          {' · '}
          {work.title_cn || work.title_en || work.original_title}
        </Title>
      </div>

      <Form form={form} layout="vertical" initialValues={initialValues}>
        <Card title={t('works.sectionBasic')} size="small" style={{ marginBottom: 16 }}>
          <Form.Item name="title_cn" label={titleLabel('cnTitle')}>
            <Input maxLength={512} />
          </Form.Item>
          <Form.Item name="title_en" label={titleLabel('enTitle')}>
            <Input maxLength={512} />
          </Form.Item>
          <Form.Item name="original_title" label={titleLabel('originalTitle')}>
            <Input maxLength={512} />
          </Form.Item>
          <Form.Item name="aliases" label={t('works.aliases')}>
            <Select mode="tags" allowClear open={false} placeholder={t('filter.enterValue')} />
          </Form.Item>
          <Form.Item name="description" label={t('works.editDescription')}>
            <Input.TextArea rows={3} maxLength={2048} />
          </Form.Item>
          <Form.Item name="poster_url" label={t('works.posterUrl')}>
            <Input maxLength={1024} />
          </Form.Item>
          <Form.Item name="genre" label={t('works.editGenre')}>
            <Select
              mode="multiple"
              allowClear
              placeholder={t('common.unknown')}
              options={GENRE_NAMES.map((g) => ({
                value: g,
                label: t(`genre.${genreSlug(g)}` as never, { defaultValue: g }),
              }))}
            />
          </Form.Item>
          <Form.Item name="status" label={t('common.status')}>
            <Input maxLength={100} />
          </Form.Item>
          <Form.Item name="rating" label={titleLabel('rating')}>
            <InputNumber min={0} max={10} step={0.1} style={{ width: '100%' }} />
          </Form.Item>
        </Card>

        <Card title={t('works.sectionTypeAnime')} size="small" style={{ marginBottom: 16 }}>
          <Form.Item name="content_type" label={t('works.contentType')}>
            <Select
              allowClear
              placeholder={t('common.unknown')}
              options={[
                { value: 'tv', label: t('works.tv') },
                { value: 'movie', label: t('works.movie') },
              ]}
            />
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
        </Card>

        <Card title={t('works.sectionAiring')} size="small" style={{ marginBottom: 16 }}>
          {contentType === 'tv' ? (
            <>
              {/* season_number is an identity attribute — display only. */}
              <Form.Item label={t('works.seasonNumber')}>
                <Typography.Text>
                  {(work as TVSeries).season_number != null
                    ? seasonLabel(t, (work as TVSeries).season_number)
                    : t('common.unknown')}
                </Typography.Text>
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
        </Card>

        <Card title={t('works.sectionExternal')} size="small" style={{ marginBottom: 16 }}>
          <Alert
            type="warning"
            showIcon
            message={t('works.externalManagedHint')}
            style={{ marginBottom: 12 }}
          />
          <Form.Item name="external_id" label={t('works.externalId')}>
            <Input maxLength={512} />
          </Form.Item>
          <Form.Item name="external_source" label={t('works.externalSource')}>
            <Input maxLength={100} />
          </Form.Item>
        </Card>

        <Space style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <Button onClick={() => navigate(detailPath)}>{t('common.cancel')}</Button>
          <Button type="primary" loading={saving} onClick={submit}>
            {t('common.save')}
          </Button>
        </Space>
      </Form>
    </div>
  );
}
