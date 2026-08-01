import { Tooltip, Typography } from 'antd';

const { Text } = Typography;

interface EllipsisTextProps {
  text: string;
  danger?: boolean;
}

/**
 * Single-line truncated text with the full value in a tooltip. Used for
 * flex-width table columns (torrent names, resource titles) so the cell
 * takes whatever width the compact columns leave.
 */
export default function EllipsisText({ text, danger }: EllipsisTextProps) {
  return (
    <Tooltip title={text} placement="topLeft">
      <Text
        type={danger ? 'danger' : undefined}
        style={{
          fontSize: 13,
          display: 'block',
          maxWidth: '100%',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {text}
      </Text>
    </Tooltip>
  );
}
