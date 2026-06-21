import { Pagination as AntdPagination } from 'antd';

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
  return (
    <AntdPagination
      current={page}
      total={total}
      pageSize={pageSize}
      onChange={onPageChange}
      showSizeChanger={false}
      showTotal={(total, range) => `${range[0]}-${range[1]} of ${total} items`}
      size="small"
    />
  );
}
