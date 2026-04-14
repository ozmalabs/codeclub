import { useState } from 'react';
import { useDashboard } from '../api/hooks';
import { useCreateTask } from '../api/hooks';
import { useRunTask } from '../api/hooks';
import { StatusBadge } from '../components/StatusBadge';
import type { HardwareEndpoint, ActivityEvent } from '../types';

export default function Dashboard() {
  const { data, isLoading, error } = useDashboard();
  const createTask = useCreateTask();
  const runTask = useRunTask();
  const [quickTitle, setQuickTitle] = useState('');
  const [quickDesc, setQuickDesc] = useState('');

  async function handleQuickLaunch() {
    if (!quickTitle.trim()) return;
    const task = await createTask.mutateAsync({
      title: quickTitle.trim(),
      description: quickDesc.trim() || quickTitle.trim(),
    });
    await runTask.mutateAsync(task.id);
    setQuickTitle('');
    setQuickDesc('');
  }

  if (isLoading) return <p className="text-slate-400">Loading...</p>;
  if (error) return <p className="text-red-400">Error loading dashboard: {(error as Error).message}</p>;
  if (!data) return null;

  const stats = [
    { label: 'Queue Depth', value: data.queue_depth, icon: '📬', accent: 'text-sky-400' },
    { label: 'Active Runs', value: data.active_runs, icon: '⚡', accent: 'text-amber-400' },
    { label: 'Completed Today', value: data.completed_today, icon: '✅', accent: 'text-emerald-400' },
    { label: 'Cost Today', value: `$${data.total_cost_today.toFixed(4)}`, icon: '💰', accent: 'text-green-400' },
  ];

  const isLaunching = createTask.isPending || runTask.isPending;

  return (
    <div className="space-y-8">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-white">Dashboard</h1>
        <p className="text-sm text-slate-500 mt-1">caveman watch all thing</p>
      </div>

      {/* Stat Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {stats.map((s) => (
          <div key={s.label} className="bg-slate-800 rounded-lg p-4 border border-slate-700">
            <div className="flex items-center justify-between">
              <span className="text-slate-400 text-sm">{s.label}</span>
              <span className="text-lg">{s.icon}</span>
            </div>
            <p className={`text-2xl font-bold mt-2 ${s.accent}`}>{s.value}</p>
          </div>
        ))}
      </div>

      {/* Quick Launch */}
      <div className="bg-slate-800 rounded-lg p-4 border border-slate-700">
        <h2 className="text-sm font-semibold text-slate-300 mb-3">⚡ Quick Launch</h2>
        <div className="flex gap-3">
          <input
            type="text"
            placeholder="Task title"
            value={quickTitle}
            onChange={(e) => setQuickTitle(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleQuickLaunch()}
            className="flex-1 bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-sky-500"
          />
          <input
            type="text"
            placeholder="Description (optional)"
            value={quickDesc}
            onChange={(e) => setQuickDesc(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleQuickLaunch()}
            className="flex-1 bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-sky-500"
          />
          <button
            onClick={handleQuickLaunch}
            disabled={!quickTitle.trim() || isLaunching}
            className="px-5 py-2 bg-sky-600 hover:bg-sky-500 disabled:bg-slate-600 disabled:cursor-not-allowed text-white text-sm font-medium rounded transition-colors"
          >
            {isLaunching ? 'Launching...' : 'Run'}
          </button>
        </div>
        {(createTask.isError || runTask.isError) && (
          <p className="text-red-400 text-xs mt-2">
            {(createTask.error as Error)?.message || (runTask.error as Error)?.message}
          </p>
        )}
      </div>

      {/* Hardware + Activity side by side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Hardware Status */}
        <div className="bg-slate-800 rounded-lg p-4 border border-slate-700">
          <h2 className="text-sm font-semibold text-slate-300 mb-3">🖥️ Hardware Status</h2>
          {data.hardware_status.length === 0 ? (
            <p className="text-slate-500 text-sm">No endpoints configured</p>
          ) : (
            <div className="space-y-2">
              {data.hardware_status.map((ep: HardwareEndpoint) => (
                <div
                  key={ep.name}
                  className="flex items-center justify-between bg-slate-900 rounded px-3 py-2"
                >
                  <div className="flex items-center gap-2">
                    <span
                      className={`w-2 h-2 rounded-full ${ep.alive ? 'bg-emerald-400' : 'bg-red-500'}`}
                    />
                    <span className="text-sm text-slate-200">{ep.name}</span>
                  </div>
                  <span className="text-xs text-slate-500">
                    {ep.response_ms !== null ? `${ep.response_ms}ms` : '—'}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Recent Activity */}
        <div className="bg-slate-800 rounded-lg p-4 border border-slate-700">
          <h2 className="text-sm font-semibold text-slate-300 mb-3">📡 Recent Activity</h2>
          {data.recent_activity.length === 0 ? (
            <p className="text-slate-500 text-sm">No recent activity</p>
          ) : (
            <div className="space-y-1 max-h-80 overflow-y-auto pr-1">
              {data.recent_activity.slice(0, 20).map((ev: ActivityEvent) => (
                <div key={ev.id} className="flex items-start gap-2 py-1.5 border-b border-slate-700/50 last:border-0">
                  <div className="w-1.5 h-1.5 rounded-full bg-sky-500 mt-1.5 shrink-0" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-medium text-slate-200">{ev.event}</span>
                      {ev.entity_type && (
                        <StatusBadge status={ev.entity_type} />
                      )}
                    </div>
                    {ev.detail && (
                      <p className="text-xs text-slate-500 truncate">{ev.detail}</p>
                    )}
                    <p className="text-xs text-slate-600">
                      {new Date(ev.created_at).toLocaleTimeString()}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
