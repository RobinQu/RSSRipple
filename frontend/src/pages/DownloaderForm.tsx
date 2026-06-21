import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { downloadersApi } from '../api/downloaders';

export default function DownloaderForm() {
  const navigate = useNavigate();
  const [form, setForm] = useState({ name: '', url: '', username: '', password: '', download_dir: '' });
  const [saving, setSaving] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    const res = await downloadersApi.create({
      name: form.name,
      type: 'transmission',
      url: form.url,
      username: form.username || undefined,
      password: form.password || undefined,
      download_dir: form.download_dir || undefined,
    });
    setSaving(false);
    if (res.success) navigate('/downloaders');
  };

  return (
    <div className="max-w-xl">
      <h1 className="text-2xl font-bold mb-6">Add Downloader</h1>
      <form onSubmit={handleSubmit} className="bg-white rounded-xl border shadow-sm p-6 space-y-4">
        <div>
          <label className="block text-sm font-medium mb-1">Name</label>
          <input type="text" required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
            className="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 outline-none" placeholder="My Transmission" />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">API URL</label>
          <input type="url" required value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })}
            className="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 outline-none" placeholder="http://transmission:9091/transmission/rpc" />
        </div>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1">Username</label>
            <input type="text" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })}
              className="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 outline-none" />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">Password</label>
            <input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })}
              className="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 outline-none" />
          </div>
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">Download Directory</label>
          <input type="text" value={form.download_dir} onChange={(e) => setForm({ ...form, download_dir: e.target.value })}
            className="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 outline-none"
            placeholder="/downloads/anime" />
          <p className="text-xs text-gray-400 mt-1">Directory on the downloader where files will be saved.</p>
        </div>
        <div className="flex gap-3 pt-2">
          <button type="submit" disabled={saving} className="px-6 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50">
            {saving ? 'Saving...' : 'Add Downloader'}
          </button>
          <button type="button" onClick={() => navigate('/downloaders')} className="px-6 py-2 border rounded-lg text-sm hover:bg-gray-50">Cancel</button>
        </div>
      </form>
    </div>
  );
}
