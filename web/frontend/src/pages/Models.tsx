import { useState, useMemo } from 'react';
import { useModels } from '../api/hooks';
import type { ModelInfo } from '../types';
import { CostBadge, SmashBar } from '../components/StatusBadge';

type SortKey = 'name' | 'params' | 'cost';

const PROVIDER_COLORS: Record<string, string> = {
  openai: 'bg-emerald-900 text-emerald-300',
  anthropic: 'bg-amber-900 text-amber-300',
  google: 'bg-sky-900 text-sky-300',
  meta: 'bg-indigo-900 text-indigo-300',
  mistral: 'bg-orange-900 text-orange-300',
  ollama: 'bg-purple-900 text-purple-300',
  local: 'bg-slate-700 text-slate-300',
};

function providerBadge(provider: string) {
  const cls = PROVIDER_COLORS[provider.toLowerCase()] ?? 'bg-slate-700 text-slate-300';
  return (
    <span className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${cls}`}>
      {provider}
    </span>
  );
}

function ModelCard({ model }: { model: ModelInfo }) {
  return (
    <div className="rounded-lg bg-slate-800 p-4 shadow ring-1 ring-slate-700 hover:ring-sky-600 transition-all">
      {/* header */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-slate-100">{model.name}</h3>
          <div className="mt-1">{providerBadge(model.provider)}</div>
        </div>
        <span
          className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${model.alive ? 'bg-emerald-400 shadow-emerald-400/40 shadow-[0_0_6px]' : 'bg-red-500'}`}
          title={model.alive ? 'alive' : 'offline'}
        />
      </div>

      {/* specs grid */}
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
        {model.params_b != null && (
          <>
            <dt className="text-slate-400">Params</dt>
            <dd className="text-slate-200 text-right">{model.params_b}B</dd>
          </>
        )}
        {model.quant != null && (
          <>
            <dt className="text-slate-400">Quant</dt>
            <dd className="text-slate-200 text-right">{model.quant}</dd>
          </>
        )}
        {model.context_window != null && (
          <>
            <dt className="text-slate-400">Context</dt>
            <dd className="text-slate-200 text-right">{(model.context_window / 1000).toFixed(0)}k</dd>
          </>
        )}
      </dl>

      {/* cost */}
      {(model.cost_per_mtok_in != null || model.cost_per_mtok_out != null) && (
        <div className="mt-3 flex items-center gap-2 text-xs">
          <span className="text-slate-400">$/Mtok</span>
          <div className="flex gap-1.5">
            {model.cost_per_mtok_in != null && <CostBadge cost={model.cost_per_mtok_in} />}
            {model.cost_per_mtok_out != null && (
              <span className="rounded bg-amber-900/60 px-1.5 py-0.5 text-amber-300">
                out ${model.cost_per_mtok_out.toFixed(2)}
              </span>
            )}
          </div>
        </div>
      )}

      {/* smash bar */}
      {model.smash_range && (
        <div className="mt-3">
          <span className="mb-1 block text-xs text-slate-400">Smash range</span>
          <SmashBar
            low={model.smash_range.low}
            sweet={model.smash_range.sweet}
            high={model.smash_range.high}
            min_clarity={model.smash_range.min_clarity}
          />
        </div>
      )}
    </div>
  );
}

export default function Models() {
  const { data: models, isLoading, error } = useModels();
  const [search, setSearch] = useState('');
  const [provider, setProvider] = useState('');
  const [sort, setSort] = useState<SortKey>('name');

  const providers = useMemo(() => {
    if (!models) return [];
    return [...new Set(models.map((m) => m.provider))].sort();
  }, [models]);

  const filtered = useMemo(() => {
    if (!models) return [];
    let list = models;

    if (search) {
      const q = search.toLowerCase();
      list = list.filter((m) => m.name.toLowerCase().includes(q));
    }
    if (provider) {
      list = list.filter((m) => m.provider === provider);
    }

    const sorted = [...list];
    if (sort === 'name') sorted.sort((a, b) => a.name.localeCompare(b.name));
    else if (sort === 'params') sorted.sort((a, b) => (b.params_b ?? 0) - (a.params_b ?? 0));
    else if (sort === 'cost') sorted.sort((a, b) => (a.cost_per_mtok_in ?? Infinity) - (b.cost_per_mtok_in ?? Infinity));

    return sorted;
  }, [models, search, provider, sort]);

  return (
    <div>
      {/* header */}
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-slate-100">Models</h1>
        <p className="text-sm text-slate-400">all the brain in cave</p>
      </div>

      {/* toolbar */}
      <div className="mb-5 flex flex-wrap items-center gap-3">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search models…"
          className="w-56 rounded-md border border-slate-600 bg-slate-900 px-3 py-1.5 text-sm text-slate-200 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
        />

        <select
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          className="rounded-md border border-slate-600 bg-slate-900 px-3 py-1.5 text-sm text-slate-200 focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
        >
          <option value="">All providers</option>
          {providers.map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>

        <div className="flex items-center gap-1 rounded-md border border-slate-600 bg-slate-900 text-sm">
          {(['name', 'params', 'cost'] as SortKey[]).map((key) => (
            <button
              key={key}
              onClick={() => setSort(key)}
              className={`px-3 py-1.5 capitalize transition-colors ${sort === key ? 'bg-sky-600 text-white' : 'text-slate-400 hover:text-slate-200'}`}
            >
              {key}
            </button>
          ))}
        </div>

        {models && (
          <span className="ml-auto text-xs text-slate-500">
            {filtered.length} of {models.length} models
          </span>
        )}
      </div>

      {/* states */}
      {isLoading && (
        <div className="flex items-center justify-center py-20 text-slate-400">
          <svg className="mr-2 h-5 w-5 animate-spin" viewBox="0 0 24 24" fill="none">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
          </svg>
          Loading models…
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-red-800 bg-red-900/30 p-4 text-sm text-red-300">
          Failed to load models: {(error as Error).message}
        </div>
      )}

      {/* grid */}
      {filtered.length > 0 && (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {filtered.map((m) => (
            <ModelCard key={m.id} model={m} />
          ))}
        </div>
      )}

      {models && !isLoading && filtered.length === 0 && (
        <p className="py-16 text-center text-sm text-slate-500">No models match your filters.</p>
      )}
    </div>
  );
}
