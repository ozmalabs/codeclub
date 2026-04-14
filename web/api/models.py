"""
Pydantic schemas for the web API.

These are the contracts. Every router and service uses these types.
Backend ↔ Frontend agreement lives here.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Task state machine ──────────────────────────────────────────────────────
#
#   pending → queued → running → review → done
#                       │         │
#                       │    fixing → running (retry, up to N rounds)
#                       │         │
#                       ↓         ↓
#                     failed    failed
#                       ↑
#                  cancelled (any state)

class TaskStatus(str, Enum):
    pending = "pending"
    queued = "queued"
    running = "running"
    review = "review"
    fixing = "fixing"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class PhaseStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    skipped = "skipped"


# ── Task schemas ────────────────────────────────────────────────────────────

class TaskCreate(BaseModel):
    title: str
    description: str
    setup: str = "best_local_first"
    stack: str | None = None
    language: str = "python"
    budget: str = "cheap"
    git_enabled: bool = False
    priority: int = 50
    max_fix_rounds: int = 5
    map_model: str | None = None
    fill_model: str | None = None
    review_model: str | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    setup: str | None = None
    stack: str | None = None
    priority: int | None = None
    budget: str | None = None
    git_enabled: bool | None = None
    max_fix_rounds: int | None = None
    map_model: str | None = None
    fill_model: str | None = None
    review_model: str | None = None


class PhaseInfo(BaseModel):
    phase: str
    status: PhaseStatus = PhaseStatus.pending
    started_at: str | None = None
    elapsed_s: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    error: str | None = None


class TaskResponse(BaseModel):
    id: str
    title: str
    description: str
    status: TaskStatus
    priority: int
    setup: str
    stack: str | None
    language: str
    budget: str
    complexity: str | None
    git_enabled: bool
    branch: str | None
    worktree_path: str | None
    pr_url: str | None
    final_code: str | None
    test_output: str | None
    review_json: Any | None
    ledger_json: Any | None
    phases: list[PhaseInfo]
    error: str | None
    fix_rounds: int
    max_fix_rounds: int
    map_model: str | None
    fill_model: str | None
    review_model: str | None
    parent_task_id: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


class TaskList(BaseModel):
    tasks: list[TaskResponse]
    total: int


# ── Run schemas ─────────────────────────────────────────────────────────────

class RunResponse(BaseModel):
    id: int
    task_id: str
    attempt: int
    status: str
    phases: list[PhaseInfo]
    code_snapshot: str | None
    test_output: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    elapsed_s: float | None
    created_at: str


# ── Model & hardware schemas ───────────────────────────────────────────────

class SmashRangeResponse(BaseModel):
    low: int
    sweet: int
    high: int
    min_clarity: int


class ModelResponse(BaseModel):
    id: str
    name: str
    provider: str
    family: str
    params_b: float
    quant: str
    cost_input: float
    cost_output: float
    context: int
    local: bool
    phases: list[str]
    smash: SmashRangeResponse | None = None
    tags: list[str] = []


class EndpointResponse(BaseModel):
    url: str
    provider: str
    model_id: str
    alive: bool
    response_ms: float | None = None
    is_gpu: bool


class HardwareResponse(BaseModel):
    has_gpu: bool
    total_vram_mb: int
    ram_mb: int
    endpoints: list[EndpointResponse]


class RoutingDecision(BaseModel):
    phase: str
    model_id: str
    model_name: str
    reason: str


# ── Tournament schemas ──────────────────────────────────────────────────────

class TournamentStartRequest(BaseModel):
    task_ids: list[str] | None = None  # None = all tasks
    optimize: str = "balanced"
    quick: bool = False


class FightResultResponse(BaseModel):
    task_id: str
    mode: str
    model: str
    mapper: str | None
    quality: float
    tests_passed: int
    tests_total: int
    elapsed_s: float
    cost_usd: float
    energy_j: float | None
    smash_fit: float
    smash_measured: int
    fitness: float


# ── Smash / efficiency map schemas ─────────────────────────────────────────

class SmashCoordResponse(BaseModel):
    difficulty: int
    clarity: int


class SmashGridResponse(BaseModel):
    model_name: str
    smash: SmashRangeResponse
    difficulties: list[float]
    clarities: list[float]
    grid: list[list[float]]  # efficiency_matrix[clarity_idx][difficulty_idx]
    task_coords: dict[str, SmashCoordResponse] = {}


class SmashRouteResponse(BaseModel):
    coord: SmashCoordResponse
    recommended_models: list[dict[str, Any]]  # [{name, fit, smash, ...}]


# ── Dashboard schemas ──────────────────────────────────────────────────────

class DashboardResponse(BaseModel):
    queue_depth: int
    active_runs: int
    completed_today: int
    failed_today: int
    total_cost_today: float
    hardware: HardwareResponse | None
    recent_activity: list[dict[str, Any]]


# ── Settings schemas ────────────────────────────────────────────────────────

class SettingsResponse(BaseModel):
    settings: dict[str, str]


class SettingsUpdate(BaseModel):
    settings: dict[str, str]


# ── Activity log ────────────────────────────────────────────────────────────

class ActivityEvent(BaseModel):
    id: int
    event: str
    entity_type: str | None
    entity_id: str | None
    detail: Any
    created_at: str


# ── SSE event types ─────────────────────────────────────────────────────────
# These aren't Pydantic models — they're the event names and data shapes
# sent over SSE streams. Documented here as the contract.
#
# Task stream (GET /api/tasks/{id}/stream):
#   event: phase    data: PhaseInfo
#   event: log      data: {"message": str}
#   event: test     data: {"name": str, "passed": bool, "error": str|null}
#   event: code     data: {"code": str}
#   event: review   data: {"verdict": str, "issues": [...]}
#   event: done     data: {"status": str, "quality": float, "cost": float}
#   event: error    data: {"message": str}
#
# Tournament stream (GET /api/tournament/run/{id}/stream):
#   event: fight    data: FightResultResponse
#   event: task     data: {"task_id": str, "status": str}
#   event: done     data: {"champions": int, "total_fights": int}
