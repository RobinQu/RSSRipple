import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { Plus, RefreshCw, Trash2 } from 'lucide-react';
import { channelsApi } from '../api/channels';
import StatusBadge from '../components/StatusBadge';
import Pagination from '../components/Pagination';
import { timeAgo } from '../utils/format';
import type { Channel } from '../types';

export default function Channels() {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);

  const fetchChannels = async () => {
    setLoading(true);
    const res = await channelsApi.list(page, 20);
    if (res.success) {
      setChannels(res.data);
      if (res.meta) setTotal(res.meta.total);
    }
    setLoading(false);
  };

  useEffect(() => { fetchChannels(); }, [page]);

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this channel?')) return;
    await channelsApi.delete(id);
    fetchChannels();
  };

  const handleFetch = async (id: string) => {
    await channelsApi.fetch(id);
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">Channels</h1>
        <Link to="/channels/new" className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700">
          <Plus size={16} /> Create Channel
        </Link>
      </div>

      {loading ? (
        <p className="text-gray-400">Loading...</p>
      ) : channels.length === 0 ? (
        <div className="bg-white rounded-xl border p-12 text-center">
          <p className="text-gray-400">No channels yet. Create one to get started.</p>
        </div>
      ) : (
        <div className="bg-white rounded-xl border shadow-sm overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Name</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Type</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Status</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Last Fetched</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {channels.map((ch) => (
                <tr key={ch.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium">{ch.name}</td>
                  <td className="px-4 py-3 text-gray-500">{ch.type}</td>
                  <td className="px-4 py-3"><StatusBadge status={ch.status} /></td>
                  <td className="px-4 py-3 text-gray-500 text-xs">
                    {ch.last_fetched_at ? timeAgo(ch.last_fetched_at) : 'Never'}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex items-center justify-end gap-1">
                      <button onClick={() => handleFetch(ch.id)} className="p-1.5 hover:bg-gray-100 rounded-lg" title="Fetch now">
                        <RefreshCw size={16} className="text-gray-500" />
                      </button>
                      <button onClick={() => handleDelete(ch.id)} className="p-1.5 hover:bg-red-50 rounded-lg" title="Delete">
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
