import {
  Alert,
  Button,
  Divider,
  Input,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from 'antd';
import { Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { seasonLabel } from '../../utils/season';
import { workKeyOf } from '../wizardValidation';
import type { ResourceEditWizardState } from './useResourceEditWizard';

const { Text } = Typography;

/** Step ① — collection & works association (two-level for TV: the
 * collection first, then season works among its members). */
export default function StepAssociation({
  wiz,
  alert,
}: {
  wiz: ResourceEditWizardState;
  alert: React.ReactNode;
}) {
  const { t } = useTranslation();
  const {
    isBatch,
    setIsBatch,
    audioLinked,
    collectionId,
    setCollectionId,
    collections,
    collSearching,
    searchCollections,
    newCollTitle,
    setNewCollTitle,
    creatingColl,
    createCollectionAndAttach,
    works,
    workTitles,
    workSeasons,
    removeWork,
    setPickerOpen,
    deriveScopeLabel,
  } = wiz;

  return (
    <>
      {alert}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
        <Text>{t('resource.isBatch')}</Text>
        <Switch
          checked={isBatch}
          onChange={(v) => setIsBatch(v)}
          disabled={audioLinked}
        />
      </div>
      {isBatch && (
        <div
          style={{
            marginBottom: 12,
            padding: '8px 10px',
            border: '1px solid var(--rr-border-soft)',
            borderRadius: 6,
          }}
        >
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 6 }}>
            {t('resource.collectionFirstHint')}
          </Text>
          <Select
            showSearch
            allowClear
            style={{ width: '100%' }}
            placeholder={t('resource.collectionLabel')}
            value={collectionId ?? undefined}
            onSearch={(q) => void searchCollections(q)}
            filterOption={false}
            loading={collSearching}
            notFoundContent={collSearching ? <Spin size="small" /> : null}
            onChange={(v) => setCollectionId(v ?? null)}
            options={collections.map((c) => ({ value: c.id, label: c.name }))}
          />
          <Divider plain style={{ margin: '8px 0' }}>{t('resource.or')}</Divider>
          <Space.Compact style={{ width: '100%' }}>
            <Input
              value={newCollTitle}
              onChange={(e) => setNewCollTitle(e.target.value)}
              placeholder={t('resource.collectionCreatePlaceholder')}
              onPressEnter={() => void createCollectionAndAttach()}
            />
            <Button
              type="primary"
              loading={creatingColl}
              disabled={!newCollTitle.trim()}
              onClick={() => void createCollectionAndAttach()}
            >
              {t('resource.collectionCreateBtn')}
            </Button>
          </Space.Compact>
        </div>
      )}
      <div style={{ marginBottom: 6 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>{t('resource.stepWorkType')}</Text>
      </div>
      <Space wrap style={{ marginBottom: 8 }}>
        {works.map((w) => {
          const key = workKeyOf(w.work_type, w.work_id);
          const sLabel =
            w.work_type === 'series' ? seasonLabel(t, workSeasons[key]) : '';
          return (
            <Tag
              key={key}
              color={w.work_type === 'series' ? 'blue' : 'green'}
              closable={!audioLinked}
              onClose={() => removeWork(key)}
            >
              {workTitles[key] || w.work_id}
              {sLabel ? ` · ${sLabel}` : ''}
            </Tag>
          );
        })}
        <Button
          size="small"
          icon={<Plus size={13} />}
          disabled={audioLinked}
          onClick={() => setPickerOpen(true)}
        >
          {t('resource.addWork')}
        </Button>
      </Space>
      {audioLinked && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 8 }}
          message={t('resource.audioLinkHint')}
        />
      )}
      {isBatch && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t('resource.scopeAutoPreview', { scope: deriveScopeLabel })}
        </Text>
      )}
    </>
  );
}
