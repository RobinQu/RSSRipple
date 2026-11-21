import { Badge, Button, Input, Space } from 'antd';
import { Filter, Search } from 'lucide-react';
import { useTranslation } from 'react-i18next';

interface Props {
  value: string;
  onChange: (value: string) => void;
  filterCount: number;
  onOpenFilters: () => void;
}

/** Compact keyword + field-filter controls for the channel resource lists. */
export default function ResourceSearchBar({
  value,
  onChange,
  filterCount,
  onOpenFilters,
}: Props) {
  const { t } = useTranslation();
  return (
    <Space size={8} wrap>
      <Input
        allowClear
        size="small"
        prefix={<Search size={14} />}
        placeholder={t('channels.resourceSearchPlaceholder')}
        aria-label={t('channels.resourceSearchPlaceholder')}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        maxLength={200}
        style={{ width: 280 }}
      />
      <Badge count={filterCount} size="small" offset={[-2, 2]}>
        <Button size="small" icon={<Filter size={14} />} onClick={onOpenFilters}>
          {t('channels.fieldFilter')}
        </Button>
      </Badge>
    </Space>
  );
}
