import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { Pause, Play, RotateCcw, Trash2, CheckCircle, SkipForward, FlaskConical, Loader } from 'lucide-react';
import { agentsApi } from '../api/agents';
import { tasksApi, decisionsApi } from '../api/tasks';
import StatusBadge from '../components/StatusBadge';
import ProgressBar from '../components/ProgressBar';
import Pagination from '../components/Pagination';
import { formatSpeed, formatEta, timeAgo } from '../utils/format';
import type { Agent, DownloadTask, PendingDecision, FilterTestResponse } from '../types';

export default function AgentDetail() {
  const { id } = useParams<{ id: string }>();
  const [agent, setAgent] = useState<Agent | null>(null);
  const [tab, setTab] = useState<'tasks' | 'decisions' | 'filters'>('tasks');
  const [tasks, setTasks] = useState<DownloadTask[]>([]);
  const [decisions, setDecisions] = useState<PendingDecision[]>([]);
  const [taskPage, setTaskPage] = useState(1);
  const [taskTotal, setTaskTotal] = useState(0);
  const [decPage, setDecPage] = useState(1);
  const [decTotal, setDecTotal] = useState(0);
  const [filterTest, setFilterTest] = useState<FilterTestResponse | null>(null);
  const [testingFilters, setTestingFilters] = useState(false);

  useEffect(() => {
    if (id) agentsApi.get(id).then(r => { if (r.success) setAgent(r.data); });
  }, [id]);

  useEffect(() => {
    if (!id) return;
    tasksApi.listByAgent(id, taskPage).then(r => { if (r.success) { setTasks(r.data); if (r.meta) setTaskTotal(r.meta.total); } });
  }, [id, taskPage]);

  useEffect(() => {
    if (!id) return;
    decisionsApi.listByAgent(id, decPage).then(r => { if (r.success) { setDecisions(r.data); if (r.meta) setDecTotal(r.meta.total); } });
  }, [id, decPage]);

  if (!agent) return <div className="text-gray-400">Loading...</div>;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">{agent.name}</h1>
          <p className="text-sm text-gray-500 mt-1">
            <StatusBadge status={agent.status} /> &middot; {agent.filters?.length || 0} filters &middot; LLM: {agent.llm_enabled ? 'On' : 'Off'}
          </p>
        </div>
      </div>

      <div className="flex gap-4 border-b mb-4">
        <button onClick={() => setTab('tasks')} className={`pb-2 text-sm font-medium border-b-2 transition ${tab === 'tasks' ? 'border-blue-600 text-blue-600' : 'border-transparent text-gray-500 hover:text-gray-700'}`}>
          Download Tasks ({taskTotal})
        </button>
        <button onClick={() => setTab('decisions')} className={`pb-2 text-sm font-medium border-b-2 transition ${tab === 'decisions' ? 'border-blue-600 text-blue-600' : 'border-transparent text-gray-500 hover:text-gray-700'}`}>
          Pending Decisions ({decTotal})
        </button>
        <button onClick={() => setTab('filters')} className={`pb-2 text-sm font-medium border-b-2 transition ${tab === 'filters' ? 'border-blue-600 text-blue-600' : 'border-transparent text-gray-500 hover:text-gray-700'}`}>
          Filters ({agent.filters?.length || 0})
        </button>
      </div>

      {tab === 'tasks' && (
        <div className="bg-white rounded-xl border shadow-sm overflow-hidden">
          {tasks.length === 0 ? <p className="p-8 text-center text-gray-400 text-sm">No tasks yet</p> : (
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b">
                <tr>
                  <th className="text-left px-4 py-3 font-medium text-gray-600">Title</th>
                  <th className="text-left px-4 py-3 font-medium text-gray-600">Status</th>
                  <th className="text-left px-4 py-3 font-medium text-gray-600">Progress</th>
                  <th className="text-left px-4 py-3 font-medium text-gray-600">Speed</th>
                  <th className="text-right px-4 py-3 font-medium text-gray-600">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {tasks.map(t => (
                  <tr key={t.id} className="hover:bg-gray-50">
                    <td className="px-4 py-3 max-w-xs truncate font-medium" title={t.file_resource?.title_raw}>{t.file_resource?.title_raw || t.id.slice(0, 8)}</td>
                    <td className="px-4 py-3"><StatusBadge status={t.status} /></td>
                    <td className="px-4 py-3 w-48"><div className="flex items-center gap-2"><ProgressBar progress={t.progress} className="flex-1" /><span className="text-xs text-gray-500 w-12">{t.progress.toFixed(0)}%</span></div></td>
                    <td className="px-4 py-3 text-gray-500 text-xs">{formatSpeed(t.download_speed)} ETA:{formatEta(t.eta)}</td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-1">
                        {t.status === 'downloading' && <button onClick={() => tasksApi.pause(t.id).then(() => setTaskPage(p => p))} className="p-1.5 hover:bg-gray-100 rounded-lg" title="Pause"><Pause size={14} /></button>}
                        {t.status === 'paused' && <button onClick={() => tasksApi.resume(t.id).then(() => setTaskPage(p => p))} className="p-1.5 hover:bg-gray-100 rounded-lg" title="Resume"><Play size={14} className="text-green-600" /></button>}
                        {t.status === 'error' && <button onClick={() => tasksApi.retry(t.id).then(() => setTaskPage(p => p))} className="p-1.5 hover:bg-gray-100 rounded-lg" title="Retry"><RotateCcw size={14} className="text-blue-600" /></button>}
                        <button onClick={() => tasksApi.delete(t.id).then(() => setTaskPage(p => p))} className="p-1.5 hover:bg-red-50 rounded-lg" title="Delete"><Trash2 size={14} className="text-red-500" /></button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div className="px-4 pb-4"><Pagination page={taskPage} pageSize={20} total={taskTotal} onPageChange={setTaskPage} /></div>
        </div>
      )}

      {tab === 'decisions' && (
        <div className="space-y-3">
          {decisions.length === 0 ? <p className="bg-white rounded-xl border p-8 text-center text-gray-400 text-sm">No pending decisions</p> : decisions.map(d => (
            <div key={d.id} className="bg-white rounded-xl border shadow-sm p-4">
              <div className="flex items-start justify-between mb-2">
                <div>
                  <p className="font-medium text-sm">{d.reason}</p>
                  <p className="text-xs text-gray-500 mt-1">{d.candidates.length} candidates &middot; {timeAgo(d.created_at)}</p>
                  {d.llm_suggestion && <p className="text-xs text-blue-600 mt-1">LLM: {d.llm_suggestion}</p>}
                </div>
                <StatusBadge status={d.status} />
              </div>
              {d.status === 'pending' && (
                <div className="flex gap-2 mt-3">
                  {d.candidates.map(cid => (
                    <button key={cid} onClick={() => decisionsApi.confirm(d.id, cid).then(() => setDecPage(p => p))}
                      className="flex items-center gap-1 px-3 py-1.5 bg-blue-600 text-white rounded-lg text-xs hover:bg-blue-700">
                      <CheckCircle size={12} /> {cid.slice(0, 8)}
                    </button>
                  ))}
                  <button onClick={() => decisionsApi.skip(d.id).then(() => setDecPage(p => p))}
                    className="flex items-center gap-1 px-3 py-1.5 border rounded-lg text-xs hover:bg-gray-50">
                    <SkipForward size={12} /> Skip
                  </button>
                </div>
              )}
            </div>
          ))}
          <Pagination page={decPage} pageSize={20} total={decTotal} onPageChange={setDecPage} />
        </div>
      )}

      {tab === 'filters' && (
        <div>
          <div className="flex items-center justify-between mb-4">
            <p className="text-sm text-gray-500">
              Test filters against the channel's synced resources to see which ones match.
            </p>
            <button
              onClick={async () => {
                if (!id) return;
                setTestingFilters(true);
                const res = await agentsApi.testFilters(id);
                setTestingFilters(false);
                if (res.success) setFilterTest(res.data);
              }}
              disabled={testingFilters || !agent.filters?.length}
              className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
            >
              {testingFilters ? <Loader size={14} className="animate-spin" /> : <FlaskConical size={14} />}
              {testingFilters ? 'Testing...' : 'Test Filters'}
            </button>
          </div>

          {!agent.filters?.length && (
            <p className="bg-white rounded-xl border p-8 text-center text-gray-400 text-sm">No filters configured for this agent.</p>
          )}

          {filterTest && (
            <div>
              <div className="flex gap-4 mb-4">
                <div className="bg-white rounded-lg border px-4 py-2 text-sm">
                  <span className="text-gray-500">Total: </span><span className="font-medium">{filterTest.total_resources}</span>
                </div>
                <div className="bg-emerald-50 border border-emerald-200 rounded-lg px-4 py-2 text-sm">
                  <span className="text-emerald-600">Matched: </span><span className="font-medium">{filterTest.matched}</span>
                </div>
                <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-2 text-sm">
                  <span className="text-red-600">Failed: </span><span className="font-medium">{filterTest.failed}</span>
                </div>
              </div>
              <div className="space-y-2 max-h-[600px] overflow-auto">
                {filterTest.results.map(r => (
                  <div key={r.resource_id} className={`bg-white rounded-lg border p-3 text-sm ${r.all_required_passed ? 'border-emerald-200' : 'border-red-200'}`}>
                    <div className="flex items-center gap-2 mb-2">
                      {r.all_required_passed ? <CheckCircle size={14} className="text-emerald-500" /> : <span className="text-red-500 text-xs font-bold">FAIL</span>}
                      <span className="font-medium truncate flex-1" title={r.title_raw}>{r.title_raw}</span>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {r.filters.map((f, i) => (
                        <span key={i} className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs ${f.passed ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700'}`}>
                          {f.field}: {f.resource_value || '∅'} {f.passed ? '=' : '≠'} {f.filter_value}
                          {f.is_required && <span className="font-bold">*</span>}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
