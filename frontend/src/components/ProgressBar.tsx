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
      strokeColor="var(--rr-primary)"
      trailColor="var(--rr-border)"
      format={(p) => `${p?.toFixed(2)}%`}
    />
  );
}
