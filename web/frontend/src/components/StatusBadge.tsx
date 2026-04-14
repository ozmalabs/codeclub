import type { TaskStatus } from '../types';

const STATUS_COLORS: Record<TaskStatus | string, string> = {
  pending: 'bg-slate-600 text-slate-200',
  queued: 'bg-blue-900 text-blue-300',
  running: 'bg-sky-800 text-sky-200',
  review: 'bg-purple-900 text-purple-300',
  fixing: 'bg-amber-900 text-amber-300',
  done: 'bg-emerald-900 text-emerald-300',
  failed: 'bg-red-900 text-red-300',
  cancelled: 'bg-gray-700 text-gray-400',
  passed: 'bg-emerald-900 text-emerald-300',
};

export function StatusBadge({ status }: { status: string }) {
  const color = STATUS_COLORS[status] ?? 'bg-slate-700 text-slate-300';
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${color}`}>
      {status}
    </span>
  );
}

export function CostBadge({ cost }: { cost: number }) {
  const formatted = cost < 0.01 ? `$${(cost * 100).toFixed(2)}¢` : `$${cost.toFixed(4)}`;
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-green-900 text-green-300">
      {formatted}
    </span>
  );
}

export function SmashBar({ low, sweet, high, min_clarity }: { low: number; sweet: number; high: number; min_clarity: number }) {
  return (
    <div className="flex items-center gap-1 text-xs">
      <span className="text-slate-400">🏏</span>
      <div className="relative w-24 h-3 bg-slate-700 rounded overflow-hidden">
        <div
          className="absolute h-full bg-amber-800 opacity-50"
          style={{ left: `${low}%`, width: `${high - low}%` }}
        />
        <div
          className="absolute h-full bg-amber-500"
          style={{ left: `${sweet - 2}%`, width: '4%' }}
        />
      </div>
      <span className="text-slate-500">{low}-{sweet}-{high}</span>
      <span className="text-slate-600">✨{min_clarity}+</span>
    </div>
  );
}
