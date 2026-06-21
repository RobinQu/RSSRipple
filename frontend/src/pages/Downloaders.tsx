import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { Plus, Trash2, Zap } from 'lucide-react';
import { downloadersApi } from '../api/downloaders';
import StatusBadge from '../components/StatusBadge';
import Pagination from '../components/Pagination';
import type { DownloaderInstance } from '../types';

export default function Downloaders() {
  const [items, setItems] = useState<DownloaderInstance[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);

  const fetch = async () => {
    setLoading(true);
    const res = await downloadersApi.list(page, 20);
    if (res.success) { setItems(res.data); if (res.meta) setTotal(res.meta.total); }
    setLoading(false);
  };

  useEffect(() => { fetch(); }, [page]);

  const handleTest = async (id: string) => {
    const res = await downloadersApi.test(id);
    if (res.success) alert(res.data.message);
  };

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this downloader?')) return;
    await downloadersApi.delete(id);
    fetch();
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">Downloaders</h1>
        <Link to="/downloaders/new" className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700">
          <Plus size={16} /> Add Downloader
        </Link>
      </div>
      {loading ? <p className="text-gray-400">Loading...</p> : items.length === 0 ? (
        <div className="bg-white rounded-xl border p-12 text-center"><p className="text-gray-400">No downloaders configured.</p></div>
      ) : (
        <div className="bg-white rounded-xl border shadow-sm overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Name</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Type</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">URL</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Download Dir</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Status</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {items.map((dl) => (
                <tr key={dl.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium">{dl.name}</td>
                  <td className="px-4 py-3 text-gray-500">{dl.type}</td>
                  <td className="px-4 py-3 text-gray-500 text-xs truncate max-w-xs">{dl.url}</td>
                  <td className="px-4 py-3 text-gray-500 text-xs">{dl.download_dir || '—'}</td>
                  <td className="px-4 py-3"><StatusBadge status={dl.status} /></td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex items-center justify-end gap-1">
                      <button onClick={() => handleTest(dl.id)} className="p-1.5 hover:bg-gray-100 rounded-lg" title="Test connection">
                        <Zap size={16} className="text-amber-500" />
                      </button>
                      <button onClick={() => handleDelete(dl.id)} className="p-1.5 hover:bg-red-50 rounded-lg" title="Delete">
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
