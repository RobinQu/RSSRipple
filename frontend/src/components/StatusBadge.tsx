interface Props {
  status: string;
}

const colorMap: Record<string, string> = {
  active: 'bg-blue-100 text-blue-800',
  downloading: 'bg-blue-100 text-blue-800',
  queued: 'bg-indigo-100 text-indigo-800',
  completed: 'bg-emerald-100 text-emerald-800',
  connected: 'bg-emerald-100 text-emerald-800',
  decided: 'bg-emerald-100 text-emerald-800',
  paused: 'bg-amber-100 text-amber-800',
  pending: 'bg-gray-100 text-gray-800',
  inactive: 'bg-gray-100 text-gray-600',
  disconnected: 'bg-gray-100 text-gray-600',
  error: 'bg-red-100 text-red-800',
  cancelled: 'bg-red-100 text-red-600',
  expired: 'bg-gray-100 text-gray-500',
  skipped: 'bg-gray-100 text-gray-500',
};

export default function StatusBadge({ status }: Props) {
  const colors = colorMap[status] || 'bg-gray-100 text-gray-800';
  return (
    <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${colors}`}>
      {status}
    </span>
  );
}
