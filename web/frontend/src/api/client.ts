const BASE = '/api';

function ensureSlash(path: string): string {
  if (path.includes('?')) {
    const [p, q] = path.split('?', 2);
    return `${p.endsWith('/') ? p : p + '/'}?${q}`;
  }
  return path.endsWith('/') ? path : path + '/';
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${ensureSlash(path)}`, {
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status}: ${body}`);
  }
  return res.json();
}

// Tasks
export const api = {
  tasks: {
    list: (status?: string) =>
      request<import('../types').Task[]>(`/tasks${status ? `?status=${status}` : ''}`),
    get: (id: string) => request<import('../types').Task>(`/tasks/${id}`),
    create: (data: import('../types').TaskCreate) =>
      request<import('../types').Task>('/tasks', { method: 'POST', body: JSON.stringify(data) }),
    update: (id: string, data: Partial<import('../types').TaskCreate>) =>
      request<import('../types').Task>(`/tasks/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
    delete: (id: string) => request<void>(`/tasks/${id}`, { method: 'DELETE' }),
    run: (id: string) => request<import('../types').Task>(`/tasks/${id}/run`, { method: 'POST' }),
    cancel: (id: string) => request<import('../types').Task>(`/tasks/${id}/cancel`, { method: 'POST' }),
    retry: (id: string) => request<import('../types').Task>(`/tasks/${id}/retry`, { method: 'POST' }),
  },
  runs: {
    list: (taskId?: string) =>
      request<import('../types').Run[]>(`/runs${taskId ? `?task_id=${taskId}` : ''}`),
    get: (id: number) => request<import('../types').Run>(`/runs/${id}`),
  },
  models: {
    list: () => request<import('../types').ModelInfo[]>('/models'),
    get: (id: string) => request<import('../types').ModelInfo>(`/models/${id}`),
    probe: () => request<import('../types').HardwareEndpoint[]>('/models/probe', { method: 'POST' }),
  },
  hardware: {
    get: () => request<{ endpoints: import('../types').HardwareEndpoint[] }>('/hardware'),
    probe: () => request<import('../types').HardwareEndpoint[]>('/hardware/probe', { method: 'POST' }),
  },
  smash: {
    models: () => request<import('../types').ModelInfo[]>('/smash/models'),
    grid: (model: string) => request<import('../types').SmashGridPoint[]>(`/smash/map/${model}`),
    overlay: () => request<Record<string, import('../types').SmashGridPoint[]>>('/smash/overlay'),
    route: (description: string, role: string) =>
      request<{ model: string; coord: import('../types').SmashCoord; fit: number }>(
        `/smash/route?description=${encodeURIComponent(description)}&role=${role}`
      ),
  },
  tournament: {
    results: () => request<import('../types').TournamentResult[]>('/tournament/results'),
    tasks: () => request<import('../types').TournamentTask[]>('/tournament/tasks'),
    leaderboard: () => request<import('../types').LeaderboardEntry[]>('/tournament/leaderboard'),
    start: (opts: import('../types').TournamentStartOpts) =>
      request<{ status: string; run_id: string }>('/tournament/start', {
        method: 'POST',
        body: JSON.stringify(opts),
      }),
  },
  dashboard: {
    get: () => request<import('../types').DashboardData>('/dashboard'),
  },
  settings: {
    list: () => request<import('../types').Setting[]>('/settings'),
    update: (settings: Record<string, string>) =>
      request<void>('/settings', { method: 'PUT', body: JSON.stringify(settings) }),
    presets: () => request<string[]>('/settings/presets'),
  },
};

// SSE helper
export function subscribeSSE(path: string, onEvent: (event: import('../types').SSEEvent) => void): () => void {
  const source = new EventSource(`${BASE}${path}`);
  const types = ['phase', 'log', 'test', 'code', 'review', 'error', 'fight', 'task', 'done'] as const;
  for (const type of types) {
    source.addEventListener(type, (e) => {
      onEvent({ type, data: JSON.parse((e as MessageEvent).data) } as import('../types').SSEEvent);
    });
  }
  source.onerror = () => source.close();
  return () => source.close();
}
