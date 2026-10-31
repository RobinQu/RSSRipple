import { AutoComplete, DatePicker, Input, InputNumber, Select, Typography } from 'antd';
import dayjs from 'dayjs';
import { useTranslation } from 'react-i18next';
import { GENRE_NAMES, genreSlug } from '../../constants/genres';
import { workKeyOf } from '../wizardValidation';
import {
  DIRECT_METADATA_FIELD_KEYS,
  LANG_PRESETS,
  MEDIA_TEXT_KEYS,
  mediaLabelI18nKey,
  missingWorkMetadataFields,
  workMetadataLabelKey,
  type WorkMetadataFieldKey,
} from './types';
import type { ResourceEditWizardState } from './useResourceEditWizard';

const { Text } = Typography;

/** Step ③ — generic media fields (dropdowns fed by system-observed values). */
export default function StepMedia({
  wiz,
  alert,
  compact,
}: {
  wiz: ResourceEditWizardState;
  alert: React.ReactNode;
  compact: boolean;
}) {
  const { t } = useTranslation();
  const {
    detail,
    works,
    directMetadata,
    setDirectMetadata,
    workFieldsTarget,
    workMetadata,
    setWorkMetadata,
    media,
    setMedia,
    mediaOptions,
    subtitleLangs,
    setSubtitleLangs,
  } = wiz;

  if (!detail) return null;

  const workTargetActive = workFieldsTarget && works.some((w) =>
    workKeyOf(w.work_type, w.work_id) ===
    workKeyOf(workFieldsTarget.work_type, workFieldsTarget.work_id)
  );
  const missingWorkFields = missingWorkMetadataFields(
    detail.missing_fields ?? [], Boolean(workTargetActive),
  );
  const showWorkField = (key: WorkMetadataFieldKey) => missingWorkFields.includes(key);

  return (
    <>
      {alert}
      {DIRECT_METADATA_FIELD_KEYS.filter((k) =>
        (detail.missing_fields ?? []).includes(k),
      ).map((k) => (
        <div key={k} style={{ marginBottom: 12 }}>
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
            {t(mediaLabelI18nKey(k))}
          </Text>
          <Input
            value={directMetadata[k]}
            onChange={(event) => setDirectMetadata((prev) => ({
              ...prev, [k]: event.target.value,
            }))}
            status={directMetadata[k].trim() ? undefined : 'error'}
          />
        </div>
      ))}
      {missingWorkFields.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: compact ? '1fr' : '1fr 1fr', gap: 12, marginBottom: 12 }}>
          {showWorkField('year') && (
            <div>
              <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                {t(`resource.${workMetadataLabelKey('year')}`)}
              </Text>
              <DatePicker
                picker="year"
                style={{ width: '100%' }}
                value={workMetadata.date ? dayjs(workMetadata.date) : null}
                onChange={(value) => setWorkMetadata((prev) => ({
                  ...prev, date: value ? value.format('YYYY') : null,
                }))}
                status={workMetadata.date ? undefined : 'error'}
              />
            </div>
          )}
          {showWorkField('rating') && (
            <div>
              <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                {t(`resource.${workMetadataLabelKey('rating')}`)}
              </Text>
              <InputNumber
                style={{ width: '100%' }}
                min={0}
                max={10}
                step={0.1}
                value={workMetadata.rating}
                onChange={(value) => setWorkMetadata((prev) => ({
                  ...prev, rating: typeof value === 'number' ? value : null,
                }))}
                status={workMetadata.rating == null ? 'error' : undefined}
              />
            </div>
          )}
          {showWorkField('is_anime') && (
            <div>
              <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                {t('works.animeStatus')}
              </Text>
              <Select
                style={{ width: '100%' }}
                allowClear
                value={workMetadata.is_anime}
                onChange={(value?: boolean) => setWorkMetadata((prev) => ({
                  ...prev, is_anime: value ?? null,
                }))}
                options={[
                  { value: true, label: t('works.anime') },
                  { value: false, label: t('works.liveAction') },
                ]}
                status={workMetadata.is_anime == null ? 'error' : undefined}
              />
            </div>
          )}
          {showWorkField('genre') && (
            <div>
              <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                {t('works.editGenre')}
              </Text>
              <Select
                mode="multiple"
                style={{ width: '100%' }}
                value={workMetadata.genre}
                onChange={(value: string[]) => setWorkMetadata((prev) => ({ ...prev, genre: value }))}
                options={GENRE_NAMES.map((genre) => ({
                  value: genre,
                  label: t(`genre.${genreSlug(genre)}`, { defaultValue: genre }),
                }))}
                status={workMetadata.genre.length ? undefined : 'error'}
              />
            </div>
          )}
        </div>
      )}
      <div style={{ display: 'grid', gridTemplateColumns: compact ? '1fr' : '1fr 1fr', gap: 12 }}>
        {MEDIA_TEXT_KEYS.map((k) => (
          <div key={k}>
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
              {t(mediaLabelI18nKey(k))}
            </Text>
            <AutoComplete
              style={{ width: '100%' }}
              value={media[k]}
              onChange={(v) => setMedia((prev) => ({ ...prev, [k]: v }))}
              options={(mediaOptions[k] ?? []).map((v) => ({ value: v }))}
              filterOption={(input, opt) =>
                String(opt?.value ?? '')
                  .toLowerCase()
                  .includes(input.toLowerCase())
              }
              allowClear
            />
          </div>
        ))}
        <div>
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
            {t('resource.subtitleLangs')}
          </Text>
          <Select
            mode="tags"
            style={{ width: '100%' }}
            value={subtitleLangs}
            onChange={setSubtitleLangs}
            options={LANG_PRESETS.map((l) => ({ value: l, label: l === 'multi' ? t('channels.langMulti') : l }))}
            tokenSeparators={[',']}
          />
        </div>
      </div>
    </>
  );
}
