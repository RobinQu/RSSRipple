import { Typography } from 'antd';

const { Text } = Typography;

const codeStyle = {
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-all',
} as const;

/** Highlighted src → dst path pair for a single organize op. Paths wrap and
    are never ellipsized; the destination is visually emphasized. */
export default function OrganizeOpPaths({
  src,
  dst,
}: {
  src: string;
  dst: string | null;
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
      <Text code style={codeStyle}>{src}</Text>
      {dst ? (
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 6 }}>
          <Text type="secondary" style={{ flexShrink: 0, lineHeight: '22px' }}>↓</Text>
          <Text code type="success" style={{ ...codeStyle, flex: 1, minWidth: 0 }}>{dst}</Text>
        </div>
      ) : null}
    </div>
  );
}
