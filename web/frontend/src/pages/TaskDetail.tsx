import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { subscribeTaskSSE } from '../api/client';
import {
  useCancelTask,
  useCommitTask,
  useCreatePR,
  useCreateWorktree,
  useGitDiff,
  useRunTask,
  useTask,
} from '../api/hooks';
import { CostBadge, StatusBadge } from '../components/StatusBadge';
import type {
  CommitResponse,
  DiffResponse,
  Phase,
  PhaseInfo,
  PRResponse,
  ReviewResult,
  Task,
  TaskSSEEvent,
  TaskStatus,
} from '../types';

const PHASE_ORDER: Phase[] = ['spec', 'generate', 'test', 'review', 'fix', 'commit'];

const PHASE_STATUS_COLORS: Record<string, { bg: string; ring: string; text: string }> = {
  pending: { bg: 'bg-slate-700', ring: 'ring-slate-600', text: 'text-slate-400' },
  running: { bg: 'bg-sky-600', ring: 'ring-sky-400', text: 'text-sky-200' },
  done: { bg: 'bg-emerald-600', ring: 'ring-emerald-400', text: 'text-emerald-200' },
  failed: { bg: 'bg-red-600', ring: 'ring-red-400', text: 'text-red-200' },
  skipped: { bg: 'bg-slate-800', ring: 'ring-slate-700', text: 'text-slate-500' },
};

type LiveLogEntry = {
  id: number;
  type: TaskSSEEvent['type'];
  message: string;
  tone: 'info' | 'success' | 'warning' | 'danger';
};

function sortPhases(phases: PhaseInfo[]) {
  return [...phases].sort((a, b) => PHASE_ORDER.indexOf(a.phase) - PHASE_ORDER.indexOf(b.phase));
}

function mergePhase(phases: PhaseInfo[], nextPhase: PhaseInfo) {
  const next = phases.filter((phase) => phase.phase !== nextPhase.phase);
  next.push(nextPhase);
  return sortPhases(next);
}

function isStreamingStatus(status: TaskStatus) {
  return status === 'queued' || status === 'running';
}

function reviewFromEvent(event: Extract<TaskSSEEvent, { type: 'review' }>, previous: ReviewResult | null): ReviewResult {
  const verdict = event.data.verdict.toLowerCase();
  const passed = !['fail', 'failed', 'reject', 'rejected'].some((word) => verdict.includes(word));
  return {
    passed,
    quality: previous?.quality ?? 0,
    issues: event.data.issues,
  };
}

function liveLogColor(tone: LiveLogEntry['tone']) {
  switch (tone) {
    case 'success':
      return 'text-emerald-300';
    case 'warning':
      return 'text-amber-300';
    case 'danger':
      return 'text-red-300';
    default:
      return 'text-slate-300';
  }
}

function diffLineClass(line: string) {
  if (line.startsWith('+') && !line.startsWith('+++')) {
    return 'bg-emerald-950/60 text-emerald-200';
  }
  if (line.startsWith('-') && !line.startsWith('---')) {
    return 'bg-red-950/60 text-red-200';
  }
  return 'text-slate-300';
}

