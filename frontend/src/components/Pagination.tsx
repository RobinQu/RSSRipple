import { Pagination as AntdPagination } from 'antd';
import { useTranslation } from 'react-i18next';

interface PaginationProps {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
}

export default function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
}: PaginationProps) {
  const { t } = useTranslation();
  return (
    <AntdPagination
      current={page}
      total={total}
      pageSize={pageSize}
      onChange={onPageChange}
      showSizeChanger={false}
      showTotal={(total, range) => t('pagination.range', { from: range[0], to: range[1], total })}
      size="small"
    />
  );
}
