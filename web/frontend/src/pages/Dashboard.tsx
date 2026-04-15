import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useCreateTask, useDashboard, usePipelineStatus, useRunTask } from '../api/hooks';
import { StatusBadge } from '../components/StatusBadge';
import type { ActivityEvent, HardwareEndpoint } from '../types';

const ACTIVITY_ICONS: Record<string, string> = {
  task_created: '📝',
  task_started: '▶',
  task_done: '✅',
  task_failed: '❌',
  tournament_started: '⚔️',
  tournament_completed: '🏆',
};

function CardSkeleton() {
  return <div className="h-24 animate-pulse rounded-lg border border-slate-700 bg-slate-800/50" />;
}

function SectionSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4">
      <div className="mb-4 h-4 w-40 animate-pulse rounded bg-slate-700" />
      <div className="space-y-3">
        {Array.from({ length: rows }).map((_, index) => (
          <div key={index} className="h-12 animate-pulse rounded bg-slate-900/80" />
        ))}
      </div>
    </div>
  );
}

function PipelineIndicator({ paused, isLoading }: { paused?: boolean; isLoading: boolean }) {
  if (isLoading) {
    return <div className="h-9 w-40 animate-pulse rounded-full bg-slate-800" />;
  }

  const isPaused = Boolean(paused);
  return (
    <div
      className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm font-medium ${
        isPaused
          ? 'border-amber-700 bg-amber-950/60 text-amber-300'
          : 'border-emerald-700 bg-emerald-950/60 text-emerald-300'
      }`}
    >
      <span>{isPaused ? '⏸' : '▶'}</span>
      <span>Pipeline: {isPaused ? 'Paused' : 'Running'}</span>
    </div>
  );
}

function formatDetailValue(value: unknown): string {
  if (value == null) return '—';
  if (Array.isArray(value)) return value.map((item) => formatDetailValue(item)).join(', ');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function parseDetail(detail: string | null): Array<{ key: string; value: string }> {
  if (!detail) return [];

  try {
    const parsed: unknown = JSON.parse(detail);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return Object.entries(parsed as Record<string, unknown>).map(([key, value]) => ({
        key,
        value: formatDetailValue(value),
      }));
    }
    return [{ key: 'detail', value: formatDetailValue(parsed) }];
  } catch {
    return [{ key: 'detail', value: detail }];
  }
}

export default function Dashboard() {
  const { data, isLoading, error } = useDashboard();
  const { data: pipelineStatus, isLoading: pipelineLoading } = usePipelineStatus();
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

  if (isLoading) {
    return (
      <div className="space-y-8">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="space-y-2">
            <div className="h-8 w-40 animate-pulse rounded bg-slate-800" />
            <div className="h-4 w-52 animate-pulse rounded bg-slate-800" />
          </div>
          <PipelineIndicator isLoading paused />
        </div>
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4 xl:grid-cols-5">
          {Array.from({ length: 5 }).map((_, index) => (
            <CardSkeleton key={index} />
          ))}
        </div>
        <SectionSkeleton rows={2} />
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <SectionSkeleton rows={4} />
          <SectionSkeleton rows={5} />
        </div>
      </div>
    );
  }

  if (error) {
    return <p className="text-red-400">Error loading dashboard: {(error as Error).message}</p>;
  }

  if (!data) return null;

  const stats = [
    { label: 'Queue Depth', value: data.queue_depth, icon: '📬', accent: 'text-sky-400' },
    { label: 'Active Runs', value: data.active_runs, icon: '⚡', accent: 'text-amber-400' },
    { label: 'Completed Today', value: data.completed_today, icon: '✅', accent: 'text-emerald-400' },
    { label: 'Failed Today', value: data.failed_today, icon: '💥', accent: 'text-red-400' },
    { label: 'Cost Today', value: `$${data.total_cost_today.toFixed(4)}`, icon: '💰', accent: 'text-green-400' },
  ];

  const isLaunching = createTask.isPending || runTask.isPending;

  return (
    <div className="space-y-8">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Dashboard</h1>
          <p className="mt-1 text-sm text-slate-500">caveman watch all thing</p>
        </div>
        <PipelineIndicator paused={pipelineStatus?.paused} isLoading={pipelineLoading} />
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4 xl:grid-cols-5">
        {stats.map((s) => (
          <div key={s.label} className="rounded-lg border border-slate-700 bg-slate-800/50 p-4">
            <div className="flex items-center justify-between">
              <span className="text-sm text-slate-400">{s.label}</span>
              <span className="text-lg">{s.icon}</span>
            </div>
            <p className={`mt-2 text-2xl font-bold ${s.accent}`}>{s.value}</p>
          </div>
        ))}
      </div>

      <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4">
        <h2 className="mb-3 text-sm font-semibold text-slate-300">⚡ Quick Launch</h2>
        <div className="flex flex-col gap-3 lg:flex-row">
          <input
            type="text"
            placeholder="Task title"
            value={quickTitle}
            onChange={(e) => setQuickTitle(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && void handleQuickLaunch()}
            className="flex-1 rounded border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-sky-500 focus:outline-none"
          />
          <input
            type="text"
            placeholder="Description (optional)"
            value={quickDesc}
            onChange={(e) => setQuickDesc(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && void handleQuickLaunch()}
            className="flex-1 rounded border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-sky-500 focus:outline-none"
          />
          <button
            onClick={() => void handleQuickLaunch()}
            disabled={!quickTitle.trim() || isLaunching}
            className="rounded-lg bg-sky-600 px-5 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-500 disabled:cursor-not-allowed disabled:bg-slate-600"
          >
            {isLaunching ? 'Launching...' : 'Run'}
          </button>
        </div>
        {(createTask.isError || runTask.isError) && (
          <p className="mt-2 text-xs text-red-400">
            {(createTask.error as Error)?.message || (runTask.error as Error)?.message}
          </p>
        )}
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-300">🖥️ Hardware Status</h2>
          {data.hardware_status.length === 0 ? (
            <div className="rounded-lg border border-dashed border-slate-700 bg-slate-900/60 p-6 text-sm text-slate-500">
              No hardware endpoints yet. Caveman says wire up a model box and come back.
            </div>
          ) : (
            <div className="space-y-2">
              {data.hardware_status.map((ep: HardwareEndpoint) => (
                <div key={ep.name} className="flex items-center justify-between rounded-lg bg-slate-900 px-3 py-2">
                  <div className="flex items-center gap-2">
                    <span className={`h-2 w-2 rounded-full ${ep.alive ? 'bg-emerald-400' : 'bg-red-500'}`} />
                    <span className="text-sm text-slate-200">{ep.name}</span>
                  </div>
                  <span className="text-xs text-slate-500">{ep.response_ms !== null ? `${ep.response_ms}ms` : '—'}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-300">📡 Recent Activity</h2>
          {data.recent_activity.length === 0 ? (
            <div className="rounded-lg border border-dashed border-slate-700 bg-slate-900/60 p-6 text-sm text-slate-500">
              No recent activity. Start task, start tournament, then dashboard make pretty story.
            </div>
          ) : (
            <div className="space-y-2 pr-1">
              {data.recent_activity.slice(0, 20).map((ev: ActivityEvent) => {
                const details = parseDetail(ev.detail);
                const icon = ACTIVITY_ICONS[ev.event] ?? '📡';
                return (
                  <div key={ev.id} className="rounded-lg border border-slate-700/70 bg-slate-900/60 p-3">
                    <div className="flex items-start gap-3">
                      <div className="pt-0.5 text-lg">{icon}</div>
                      <div className="min-w-0 flex-1 space-y-1.5">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-medium text-slate-200">{ev.event}</span>
                          {ev.entity_type && <StatusBadge status={ev.entity_type} />}
                          {ev.entity_type === 'task' && ev.entity_id ? (
                            <Link to={`/tasks/${ev.entity_id}`} className="text-xs text-sky-400 hover:text-sky-300 hover:underline">
                              {ev.entity_id}
                            </Link>
                          ) : ev.entity_id ? (
                            <span className="text-xs text-slate-500">{ev.entity_id}</span>
                          ) : null}
                        </div>
                        {details.length > 0 && (
                          <dl className="grid grid-cols-1 gap-x-3 gap-y-1 text-xs sm:grid-cols-2">
                            {details.map((detail) => (
                              <div key={`${ev.id}-${detail.key}`} className="flex gap-2">
                                <dt className="shrink-0 text-slate-500">{detail.key}:</dt>
                                <dd className="min-w-0 break-words text-slate-400">{detail.value}</dd>
                              </div>
                            ))}
                          </dl>
                        )}
                        <p className="text-xs text-slate-600">{new Date(ev.created_at).toLocaleString()}</p>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