function PhasePipeline({ phases }: { phases: PhaseInfo[] }) {
  const phaseMap = new Map(phases.map((phase) => [phase.phase, phase]));

  return (
    <div className="flex items-center gap-1 overflow-x-auto pb-1">
      {PHASE_ORDER.map((phase, index) => {
        const info = phaseMap.get(phase);
        const status = info?.status ?? 'pending';
        const colors = PHASE_STATUS_COLORS[status] ?? PHASE_STATUS_COLORS.pending;
        return (
          <div key={phase} className="flex items-center">
            <div className="flex flex-col items-center">
              <div
                className={`flex h-9 w-9 items-center justify-center rounded-full text-xs font-bold ring-2 ${colors.bg} ${colors.ring} ${colors.text}`}
                title={`${phase}: ${status}${info?.elapsed_s != null ? ` (${info.elapsed_s.toFixed(1)}s)` : ''}`}
              >
                {phase.slice(0, 3).toUpperCase()}
              </div>
              <span className="mt-1 text-[10px] text-slate-500">{phase}</span>
            </div>
            {index < PHASE_ORDER.length - 1 && (
              <div className={`mx-0.5 h-0.5 w-6 ${status === 'done' ? 'bg-emerald-600' : 'bg-slate-700'}`} />
            )}
          </div>
        );
      })}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="overflow-hidden rounded-lg border border-slate-700 bg-slate-800/50">
      <div className="border-b border-slate-700 bg-slate-800/60 px-4 py-2.5">
        <h3 className="text-sm font-semibold text-slate-200">{title}</h3>
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function KV({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="w-24 shrink-0 text-xs text-slate-500">{label}</span>
      <span className="text-sm text-slate-200">{children}</span>
    </div>
  );
}

function DiffViewer({ diff }: { diff: string }) {
  if (!diff.trim()) {
    return <p className="text-sm text-slate-500">No diff yet. Caveman see clean branch.</p>;
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-700 bg-slate-950 font-mono text-xs">
      {diff.split('\n').map((line, index) => (
        <div key={`${index}-${line}`} className={`whitespace-pre px-3 py-0.5 ${diffLineClass(line)}`}>
          {line || ' '}
        </div>
      ))}
    </div>
  );
}

function DiffSummary({ diff }: { diff: DiffResponse }) {
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs text-slate-400">
      <span>{diff.files_changed} files changed</span>
      <span className="text-emerald-300">+{diff.insertions}</span>
      <span className="text-red-300">-{diff.deletions}</span>
    </div>
  );
}

function TaskDetailContent({ task }: { task: Task }) {
  const qc = useQueryClient();
  const runTask = useRunTask();
  const cancelTask = useCancelTask();
  const createWorktree = useCreateWorktree();
  const commitTask = useCommitTask();
  const createPR = useCreatePR();
  const [streamPhases, setStreamPhases] = useState<PhaseInfo[] | null>(null);
  const [streamCode, setStreamCode] = useState<string | null>(null);
  const [streamTestOutput, setStreamTestOutput] = useState<string | null>(null);
  const [streamReview, setStreamReview] = useState<ReviewResult | null>(null);
  const [liveEntries, setLiveEntries] = useState<LiveLogEntry[]>([]);
  const [streamConnected, setStreamConnected] = useState(false);
  const [showDiff, setShowDiff] = useState(false);
  const [baseBranch, setBaseBranch] = useState('main');
  const [commitMessage, setCommitMessage] = useState('');
  const [commitResult, setCommitResult] = useState<CommitResponse | null>(null);
  const [prResult, setPrResult] = useState<PRResponse | null>(null);
  const liveLogRef = useRef<HTMLDivElement | null>(null);
  const liveEntryId = useRef(0);
  const isActive = isStreamingStatus(task.status);
  const hasWorktree = Boolean(task.worktree_path);
  const prUrl = prResult?.pr_url ?? task.pr_url;
  const diffQuery = useGitDiff(task.id, task.git_enabled && hasWorktree && showDiff);

  useEffect(() => {
    if (!commitMessage.trim()) {
      setCommitMessage(`task(${task.id}): ${task.title}`);
    }
  }, [commitMessage, task.id, task.title]);

  useEffect(() => {
    if (!isActive) {
      return;
    }

    const appendEntry = (entry: Omit<LiveLogEntry, 'id'>) => {
      setLiveEntries((current) => [...current, { ...entry, id: liveEntryId.current++ }]);
    };

    return subscribeTaskSSE(
      task.id,
      (event) => {
        switch (event.type) {
          case 'phase':
            setStreamPhases((current) => mergePhase(current ?? sortPhases(task.phases), event.data));
            appendEntry({
              type: 'phase',
              tone: event.data.status === 'failed' ? 'danger' : event.data.status === 'done' ? 'success' : 'info',
              message: `[phase] ${event.data.phase} → ${event.data.status}`,
            });
            return;
          case 'log':
            appendEntry({ type: 'log', tone: 'info', message: event.data.message });
            return;
          case 'test': {
            const line = `${event.data.passed ? '✓' : '✗'} ${event.data.name}${event.data.error ? ` — ${event.data.error}` : ''}`;
            setStreamTestOutput((current) => (current ? `${current}\n${line}` : line));
            appendEntry({ type: 'test', tone: event.data.passed ? 'success' : 'danger', message: `[test] ${line}` });
            return;
          }
          case 'code':
            setStreamCode(event.data.code);
            appendEntry({ type: 'code', tone: 'info', message: '[code] received updated code snapshot' });
            return;
          case 'review':
            setStreamReview((current) => reviewFromEvent(event, current ?? task.review));
            appendEntry({
              type: 'review',
              tone: event.data.issues.length === 0 ? 'success' : 'warning',
              message: `[review] ${event.data.verdict}${event.data.issues.length ? ` (${event.data.issues.length} issue${event.data.issues.length === 1 ? '' : 's'})` : ''}`,
            });
            return;
          case 'done':
            appendEntry({
              type: 'done',
              tone: event.data.status === 'done' ? 'success' : 'warning',
              message: `[done] status=${event.data.status} quality=${(event.data.quality ?? 0).toFixed(1)} cost=$${(event.data.cost ?? 0).toFixed(4)}`,
            });
            setStreamConnected(false);
            void qc.invalidateQueries({ queryKey: ['task', task.id] });
            void qc.invalidateQueries({ queryKey: ['tasks'] });
            void qc.invalidateQueries({ queryKey: ['dashboard'] });
            void qc.invalidateQueries({ queryKey: ['pipeline-status'] });
            return;
          case 'error':
            appendEntry({ type: 'error', tone: 'danger', message: `[error] ${event.data.message}` });
            return;
        }
      },
      {
        onOpen: () => setStreamConnected(true),
        onError: () => setStreamConnected(false),
        onClose: () => setStreamConnected(false),
      },
    );
  }, [isActive, qc, task]);

  useEffect(() => {
    const container = liveLogRef.current;
    if (!container) return;
    container.scrollTop = container.scrollHeight;
  }, [liveEntries]);

  const phases = isActive ? streamPhases ?? sortPhases(task.phases) : sortPhases(task.phases);
  const code = isActive ? streamCode ?? task.final_code ?? '' : task.final_code ?? streamCode ?? '';
  const testOutput = isActive ? streamTestOutput ?? task.test_output ?? '' : task.test_output ?? streamTestOutput ?? '';
  const review = isActive ? streamReview ?? task.review : task.review ?? streamReview;
  const canRun = task.status === 'pending' || task.status === 'failed';
  const canCancel = task.status === 'running' || task.status === 'queued';
  const canRetry = task.status === 'failed';
  const showCode = Boolean(code) || phases.some((phase) => phase.phase === 'generate' && phase.status !== 'pending');
  const showTestOutput = Boolean(testOutput) || phases.some((phase) => phase.phase === 'test' && phase.status !== 'pending');

  async function handleCreateWorktree() {
    const worktree = await createWorktree.mutateAsync({ taskId: task.id, baseBranch: baseBranch.trim() || 'main' });
    if (worktree.branch) {
      setShowDiff(true);
    }
  }

  async function handleCommit() {
    const result = await commitTask.mutateAsync({ taskId: task.id, message: commitMessage.trim() });
    setCommitResult(result);
  }

  async function handleCreatePR() {
    const result = await createPR.mutateAsync(task.id);
    setPrResult(result);
  }

  return (
    <div className="space-y-6">
      <Link to="/tasks" className="inline-flex items-center gap-1 text-sm text-slate-400 transition-colors hover:text-sky-400">
        ← Back to tasks
      </Link>

      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold text-slate-100">{task.title}</h1>
            <StatusBadge status={task.status} />
          </div>
          <div className="flex flex-wrap items-center gap-3 text-xs text-slate-500">
            <span>
              Priority: <span className="font-medium text-amber-400">{task.priority}</span>
            </span>
            <span>
              Setup: <span className="text-slate-300">{task.setup}</span>
            </span>
            <span>
              Language: <span className="text-slate-300">{task.language}</span>
            </span>
            {task.complexity && (
              <span>
                Complexity: <span className="text-slate-300">{task.complexity}</span>
              </span>
            )}
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {canRun && (
            <button
              onClick={() => runTask.mutate(task.id)}
              disabled={runTask.isPending}
              className="rounded-lg bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white transition-colors hover:bg-emerald-500 disabled:opacity-50"
            >
              {runTask.isPending ? 'Starting…' : '▶ Run'}
            </button>
          )}
          {canCancel && (
            <button
              onClick={() => cancelTask.mutate(task.id)}
              disabled={cancelTask.isPending}
              className="rounded-lg bg-red-700 px-4 py-1.5 text-sm font-medium text-white transition-colors hover:bg-red-600 disabled:opacity-50"
            >
              {cancelTask.isPending ? 'Cancelling…' : '✕ Cancel'}
            </button>
          )}
          {canRetry && (
            <button
              onClick={() => runTask.mutate(task.id)}
              disabled={runTask.isPending}
              className="rounded-lg bg-amber-600 px-4 py-1.5 text-sm font-medium text-white transition-colors hover:bg-amber-500 disabled:opacity-50"
            >
              {runTask.isPending ? 'Retrying…' : '↻ Retry'}
            </button>
          )}
        </div>
      </div>

      <Section title="Pipeline">
        <PhasePipeline phases={phases} />
      </Section>

      {isActive && (
        <Section title="Live Log">
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-xs text-slate-400">
              <span className={`h-2.5 w-2.5 rounded-full ${streamConnected ? 'animate-pulse bg-emerald-400' : 'bg-slate-500'}`} />
              <span className={streamConnected ? 'text-emerald-300' : 'text-slate-500'}>
                {streamConnected ? 'Streaming from pipeline' : 'Connecting to task stream…'}
              </span>
            </div>
            <div
              ref={liveLogRef}
              className="max-h-80 overflow-y-auto rounded-lg border border-slate-700 bg-slate-950 p-3 font-mono text-xs leading-relaxed"
            >
              {liveEntries.length === 0 ? (
                <p className="text-slate-500">Waiting for live task events…</p>
              ) : (
                <div className="space-y-1">
                  {liveEntries.map((entry) => (
                    <div key={entry.id} className={liveLogColor(entry.tone)}>
                      {entry.message}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </Section>
      )}

      {task.error && (
        <div className="rounded-lg border border-red-800 bg-red-950/40 p-4">
          <h3 className="mb-1 text-sm font-semibold text-red-300">Error</h3>
          <pre className="whitespace-pre-wrap font-mono text-xs text-red-200">{task.error}</pre>
        </div>
      )}

      <Section title="Description">
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-300">{task.description}</p>
      </Section>

      {showCode && (
        <Section title="Generated Code">
          <pre className="max-h-[32rem] overflow-x-auto overflow-y-auto whitespace-pre-wrap rounded-lg bg-slate-900 p-4 font-mono text-xs leading-relaxed text-emerald-300">
            {code || '(code available after generation completes)'}
          </pre>
        </Section>
      )}

      {showTestOutput && (
        <Section title="Test Output">
          <pre className="max-h-64 overflow-x-auto overflow-y-auto whitespace-pre-wrap rounded-lg bg-slate-900 p-4 font-mono text-xs leading-relaxed text-slate-300">
            {testOutput || '(no test output captured)'}
          </pre>
        </Section>
      )}

      {review && (
        <Section title="Review">
          <div className="space-y-3">
            <div className="flex items-center gap-3">
              <StatusBadge status={review.passed ? 'passed' : 'failed'} />
              <span className="text-sm text-slate-300">
                Quality: <span className="font-semibold text-amber-400">{review.quality.toFixed(1)}</span>/10
              </span>
            </div>
            {review.issues.length > 0 ? (
              <ul className="space-y-1">
                {review.issues.map((issue, index) => (
                  <li key={index} className="flex items-start gap-2 text-sm text-slate-400">
                    <span className="mt-0.5 text-red-400">•</span>
                    <span>{issue}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm italic text-slate-500">No issues found.</p>
            )}
          </div>
        </Section>
      )}

      <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
        {task.ledger && (
          <Section title="Ledger">
            <div className="space-y-2">
              <KV label="Tokens In">{task.ledger.tokens_in.toLocaleString()}</KV>
              <KV label="Tokens Out">{task.ledger.tokens_out.toLocaleString()}</KV>
              <KV label="Cost">
                <CostBadge cost={task.ledger.cost_usd} />
              </KV>
              <KV label="Elapsed">{task.ledger.elapsed_s.toFixed(1)}s</KV>
            </div>
          </Section>
        )}

        {(task.map_model || task.fill_model || task.review_model) && (
          <Section title="Model Routing">
            <div className="space-y-2">
              {task.map_model && <KV label="Map">{task.map_model}</KV>}
              {task.fill_model && <KV label="Fill">{task.fill_model}</KV>}
              {task.review_model && <KV label="Review">{task.review_model}</KV>}
              <KV label="Fix Rounds">
                {task.fix_rounds} / {task.max_fix_rounds}
              </KV>
            </div>
          </Section>
        )}
      </div>

      {task.git_enabled && (
        <Section title="Git">
          <div className="space-y-4">
            <div className="space-y-2">
              {task.branch && (
                <KV label="Branch">
                  <code className="rounded bg-slate-900 px-1.5 py-0.5 text-xs text-sky-400">{task.branch}</code>
                </KV>
              )}
              {task.worktree_path ? (
                <KV label="Worktree">
                  <code className="font-mono text-xs text-slate-400">{task.worktree_path}</code>
                </KV>
              ) : (
                <div className="rounded-lg border border-dashed border-slate-700 bg-slate-900/60 p-4">
                  <p className="text-sm text-slate-400">No worktree yet. Caveman need branch cave before commit and PR.</p>
                  <div className="mt-3 flex flex-col gap-3 md:flex-row md:items-center">
                    <input
                      type="text"
                      value={baseBranch}
                      onChange={(e) => setBaseBranch(e.target.value)}
                      placeholder="Base branch"
                      className="w-full rounded-lg border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-sky-500 focus:outline-none md:max-w-xs"
                    />
                    <button
                      type="button"
                      onClick={() => void handleCreateWorktree()}
                      disabled={createWorktree.isPending}
                      className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-500 disabled:cursor-not-allowed disabled:bg-slate-600"
                    >
                      {createWorktree.isPending ? 'Creating…' : 'Create Worktree'}
                    </button>
                  </div>
                </div>
              )}
              {prUrl && (
                <KV label="PR">
                  <a href={prUrl} target="_blank" rel="noopener noreferrer" className="text-xs text-sky-400 underline hover:text-sky-300">
                    {prUrl}
                  </a>
                </KV>
              )}
            </div>

            {(createWorktree.isError || commitTask.isError || createPR.isError || diffQuery.isError) && (
              <div className="space-y-1 text-sm text-red-400">
                {createWorktree.isError && <p>{(createWorktree.error as Error).message}</p>}
                {commitTask.isError && <p>{(commitTask.error as Error).message}</p>}
                {createPR.isError && <p>{(createPR.error as Error).message}</p>}
                {diffQuery.isError && <p>{(diffQuery.error as Error).message}</p>}
              </div>
            )}

            {hasWorktree && (
              <>
                <div className="rounded-lg border border-slate-700 bg-slate-900/60 p-4">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
                    <div>
                      <h4 className="text-sm font-semibold text-slate-200">Commit cave changes</h4>
                      <p className="mt-1 text-xs text-slate-500">Make one tidy commit before shiny PR button.</p>
                    </div>
                    <div className="flex flex-col gap-3 lg:w-[32rem] lg:flex-row">
                      <input
                        type="text"
                        value={commitMessage}
                        onChange={(e) => setCommitMessage(e.target.value)}
                        placeholder="Commit message"
                        className="flex-1 rounded-lg border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-sky-500 focus:outline-none"
                      />
                      <button
                        type="button"
                        onClick={() => void handleCommit()}
                        disabled={!commitMessage.trim() || commitTask.isPending}
                        className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-500 disabled:cursor-not-allowed disabled:bg-slate-600"
                      >
                        {commitTask.isPending ? 'Committing…' : 'Commit'}
                      </button>
                    </div>
                  </div>
                  {commitResult && (
                    <p className="mt-3 text-xs text-emerald-300">
                      Commit ready: <code>{commitResult.sha}</code> — {commitResult.message}
                    </p>
                  )}
                </div>

                <div className="rounded-lg border border-slate-700 bg-slate-900/60 p-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div>
                      <h4 className="text-sm font-semibold text-slate-200">Pull Request</h4>
                      <p className="mt-1 text-xs text-slate-500">Open branch to rest of tribe.</p>
                    </div>
                    <button
                      type="button"
                      onClick={() => void handleCreatePR()}
                      disabled={Boolean(prUrl) || createPR.isPending}
                      className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-500 disabled:cursor-not-allowed disabled:bg-slate-600"
                    >
                      {createPR.isPending ? 'Opening…' : prUrl ? 'PR Created' : 'Create PR'}
                    </button>
                  </div>
                  {prResult && (
                    <p className="mt-3 text-xs text-emerald-300">Opened PR #{prResult.pr_number}</p>
                  )}
                </div>

                <div className="rounded-lg border border-slate-700 bg-slate-900/60 p-4">
                  <button
                    type="button"
                    onClick={() => setShowDiff((current) => !current)}
                    className="flex w-full items-center justify-between gap-3 text-left"
                  >
                    <div>
                      <h4 className="text-sm font-semibold text-slate-200">Diff Viewer</h4>
                      <p className="mt-1 text-xs text-slate-500">Peek changes without leaving cave.</p>
                    </div>
                    <span className="text-sm text-sky-400">{showDiff ? 'Hide diff ▲' : 'Show diff ▼'}</span>
                  </button>

                  {showDiff && (
                    <div className="mt-4 space-y-3">
                      {diffQuery.isLoading ? (
                        <div className="h-40 animate-pulse rounded-lg border border-slate-700 bg-slate-950" />
                      ) : diffQuery.data ? (
                        <>
                          <DiffSummary diff={diffQuery.data} />
                          <DiffViewer diff={diffQuery.data.diff} />
                        </>
                      ) : (
                        <p className="text-sm text-slate-500">No diff data yet.</p>
                      )}
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        </Section>
      )}

      <div className="flex flex-wrap items-center gap-6 pt-2 text-xs text-slate-600">
        <span>Created: {new Date(task.created_at).toLocaleString()}</span>
        {task.started_at && <span>Started: {new Date(task.started_at).toLocaleString()}</span>}
        {task.completed_at && <span>Completed: {new Date(task.completed_at).toLocaleString()}</span>}
      </div>
    </div>
  );
}

export default function TaskDetail() {
  const { id } = useParams<{ id: string }>();
  const { data: task, isLoading, isError } = useTask(id ?? '');

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-sky-500 border-t-transparent" />
      </div>
    );
  }

  if (isError || !task) {
    return (
      <div className="py-16 text-center">
        <p className="text-sm text-red-400">Failed to load task{id ? ` "${id}"` : ''}.</p>
        <Link to="/tasks" className="mt-2 inline-block text-sm text-sky-400 hover:text-sky-300">
          ← Back to tasks
        </Link>
      </div>
    );
  }

  return <TaskDetailContent key={task.id} task={task} />;
}
