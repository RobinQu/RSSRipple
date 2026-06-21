interface Props {
  progress: number;
  className?: string;
}

export default function ProgressBar({ progress, className = '' }: Props) {
  const pct = Math.min(100, Math.max(0, progress));
  return (
    <div className={`w-full bg-gray-200 rounded-full h-2.5 overflow-hidden ${className}`}>
      <div
        className="bg-blue-600 h-2.5 rounded-full transition-all duration-300"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}
