import type {
  BranchInfo,
  CommitResponse,
  DiffResponse,
  PipelineStatus,
  PRResponse,
  SSEEvent,
  Task,
  TaskCreate,
  TaskListResponse,
  TaskRawResponse,
  TaskSSEEvent,
  WorktreeInfo,
} from '../types';

const BASE = '/api';

function ensureSlash(path: string): string {
  if (path.includes('?')) {
    const [p, q] = path.split('?', 2);
    return `${p.endsWith('/') ? p : `${p}/`}?${q}`;
  }
  return path.endsWith('/') ? path : `${path}/`;
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
  if (res.status === 204) {
    return undefined as T;
  }
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

function normalizeTask(task: TaskRawResponse): Task {
  const { review_json, ledger_json, ...rest } = task;
  const review = review_json
    ? {
        passed: !!review_json.approved,
        quality: (review_json.score as number) ?? 0,
        issues: (review_json.issues as string[]) ?? [],
      }
    : null;
  const ledger = ledger_json
    ? {
        tokens_in: ((ledger_json.total_tokens_in ?? ledger_json.tokens_in ?? 0) as number),
        tokens_out: ((ledger_json.total_tokens_out ?? ledger_json.tokens_out ?? 0) as number),
        cost_usd: ((ledger_json.total_cost_usd ?? ledger_json.cost_usd ?? 0) as number),
        elapsed_s: ((ledger_json.total_time_s ?? ledger_json.elapsed_s ?? 0) as number),
      }
    : null;
  return { ...rest, review, ledger };
}

// Tasks
export const api = {
  tasks: {
    list: async (status?: string) => {
      const data = await request<TaskRawResponse[] | TaskListResponse>(
        `/tasks${status ? `?status=${status}` : ''}`,
      );
      const tasks = Array.isArray(data) ? data : data.tasks;
      return tasks.map(normalizeTask);
    },
    get: async (id: string) => normalizeTask(await request<TaskRawResponse>(`/tasks/${id}`)),
    create: async (data: TaskCreate) =>
      normalizeTask(await request<TaskRawResponse>('/tasks', { method: 'POST', body: JSON.stringify(data) })),
    update: async (id: string, data: Partial<TaskCreate>) =>
      normalizeTask(
        await request<TaskRawResponse>(`/tasks/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
      ),
    delete: (id: string) => request<void>(`/tasks/${id}`, { method: 'DELETE' }),
    run: async (id: string) =>
      normalizeTask(await request<TaskRawResponse>(`/tasks/${id}/run`, { method: 'POST' })),
    cancel: async (id: string) =>
      normalizeTask(await request<TaskRawResponse>(`/tasks/${id}/cancel`, { method: 'POST' })),
    retry: async (id: string) =>
      normalizeTask(await request<TaskRawResponse>(`/tasks/${id}/retry`, { method: 'POST' })),
    pipeline: {
      status: () => request<PipelineStatus>('/tasks/pipeline/status'),
      pause: () => request<void>('/tasks/pipeline/pause', { method: 'POST' }),
      resume: () => request<void>('/tasks/pipeline/resume', { method: 'POST' }),
    },
    bulk: {
      run: (ids: string[]) =>
        request<void>('/tasks/bulk/run', { method: 'POST', body: JSON.stringify({ task_ids: ids }) }),
      cancel: (ids: string[]) =>
        request<void>('/tasks/bulk/cancel', { method: 'POST', body: JSON.stringify({ task_ids: ids }) }),
      delete: (ids: string[]) =>
        request<void>('/tasks/bulk/delete', { method: 'POST', body: JSON.stringify({ task_ids: ids }) }),
    },
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
        `/smash/route?description=${encodeURIComponent(description)}&role=${role}`,
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
  git: {
    worktrees: () => request<WorktreeInfo[]>('/git'),
    branches: () => request<BranchInfo[]>('/git/branches'),
    createWorktree: (taskId: string, baseBranch?: string) =>
      request<WorktreeInfo>('/git/worktree', {
        method: 'POST',
        body: JSON.stringify({ task_id: taskId, base_branch: baseBranch ?? 'main' }),
      }),
    removeWorktree: (taskId: string) => request<void>(`/git/worktree/${taskId}`, { method: 'DELETE' }),
    diff: (taskId: string) => request<DiffResponse>(`/git/diff/${taskId}`),
    commit: (taskId: string, message: string) =>
      request<CommitResponse>(`/git/commit/${taskId}`, {
        method: 'POST',
        body: JSON.stringify({ message }),
      }),
    createPR: (taskId: string) => request<PRResponse>(`/git/pr/${taskId}`, { method: 'POST' }),
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

type SSECallbacks = {
  onOpen?: () => void;
  onError?: () => void;
  onClose?: () => void;
};

// SSE helper
export function subscribeSSE<TEvent extends SSEEvent = SSEEvent>(
  path: string,
  onEvent: (event: TEvent) => void,
  callbacks?: SSECallbacks,
): () => void {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const source = new EventSource(`${BASE}${normalizedPath}`);
  const types = ['phase', 'log', 'test', 'code', 'review', 'error', 'fight', 'task', 'done'] as const;
  let closed = false;

  const close = () => {
    if (closed) return;
    closed = true;
    source.close();
    callbacks?.onClose?.();
  };

  source.onopen = () => callbacks?.onOpen?.();
  for (const type of types) {
    source.addEventListener(type, (e) => {
      onEvent({ type, data: JSON.parse((e as MessageEvent).data) } as TEvent);
    });
  }
  source.onerror = () => {
    callbacks?.onError?.();
    close();
  };
  return close;
}

export function subscribeTaskSSE(
  taskId: string,
  onEvent: (event: TaskSSEEvent) => void,
  callbacks?: SSECallbacks,
): () => void {
  return subscribeSSE<TaskSSEEvent>(`/tasks/${taskId}/stream`, onEvent, callbacks);
}
