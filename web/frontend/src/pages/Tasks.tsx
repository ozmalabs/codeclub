import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  useBulkCancelTasks,
  useBulkDeleteTasks,
  useBulkRunTasks,
  useCreateTask,
  usePausePipeline,
  usePipelineStatus,
  useResumePipeline,
  useTasks,
} from '../api/hooks';
import { CostBadge, StatusBadge } from '../components/StatusBadge';
import type { Task, TaskCreate, TaskStatus } from '../types';

const STATUS_FILTERS: Array<{ label: string; value: TaskStatus | 'all' }> = [
  { label: 'All', value: 'all' },
  { label: 'Pending', value: 'pending' },
  { label: 'Queued', value: 'queued' },
  { label: 'Running', value: 'running' },
  { label: 'Done', value: 'done' },
  { label: 'Failed', value: 'failed' },
];

const LANGUAGES = ['python', 'typescript', 'javascript', 'go', 'rust', 'java', 'c', 'cpp', 'other'];
const SETUP_PRESETS = ['default', 'minimal', 'full', 'monorepo'];
const EMPTY_TASKS: Task[] = [];

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
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

  const { data: tasks, isLoading, error } = useTasks(statusFilter === 'all' ? undefined : statusFilter);
  const { data: pipelineStatus, isLoading: pipelineLoading, error: pipelineError } = usePipelineStatus();
  const createTask = useCreateTask();
  const pausePipeline = usePausePipeline();
  const resumePipeline = useResumePipeline();
  const bulkRun = useBulkRunTasks();
  const bulkCancel = useBulkCancelTasks();
  const bulkDelete = useBulkDeleteTasks();

  const taskList = tasks ?? EMPTY_TASKS;
  const visibleIds = useMemo(() => taskList.map((task) => task.id), [taskList]);
  const selectedVisibleIds = useMemo(
    () => selectedIds.filter((id) => visibleIds.includes(id)),
    [selectedIds, visibleIds],
  );
  const allVisibleSelected = visibleIds.length > 0 && selectedVisibleIds.length === visibleIds.length;
  const queueDepth = pipelineStatus?.queue_depth ?? taskList.filter((task) => task.status === 'queued').length;
  const isPipelineBusy = pausePipeline.isPending || resumePipeline.isPending;
  const isBulkBusy = bulkRun.isPending || bulkCancel.isPending || bulkDelete.isPending;
  const bulkError = bulkRun.error ?? bulkCancel.error ?? bulkDelete.error;

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

  function toggleTaskSelection(taskId: string) {
    setSelectedIds((current) =>
      current.includes(taskId) ? current.filter((id) => id !== taskId) : [...current, taskId],
    );
  }

  function toggleAllVisible() {
    setSelectedIds(allVisibleSelected ? [] : visibleIds);
  }

  async function handleBulkRun() {
    if (selectedVisibleIds.length === 0) return;
    await bulkRun.mutateAsync(selectedVisibleIds);
    setSelectedIds([]);
  }

  async function handleBulkCancel() {
    if (selectedVisibleIds.length === 0) return;
    await bulkCancel.mutateAsync(selectedVisibleIds);
    setSelectedIds([]);
  }

  async function handleBulkDelete() {
    if (selectedVisibleIds.length === 0) return;
    if (!window.confirm(`Delete ${selectedVisibleIds.length} selected task${selectedVisibleIds.length === 1 ? '' : 's'}?`)) {
      return;
    }
    await bulkDelete.mutateAsync(selectedVisibleIds);
    setSelectedIds([]);
  }

  async function handleTogglePipeline() {
    if (pipelineStatus?.paused) {
      await resumePipeline.mutateAsync();
      return;
    }
    await pausePipeline.mutateAsync();
  }

  if (isLoading) return <p className="text-slate-400">Loading...</p>;
  if (error) return <p className="text-red-400">Error loading tasks: {(error as Error).message}</p>;

  return (
    <div className="space-y-6 pb-24">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">
            Tasks <span className="text-base font-normal text-slate-500">({taskList.length})</span>
          </h1>
          <p className="mt-1 text-sm text-slate-400">Track the queue, control the pipeline, and manage tasks in bulk.</p>
        </div>
        <button
          onClick={() => setShowCreate(!showCreate)}
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-500"
        >
          {showCreate ? 'Cancel' : '+ New Task'}
        </button>
      </div>

      <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span
              className={`inline-flex items-center gap-2 rounded-full px-3 py-1 font-medium ${
                pipelineStatus?.paused
                  ? 'bg-amber-950/70 text-amber-300'
                  : 'bg-emerald-950/70 text-emerald-300'
              }`}
            >
              <span
                className={`h-2.5 w-2.5 rounded-full ${
                  pipelineStatus?.paused ? 'bg-amber-400' : 'bg-emerald-400 animate-pulse'
                }`}
              />
              Pipeline {pipelineStatus?.paused ? 'paused' : 'running'}
            </span>
            <span className="text-slate-400">
              Queue depth: <span className="font-semibold text-slate-200">{queueDepth}</span>
            </span>
            {pipelineLoading && <span className="text-slate-500">Refreshing pipeline status…</span>}
          </div>
          <button
            onClick={() => void handleTogglePipeline()}
            disabled={isPipelineBusy || pipelineLoading}
            className={`rounded-lg px-4 py-2 text-sm font-medium text-white transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
              pipelineStatus?.paused ? 'bg-emerald-600 hover:bg-emerald-500' : 'bg-amber-600 hover:bg-amber-500'
            }`}
          >
            {isPipelineBusy ? 'Updating…' : pipelineStatus?.paused ? 'Resume Pipeline' : 'Pause Pipeline'}
          </button>
        </div>
        {pipelineError && <p className="mt-3 text-xs text-red-400">{(pipelineError as Error).message}</p>}
      </div>

      {showCreate && (
        <form onSubmit={handleCreate} className="space-y-4 rounded-lg border border-slate-700 bg-slate-800/50 p-5">
          <h2 className="text-sm font-semibold text-slate-300">Create New Task</h2>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs text-slate-400">Title</label>
              <input
                type="text"
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                placeholder="What need doing?"
                className="w-full rounded border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-sky-500 focus:outline-none"
                required
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-slate-400">Language</label>
              <select
                value={form.language}
                onChange={(e) => setForm({ ...form, language: e.target.value })}
                className="w-full rounded border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white focus:border-sky-500 focus:outline-none"
              >
                {LANGUAGES.map((language) => (
                  <option key={language} value={language}>
                    {language}
                  </option>
                ))}
              </select>
            </div>
            <div className="md:col-span-2">
              <label className="mb-1 block text-xs text-slate-400">Description</label>
              <textarea
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="Describe the task..."
                rows={3}
                className="w-full resize-none rounded border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-sky-500 focus:outline-none"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-slate-400">Setup Preset</label>
              <select
                value={form.setup}
                onChange={(e) => setForm({ ...form, setup: e.target.value })}
                className="w-full rounded border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white focus:border-sky-500 focus:outline-none"
              >
                {SETUP_PRESETS.map((preset) => (
                  <option key={preset} value={preset}>
                    {preset}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex items-end">
              <label className="flex cursor-pointer items-center gap-2">
                <input
                  type="checkbox"
                  checked={form.git_enabled}
                  onChange={(e) => setForm({ ...form, git_enabled: e.target.checked })}
                  className="h-4 w-4 rounded border-slate-600 bg-slate-900 text-sky-500 focus:ring-sky-500"
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
              className="px-4 py-2 text-sm text-slate-400 transition-colors hover:text-slate-200"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!form.title.trim() || createTask.isPending}
              className="rounded-lg bg-emerald-600 px-5 py-2 text-sm font-medium text-white transition-colors hover:bg-emerald-500 disabled:cursor-not-allowed disabled:bg-slate-600"
            >
              {createTask.isPending ? 'Creating...' : 'Create Task'}
            </button>
          </div>
          {createTask.isError && <p className="text-xs text-red-400">{(createTask.error as Error).message}</p>}
        </form>
      )}

      <div className="flex flex-col gap-3 rounded-lg border border-slate-700 bg-slate-800/50 p-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex flex-wrap gap-2">
          {STATUS_FILTERS.map((filter) => (
            <button
              key={filter.value}
              onClick={() => setStatusFilter(filter.value)}
              className={`rounded px-3 py-1.5 text-xs font-medium transition-colors ${
                statusFilter === filter.value
                  ? 'bg-sky-600 text-white'
                  : 'bg-slate-900 text-slate-400 hover:bg-slate-700 hover:text-slate-200'
              }`}
            >
              {filter.label}
            </button>
          ))}
        </div>
        <button
          onClick={toggleAllVisible}
          disabled={visibleIds.length === 0}
          className="self-start text-xs font-medium text-slate-400 transition-colors hover:text-sky-300 disabled:cursor-not-allowed disabled:text-slate-600"
        >
          {allVisibleSelected ? 'Deselect all' : 'Select all'}
        </button>
      </div>

      {taskList.length === 0 ? (
        <div className="py-12 text-center">
          <p className="text-slate-500">No tasks found</p>
        </div>
      ) : (
        <div className="space-y-3">
          {taskList.map((task) => {
            const selected = selectedVisibleIds.includes(task.id);
            return (
              <div
                key={task.id}
                className={`flex gap-3 rounded-lg border bg-slate-800/50 p-4 transition-colors ${
                  selected ? 'border-sky-500/70' : 'border-slate-700 hover:border-sky-600/50'
                }`}
              >
                <div className="flex items-start pt-1">
                  <input
                    type="checkbox"
                    checked={selected}
                    onChange={() => toggleTaskSelection(task.id)}
                    className="h-4 w-4 rounded border-slate-600 bg-slate-900 text-sky-500 focus:ring-sky-500"
                    aria-label={`Select task ${task.title}`}
                  />
                </div>
                <Link to={`/tasks/${task.id}`} className="block min-w-0 flex-1">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0 flex-1">
                      <div className="mb-1 flex items-center gap-2">
                        <h3 className="truncate text-sm font-semibold text-white">{task.title}</h3>
                        <StatusBadge status={task.status} />
                      </div>
                      <div className="flex flex-wrap items-center gap-3 text-xs text-slate-500">
                        <span>Priority {task.priority}</span>
                        <span className="text-slate-600">•</span>
                        <span>{task.language}</span>
                        <span className="text-slate-600">•</span>
                        <span>{new Date(task.created_at).toLocaleString()}</span>
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">{task.ledger && <CostBadge cost={task.ledger.cost_usd} />}</div>
                  </div>
                </Link>
              </div>
            );
          })}
        </div>
      )}

      {selectedVisibleIds.length > 0 && (
        <div className="sticky bottom-4 z-20 rounded-lg border border-slate-700 bg-slate-900/95 p-4 shadow-lg shadow-slate-950/50 backdrop-blur">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <p className="text-sm text-slate-300">
              <span className="font-semibold text-white">{selectedVisibleIds.length}</span> task
              {selectedVisibleIds.length === 1 ? '' : 's'} selected
            </p>
            <div className="flex flex-wrap gap-2">
              <button
                onClick={() => void handleBulkRun()}
                disabled={isBulkBusy}
                className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {bulkRun.isPending ? 'Running…' : 'Run Selected'}
              </button>
              <button
                onClick={() => void handleBulkCancel()}
                disabled={isBulkBusy}
                className="rounded-lg bg-amber-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-amber-500 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {bulkCancel.isPending ? 'Cancelling…' : 'Cancel Selected'}
              </button>
              <button
                onClick={() => void handleBulkDelete()}
                disabled={isBulkBusy}
                className="rounded-lg bg-red-700 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-red-600 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {bulkDelete.isPending ? 'Deleting…' : 'Delete Selected'}
              </button>
            </div>
          </div>
          {bulkError && <p className="mt-3 text-xs text-red-400">{(bulkError as Error).message}</p>}
        </div>
      )}
    </div>
  );
}
