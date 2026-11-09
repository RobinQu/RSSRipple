import { Select } from 'antd';
import { useTranslation } from 'react-i18next';
import type { FileAssignmentRole } from '../../types';
import { eligibleSubtitleParents, type PlacementMap } from './placementState';
import type { Placement } from './types';

const FILE_ROLES: FileAssignmentRole[] = ['episode', 'subtitle', 'extra', 'excluded'];

export default function FileRoleControls({
  path,
  placement,
  placements,
  onRoleChange,
  onAttachedChange,
}: {
  path: string;
  placement: Placement | undefined;
  placements: PlacementMap;
  onRoleChange: (path: string, role: FileAssignmentRole) => void;
  onAttachedChange: (path: string, attachedToPath: string | null) => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      style={{ display: 'flex', flexWrap: 'wrap', gap: 4, justifyContent: 'flex-end' }}
      onPointerDown={(event) => event.stopPropagation()}
    >
      <Select<FileAssignmentRole>
        size="small"
        aria-label={t('resource.fileRole')}
        placeholder={t('resource.fileRoleUnset')}
        value={placement?.fileRole}
        onChange={(role) => onRoleChange(path, role)}
        options={FILE_ROLES.map((role) => ({
          value: role,
          label: t(`resource.fileRole_${role}`),
        }))}
        style={{ width: 92, flexShrink: 0 }}
      />
      {placement?.fileRole === 'subtitle' && (
        <Select
          size="small"
          allowClear
          showSearch
          aria-label={t('resource.attachedEpisode')}
          placeholder={t('resource.attachedEpisode')}
          value={placement.attachedToPath ?? undefined}
          onChange={(value?: string) => onAttachedChange(path, value ?? null)}
          options={eligibleSubtitleParents(placements, path)
            .map((episodePath) => ({ value: episodePath, label: episodePath }))}
          optionFilterProp="label"
          style={{ width: 180, maxWidth: '100%' }}
        />
      )}
    </div>
  );
}
