import { useState, useCallback } from 'react';
import { Bot, AlertTriangle, Download } from 'lucide-react';
import { dashboardApi } from '../api/tasks';
import { usePolling } from '../hooks/usePolling';
import StatusBadge from '../components/StatusBadge';
import ProgressBar from '../components/ProgressBar';
import { formatSpeed, formatEta, timeAgo } from '../utils/format';
import type { DashboardData } from '../types';

export default function Dashboard() {
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchData = useCallback(async () => {
    const res = await dashboardApi.get();
    if (res.success) setDashboard(res.data);
    setLoading(false);
  }, []);

  usePolling(fetchData, 10000);

  if (loading) return <div className="text-gray-500">Loading...</div>;
  if (!dashboard) return <div className="text-gray-500">No data</div>;

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Dashboard</h1>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
        <div className="bg-white rounded-xl border p-5 shadow-sm">
          <div className="flex items-center gap-3 text-sm text-gray-500 mb-1">
            <Bot size={18} /> Active Agents
          </div>
          <div className="text-3xl font-bold">{dashboard.active_agents}</div>
        </div>
        <div className="bg-white rounded-xl border p-5 shadow-sm">
          <div className="flex items-center gap-3 text-sm text-gray-500 mb-1">
            <Download size={18} /> Active Downloads
          </div>
          <div className="text-3xl font-bold">{dashboard.active_downloads.length}</div>
        </div>
        <div className="bg-white rounded-xl border p-5 shadow-sm">
          <div className="flex items-center gap-3 text-sm text-gray-500 mb-1">
            <AlertTriangle size={18} /> Pending Decisions
          </div>
          <div className="text-3xl font-bold">{dashboard.pending_decisions.length}</div>
        </div>
      </div>

      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">Active Downloads</h2>
        {dashboard.active_downloads.length === 0 ? (
          <p className="text-gray-400 text-sm">No active downloads</p>
        ) : (
          <div className="bg-white rounded-xl border shadow-sm divide-y">
            {dashboard.active_downloads.map((task) => (
              <div key={task.id} className="p-4">
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-sm truncate max-w-md">
                      {task.file_resource?.title_raw || task.id}
                    </span>
                    <StatusBadge status={task.status} />
                  </div>
                  <div className="text-sm text-gray-500 flex gap-4">
                    <span>{formatSpeed(task.download_speed)}</span>
                    <span>ETA: {formatEta(task.eta)}</span>
                    <span>{task.progress.toFixed(1)}%</span>
                  </div>
                </div>
                <ProgressBar progress={task.progress} />
              </div>
            ))}
          </div>
        )}
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-3">Pending Decisions</h2>
        {dashboard.pending_decisions.length === 0 ? (
          <p className="text-gray-400 text-sm">No pending decisions</p>
        ) : (
          <div className="bg-white rounded-xl border shadow-sm divide-y">
            {dashboard.pending_decisions.map((d) => (
              <div key={d.id} className="p-4 flex items-center justify-between">
                <div>
                  <p className="font-medium text-sm">{d.reason}</p>
                  <p className="text-xs text-gray-500 mt-1">
                    {d.candidates.length} candidates &middot; {timeAgo(d.created_at)}
                  </p>
                </div>
                <StatusBadge status={d.status} />
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
