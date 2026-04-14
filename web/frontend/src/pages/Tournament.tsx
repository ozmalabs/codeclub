import { useState, useMemo } from 'react';
import { useTournamentResults } from '../api/hooks';
import type { TournamentResult } from '../types';

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
  const { data: results, isLoading } = useTournamentResults();
  const [sortKey, setSortKey] = useState<SortKey>('fitness');
  const [sortAsc, setSortAsc] = useState(false);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

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

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-slate-400 animate-pulse">Loading tournament results…</p>
      </div>
    );
  }

  if (!results?.length) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-bold text-white">⚔️ Tournament</h1>
          <p className="text-slate-400 mt-1">model fight in cave arena</p>
        </div>
        <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-12 text-center">
          <p className="text-slate-400 text-lg mb-2">No tournament results yet.</p>
          <p className="text-slate-500 text-sm font-mono">
            Run <code className="bg-slate-700 px-2 py-0.5 rounded">python tournament.py</code> to generate some.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">⚔️ Tournament</h1>
          <p className="text-slate-400 mt-1">model fight in cave arena</p>
        </div>
        <span className="text-sm text-slate-500">
          {results.length} result{results.length !== 1 && 's'} · {grouped.size} task
          {grouped.size !== 1 && 's'}
        </span>
      </div>

      {/* Table */}
      <div className="overflow-x-auto rounded-lg border border-slate-700">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-slate-800 border-b border-slate-700">
              {/* Expand toggle column */}
              <th className="w-8" />
              {COLUMNS.map((col) => (
                <th
                  key={col.key}
                  onClick={() => handleSort(col.key)}
                  className={`px-3 py-2.5 font-medium cursor-pointer select-none hover:text-sky-400 transition-colors ${
                    col.align === 'right' ? 'text-right' : 'text-left'
                  } ${sortKey === col.key ? 'text-sky-400' : 'text-slate-400'}`}
                >
                  {col.label}
                  {sortKey === col.key && (
                    <span className="ml-1">{sortAsc ? '↑' : '↓'}</span>
                  )}
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
                  taskId={taskId}
                  rows={rows}
                  isCollapsed={isCollapsed}
                  onToggle={() => toggleGroup(taskId)}
                />
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function GroupRows({
  taskId: _taskId,
  rows,
  isCollapsed,
  onToggle,
}: {
  taskId: string;
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
