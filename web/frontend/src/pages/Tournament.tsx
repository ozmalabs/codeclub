import { useEffect, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { subscribeSSE } from '../api/client';
import { useStartTournament, useTournamentLeaderboard, useTournamentResults, useTournamentTasks } from '../api/hooks';
import type { LeaderboardEntry, TournamentFightResult, TournamentResult, TournamentStartOpts, TournamentTask } from '../types';

type SortKey = keyof Pick<
  TournamentResult,
  'task_id' | 'mode' | 'model' | 'mapper' | 'quality' | 'tests_passed' | 'elapsed_s' | 'cost_usd' | 'fitness' | 'smash_fit'
>;

const COLUMNS: { key: SortKey; label: string; align?: 'right' }[] = [
  { key: 'task_id', label: 'Task' },
  { key: 'mode', label: 'Mode' },
  { key: 'model', label: 'Model' },
  { key: 'mapper', label: 'Mapper' },
  { key: 'quality', label: 'Quality', align: 'right' },
  { key: 'tests_passed', label: 'Tests', align: 'right' },
  { key: 'elapsed_s', label: 'Time', align: 'right' },
  { key: 'cost_usd', label: 'Cost', align: 'right' },
  { key: 'fitness', label: 'Fitness', align: 'right' },
  { key: 'smash_fit', label: 'Smash Fit', align: 'right' },
];

const OPTIMIZE_OPTIONS: TournamentStartOpts['optimize'][] = ['balanced', 'fastest', 'greenest', 'cheapest'];

function qualityColor(v: number | null | undefined): string {
  if (v == null) return 'text-slate-500';
  if (v >= 0.8) return 'text-emerald-400';
  if (v >= 0.5) return 'text-amber-400';
  return 'text-red-400';
}

function fitnessColor(v: number | null | undefined): string {
  if (v == null) return 'text-slate-500';
  if (v >= 0.7) return 'text-emerald-400';
  if (v >= 0.4) return 'text-amber-400';
  return 'text-red-400';
}

function fmt(key: SortKey, row: TournamentResult): string {
  const v = row[key];
  if (v == null) return '—';
  switch (key) {
    case 'quality':
    case 'fitness':
    case 'smash_fit':
      return (v as number).toFixed(2);
    case 'elapsed_s':
      return `${(v as number).toFixed(1)}s`;
    case 'cost_usd':
      return `$${(v as number).toFixed(4)}`;
    case 'tests_passed':
      return `${row.tests_passed}/${row.tests_total}`;
    default:
      return String(v);
  }
}

function cellColor(key: SortKey, row: TournamentResult): string {
  if (key === 'quality') return qualityColor(row.quality);
  if (key === 'fitness' || key === 'smash_fit') return fitnessColor(row[key]);
  return 'text-slate-300';
}

export default function Tournament() {
  const qc = useQueryClient();
  const { data: results, isLoading } = useTournamentResults();
  const { data: tasks } = useTournamentTasks();
  const { data: leaderboard } = useTournamentLeaderboard();
  const startTournament = useStartTournament();
  const [sortKey, setSortKey] = useState<SortKey>('fitness');
  const [sortAsc, setSortAsc] = useState(false);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [formOpen, setFormOpen] = useState(true);
  const [optimize, setOptimize] = useState<NonNullable<TournamentStartOpts['optimize']>>('balanced');
  const [selectedTaskId, setSelectedTaskId] = useState('');
  const [quickMode, setQuickMode] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<'idle' | 'connecting' | 'running' | 'done'>('idle');
  const [liveFights, setLiveFights] = useState<TournamentFightResult[]>([]);
  const [taskStatuses, setTaskStatuses] = useState<Record<string, string>>({});
  const [doneSummary, setDoneSummary] = useState<{ champions: number; total_fights: number } | null>(null);

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortAsc((a) => !a);
    } else {
      setSortKey(key);
      setSortAsc(false);
    }
  };

  const sorted = useMemo(() => {
    if (!results?.length) return [];
    return [...results].sort((a, b) => {
      const av = a[sortKey] ?? -Infinity;
      const bv = b[sortKey] ?? -Infinity;
      if (av < bv) return sortAsc ? -1 : 1;
      if (av > bv) return sortAsc ? 1 : -1;
      return 0;
    });
  }, [results, sortKey, sortAsc]);

  const grouped = useMemo(() => {
    const map = new Map<string, TournamentResult[]>();
    for (const r of sorted) {
      const key = r.task_id;
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(r);
    }
    return map;
  }, [sorted]);

  const toggleGroup = (taskId: string) =>
    setCollapsed((c) => ({ ...c, [taskId]: !c[taskId] }));

  const sortedLeaderboard = useMemo(() => {
    return [...(leaderboard ?? [])].sort((a, b) => b.wins - a.wins || b.avg_fitness - a.avg_fitness);
  }, [leaderboard]);

  const selectedTask = useMemo(
    () => tasks?.find((task) => task.name === selectedTaskId) ?? null,
    [tasks, selectedTaskId]
  );

  const showLivePanel = startTournament.isPending || runId !== null || streamState !== 'idle';
  const progressPercent = doneSummary?.total_fights
    ? Math.min(100, Math.round((liveFights.length / doneSummary.total_fights) * 100))
    : null;

  useEffect(() => {
    if (!runId) return;
    setStreamState('connecting');
    const unsubscribe = subscribeSSE(`/tournament/stream/${runId}`, (event) => {
      if (event.type === 'fight') {
        setStreamState('running');
        setLiveFights((prev) => [event.data, ...prev]);
        return;
      }
      if (event.type === 'task') {
        setTaskStatuses((prev) => ({ ...prev, [event.data.task_id]: event.data.status }));
        return;
      }
      if (event.type === 'done' && 'champions' in event.data) {
        setStreamState('done');
        setDoneSummary(event.data);
        void qc.invalidateQueries({ queryKey: ['tournament-results'] });
        void qc.invalidateQueries({ queryKey: ['tournament-leaderboard'] });
      }
    });
    return unsubscribe;
  }, [runId, qc]);

  async function handleStartTournament() {
    const payload: TournamentStartOpts = {
      optimize,
      quick: quickMode,
      ...(selectedTaskId ? { task_id: selectedTaskId } : {}),
    };
    const started = await startTournament.mutateAsync(payload);
    setRunId(started.run_id);
    setStreamState('connecting');
    setLiveFights([]);
    setTaskStatuses({});
    setDoneSummary(null);
  }

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <h1 className="text-2xl font-bold text-white">⚔️ Tournament</h1>
            <p className="mt-1 text-slate-400">model fight in cave arena</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm text-slate-500">
              {(results?.length ?? 0)} result{results && results.length !== 1 && 's'} · {grouped.size} task
              {grouped.size !== 1 && 's'}
            </span>
            <button
              type="button"
              onClick={() => setFormOpen((open) => !open)}
              className="rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-500"
            >
              {formOpen ? 'Hide cave controls' : 'Start Tournament'}
            </button>
          </div>
        </div>

        {formOpen && (
          <div className="mt-5 border-t border-slate-700 pt-5">
            <div className="grid gap-4 md:grid-cols-3">
              <label className="space-y-2">
                <span className="text-xs uppercase tracking-wide text-slate-400">Optimize strategy</span>
                <select
                  value={optimize}
                  onChange={(e) => setOptimize(e.target.value as NonNullable<TournamentStartOpts['optimize']>)}
                  className="w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 focus:border-sky-500 focus:outline-none"
                >
                  {OPTIMIZE_OPTIONS.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>

              <label className="space-y-2">
                <span className="text-xs uppercase tracking-wide text-slate-400">Task filter</span>
                <select
                  value={selectedTaskId}
                  onChange={(e) => setSelectedTaskId(e.target.value)}
                  className="w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-200 focus:border-sky-500 focus:outline-none"
                >
                  <option value="">All cave tasks</option>
                  {(tasks ?? []).map((task) => (
                    <option key={task.name} value={task.name}>
                      {task.name}
                    </option>
                  ))}
                </select>
              </label>

              <div className="flex items-end justify-between rounded-md border border-slate-700 bg-slate-900/60 px-4 py-3">
                <label className="flex items-center gap-3 text-sm text-slate-300">
                  <input
                    type="checkbox"
                    checked={quickMode}
                    onChange={(e) => setQuickMode(e.target.checked)}
                    className="h-4 w-4 rounded border-slate-600 bg-slate-800 text-sky-500 focus:ring-sky-500"
                  />
                  Quick mode
                </label>
                <button
                  type="button"
                  onClick={() => void handleStartTournament()}
                  disabled={startTournament.isPending}
                  className="rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-500 disabled:cursor-not-allowed disabled:bg-slate-700"
                >
                  {startTournament.isPending ? 'Sending warriors…' : 'Start Tournament'}
                </button>
              </div>
            </div>

            <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-slate-500">
              <span>Strategy: <span className="text-slate-300">{optimize}</span></span>
              <span>Task: <span className="text-slate-300">{selectedTask?.name ?? 'all tasks'}</span></span>
              <span>Quick: <span className="text-slate-300">{quickMode ? 'yes' : 'no'}</span></span>
            </div>

            {startTournament.isError && (
              <p className="mt-3 text-sm text-red-400">
                Cave gate closed: {(startTournament.error as Error).message}
              </p>
            )}
          </div>
        )}
      </div>

      {showLivePanel && (
        <section className="rounded-lg border border-slate-700 bg-slate-800/50 p-5">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <h2 className="text-lg font-semibold text-slate-100">📡 Live Stream</h2>
              <p className="mt-1 text-sm text-slate-400">
                {startTournament.isPending
                  ? 'cave runner asking arena chief…'
                  : streamState === 'done'
                    ? 'tournament complete, cave cheers loud'
                    : `watch fight sparks fly${runId ? ` · run ${runId}` : ''}`}
              </p>
            </div>
            <div className="text-sm text-slate-400">
              <div>Fights seen: <span className="text-slate-200">{liveFights.length}</span></div>
              {doneSummary && (
                <div>
                  Champions: <span className="text-emerald-400">{doneSummary.champions}</span> / {doneSummary.total_fights}
                </div>
              )}
            </div>
          </div>

          <div className="mt-4 h-2 overflow-hidden rounded-full bg-slate-900">
            {progressPercent != null ? (
              <div className="h-full bg-emerald-500 transition-all" style={{ width: `${progressPercent}%` }} />
            ) : (
              <div className="h-full w-1/3 animate-pulse rounded-full bg-sky-500" />
            )}
          </div>

          {Object.keys(taskStatuses).length > 0 && (
            <div className="mt-4 flex flex-wrap gap-2">
              {Object.entries(taskStatuses).map(([taskId, status]) => (
                <span
                  key={taskId}
                  className="rounded-full border border-slate-700 bg-slate-900 px-2.5 py-1 text-xs text-slate-300"
                >
                  {taskId}: <span className="text-sky-400">{status}</span>
                </span>
              ))}
            </div>
          )}

          <div className="mt-4 overflow-x-auto rounded-lg border border-slate-700">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-700 bg-slate-900/80">
                  <th className="px-3 py-2 text-left font-medium text-slate-400">Task</th>
                  <th className="px-3 py-2 text-left font-medium text-slate-400">Model</th>
                  <th className="px-3 py-2 text-left font-medium text-slate-400">Mode</th>
                  <th className="px-3 py-2 text-right font-medium text-slate-400">Quality</th>
                  <th className="px-3 py-2 text-right font-medium text-slate-400">Fitness</th>
                  <th className="px-3 py-2 text-right font-medium text-slate-400">Cost</th>
                </tr>
              </thead>
              <tbody>
                {liveFights.length > 0 ? (
                  liveFights.map((fight, idx) => (
                    <tr key={`${fight.task_id}-${fight.model}-${fight.mode}-${idx}`} className="border-b border-slate-800/70">
                      <td className="px-3 py-2 text-slate-300">{fight.task_id}</td>
                      <td className="px-3 py-2 font-mono text-xs text-slate-300">{fight.model}</td>
                      <td className="px-3 py-2 text-slate-400">{fight.mode}</td>
                      <td className={`px-3 py-2 text-right ${qualityColor(fight.quality)}`}>{fight.quality.toFixed(2)}</td>
                      <td className={`px-3 py-2 text-right ${fitnessColor(fight.fitness)}`}>{fight.fitness.toFixed(2)}</td>
                      <td className="px-3 py-2 text-right text-slate-300">${fight.cost_usd.toFixed(4)}</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={6} className="px-3 py-6 text-center text-slate-500">
                      {startTournament.isPending ? 'warming up arena...' : 'waiting for first fight...'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="rounded-lg border border-slate-700 bg-slate-800/50 p-5">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold text-slate-100">🏆 Champion Leaderboard</h2>
            <p className="mt-1 text-sm text-slate-400">quality 1.0 heroes at top of cave wall</p>
          </div>
        </div>
        <div className="overflow-x-auto rounded-lg border border-slate-700">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-700 bg-slate-900/80">
                <th className="px-3 py-2 text-left font-medium text-slate-400">Model</th>
                <th className="px-3 py-2 text-right font-medium text-slate-400">Wins</th>
                <th className="px-3 py-2 text-right font-medium text-slate-400">Avg Fitness</th>
                <th className="px-3 py-2 text-right font-medium text-slate-400">Avg Cost</th>
                <th className="px-3 py-2 text-left font-medium text-slate-400">Best Task</th>
              </tr>
            </thead>
            <tbody>
              {sortedLeaderboard.length > 0 ? (
                sortedLeaderboard.map((entry) => <LeaderboardRow key={entry.model} entry={entry} />)
              ) : (
                <tr>
                  <td colSpan={5} className="px-3 py-6 text-center text-slate-500">
                    No champions carved in stone yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="rounded-lg border border-slate-700 bg-slate-800/50 p-5">
        <div className="mb-4">
          <h2 className="text-lg font-semibold text-slate-100">🪵 Task List</h2>
          <p className="mt-1 text-sm text-slate-400">which cave trials wait for the fighters</p>
        </div>
        <div className="overflow-x-auto rounded-lg border border-slate-700">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-700 bg-slate-900/80">
                <th className="px-3 py-2 text-left font-medium text-slate-400">Task</th>
                <th className="px-3 py-2 text-left font-medium text-slate-400">Language</th>
                <th className="px-3 py-2 text-right font-medium text-slate-400">Difficulty</th>
                <th className="px-3 py-2 text-right font-medium text-slate-400">Tests</th>
                <th className="px-3 py-2 text-left font-medium text-slate-400">Description</th>
              </tr>
            </thead>
            <tbody>
              {(tasks ?? []).length > 0 ? (
                (tasks ?? []).map((task) => <TaskRow key={task.name} task={task} />)
              ) : (
                <tr>
                  <td colSpan={5} className="px-3 py-6 text-center text-slate-500">
                    No cave tasks available.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="rounded-lg border border-slate-700 bg-slate-800/50">
        <div className="flex items-center justify-between border-b border-slate-700 px-5 py-4">
          <div>
            <h2 className="text-lg font-semibold text-slate-100">⚔️ Results Table</h2>
            <p className="mt-1 text-sm text-slate-400">sortable trophy pile grouped by task</p>
          </div>
        </div>

        {isLoading ? (
          <div className="flex h-40 items-center justify-center">
            <p className="animate-pulse text-slate-400">Loading tournament results…</p>
          </div>
        ) : !(results?.length ?? 0) ? (
          <div className="p-12 text-center">
            <p className="mb-2 text-lg text-slate-400">No tournament results yet.</p>
            <p className="font-mono text-sm text-slate-500">
              Run <code className="rounded bg-slate-700 px-2 py-0.5">python tournament.py</code> to generate some.
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-700 bg-slate-800">
                  <th className="w-8" />
                  {COLUMNS.map((col) => (
                    <th
                      key={col.key}
                      onClick={() => handleSort(col.key)}
                      className={`cursor-pointer select-none px-3 py-2.5 font-medium transition-colors hover:text-sky-400 ${
                        col.align === 'right' ? 'text-right' : 'text-left'
                      } ${sortKey === col.key ? 'text-sky-400' : 'text-slate-400'}`}
                    >
                      {col.label}
                      {sortKey === col.key && <span className="ml-1">{sortAsc ? '↑' : '↓'}</span>}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {[...grouped.entries()].map(([taskId, rows]) => {
                  const isCollapsed = collapsed[taskId] ?? false;
                  return (
                    <GroupRows
                      key={taskId}
                      rows={rows}
                      isCollapsed={isCollapsed}
                      onToggle={() => toggleGroup(taskId)}
                    />
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function LeaderboardRow({ entry }: { entry: LeaderboardEntry }) {
  return (
    <tr className="border-b border-slate-800/70">
      <td className="px-3 py-2 font-mono text-xs text-slate-200">{entry.model}</td>
      <td className="px-3 py-2 text-right text-emerald-400">{entry.wins}</td>
      <td className={`px-3 py-2 text-right ${fitnessColor(entry.avg_fitness)}`}>{entry.avg_fitness.toFixed(2)}</td>
      <td className="px-3 py-2 text-right text-slate-300">${entry.avg_cost.toFixed(4)}</td>
      <td className="px-3 py-2 text-slate-400">{entry.best_task}</td>
    </tr>
  );
}

function TaskRow({ task }: { task: TournamentTask }) {
  return (
    <tr className="border-b border-slate-800/70">
      <td className="px-3 py-2 text-slate-200">{task.name}</td>
      <td className="px-3 py-2 text-slate-400">{task.lang}</td>
      <td className="px-3 py-2 text-right text-slate-300">{task.base_difficulty}</td>
      <td className="px-3 py-2 text-right text-slate-300">{task.num_tests}</td>
      <td className="px-3 py-2 text-slate-400">{task.description}</td>
    </tr>
  );
}

function GroupRows({
  rows,
  isCollapsed,
  onToggle,
}: {
  rows: TournamentResult[];
  isCollapsed: boolean;
  onToggle: () => void;
}) {
  // First row always visible as the group header
  const [first, ...rest] = rows;
  const hasMultiple = rows.length > 1;

  return (
    <>
      {/* Group header row */}
      <tr
        className="border-b border-slate-700/50 bg-slate-900 hover:bg-slate-800/80 transition-colors"
        onClick={hasMultiple ? onToggle : undefined}
        style={hasMultiple ? { cursor: 'pointer' } : undefined}
      >
        <td className="pl-3 py-2 text-slate-500 text-xs w-8">
          {hasMultiple && (
            <span className="inline-block transition-transform" style={{ transform: isCollapsed ? '' : 'rotate(90deg)' }}>
              ▶
            </span>
          )}
        </td>
        <ResultCells row={first} showTask />
      </tr>

      {/* Expandable child rows */}
      {!isCollapsed &&
        rest.map((row) => (
          <tr
            key={row.id}
            className="border-b border-slate-800/50 bg-slate-900/50 hover:bg-slate-800/40 transition-colors"
          >
            <td />
            <ResultCells row={row} showTask={false} />
          </tr>
        ))}
    </>
  );
}

function ResultCells({ row, showTask }: { row: TournamentResult; showTask: boolean }) {
  return (
    <>
      {COLUMNS.map((col) => {
        const isTask = col.key === 'task_id';
        return (
          <td
            key={col.key}
            className={`px-3 py-2 whitespace-nowrap ${
              col.align === 'right' ? 'text-right' : 'text-left'
            } ${isTask && !showTask ? 'text-slate-600' : cellColor(col.key, row)} ${
              col.key === 'model' ? 'font-mono text-xs' : ''
            }`}
          >
            {isTask && !showTask ? '↳' : fmt(col.key, row)}
          </td>
        );
      })}
    </>
  );
}
