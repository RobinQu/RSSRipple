import { Grid, Modal } from 'antd';
import { useTranslation } from 'react-i18next';
import ResourceEditWizard from './ResourceEditWizard';
import type { FileResource } from '../types';

interface ResourceCorrectionModalProps {
  resourceId: string | null;
  open: boolean;
  onClose: () => void;
  onSaved?: (updated: FileResource) => void;
}

/** Thin modal host for the unified five-step edit wizard. All actions live inside
 * the wizard body; failures keep the modal open so the user can retry. */
export default function ResourceCorrectionModal({
  resourceId,
  open,
  onClose,
  onSaved,
}: ResourceCorrectionModalProps) {
  const { t } = useTranslation();
  const screens = Grid.useBreakpoint();
  const mobile = !screens.md;

  return (
    <Modal
      open={open && resourceId !== null}
      title={t('resource.correctTitle')}
      footer={null}
      onCancel={onClose}
      destroyOnHidden
      width={mobile ? '100%' : 'min(1440px, calc(100vw - 48px))'}
      centered={!mobile}
      style={mobile ? { top: 0, maxWidth: '100%', margin: 0, paddingBottom: 0 } : undefined}
      styles={{
        body: {
          height: mobile ? 'calc(100dvh - 55px)' : 'min(760px, calc(100vh - 150px))',
          overflow: 'hidden',
          paddingRight: 4,
        },
      }}
    >
      {resourceId !== null && (
        <ResourceEditWizard
          resourceId={resourceId}
          onDone={(updated) => {
            if (updated) onSaved?.(updated);
            onClose();
          }}
        />
      )}
    </Modal>
  );
}
