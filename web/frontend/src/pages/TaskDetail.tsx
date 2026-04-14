import { useParams, Link } from 'react-router-dom';
import { useTask, useRunTask, useCancelTask } from '../api/hooks';
import { StatusBadge, CostBadge } from '../components/StatusBadge';
import type { Task, Phase, PhaseInfo } from '../types';

const PHASE_ORDER: Phase[] = ['spec', 'generate', 'test', 'review', 'fix', 'commit'];

const PHASE_STATUS_COLORS: Record<string, { bg: string; ring: string; text: string }> = {
  pending:  { bg: 'bg-slate-700',   ring: 'ring-slate-600',   text: 'text-slate-400' },
  running:  { bg: 'bg-sky-600',     ring: 'ring-sky-400',     text: 'text-sky-200' },
  done:     { bg: 'bg-emerald-600', ring: 'ring-emerald-400', text: 'text-emerald-200' },
  failed:   { bg: 'bg-red-600',     ring: 'ring-red-400',     text: 'text-red-200' },
  skipped:  { bg: 'bg-slate-800',   ring: 'ring-slate-700',   text: 'text-slate-500' },
};

function PhasePipeline({ phases }: { phases: PhaseInfo[] }) {
  const phaseMap = new Map(phases.map((p) => [p.phase, p]));

  return (
    <div className="flex items-center gap-1">
      {PHASE_ORDER.map((phase, i) => {
        const info = phaseMap.get(phase);
        const status = info?.status ?? 'pending';
        const colors = PHASE_STATUS_COLORS[status] ?? PHASE_STATUS_COLORS.pending;
        return (
          <div key={phase} className="flex items-center">
            <div className="flex flex-col items-center">
              <div
                className={`w-9 h-9 rounded-full flex items-center justify-center text-xs font-bold ring-2 ${colors.bg} ${colors.ring} ${colors.text}`}
                title={`${phase}: ${status}${info?.elapsed_s != null ? ` (${info.elapsed_s.toFixed(1)}s)` : ''}`}
              >
                {phase.slice(0, 3).toUpperCase()}
              </div>
              <span className="text-[10px] text-slate-500 mt-1">{phase}</span>
            </div>
            {i < PHASE_ORDER.length - 1 && (
              <div className={`w-6 h-0.5 mx-0.5 ${status === 'done' ? 'bg-emerald-600' : 'bg-slate-700'}`} />
            )}
          </div>
        );
      })}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-slate-700 bg-slate-800/40 overflow-hidden">
      <div className="px-4 py-2.5 border-b border-slate-700 bg-slate-800/60">
        <h3 className="text-sm font-semibold text-slate-200">{title}</h3>
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function KV({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-xs text-slate-500 w-24 shrink-0">{label}</span>
      <span className="text-sm text-slate-200">{children}</span>
    </div>
  );
}

export default function TaskDetail() {
  const { id } = useParams<{ id: string }>();
  const { data: task, isLoading, isError } = useTask(id!);
  const runTask = useRunTask();
  const cancelTask = useCancelTask();

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-2 border-sky-500 border-t-transparent" />
      </div>
    );
  }

  if (isError || !task) {
    return (
      <div className="text-center py-16">
        <p className="text-red-400 text-sm">Failed to load task{id ? ` "${id}"` : ''}.</p>
        <Link to="/tasks" className="text-sky-400 hover:text-sky-300 text-sm mt-2 inline-block">
          ← Back to tasks
        </Link>
      </div>
    );
  }

  const t = task as Task;
  const canRun = t.status === 'pending' || t.status === 'failed';
  const canCancel = t.status === 'running' || t.status === 'queued';
  const canRetry = t.status === 'failed';

  return (
    <div className="space-y-6">
      {/* Back link */}
      <Link to="/tasks" className="inline-flex items-center gap-1 text-sm text-slate-400 hover:text-sky-400 transition-colors">
        ← Back to tasks
      </Link>

      {/* Title row */}
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold text-slate-100">{t.title}</h1>
            <StatusBadge status={t.status} />
          </div>
          <div className="flex items-center gap-3 text-xs text-slate-500">
            <span>Priority: <span className="text-amber-400 font-medium">{t.priority}</span></span>
            <span>Setup: <span className="text-slate-300">{t.setup}</span></span>
            <span>Language: <span className="text-slate-300">{t.language}</span></span>
            {t.complexity && <span>Complexity: <span className="text-slate-300">{t.complexity}</span></span>}
          </div>
        </div>

        {/* Action buttons */}
        <div className="flex items-center gap-2 shrink-0">
          {canRun && (
            <button
              onClick={() => runTask.mutate(t.id)}
              disabled={runTask.isPending}
              className="px-4 py-1.5 rounded-lg text-sm font-medium bg-emerald-600 hover:bg-emerald-500 text-white disabled:opacity-50 transition-colors"
            >
              {runTask.isPending ? 'Starting…' : '▶ Run'}
            </button>
          )}
          {canCancel && (
            <button
              onClick={() => cancelTask.mutate(t.id)}
              disabled={cancelTask.isPending}
              className="px-4 py-1.5 rounded-lg text-sm font-medium bg-red-700 hover:bg-red-600 text-white disabled:opacity-50 transition-colors"
            >
              {cancelTask.isPending ? 'Cancelling…' : '✕ Cancel'}
            </button>
          )}
          {canRetry && (
            <button
              onClick={() => runTask.mutate(t.id)}
              disabled={runTask.isPending}
              className="px-4 py-1.5 rounded-lg text-sm font-medium bg-amber-600 hover:bg-amber-500 text-white disabled:opacity-50 transition-colors"
            >
              {runTask.isPending ? 'Retrying…' : '↻ Retry'}
            </button>
          )}
        </div>
      </div>

      {/* Phase pipeline */}
      <Section title="Pipeline">
        <PhasePipeline phases={t.phases} />
      </Section>

      {/* Error */}
      {t.error && (
        <div className="rounded-lg border border-red-800 bg-red-950/40 p-4">
          <h3 className="text-sm font-semibold text-red-300 mb-1">Error</h3>
          <pre className="text-xs text-red-200 whitespace-pre-wrap font-mono">{t.error}</pre>
        </div>
      )}

      {/* Description */}
      <Section title="Description">
        <p className="text-sm text-slate-300 whitespace-pre-wrap leading-relaxed">{t.description}</p>
      </Section>

      {/* Code viewer */}
      {t.phases.some((p) => p.phase === 'generate' && p.status === 'done') && (
        <Section title="Generated Code">
          <pre className="bg-slate-900 rounded-lg p-4 text-xs text-emerald-300 font-mono overflow-x-auto max-h-[32rem] overflow-y-auto leading-relaxed whitespace-pre-wrap">
            {(t as any).final_code ?? '(code available after generation completes)'}
          </pre>
        </Section>
      )}

      {/* Test output */}
      {t.phases.some((p) => p.phase === 'test' && (p.status === 'done' || p.status === 'failed')) && (
        <Section title="Test Output">
          <pre className="bg-slate-900 rounded-lg p-4 text-xs text-slate-300 font-mono overflow-x-auto max-h-64 overflow-y-auto leading-relaxed whitespace-pre-wrap">
            {(t as any).test_output ?? '(no test output captured)'}
          </pre>
        </Section>
      )}

      {/* Review */}
      {t.review && (
        <Section title="Review">
          <div className="space-y-3">
            <div className="flex items-center gap-3">
              <StatusBadge status={t.review.passed ? 'passed' : 'failed'} />
              <span className="text-sm text-slate-300">
                Quality: <span className="text-amber-400 font-semibold">{t.review.quality.toFixed(1)}</span>/10
              </span>
            </div>
            {t.review.issues.length > 0 && (
              <ul className="space-y-1">
                {t.review.issues.map((issue, i) => (
                  <li key={i} className="flex items-start gap-2 text-sm text-slate-400">
                    <span className="text-red-400 mt-0.5">•</span>
                    <span>{issue}</span>
                  </li>
                ))}
              </ul>
            )}
            {t.review.issues.length === 0 && (
              <p className="text-sm text-slate-500 italic">No issues found.</p>
            )}
          </div>
        </Section>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Ledger */}
        {t.ledger && (
          <Section title="Ledger">
            <div className="space-y-2">
              <KV label="Tokens In">{t.ledger.tokens_in.toLocaleString()}</KV>
              <KV label="Tokens Out">{t.ledger.tokens_out.toLocaleString()}</KV>
              <KV label="Cost"><CostBadge cost={t.ledger.cost_usd} /></KV>
              <KV label="Elapsed">{t.ledger.elapsed_s.toFixed(1)}s</KV>
            </div>
          </Section>
        )}

        {/* Model routing */}
        {(t.map_model || t.fill_model || t.review_model) && (
          <Section title="Model Routing">
            <div className="space-y-2">
              {t.map_model && <KV label="Map">{t.map_model}</KV>}
              {t.fill_model && <KV label="Fill">{t.fill_model}</KV>}
              {t.review_model && <KV label="Review">{t.review_model}</KV>}
              <KV label="Fix Rounds">{t.fix_rounds} / {t.max_fix_rounds}</KV>
            </div>
          </Section>
        )}
      </div>

      {/* Git info */}
      {t.git_enabled && (
        <Section title="Git">
          <div className="space-y-2">
            {t.branch && <KV label="Branch"><code className="text-sky-400 text-xs bg-slate-900 px-1.5 py-0.5 rounded">{t.branch}</code></KV>}
            {t.worktree_path && <KV label="Worktree"><code className="text-xs text-slate-400 font-mono">{t.worktree_path}</code></KV>}
            {t.pr_url && (
              <KV label="PR">
                <a href={t.pr_url} target="_blank" rel="noopener noreferrer" className="text-sky-400 hover:text-sky-300 underline text-xs">
                  {t.pr_url}
                </a>
              </KV>
            )}
          </div>
        </Section>
      )}

      {/* Timestamps */}
      <div className="flex items-center gap-6 text-xs text-slate-600 pt-2">
        <span>Created: {new Date(t.created_at).toLocaleString()}</span>
        {t.started_at && <span>Started: {new Date(t.started_at).toLocaleString()}</span>}
        {t.completed_at && <span>Completed: {new Date(t.completed_at).toLocaleString()}</span>}
      </div>
    </div>
  );
}
