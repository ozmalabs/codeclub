import { useMemo } from 'react';
import { useHardware } from '../api/hooks';
import type { HardwareEndpoint } from '../types';

function classify(ep: HardwareEndpoint): string {
  const u = ep.url.toLowerCase();
  if (u.includes('localhost') || u.includes('127.0.0.1')) return 'local';
  if (u.includes('ollama') || u.includes(':11434')) return 'ollama';
  return 'cloud';
}

const GROUP_ORDER = ['local', 'ollama', 'cloud'] as const;
const GROUP_LABELS: Record<string, string> = {
  local: '🖥  Local',
  ollama: '🦙  Ollama',
  cloud: '☁️  Cloud',
};

function EndpointCard({ ep }: { ep: HardwareEndpoint }) {
  const latencyColor =
    ep.response_ms == null
      ? 'text-slate-500'
      : ep.response_ms < 200
        ? 'text-emerald-400'
        : ep.response_ms < 1000
          ? 'text-amber-400'
          : 'text-red-400';

  return (
    <div className="rounded-lg bg-slate-800 p-4 shadow ring-1 ring-slate-700 hover:ring-sky-600 transition-all">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-slate-100">{ep.name}</h3>
          <p className="mt-0.5 truncate text-xs text-slate-500">{ep.url}</p>
        </div>
        {/* alive dot — larger for emphasis */}
        <span
          className={`mt-0.5 h-4 w-4 shrink-0 rounded-full ${ep.alive ? 'bg-emerald-400 shadow-emerald-400/40 shadow-[0_0_8px]' : 'bg-red-500 shadow-red-500/30 shadow-[0_0_8px]'}`}
          title={ep.alive ? 'online' : 'offline'}
        />
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
        <dt className="text-slate-400">Response</dt>
        <dd className={`text-right font-mono ${latencyColor}`}>
          {ep.response_ms != null ? `${ep.response_ms.toFixed(0)} ms` : '—'}
        </dd>

        <dt className="text-slate-400">Last checked</dt>
        <dd className="text-right text-slate-300">
          {ep.last_checked ? new Date(ep.last_checked).toLocaleTimeString() : '—'}
        </dd>

        <dt className="text-slate-400">Status</dt>
        <dd className="text-right">
          <span
            className={`inline-block rounded px-1.5 py-0.5 text-xs font-medium ${ep.alive ? 'bg-emerald-900 text-emerald-300' : 'bg-red-900 text-red-300'}`}
          >
            {ep.alive ? 'online' : 'offline'}
          </span>
        </dd>
      </dl>
    </div>
  );
}

export default function Hardware() {
  const { data, isLoading, isFetching, refetch, error } = useHardware();
  const endpoints = data?.endpoints ?? [];

  const grouped = useMemo(() => {
    const map: Record<string, HardwareEndpoint[]> = {};
    for (const ep of endpoints) {
      const group = classify(ep);
      (map[group] ??= []).push(ep);
    }
    return map;
  }, [endpoints]);

  const onlineCount = endpoints.filter((e) => e.alive).length;

  return (
    <div>
      {/* header */}
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">Hardware</h1>
          <p className="text-sm text-slate-400">what rock we bang code on</p>
        </div>

        <div className="flex items-center gap-4">
          {endpoints.length > 0 && (
            <span className="text-xs text-slate-500">
              {onlineCount}/{endpoints.length} online
            </span>
          )}

          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="inline-flex items-center gap-2 rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white shadow transition-colors hover:bg-sky-500 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {isFetching && (
              <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
              </svg>
            )}
            {isFetching ? 'Probing…' : 'Probe All'}
          </button>
        </div>
      </div>

      {/* loading */}
      {isLoading && (
        <div className="flex items-center justify-center py-20 text-slate-400">
          <svg className="mr-2 h-5 w-5 animate-spin" viewBox="0 0 24 24" fill="none">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
          </svg>
          Probing endpoints…
        </div>
      )}

      {/* error */}
      {error && (
        <div className="rounded-lg border border-red-800 bg-red-900/30 p-4 text-sm text-red-300">
          Failed to probe hardware: {(error as Error).message}
        </div>
      )}

      {/* grouped endpoint cards */}
      {!isLoading && endpoints.length > 0 && (
        <div className="space-y-8">
          {GROUP_ORDER.filter((g) => grouped[g]?.length).map((group) => (
            <section key={group}>
              <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">
                {GROUP_LABELS[group]}
              </h2>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
                {grouped[group]!.map((ep) => (
                  <EndpointCard key={ep.name + ep.url} ep={ep} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}

      {!isLoading && endpoints.length === 0 && !error && (
        <p className="py-16 text-center text-sm text-slate-500">No hardware endpoints configured.</p>
      )}
    </div>
  );
}
