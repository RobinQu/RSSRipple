import { App } from 'antd';
import type { TFunction } from 'i18next';
import { organizeApi } from '../api/organize';
import type { CancelPlanFlags } from './CancelPlanOptions';
import CancelPlanOptions from './CancelPlanOptions';

type AppApis = ReturnType<typeof App.useApp>;

// Shared cancel-with-options confirm used by both the plan list
// (MediaLibrary) and the plan drawer (OrganizePlanDrawer).
export function confirmCancelPlan(params: {
  modal: AppApis['modal'];
  message: AppApis['message'];
  t: TFunction;
  planId: string;
  onDone: () => void;
}) {
  const { modal, message, t, planId, onDone } = params;
  const flags: CancelPlanFlags = { delete_task: false, delete_data: false };
  modal.confirm({
    title: t('organize.cancelConfirm'),
    content: (
      <CancelPlanOptions t={t} onChange={(f) => Object.assign(flags, f)} />
    ),
    okText: t('common.confirm'),
    okButtonProps: { danger: true },
    cancelText: t('common.cancel'),
    onOk: async () => {
      const r = await organizeApi.cancelPlan(
        planId,
        flags.delete_task ? flags : undefined,
      );
      if (r.success) {
        message.success(t('organize.cancelled'));
        onDone();
      } else {
        message.error(r.error?.message || t('organize.cancelFailed'));
      }
    },
  });
}
