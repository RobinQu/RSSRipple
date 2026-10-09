import {
  Alert,
  Button,
  Empty,
  Grid,
  Popconfirm,
  Space,
  Spin,
  Steps,
  Typography,
} from 'antd';
import { RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { workKeyOf } from './wizardValidation';
import ReviewRows from './wizard/ReviewRows';
import StepAssociation from './wizard/StepAssociation';
import StepFiles from './wizard/StepFiles';
import StepMedia from './wizard/StepMedia';
import WorkPickerModal from './wizard/WorkPickerModal';
import { useResourceEditWizard } from './wizard/useResourceEditWizard';
import type { FileResource } from '../types';

const { Text } = Typography;

interface ResourceEditWizardProps {
  resourceId: string;
  initialStep?: number;
  /** Called once a save settles: updated resource on success, null when
   * nothing changed / closed without applicable changes. */
  onDone: (updated: FileResource | null) => void;
  /** Called after a background metadata reparse is successfully triggered —
   * hosts showing todo lists should refresh so the entry disappears. */
  onReparse?: () => void;
}

/** Four-step unified edit flow for a file resource (per-season works):
 * ① collection & works association (batch toggle; TV resources pick/create
 *    the collection first, then season works among its members),
 * ② file mapping (left: selectable work list; right: candidate files grouped
 *    by work_title_hint cluster — a cluster-level work pick expands to every
 *    member file, expanding a cluster exposes per-file tweaks and the
 *    shift-range/drag multi-select join into the selected work — the season
 *    comes from the work's own season_number, S/E prefilled from the
 *    deterministic name parses),
 * ③ generic media fields (dropdowns fed by system-observed values),
 * ④ confirmation review before the single PUT save.
 *
 * This component is only the render shell — all state and actions live in
 * ``wizard/useResourceEditWizard`` and each step pane under ``wizard/``. */
export default function ResourceEditWizard(props: ResourceEditWizardProps) {
  const { t } = useTranslation();
  const screens = Grid.useBreakpoint();
  const compact = !screens.lg;
  const wiz = useResourceEditWizard(props);
  const {
    loading,
    detail,
    step,
    setStep,
    saveError,
    setSaveError,
    saving,
    reparsing,
    analyzing,
    handleNext,
    handleSave,
    handleReparse,
    computeChanges,
  } = wiz;

  const panelStyle = (active: boolean) => ({
    display: active ? undefined : ('none' as const),
  });

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: 48 }}>
        <Spin />
      </div>
    );
  }
  if (!detail) {
    return <Empty description={t('resource.loadFailed')} />;
  }

  const stepsItems = [
    { title: t('resource.wizardStepAssociation') },
    { title: t('resource.wizardStepFiles') },
    { title: t('resource.wizardStepMedia') },
    { title: t('resource.wizardStepConfirm') },
  ];

  const changes = step === 3 ? computeChanges() : null;

  /** In-place server-validation error (422) pinned to the step that fixes
   * it — the message lists the per-work gaps verbatim. */
  const saveErrorAlert = (stepIdx: number) =>
    saveError && saveError.step === stepIdx ? (
      <Alert
        type="error"
        showIcon
        closable
        message={saveError.message}
        style={{ marginBottom: 12 }}
        onClose={() => setSaveError(null)}
      />
    ) : null;

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div
        style={{
          flexShrink: 0,
          margin: '0 4px 8px',
          padding: '8px 10px',
          border: '1px solid var(--rr-border-soft)',
          borderRadius: 6,
          background: 'var(--rr-surface-card)',
        }}
      >
        <Text ellipsis={{ tooltip: detail.title_raw }} style={{ display: 'block' }}>
          {detail.title_raw}
        </Text>
        <Text type="secondary" style={{ fontFamily: 'monospace', fontSize: 11 }}>
          {t('resource.resourceId')}：{detail.id}
        </Text>
      </div>
      <div style={{ flexShrink: 0, padding: '8px 4px 16px' }}>
        <Steps size="small" responsive current={step} items={stepsItems} />
      </div>
      <div style={{ flex: 1, minHeight: 0, overflow: step === 1 ? 'hidden' : 'auto', padding: '4px 4px 12px' }}>

      {/* Step 0 — collection & works association (two-level for TV: the
          collection first, then season works among its members) */}
      <div style={panelStyle(step === 0)}>
        <StepAssociation wiz={wiz} alert={saveErrorAlert(0)} />
      </div>

      {/* Step 1 — file mapping (left: works, right: files) */}
      <div style={{ ...panelStyle(step === 1), height: '100%', minHeight: 0 }}>
        <StepFiles wiz={wiz} alert={saveErrorAlert(1)} compact={compact} />
      </div>

      {/* Step 2 — generic media fields */}
      <div style={panelStyle(step === 2)}>
        <StepMedia wiz={wiz} alert={saveErrorAlert(2)} compact={compact} />
      </div>

      {/* Step 3 — confirmation review */}
      <div style={panelStyle(step === 3)}>
        {saveErrorAlert(3)}
        {!changes ? (
          <Spin />
        ) : (
          <ReviewRows changes={changes} />
        )}
      </div>

      </div>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexShrink: 0, padding: '12px 4px 4px', borderTop: '1px solid var(--rr-border-soft)' }}>
        <Popconfirm
          title={t('resource.reparseConfirmTitle')}
          description={t('resource.reparseConfirmDesc')}
          okText={t('common.confirm')}
          cancelText={t('common.cancel')}
          onConfirm={() => void handleReparse()}
        >
          <Button loading={reparsing} icon={<RefreshCw size={14} />}>
            {t('resource.reparseMetadata')}
          </Button>
        </Popconfirm>
        <Space size={8}>
          {step > 0 && <Button disabled={analyzing} onClick={() => setStep((s) => s - 1)}>{t('resource.prevStep')}</Button>}
          {step < 3 && (
            <Button type="primary" disabled={analyzing} onClick={handleNext}>
              {t('resource.nextStep')}
            </Button>
          )}
          {step === 3 && (
            <Button type="primary" loading={saving} onClick={() => void handleSave()}>
              {t('common.confirm')}
            </Button>
          )}
        </Space>
      </div>

      <WorkPickerModal
        open={wiz.pickerOpen}
        existingKeys={new Set(wiz.works.map((w) => workKeyOf(w.work_type, w.work_id)))}
        collectionId={wiz.collectionId}
        defaultMetadataSource={wiz.channelMetadataSource}
        defaultFallbackSources={wiz.channelFallbackSources}
        initialQuery={
          wiz.pickerCluster
            ? wiz.clusterGroups.find((g) => g.key === wiz.pickerCluster)?.title ?? ''
            : ''
        }
        onClose={() => {
          wiz.setPickerOpen(false);
          wiz.setPickerCluster(null);
        }}
        onPick={(ref, title, meta) => {
          const group = wiz.pickerCluster
            ? wiz.clusterGroups.find((g) => g.key === wiz.pickerCluster)
            : undefined;
          if (group) {
            // Whole-cluster pick: bind every member file to the picked work.
            wiz.applyClusterToWork(group, ref, title, meta?.season ?? null);
          } else {
            wiz.addWork(ref, title, meta);
          }
          wiz.setPickerOpen(false);
          wiz.setPickerCluster(null);
        }}
      />
    </div>
  );
}
