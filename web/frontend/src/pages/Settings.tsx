import { useState, useEffect } from 'react';
import { useSettings, useUpdateSettings } from '../api/hooks';
import type { Setting } from '../types';

const SECTION_DEFS: { title: string; icon: string; keys: string[] }[] = [
  {
    title: 'API Keys',
    icon: '🔑',
    keys: ['openai_api_key', 'anthropic_api_key', 'openrouter_api_key', 'groq_api_key'],
  },
  {
    title: 'Defaults',
    icon: '📐',
    keys: ['default_budget', 'default_language', 'default_setup'],
  },
  {
    title: 'Pipeline',
    icon: '⚙️',
    keys: ['max_fix_rounds', 'concurrency'],
  },
];

const API_KEY_FIELDS = new Set(SECTION_DEFS[0].keys);
const KNOWN_KEYS = new Set(SECTION_DEFS.flatMap((s) => s.keys));

const PRESETS = [
  { name: 'Minimal', desc: 'Low budget, single model, no git', values: { default_budget: 'micro', concurrency: '1' } },
  { name: 'Standard', desc: 'Balanced cost/quality, git enabled', values: { default_budget: 'small', concurrency: '2' } },
  { name: 'Full', desc: 'Max quality, tournament routing', values: { default_budget: 'large', concurrency: '4', max_fix_rounds: '5' } },
];

function maskValue(value: string): string {
  if (value.length <= 8) return '••••••••';
  return value.slice(0, 4) + '••••••••' + value.slice(-4);
}

export default function Settings() {
  const { data: settings, isLoading } = useSettings();
  const updateSettings = useUpdateSettings();

  const [edits, setEdits] = useState<Record<string, string>>({});
  const [revealedKeys, setRevealedKeys] = useState<Set<string>>(new Set());
  const [savedFlash, setSavedFlash] = useState(false);

  useEffect(() => {
    if (settings) {
      const map: Record<string, string> = {};
      (settings as Setting[]).forEach((s) => {
        map[s.key] = s.value;
      });
      setEdits(map);
    }
  }, [settings]);

  const dirty = (() => {
    if (!settings) return false;
    const orig: Record<string, string> = {};
    (settings as Setting[]).forEach((s) => {
      orig[s.key] = s.value;
    });
    return Object.keys(edits).some((k) => edits[k] !== (orig[k] ?? ''));
  })();

  function handleSave() {
    updateSettings.mutate(edits, {
      onSuccess: () => {
        setSavedFlash(true);
        setTimeout(() => setSavedFlash(false), 2000);
      },
    });
  }

  function applyPreset(values: Record<string, string>) {
    setEdits((prev) => ({ ...prev, ...values }));
  }

  function toggleReveal(key: string) {
    setRevealedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  // Collect keys not in any section
  const extraKeys = Object.keys(edits).filter((k) => !KNOWN_KEYS.has(k));

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-2 border-sky-500 border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-slate-100">Settings</h1>
        <p className="text-sm text-slate-400 mt-1">configure cave tool</p>
      </div>

      {/* Presets */}
      <section>
        <h2 className="text-lg font-semibold text-slate-200 mb-3">Presets</h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          {PRESETS.map((preset) => (
            <button
              key={preset.name}
              onClick={() => applyPreset(preset.values as Record<string, string>)}
              className="text-left rounded-lg border border-slate-700 bg-slate-800/60 p-4 hover:border-sky-600 hover:bg-slate-800 transition-colors group"
            >
              <h3 className="text-sm font-semibold text-sky-400 group-hover:text-sky-300">{preset.name}</h3>
              <p className="text-xs text-slate-400 mt-1">{preset.desc}</p>
              <div className="flex flex-wrap gap-1 mt-2">
                {Object.entries(preset.values).map(([k, v]) => (
                  <span key={k} className="text-[10px] bg-slate-700 text-slate-300 rounded px-1.5 py-0.5">
                    {k}={v}
                  </span>
                ))}
              </div>
            </button>
          ))}
        </div>
      </section>

      {/* Grouped sections */}
      {SECTION_DEFS.map((section) => (
        <section key={section.title} className="rounded-lg border border-slate-700 bg-slate-800/40 overflow-hidden">
          <div className="px-4 py-3 border-b border-slate-700 bg-slate-800/60">
            <h2 className="text-sm font-semibold text-slate-200">
              <span className="mr-2">{section.icon}</span>
              {section.title}
            </h2>
          </div>
          <div className="divide-y divide-slate-700/50">
            {section.keys.map((key) => {
              const isApiKey = API_KEY_FIELDS.has(key);
              const revealed = revealedKeys.has(key);
              const value = edits[key] ?? '';
              return (
                <div key={key} className="flex items-center gap-4 px-4 py-3">
                  <label className="w-48 text-sm text-slate-400 font-mono shrink-0">{key}</label>
                  <div className="flex-1 flex items-center gap-2">
                    <input
                      type={isApiKey && !revealed ? 'password' : 'text'}
                      value={isApiKey && !revealed ? (value ? maskValue(value) : '') : value}
                      onChange={(e) => setEdits((prev) => ({ ...prev, [key]: e.target.value }))}
                      onFocus={() => {
                        if (isApiKey && !revealed) toggleReveal(key);
                      }}
                      placeholder={isApiKey ? 'sk-••••••••' : '—'}
                      className="flex-1 bg-slate-900 border border-slate-600 rounded px-3 py-1.5 text-sm text-slate-200 font-mono placeholder:text-slate-600 focus:outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500/30"
                    />
                    {isApiKey && (
                      <button
                        onClick={() => toggleReveal(key)}
                        className="text-xs text-slate-500 hover:text-slate-300 px-2 py-1"
                        title={revealed ? 'Hide' : 'Reveal'}
                      >
                        {revealed ? '🙈' : '👁️'}
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      ))}

      {/* Extra / uncategorized settings */}
      {extraKeys.length > 0 && (
        <section className="rounded-lg border border-slate-700 bg-slate-800/40 overflow-hidden">
          <div className="px-4 py-3 border-b border-slate-700 bg-slate-800/60">
            <h2 className="text-sm font-semibold text-slate-200">
              <span className="mr-2">📦</span>Other
            </h2>
          </div>
          <div className="divide-y divide-slate-700/50">
            {extraKeys.map((key) => (
              <div key={key} className="flex items-center gap-4 px-4 py-3">
                <label className="w-48 text-sm text-slate-400 font-mono shrink-0">{key}</label>
                <input
                  type="text"
                  value={edits[key] ?? ''}
                  onChange={(e) => setEdits((prev) => ({ ...prev, [key]: e.target.value }))}
                  className="flex-1 bg-slate-900 border border-slate-600 rounded px-3 py-1.5 text-sm text-slate-200 font-mono placeholder:text-slate-600 focus:outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500/30"
                />
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Save bar */}
      <div className="flex items-center gap-4">
        <button
          onClick={handleSave}
          disabled={!dirty || updateSettings.isPending}
          className="px-5 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed bg-sky-600 hover:bg-sky-500 text-white"
        >
          {updateSettings.isPending ? 'Saving…' : 'Save Settings'}
        </button>
        {savedFlash && (
          <span className="text-sm text-emerald-400 animate-pulse">✓ Saved</span>
        )}
        {updateSettings.isError && (
          <span className="text-sm text-red-400">
            Error: {(updateSettings.error as Error).message}
          </span>
        )}
      </div>
    </div>
  );
}
