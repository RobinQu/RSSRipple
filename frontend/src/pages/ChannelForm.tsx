import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { CheckCircle, XCircle, Loader } from 'lucide-react';
import { channelsApi } from '../api/channels';

export default function ChannelForm() {
  const navigate = useNavigate();
  const [form, setForm] = useState({ name: '', url: '', fetch_interval: 1800 });
  const [saving, setSaving] = useState(false);
  const [urlStatus, setUrlStatus] = useState<'idle' | 'checking' | 'valid' | 'invalid'>('idle');
  const [urlMessage, setUrlMessage] = useState('');
  const [downloadableCount, setDownloadableCount] = useState(0);

  const validateUrl = async () => {
    if (!form.url) return;
    setUrlStatus('checking');
    const res = await channelsApi.validateUrl(form.url);
    if (res.success && res.data.valid) {
      setUrlStatus('valid');
      setDownloadableCount(res.data.downloadable_count);
      setUrlMessage(`Valid feed: ${res.data.item_count} items, ${res.data.downloadable_count} with downloads`);
    } else {
      setUrlStatus('invalid');
      setUrlMessage(res.data?.message || 'Invalid URL');
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    const res = await channelsApi.create({ ...form, type: 'rss_feed' });
    setSaving(false);
    if (res.success) navigate('/channels');
  };

  return (
    <div className="max-w-xl">
      <h1 className="text-2xl font-bold mb-6">Create Channel</h1>
      <form onSubmit={handleSubmit} className="bg-white rounded-xl border shadow-sm p-6 space-y-4">
        <div>
          <label className="block text-sm font-medium mb-1">Name</label>
          <input
            type="text" required
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            className="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 outline-none"
            placeholder="My anime feed"
          />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">RSS URL</label>
          <div className="flex gap-2">
            <input
              type="url" required
              value={form.url}
              onChange={(e) => { setForm({ ...form, url: e.target.value }); setUrlStatus('idle'); }}
              className="flex-1 border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 outline-none"
              placeholder="https://mikanani.me/RSS/..."
            />
            <button type="button" onClick={validateUrl} className="px-4 py-2 border rounded-lg text-sm hover:bg-gray-50">
              Validate
            </button>
          </div>
          {urlStatus === 'checking' && <p className="text-xs text-gray-500 mt-1 flex items-center gap-1"><Loader size={12} className="animate-spin" /> Checking...</p>}
          {urlStatus === 'valid' && (
            <div className="mt-1 space-y-0.5">
              <p className="text-xs text-emerald-600 flex items-center gap-1"><CheckCircle size={12} /> {urlMessage}</p>
              {downloadableCount === 0 && <p className="text-xs text-amber-600">Warning: no torrent files or magnet links found in feed entries</p>}
            </div>
          )}
          {urlStatus === 'invalid' && <p className="text-xs text-red-600 mt-1 flex items-center gap-1"><XCircle size={12} /> {urlMessage}</p>}
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">Fetch Interval (seconds)</label>
          <input
            type="number" min={60}
            value={form.fetch_interval}
            onChange={(e) => setForm({ ...form, fetch_interval: parseInt(e.target.value) })}
            className="w-40 border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 outline-none"
          />
        </div>
        <div className="flex gap-3 pt-2">
          <button type="submit" disabled={saving} className="px-6 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50">
            {saving ? 'Creating...' : 'Create Channel'}
          </button>
          <button type="button" onClick={() => navigate('/channels')} className="px-6 py-2 border rounded-lg text-sm hover:bg-gray-50">
            Cancel
          </button>
        </div>
      </form>
    </div>
  );
}
