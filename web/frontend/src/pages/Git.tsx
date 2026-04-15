import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  useCreateWorktree,
  useGitBranches,
  useGitWorktrees,
  useRemoveWorktree,
  useTasks,
} from '../api/hooks';
import type { Task, WorktreeInfo } from '../types';

function TableSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="h-14 animate-pulse rounded-lg bg-slate-900/80" />
      ))}
    </div>
  );
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-700 bg-slate-900/60 p-6 text-sm text-slate-500">
      <p className="font-medium text-slate-300">{title}</p>
      <p className="mt-1">{body}</p>
    </div>
  );
}

export default function Git() {
  const { data: worktrees, isLoading: worktreesLoading, error: worktreesError } = useGitWorktrees();
  const { data: branches, isLoading: branchesLoading, error: branchesError } = useGitBranches();
  const { data: tasks } = useTasks();
  const createWorktree = useCreateWorktree();
  const removeWorktree = useRemoveWorktree();

  const [showCreateModal, setShowCreateModal] = useState(false);
  const [selectedTaskId, setSelectedTaskId] = useState('');
  const [baseBranch, setBaseBranch] = useState('main');

  const taskList = tasks ?? [];
  const gitTasks = useMemo(() => taskList.filter((task) => task.git_enabled), [taskList]);
  const creatableTasks = useMemo(() => gitTasks.filter((task) => !task.worktree_path), [gitTasks]);

  useEffect(() => {
    if (showCreateModal && !selectedTaskId && creatableTasks.length > 0) {
      setSelectedTaskId(creatableTasks[0].id);
    }
  }, [creatableTasks, selectedTaskId, showCreateModal]);

  function matchTaskForWorktree(worktree: WorktreeInfo): Task | undefined {
    return taskList.find(
      (task) =>
        (task.worktree_path && task.worktree_path === worktree.path) ||
        (task.branch && task.branch === worktree.branch),
    );
  }

  async function handleCreateWorktree() {
    if (!selectedTaskId) return;
    await createWorktree.mutateAsync({ taskId: selectedTaskId, baseBranch: baseBranch.trim() || 'main' });
    setShowCreateModal(false);
    setBaseBranch('main');
  }

  async function handleRemoveWorktree(taskId: string) {
    if (!window.confirm('Remove this worktree? Caveman cannot unchop tree.')) {
      return;
    }
    await removeWorktree.mutateAsync(taskId);
  }

  return (
    <div className="space-y-6 pb-10">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Git Cave</h1>
          <p className="mt-1 text-sm text-slate-500">caveman track branches, worktrees, shiny pull request path</p>
        </div>
        <button
          type="button"
          onClick={() => setShowCreateModal(true)}
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-500"
        >
          + Create Worktree
        </button>
      </div>

      <section className="rounded-lg border border-slate-700 bg-slate-800/50 p-5">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold text-slate-200">🌿 Active Worktrees</h2>
            <p className="mt-1 text-xs text-slate-500">Branch caves with task branches marked loud.</p>
          </div>
        </div>

        {worktreesLoading ? (
          <TableSkeleton />
        ) : worktreesError ? (
          <p className="text-sm text-red-400">Failed to load worktrees: {(worktreesError as Error).message}</p>
        ) : !worktrees || worktrees.length === 0 ? (
          <EmptyState title="No active worktrees" body="Make one from task branch and keep git cave tidy." />
        ) : (
          <div className="overflow-hidden rounded-lg border border-slate-700">
            <div className="hidden grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_auto] gap-3 border-b border-slate-700 bg-slate-900/80 px-4 py-3 text-xs font-semibold uppercase tracking-wide text-slate-500 md:grid">
              <span>Path</span>
              <span>Branch</span>
              <span>Head</span>
              <span>Action</span>
            </div>
            <div className="divide-y divide-slate-700 bg-slate-900/40">
              {worktrees.map((worktree) => {
                const task = matchTaskForWorktree(worktree);
                return (
                  <div key={`${worktree.path}-${worktree.branch}`} className="grid gap-3 px-4 py-4 md:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)_auto] md:items-center">
                    <div>
                      <p className="text-xs uppercase tracking-wide text-slate-500 md:hidden">Path</p>
                      <code className="break-all text-xs text-slate-300">{worktree.path}</code>
                    </div>
                    <div>
                      <p className="text-xs uppercase tracking-wide text-slate-500 md:hidden">Branch</p>
                      <div className="flex flex-wrap items-center gap-2">
                        <code className="rounded bg-slate-800 px-2 py-1 text-xs text-sky-300">{worktree.branch}</code>
                        {worktree.is_task && (
                          <span className="rounded-full bg-emerald-950/70 px-2 py-0.5 text-[11px] font-medium text-emerald-300">
                            task branch
                          </span>
                        )}
                        {task && (
                          <Link to={`/tasks/${task.id}`} className="text-xs text-sky-400 hover:text-sky-300 hover:underline">
                            {task.id}
                          </Link>
                        )}
                      </div>
                    </div>
                    <div>
                      <p className="text-xs uppercase tracking-wide text-slate-500 md:hidden">Head</p>
                      <code className="text-xs text-slate-400">{worktree.head}</code>
                    </div>
                    <div>
                      {task ? (
                        <button
                          type="button"
                          onClick={() => void handleRemoveWorktree(task.id)}
                          disabled={removeWorktree.isPending}
                          className="rounded-lg bg-red-700 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-red-600 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          Remove
                        </button>
                      ) : (
                        <span className="text-xs text-slate-500">No task match</span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </section>

      <section className="rounded-lg border border-slate-700 bg-slate-800/50 p-5">
        <div className="mb-4">
          <h2 className="text-sm font-semibold text-slate-200">🪵 Branches</h2>
          <p className="mt-1 text-xs text-slate-500">Fresh branches first. Task branches glow a little.</p>
        </div>

        {branchesLoading ? (
          <TableSkeleton rows={6} />
        ) : branchesError ? (
          <p className="text-sm text-red-400">Failed to load branches: {(branchesError as Error).message}</p>
        ) : !branches || branches.length === 0 ? (
          <EmptyState title="No branches found" body="Git cave looks empty. Maybe backend still sleeping." />
        ) : (
          <div className="space-y-2">
            {branches.map((branch) => (
              <div
                key={branch.name}
                className={`flex flex-col gap-3 rounded-lg border px-4 py-3 md:flex-row md:items-center md:justify-between ${
                  branch.is_task_branch
                    ? 'border-sky-700 bg-sky-950/30'
                    : 'border-slate-700 bg-slate-900/50'
                }`}
              >
                <div className="min-w-0 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <code className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-200">{branch.name}</code>
                    {branch.is_task_branch && (
                      <span className="rounded-full bg-sky-900/70 px-2 py-0.5 text-[11px] font-medium text-sky-300">
                        task branch
                      </span>
                    )}
                  </div>
                  <div className="flex flex-wrap items-center gap-3 text-xs text-slate-500">
                    <span>SHA {branch.short_sha}</span>
                    <span>{new Date(branch.date).toLocaleString()}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {showCreateModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 px-4">
          <div className="w-full max-w-lg rounded-lg border border-slate-700 bg-slate-800 p-5 shadow-2xl">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="text-lg font-semibold text-white">Create Worktree</h2>
                <p className="mt-1 text-sm text-slate-500">Pick task, pick base branch, go bonk git.</p>
              </div>
              <button
                type="button"
                onClick={() => setShowCreateModal(false)}
                className="rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:border-slate-500 hover:text-slate-200"
              >
                ✕
              </button>
            </div>

            <div className="mt-5 space-y-4">
              <div>
                <label className="mb-1 block text-xs text-slate-400">Task</label>
                <select
                  value={selectedTaskId}
                  onChange={(e) => setSelectedTaskId(e.target.value)}
                  className="w-full rounded-lg border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white focus:border-sky-500 focus:outline-none"
                >
                  {creatableTasks.length === 0 ? (
                    <option value="">No git-enabled tasks without worktrees</option>
                  ) : (
                    creatableTasks.map((task) => (
                      <option key={task.id} value={task.id}>
                        {task.title} ({task.id})
                      </option>
                    ))
                  )}
                </select>
              </div>

              <div>
                <label className="mb-1 block text-xs text-slate-400">Base branch</label>
                <input
                  type="text"
                  value={baseBranch}
                  onChange={(e) => setBaseBranch(e.target.value)}
                  className="w-full rounded-lg border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-sky-500 focus:outline-none"
                  placeholder="main"
                />
              </div>

              {createWorktree.isError && (
                <p className="text-sm text-red-400">{(createWorktree.error as Error).message}</p>
              )}

              <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
                <button
                  type="button"
                  onClick={() => setShowCreateModal(false)}
                  className="rounded-lg border border-slate-600 px-4 py-2 text-sm text-slate-300 hover:border-slate-500 hover:text-white"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => void handleCreateWorktree()}
                  disabled={!selectedTaskId || createWorktree.isPending || creatableTasks.length === 0}
                  className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-500 disabled:cursor-not-allowed disabled:bg-slate-600"
                >
                  {createWorktree.isPending ? 'Creating…' : 'Create Worktree'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
