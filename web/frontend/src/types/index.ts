// Types matching web/api/models.py Pydantic schemas

export type TaskStatus = 'pending' | 'queued' | 'running' | 'review' | 'fixing' | 'done' | 'failed' | 'cancelled';
export type RunStatus = 'running' | 'passed' | 'failed';
export type Phase = 'spec' | 'generate' | 'test' | 'review' | 'fix' | 'commit';

export interface ReviewResult {
  passed: boolean;
  quality: number;
  issues: string[];
}

export interface TaskLedger {
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  elapsed_s: number;
}

export interface PhaseInfo {
  phase: Phase;
  status: 'pending' | 'running' | 'done' | 'failed' | 'skipped';
  started_at: string | null;
  elapsed_s: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  error: string | null;
}

interface TaskBase {
  id: string;
  title: string;
  description: string;
  status: TaskStatus;
  priority: number;
  setup: string;
  stack: string | null;
  language: string;
  budget: string;
  complexity: string | null;
  git_enabled: boolean;
  branch: string | null;
  worktree_path: string | null;
  pr_url: string | null;
  phases: PhaseInfo[];
  error: string | null;
  fix_rounds: number;
  max_fix_rounds: number;
  map_model: string | null;
  fill_model: string | null;
  review_model: string | null;
  parent_task_id: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  final_code: string | null;
  test_output: string | null;
}

export interface Task extends TaskBase {
  review: ReviewResult | null;
  ledger: TaskLedger | null;
}

export interface TaskRawResponse extends TaskBase {
  review_json: Record<string, unknown> | null;
  ledger_json: Record<string, unknown> | null;
}

export interface TaskListResponse {
  tasks: TaskRawResponse[];
  total: number;
}

export interface TaskCreate {
  title: string;
  description: string;
  priority?: number;
  setup?: string;
  stack?: string;
  language?: string;
  budget?: string;
  git_enabled?: boolean;
  max_fix_rounds?: number;
}

export interface PipelineStatus {
  paused: boolean;
  queue_depth: number;
}

export interface Run {
  id: number;
  task_id: string;
  attempt: number;
  status: RunStatus;
  phases: PhaseInfo[];
  code_snapshot: string | null;
  test_output: string | null;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  elapsed_s: number;
  created_at: string;
}

export interface ModelInfo {
  id: string;
  name: string;
  provider: string;
  params_b: number | null;
  quant: string | null;
  context_window: number;
  cost_per_mtok_in: number | null;
  cost_per_mtok_out: number | null;
  smash_range: SmashRange | null;
  endpoint: string | null;
  alive: boolean;
}

export interface SmashRange {
  low: number;
  sweet: number;
  high: number;
  min_clarity: number;
}

export interface SmashCoord {
  difficulty: number;
  clarity: number;
}

export interface SmashGridPoint {
  difficulty: number;
  clarity: number;
  efficiency: number;
}

export interface WorktreeInfo {
  path: string;
  branch: string;
  head: string;
  is_task: boolean;
}

export interface BranchInfo {
  name: string;
  short_sha: string;
  date: string;
  is_task_branch: boolean;
}

export interface DiffResponse {
  diff: string;
  files_changed: number;
  insertions: number;
  deletions: number;
}

export interface CommitResponse {
  sha: string;
  message: string;
}

export interface PRResponse {
  pr_url: string;
  pr_number: number;
}

export interface DashboardData {
  queue_depth: number;
  active_runs: number;
  completed_today: number;
  failed_today: number;
  total_cost_today: number;
  hardware_status: HardwareEndpoint[];
  recent_activity: ActivityEvent[];
}

export interface HardwareEndpoint {
  name: string;
  url: string;
  alive: boolean;
  response_ms: number | null;
  last_checked: string | null;
}

export interface ActivityEvent {
  id: number;
  event: string;
  entity_type: string | null;
  entity_id: string | null;
  detail: string | null;
  created_at: string;
}

export interface Setting {
  key: string;
  value: string;
}

export interface TournamentResult {
  id: number;
  task_id: string;
  mode: string;
  model: string;
  mapper: string | null;
  quality: number;
  tests_passed: number;
  tests_total: number;
  elapsed_s: number;
  cost_usd: number;
  energy_j: number | null;
  smash_fit: number;
  smash_measured: number | null;
  fitness: number;
  created_at: string;
}

export interface TournamentTask {
  name: string;
  lang: string;
  base_difficulty: number;
  num_tests: number;
  description: string;
}

export interface LeaderboardEntry {
  model: string;
  wins: number;
  avg_fitness: number;
  avg_cost: number;
  best_task: string;
}

export interface TournamentStartOpts {
  task_id?: string;
  optimize?: 'balanced' | 'fastest' | 'greenest' | 'cheapest';
  quick?: boolean;
}

export interface TournamentFightResult {
  task_id: string;
  mode: string;
  model: string;
  mapper: string | null;
  quality: number;
  tests_passed: number;
  tests_total: number;
  elapsed_s: number;
  cost_usd: number;
  energy_j: number | null;
  smash_fit: number;
  smash_measured: number | null;
  fitness: number;
}

// SSE event types
export type TaskSSEEvent =
  | { type: 'phase'; data: PhaseInfo }
  | { type: 'log'; data: { message: string } }
  | { type: 'test'; data: { name: string; passed: boolean; error?: string } }
  | { type: 'code'; data: { code: string } }
  | { type: 'review'; data: { verdict: string; issues: string[] } }
  | { type: 'error'; data: { message: string } }
  | { type: 'done'; data: { status: string; quality: number; cost: number } };

export type TournamentSSEEvent =
  | { type: 'fight'; data: TournamentFightResult }
  | { type: 'task'; data: { task_id: string; status: string } }
  | { type: 'done'; data: { champions: number; total_fights: number } };

export type SSEEvent = TaskSSEEvent | TournamentSSEEvent;
