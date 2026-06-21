import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { Plus, Trash2, Play } from 'lucide-react';
import { agentsApi } from '../api/agents';
import StatusBadge from '../components/StatusBadge';
import Pagination from '../components/Pagination';
import { timeAgo } from '../utils/format';
import type { Agent } from '../types';

export default function Agents() {
  const [items, setItems] = useState<Agent[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);

  const fetch = async () => {
    setLoading(true);
    const res = await agentsApi.list(page, 20);
    if (res.success) { setItems(res.data); if (res.meta) setTotal(res.meta.total); }
    setLoading(false);
  };

  useEffect(() => { fetch(); }, [page]);

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this agent?')) return;
    await agentsApi.delete(id);
    fetch();
  };

  const handleRun = async (id: string) => {
    await agentsApi.run(id);
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">Agents</h1>
        <Link to="/agents/new" className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700">
          <Plus size={16} /> Create Agent
        </Link>
      </div>
      {loading ? <p className="text-gray-400">Loading...</p> : items.length === 0 ? (
        <div className="bg-white rounded-xl border p-12 text-center"><p className="text-gray-400">No agents created yet.</p></div>
      ) : (
        <div className="bg-white rounded-xl border shadow-sm overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Name</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Status</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Filters</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Last Run</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">LLM</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {items.map((a) => (
                <tr key={a.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <Link to={`/agents/${a.id}`} className="font-medium text-blue-600 hover:underline">{a.name}</Link>
                  </td>
                  <td className="px-4 py-3"><StatusBadge status={a.status} /></td>
                  <td className="px-4 py-3 text-gray-500">{a.filters?.length || 0}</td>
                  <td className="px-4 py-3 text-gray-500 text-xs">{a.last_run_at ? timeAgo(a.last_run_at) : 'Never'}</td>
                  <td className="px-4 py-3 text-gray-500">{a.llm_enabled ? 'On' : 'Off'}</td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex items-center justify-end gap-1">
                      <button onClick={() => handleRun(a.id)} className="p-1.5 hover:bg-gray-100 rounded-lg" title="Run now">
                        <Play size={16} className="text-green-600" />
                      </button>
                      <button onClick={() => handleDelete(a.id)} className="p-1.5 hover:bg-red-50 rounded-lg" title="Delete">
                        <Trash2 size={16} className="text-red-500" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="px-4 pb-4">
            <Pagination page={page} pageSize={20} total={total} onPageChange={setPage} />
          </div>
        </div>
      )}
    </div>
  );
}
