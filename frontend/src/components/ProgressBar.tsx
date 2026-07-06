import { Progress } from 'antd';

interface ProgressBarProps {
  progress: number;
  className?: string;
}

export default function ProgressBar({ progress }: ProgressBarProps) {
  return (
    <Progress
      percent={Math.min(100, Math.max(0, progress * 100))}
      size="small"
      strokeColor="#1863dc"
      trailColor="#d9d9dd"
      format={(p) => `${p?.toFixed(0)}%`}
    />
  );
}
