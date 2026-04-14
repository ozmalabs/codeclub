import { useState } from 'react';
import { useSmashModels } from '../api/hooks';
import type { ModelInfo } from '../types';

const API_BASE = '/api/smash/maps';

export default function SmashMaps() {
  const { data: models, isLoading } = useSmashModels();
  const [selected, setSelected] = useState<string>('');
  const [overlay, setOverlay] = useState(false);

  const modelNames = (models ?? []).map((m: ModelInfo) => m.name);
  const active = selected || modelNames[0] || '';
  const iframeSrc = overlay
    ? `${API_BASE}/overlay.html`
    : active
      ? `${API_BASE}/${encodeURIComponent(active)}.html`
      : '';

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-white">🗺️ Efficiency Maps</h1>
        <p className="text-slate-400 mt-1">
          see how brain work — like turbo compressor map
        </p>
      </div>

      {/* Controls */}
      <div className="flex items-center gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <label htmlFor="model-select" className="text-sm text-slate-300">
            Model
          </label>
          <select
            id="model-select"
            value={active}
            onChange={(e) => {
              setSelected(e.target.value);
              setOverlay(false);
            }}
            disabled={overlay || isLoading}
            className="rounded-md bg-slate-800 border border-slate-600 text-slate-200 text-sm px-3 py-1.5
                       focus:outline-none focus:ring-2 focus:ring-sky-500 disabled:opacity-50"
          >
            {isLoading && <option>Loading…</option>}
            {modelNames.map((name: string) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </div>

        <button
          onClick={() => setOverlay((v) => !v)}
          className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
            overlay
              ? 'bg-sky-600 text-white'
              : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
          }`}
        >
          {overlay ? '🔀 Overlay On' : '🔀 Overlay'}
        </button>
      </div>

      {/* Map iframe */}
      {iframeSrc ? (
        <div className="rounded-lg overflow-hidden border border-slate-700 bg-slate-900">
          <div className="bg-slate-800 px-4 py-2 text-xs text-slate-400 border-b border-slate-700">
            {overlay ? 'All models — overlay' : active}
          </div>
          <iframe
            key={iframeSrc}
            src={iframeSrc}
            title={overlay ? 'Overlay map' : `${active} efficiency map`}
            className="w-full border-0 bg-white"
            style={{ height: 'calc(100vh - 280px)', minHeight: 480 }}
          />
        </div>
      ) : (
        <div className="rounded-lg border border-slate-700 bg-slate-800/50 p-12 text-center">
          <p className="text-slate-400 text-lg">
            {isLoading
              ? 'Loading available maps…'
              : 'No models available. Run smash benchmarks first.'}
          </p>
        </div>
      )}
    </div>
  );
}
