import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useTasks, useCreateTask } from '../api/hooks';
import { StatusBadge, CostBadge } from '../components/StatusBadge';
import type { Task, TaskCreate, TaskStatus } from '../types';

const STATUS_FILTERS: Array<{ label: string; value: TaskStatus | 'all' }> = [
  { label: 'All', value: 'all' },
  { label: 'Pending', value: 'pending' },
  { label: 'Running', value: 'running' },
  { label: 'Done', value: 'done' },
  { label: 'Failed', value: 'failed' },
];

const LANGUAGES = ['python', 'typescript', 'javascript', 'go', 'rust', 'java', 'c', 'cpp', 'other'];
const SETUP_PRESETS = ['default', 'minimal', 'full', 'monorepo'];

const EMPTY_FORM: TaskCreate = {
  title: '',
  description: '',
  language: 'python',
  setup: 'default',
  git_enabled: false,
};

export default function Tasks() {
  const [statusFilter, setStatusFilter] = useState<TaskStatus | 'all'>('all');
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState<TaskCreate>({ ...EMPTY_FORM });

  const { data: tasks, isLoading, error } = useTasks(
    statusFilter === 'all' ? undefined : statusFilter,
  );
  const createTask = useCreateTask();

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!form.title.trim()) return;
    await createTask.mutateAsync({
      ...form,
      title: form.title.trim(),
      description: form.description.trim() || form.title.trim(),
    });
    setForm({ ...EMPTY_FORM });
    setShowCreate(false);
  }

  if (isLoading) return <p className="text-slate-400">Loading...</p>;
  if (error) return <p className="text-red-400">Error loading tasks: {(error as Error).message}</p>;

  const taskList: Task[] = tasks ?? [];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">
            Tasks{' '}
            <span className="text-base font-normal text-slate-500">({taskList.length})</span>
          </h1>
        </div>
        <button
          onClick={() => setShowCreate(!showCreate)}
          className="px-4 py-2 bg-sky-600 hover:bg-sky-500 text-white text-sm font-medium rounded transition-colors"
        >
          {showCreate ? 'Cancel' : '+ New Task'}
        </button>
      </div>

      {/* Create Form */}
      {showCreate && (
        <form
          onSubmit={handleCreate}
          className="bg-slate-800 rounded-lg p-5 border border-slate-700 space-y-4"
        >
          <h2 className="text-sm font-semibold text-slate-300">Create New Task</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs text-slate-400 mb-1">Title</label>
              <input
                type="text"
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                placeholder="What need doing?"
                className="w-full bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-sky-500"
                required
              />
            </div>
            <div>
              <label className="block text-xs text-slate-400 mb-1">Language</label>
              <select
                value={form.language}
                onChange={(e) => setForm({ ...form, language: e.target.value })}
                className="w-full bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
              >
                {LANGUAGES.map((l) => (
                  <option key={l} value={l}>
                    {l}
                  </option>
                ))}
              </select>
            </div>
            <div className="md:col-span-2">
              <label className="block text-xs text-slate-400 mb-1">Description</label>
              <textarea
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="Describe the task..."
                rows={3}
                className="w-full bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-sky-500 resize-none"
              />
            </div>
            <div>
              <label className="block text-xs text-slate-400 mb-1">Setup Preset</label>
              <select
                value={form.setup}
                onChange={(e) => setForm({ ...form, setup: e.target.value })}
                className="w-full bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-sky-500"
              >
                {SETUP_PRESETS.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex items-end">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={form.git_enabled}
                  onChange={(e) => setForm({ ...form, git_enabled: e.target.checked })}
                  className="w-4 h-4 rounded border-slate-600 bg-slate-900 text-sky-500 focus:ring-sky-500"
                />
                <span className="text-sm text-slate-300">Enable Git</span>
              </label>
            </div>
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={() => {
                setShowCreate(false);
                setForm({ ...EMPTY_FORM });
              }}
              className="px-4 py-2 text-sm text-slate-400 hover:text-slate-200 transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!form.title.trim() || createTask.isPending}
              className="px-5 py-2 bg-emerald-600 hover:bg-emerald-500 disabled:bg-slate-600 disabled:cursor-not-allowed text-white text-sm font-medium rounded transition-colors"
            >
              {createTask.isPending ? 'Creating...' : 'Create Task'}
            </button>
          </div>
          {createTask.isError && (
            <p className="text-red-400 text-xs">
              {(createTask.error as Error).message}
            </p>
          )}
        </form>
      )}

      {/* Filter Bar */}
      <div className="flex gap-2">
        {STATUS_FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => setStatusFilter(f.value)}
            className={`px-3 py-1.5 text-xs font-medium rounded transition-colors ${
              statusFilter === f.value
                ? 'bg-sky-600 text-white'
                : 'bg-slate-800 text-slate-400 hover:text-slate-200 hover:bg-slate-700'
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Task List */}
      {taskList.length === 0 ? (
        <div className="text-center py-12">
          <p className="text-slate-500">No tasks found</p>
        </div>
      ) : (
        <div className="space-y-3">
          {taskList.map((task: Task) => (
            <Link
              key={task.id}
              to={`/tasks/${task.id}`}
              className="block bg-slate-800 rounded-lg p-4 border border-slate-700 hover:border-sky-600/50 transition-colors"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <h3 className="text-sm font-semibold text-white truncate">{task.title}</h3>
                    <StatusBadge status={task.status} />
                  </div>
                  <div className="flex items-center gap-3 text-xs text-slate-500">
                    <span>Priority {task.priority}</span>
                    <span className="text-slate-600">•</span>
                    <span>{task.language}</span>
                    <span className="text-slate-600">•</span>
                    <span>{new Date(task.created_at).toLocaleString()}</span>
                  </div>
                </div>
                <div className="shrink-0 flex items-center gap-2">
                  {task.ledger && <CostBadge cost={task.ledger.cost_usd} />}
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
