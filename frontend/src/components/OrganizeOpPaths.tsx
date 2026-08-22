import { Tooltip, Typography } from 'antd';
import { useTranslation } from 'react-i18next';

const { Text } = Typography;

const codeStyle = {
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-all',
} as const;

/** Highlighted src → dst path pair for a single organize op. Paths wrap and
    are never ellipsized; the destination is visually emphasized. When
    `srcRelocated` is set (done plan with move/movedir ops) the source no
    longer exists on disk — it is rendered struck-through with an explanatory
    tooltip instead of looking like a live path. */
export default function OrganizeOpPaths({
  src,
  dst,
  srcRelocated = false,
}: {
  src: string;
  dst: string | null;
  srcRelocated?: boolean;
}) {
  const { t } = useTranslation();
  const srcNode = srcRelocated ? (
    <Tooltip title={t('organize.srcRelocated')}>
      <Text code delete type="secondary" style={codeStyle}>{src}</Text>
    </Tooltip>
  ) : (
    <Text code style={codeStyle}>{src}</Text>
  );
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
      {srcNode}
      {dst ? (
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 6 }}>
          <Text type="secondary" style={{ flexShrink: 0, lineHeight: '22px' }}>↓</Text>
          <Text code type="success" style={{ ...codeStyle, flex: 1, minWidth: 0 }}>{dst}</Text>
        </div>
      ) : null}
    </div>
  );
}
