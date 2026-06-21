import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, X } from 'lucide-react';
import { agentsApi } from '../api/agents';
import { channelsApi } from '../api/channels';
import { downloadersApi } from '../api/downloaders';
import type { Channel, DownloaderInstance, FilterField, FilterOperator } from '../types';

interface FilterRow {
  field: FilterField;
  operator: FilterOperator;
  value: string;
  priority: number;
  is_required: boolean;
}

const FIELDS: FilterField[] = ['subtitle_group', 'resolution', 'container', 'video_codec', 'audio_codec', 'subtitle_type', 'source', 'title_cn', 'title_en'];
const OPERATORS: FilterOperator[] = ['eq', 'contains', 'fuzzy', 'in', 'regex'];

export default function AgentForm() {
  const navigate = useNavigate();
  const [channels, setChannels] = useState<Channel[]>([]);
  const [downloaders, setDownloaders] = useState<DownloaderInstance[]>([]);
  const [form, setForm] = useState({ name: '', channel_id: '', downloader_id: '', task_expire_days: 30, llm_enabled: false, metadata_source: '', content_type: 'anime' });
  const [filters, setFilters] = useState<FilterRow[]>([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    channelsApi.list(1, 100).then(r => { if (r.success) setChannels(r.data); });
    downloadersApi.list(1, 100).then(r => { if (r.success) setDownloaders(r.data); });
  }, []);

  const addFilter = () => setFilters([...filters, { field: 'subtitle_group', operator: 'eq', value: '', priority: filters.length * 10, is_required: false }]);
  const removeFilter = (i: number) => setFilters(filters.filter((_, idx) => idx !== i));
  const updateFilter = (i: number, key: string, val: unknown) => {
    setFilters(prev => prev.map((f, idx) => idx === i ? { ...f, [key]: val } : f));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    const res = await agentsApi.create({ ...form, filters });
    setSaving(false);
    if (res.success) navigate('/agents');
  };

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-bold mb-6">Create Agent</h1>
      <form onSubmit={handleSubmit} className="bg-white rounded-xl border shadow-sm p-6 space-y-4">
        <div>
          <label className="block text-sm font-medium mb-1">Name</label>
          <input type="text" required value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}
            className="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 outline-none" />
        </div>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1">Channel</label>
            <select required value={form.channel_id} onChange={e => setForm({ ...form, channel_id: e.target.value })}
              className="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 outline-none bg-white">
              <option value="">Select channel...</option>
              {channels.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">Downloader</label>
            <select required value={form.downloader_id} onChange={e => setForm({ ...form, downloader_id: e.target.value })}
              className="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 outline-none bg-white">
              <option value="">Select downloader...</option>
              {downloaders.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium mb-1">Task Expire Days</label>
            <input type="number" min={1} value={form.task_expire_days} onChange={e => setForm({ ...form, task_expire_days: parseInt(e.target.value) })}
              className="w-full border rounded-lg px-3 py-2 text-sm outline-none" />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">Content Type</label>
            <select value={form.content_type} onChange={e => setForm({ ...form, content_type: e.target.value })}
              className="w-full border rounded-lg px-3 py-2 text-sm bg-white outline-none">
              <option value="anime">Anime</option>
              <option value="tv">TV Series</option>
              <option value="movie">Movie</option>
              <option value="mixed">Mixed</option>
            </select>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-4">
          <div className="flex items-center gap-2">
            <input type="checkbox" id="llm" checked={form.llm_enabled} onChange={e => setForm({ ...form, llm_enabled: e.target.checked })} className="rounded" />
            <label htmlFor="llm" className="text-sm">Enable LLM-assisted decisions</label>
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">Metadata Source</label>
            <select value={form.metadata_source} onChange={e => setForm({ ...form, metadata_source: e.target.value })}
              className="w-full border rounded-lg px-3 py-2 text-sm bg-white outline-none">
              <option value="">None</option>
              <option value="imdb">IMDB (Cinemagoer)</option>
              <option value="tvdb">TVDB</option>
            </select>
          </div>
        </div>

        <div className="border-t pt-4">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold">Resource Filters</h3>
            <button type="button" onClick={addFilter} className="flex items-center gap-1 text-xs text-blue-600 hover:text-blue-800">
              <Plus size={14} /> Add Filter
            </button>
          </div>
          {filters.map((f, i) => (
            <div key={i} className="flex items-center gap-2 mb-2">
              <select value={f.field} onChange={e => updateFilter(i, 'field', e.target.value)} className="border rounded px-2 py-1.5 text-sm bg-white">
                {FIELDS.map(fd => <option key={fd} value={fd}>{fd}</option>)}
              </select>
              <select value={f.operator} onChange={e => updateFilter(i, 'operator', e.target.value)} className="border rounded px-2 py-1.5 text-sm bg-white">
                {OPERATORS.map(op => <option key={op} value={op}>{op}</option>)}
              </select>
              <input type="text" value={f.value} onChange={e => updateFilter(i, 'value', e.target.value)} placeholder="Value" className="flex-1 border rounded px-2 py-1.5 text-sm" />
              <input type="number" value={f.priority} onChange={e => updateFilter(i, 'priority', parseInt(e.target.value))} className="w-16 border rounded px-2 py-1.5 text-sm" title="Priority" />
              <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={f.is_required} onChange={e => updateFilter(i, 'is_required', e.target.checked)} /> Required</label>
              <button type="button" onClick={() => removeFilter(i)} className="p-1 hover:bg-red-50 rounded"><X size={14} className="text-red-500" /></button>
            </div>
          ))}
        </div>

        <div className="flex gap-3 pt-2">
          <button type="submit" disabled={saving} className="px-6 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50">
            {saving ? 'Creating...' : 'Create Agent'}
          </button>
          <button type="button" onClick={() => navigate('/agents')} className="px-6 py-2 border rounded-lg text-sm hover:bg-gray-50">Cancel</button>
        </div>
      </form>
    </div>
  );
}
