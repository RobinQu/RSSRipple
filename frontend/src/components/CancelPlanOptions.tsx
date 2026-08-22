import { useState } from 'react';
import { Checkbox, Space, Typography } from 'antd';
import type { TFunction } from 'i18next';

const { Text } = Typography;

export interface CancelPlanFlags {
  delete_task: boolean;
  delete_data: boolean;
}

// Checkbox group rendered inside the cancel-plan modal.confirm, reporting
// every change via onChange; the confirm's onOk closure reads the latest
// flags. delete_data implies delete_task (mirrors the backend), so unchecking
// the task clears the data option and the data checkbox stays disabled until
// then.
export default function CancelPlanOptions({
  t,
  onChange,
}: {
  t: TFunction;
  onChange: (flags: CancelPlanFlags) => void;
}) {
  const [deleteTask, setDeleteTask] = useState(false);
  const [deleteData, setDeleteData] = useState(false);
  return (
    <Space direction="vertical" size={4}>
      <Checkbox
        checked={deleteTask}
        onChange={(e) => {
          const v = e.target.checked;
          setDeleteTask(v);
          if (!v) setDeleteData(false);
          onChange({ delete_task: v, delete_data: v ? deleteData : false });
        }}
      >
        {t('organize.cancelDeleteTask')}
      </Checkbox>
      <Checkbox
        checked={deleteData}
        disabled={!deleteTask}
        onChange={(e) => {
          const v = e.target.checked;
          setDeleteData(v);
          onChange({ delete_task: deleteTask, delete_data: v });
        }}
      >
        <Text type={deleteData ? 'danger' : undefined}>
          {t('organize.cancelDeleteData')}
        </Text>
      </Checkbox>
    </Space>
  );
}
