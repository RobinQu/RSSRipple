import { Progress } from 'antd';

interface ProgressBarProps {
  progress: number;
  className?: string;
}

export default function ProgressBar({ progress }: ProgressBarProps) {
  return (
    <Progress
      percent={Math.min(100, Math.max(0, progress))}
      size="small"
      strokeColor="#57c1ff"
      trailColor="#242728"
      format={(p) => `${p?.toFixed(0)}%`}
    />
  );
}
