import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeft, RefreshCw, Wand2, Loader } from 'lucide-react';
import { channelsApi } from '../api/channels';
import StatusBadge from '../components/StatusBadge';
import Pagination from '../components/Pagination';
import { timeAgo } from '../utils/format';
import type { Channel, FileResource } from '../types';

export default function ChannelDetail() {
  const { id } = useParams<{ id: string }>();
  const [channel, setChannel] = useState<Channel | null>(null);
  const [resources, setResources] = useState<FileResource[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeResult, setAnalyzeResult] = useState<{ confidence: string; mapping: Record<string, unknown> } | null>(null);

  useEffect(() => {
    if (id) channelsApi.get(id).then(r => { if (r.success) setChannel(r.data); });
  }, [id]);

  useEffect(() => {
    if (!id) return;
    channelsApi.resources(id, page).then(r => {
      if (r.success) { setResources(r.data); if (r.meta) setTotal(r.meta.total); }
    });
  }, [id, page]);

  const handleFetch = async () => {
    if (!id) return;
    await channelsApi.fetch(id);
    // Refresh resources after fetch
    setTimeout(() => {
      channelsApi.resources(id, page).then(r => {
        if (r.success) { setResources(r.data); if (r.meta) setTotal(r.meta.total); }
      });
    }, 2000);
  };

  const handleAnalyze = async () => {
    if (!id) return;
    setAnalyzing(true);
    setAnalyzeResult(null);
    const res = await channelsApi.analyze(id);
    setAnalyzing(false);
    if (res.success) {
      setAnalyzeResult({ confidence: res.data.confidence, mapping: res.data.field_mapping });
    }
  };

  const handleApplyMapping = async () => {
    if (!id || !analyzeResult) return;
    await channelsApi.applyMapping(id, { field_mapping: analyzeResult.mapping, parser_type: 'custom' });
    channelsApi.get(id).then(r => { if (r.success) setChannel(r.data); });
    setAnalyzeResult(null);
  };

  if (!channel) return <div className="text-gray-400">Loading...</div>;

  return (
    <div>
      <div className="flex items-center gap-3 mb-6">
        <Link to="/channels" className="p-2 hover:bg-gray-100 rounded-lg"><ArrowLeft size={18} /></Link>
        <div className="flex-1">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold">{channel.name}</h1>
            <StatusBadge status={channel.status} />
          </div>
          <p className="text-sm text-gray-500 mt-1 truncate">{channel.url}</p>
        </div>
        <div className="flex gap-2">
          <button onClick={handleFetch} className="flex items-center gap-2 px-3 py-2 border rounded-lg text-sm hover:bg-gray-50">
            <RefreshCw size={14} /> Fetch
          </button>
          <button onClick={handleAnalyze} disabled={analyzing} className="flex items-center gap-2 px-3 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50">
            {analyzing ? <Loader size={14} className="animate-spin" /> : <Wand2 size={14} />}
            {analyzing ? 'Analyzing...' : 'Analyze Feed'}
          </button>
        </div>
      </div>

      {/* Channel info */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <div className="bg-white rounded-lg border p-3 text-sm">
          <div className="text-gray-500 text-xs">Parser</div>
          <div className="font-medium">{channel.parser_type}</div>
        </div>
        <div className="bg-white rounded-lg border p-3 text-sm">
          <div className="text-gray-500 text-xs">Fetch Interval</div>
          <div className="font-medium">{channel.fetch_interval}s</div>
        </div>
        <div className="bg-white rounded-lg border p-3 text-sm">
          <div className="text-gray-500 text-xs">Resources</div>
          <div className="font-medium">{total}</div>
        </div>
        <div className="bg-white rounded-lg border p-3 text-sm">
          <div className="text-gray-500 text-xs">Last Fetched</div>
          <div className="font-medium">{channel.last_fetched_at ? timeAgo(channel.last_fetched_at) : 'Never'}</div>
        </div>
      </div>

      {/* Analyze result */}
      {analyzeResult && (
        <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 mb-6">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-semibold text-blue-800">
              Feed Analysis Complete — Confidence: {analyzeResult.confidence}
            </h3>
            <button onClick={handleApplyMapping} className="px-3 py-1.5 bg-blue-600 text-white rounded-lg text-xs font-medium hover:bg-blue-700">
              Apply Mapping
            </button>
          </div>
          <pre className="text-xs bg-white rounded-lg p-3 overflow-auto max-h-48 border">
            {JSON.stringify(analyzeResult.mapping, null, 2)}
          </pre>
        </div>
      )}

      {/* Resources table */}
      <div className="bg-white rounded-xl border shadow-sm overflow-hidden">
        <div className="px-4 py-3 border-b bg-gray-50">
          <h2 className="text-sm font-semibold">Synced Resources</h2>
        </div>
        {resources.length === 0 ? (
          <p className="p-8 text-center text-gray-400 text-sm">
            No resources yet. Click "Fetch" to pull from the RSS feed.
          </p>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b">
              <tr>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Title</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Group</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">EP</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Resolution</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Codec</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Container</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Published</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {resources.map(r => (
                <tr key={r.id} className="hover:bg-gray-50">
                  <td className="px-4 py-2 max-w-xs truncate" title={r.title_raw}>{r.title_raw}</td>
                  <td className="px-4 py-2 text-gray-500">{r.subtitle_group || '—'}</td>
                  <td className="px-4 py-2 text-gray-500">{r.episode ?? '—'}</td>
                  <td className="px-4 py-2 text-gray-500">{r.resolution || '—'}</td>
                  <td className="px-4 py-2 text-gray-500">{r.video_codec || '—'}</td>
                  <td className="px-4 py-2 text-gray-500">{r.container || '—'}</td>
                  <td className="px-4 py-2 text-gray-500 text-xs">{r.published_at ? timeAgo(r.published_at) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="px-4 pb-4">
          <Pagination page={page} pageSize={20} total={total} onPageChange={setPage} />
        </div>
      </div>
    </div>
  );
}
