import type { TableColumnsType } from 'antd';
import type React from 'react';

/**
 * Inject each column's string title as a `data-label` attribute on its cells,
 * so the mobile stacked-table CSS (`.stack-table` in index.css) can prefix
 * each stacked cell with its column label. Non-string titles get no label.
 */
export function withMobileLabels<T>(columns: TableColumnsType<T>): TableColumnsType<T> {
  return columns.map((col) => ({
    ...col,
    onCell: (record: T, index?: number) =>
      ({
        ...(col.onCell ? col.onCell(record, index) : {}),
        'data-label': typeof col.title === 'string' ? col.title : undefined,
      }) as React.HTMLAttributes<HTMLElement>,
  }));
}
