"""
models.py — Model registry, capability matrix, and routing.

Model selection is a lookup problem, not an LLM problem. Given:
  - task complexity (trivial → expert)
  - pipeline phase (spec / map / fill / testgen / review / report)
  - available providers (anthropic / openrouter / ollama / llama-server)
  - budget constraint (free / cheap / medium / premium)
  - observed failure history (auto-escalate after N failures)

...the router returns the best available ModelSpec.

Benchmark data
--------------
Scores are from published benchmarks where available, otherwise estimated
(marked with *). Sources:
  SWE-bench Verified: resolves real GitHub issues (code editing proxy)
  HumanEval pass@1:   writes functions from docstrings (fill quality proxy)
  LiveCodeBench:      recent problems, minimal contamination

Phase → benchmark proxy mapping:
  spec/map    → SWE-bench (architecture + understanding)
  fill        → HumanEval (isolated function generation)
  testgen     → HumanEval (writing correct assertions from spec)
  review      → SWE-bench + reasoning (understanding + finding bugs)
  report      → MMLU/instruction following (summarisation)

Complexity estimation
---------------------
Heuristic from task description: counts classes, methods, algorithm
keywords, and system complexity markers. Returns one of:
  trivial | simple | moderate | complex | expert

Dynamic escalation
------------------
PerformanceTracker records (model, phase, complexity) → outcomes.
ModelRouter.select() skips models with failure_rate > threshold.
dev_loop passes failure counts back to escalate automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from codeclub.infra.hardware import HardwareSetup


# ---------------------------------------------------------------------------
# Complexity levels
# ---------------------------------------------------------------------------

COMPLEXITY_LEVELS = ["trivial", "simple", "moderate", "complex", "expert"]

PHASES = ["spec", "map", "fill", "testgen", "review", "report"]


def estimate_complexity(task: str) -> str:
    """
    Estimate task complexity from its natural language description.

    Heuristic scoring:
      +2 per class/dataclass mentioned
      +1 per method signature (fn() -> T patterns)
      +2 per algorithm keyword (sort, cache, graph, etc.)
      +3 per systems keyword (concurrent, distributed, security, etc.)
    """
    t = task.lower()

    classes   = len(re.findall(r'\b(?:class|dataclass)\b', t))
    methods   = len(re.findall(r'\w+\s*\([^)]*\)\s*->', task))  # fn(...) ->

    algo_kw   = ['sort', 'tree', 'graph', 'queue', 'heap', 'lru', 'cache',
                 'sliding window', 'binary search', 'dynamic programming', r'\bdp\b',
                 'recursive', 'backtrack', 'topolog']
    system_kw = ['concurrent', 'async', 'thread', 'lock', 'distributed',
                 'consensus', 'sharding', 'stream', 'real.time', 'security',
                 'cryptograph', 'byzantine', 'transaction', 'acid']

    algo_score   = sum(1 for kw in algo_kw   if re.search(kw, t))
    system_score = sum(1 for kw in system_kw if re.search(kw, t))

    score = classes * 2 + methods + algo_score * 2 + system_score * 3

    if score <= 2:  return "trivial"
    if score <= 6:  return "simple"
    if score <= 12: return "moderate"
    if score <= 20: return "complex"
    return "expert"


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

@dataclass
class ModelSpec:
    """Full specification for a single model."""
    id: str                          # Canonical ID (Ollama tag or OpenRouter model ID)
    name: str                        # Display name
    provider: str                    # "anthropic" | "openrouter" | "ollama" | "llama-server"
    family: str                      # "gemma" | "llama" | "qwen" | "claude" | "rnj" | ...

    # Cost (USD per million tokens; 0 for local)
    cost_in:  float = 0.0
    cost_out: float = 0.0

    # Context window (tokens)
    context: int = 8192

    # Published benchmark scores (None = unknown)
    swe_bench:  float | None = None  # SWE-bench Verified (0–1)
    human_eval: float | None = None  # HumanEval pass@1  (0–1)
    # * = estimated from model family/size trends

    # Hardware requirements (local models only)
    local:       bool       = False
    vram_mb:     int | None = None   # GPU VRAM required (MB)
    ram_mb:      int | None = None   # CPU RAM required for CPU-only path (MB)
    tps_observed: float | None = None  # tokens/sec observed on this system

    # Quantisation metadata — enables the "fit or fall back" chain
    params_b:   float | None = None  # parameter count (billions), e.g. 8.0
    quant:      str | None   = None  # quantisation level, e.g. "q6_k", "q4_k_m"
    # base_model: canonical model name shared across all quant variants.
    # Set to the same value for rnj-1:8b, rnj-1:8b-q4, rnj-1:8b-q3, etc.
    # The router walks lower-quant variants of the same base_model when the
    # preferred quant doesn't fit available VRAM.
    base_model: str | None   = None

    # Phase suitability — set of phases this model handles well
    # Derived from benchmarks + empirical runs (see PHASE_MATRIX below)
    phases: frozenset[str] = field(default_factory=frozenset)

    # Max complexity this model handles reliably
    max_complexity: str = "moderate"

    # Tags for availability grouping
    tags: frozenset[str] = field(default_factory=frozenset)

    @property
    def free(self) -> bool:
        return self.cost_in == 0.0 and self.cost_out == 0.0

    @property
    def cost_per_map_call(self) -> float:
        """Estimated cost of a typical map call (~500 in, 500 out tokens)."""
        return (500 * self.cost_in + 500 * self.cost_out) / 1_000_000

    @property
    def swe_tier(self) -> str:
        """Map SWE-bench score to a capability tier label."""
        s = self.swe_bench or 0.0
        if s >= 0.60: return "elite"
        if s >= 0.45: return "strong"
        if s >= 0.30: return "capable"
        if s >= 0.15: return "basic"
        return "limited"


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
#
# Benchmark sources:
#   Claude: anthropic.com/research, aider.chat leaderboard
#   Llama:  meta-llama/llama3 README, lmsys.org
#   Gemma:  ai.google.dev/gemma, openrouter.ai model cards
#   Qwen:   qwen2.5 technical report, huggingface model cards
#   rnj-1:  rnj.ai announcement, aider leaderboard
#   devstral: mistral.ai announcement (46.8% SWE-bench)
#
# * = estimated from model family interpolation

REGISTRY: list[ModelSpec] = [

    # ── Cloud: Anthropic ───────────────────────────────────────────────────
    ModelSpec(
        id="claude-opus-4-6", name="Claude Opus 4.6",
        provider="anthropic", family="claude",
        cost_in=5.00, cost_out=25.00, context=200_000,
        swe_bench=0.72, human_eval=0.97,
        phases=frozenset(PHASES),
        max_complexity="expert",
        tags=frozenset({"anthropic", "cloud", "premium"}),
    ),
    ModelSpec(
        id="claude-sonnet-4-6", name="Claude Sonnet 4.6",
        provider="anthropic", family="claude",
        cost_in=3.00, cost_out=15.00, context=200_000,
        swe_bench=0.65, human_eval=0.93,
        phases=frozenset(PHASES),
        max_complexity="complex",
        tags=frozenset({"anthropic", "cloud", "medium"}),
    ),
    ModelSpec(
        id="claude-haiku-4-5", name="Claude Haiku 4.5",
        provider="anthropic", family="claude",
        cost_in=1.00, cost_out=5.00, context=200_000,
        swe_bench=0.40, human_eval=0.88,
        phases=frozenset({"fill", "testgen", "report", "spec"}),
        max_complexity="moderate",
        tags=frozenset({"anthropic", "cloud", "cheap"}),
    ),

    # ── Cloud: OpenRouter (paid) ───────────────────────────────────────────
    ModelSpec(
        id="meta-llama/llama-3.3-70b-instruct", name="Llama 3.3 70B",
        provider="openrouter", family="llama",
        cost_in=0.10, cost_out=0.32, context=131_072,
        swe_bench=0.41, human_eval=0.88,
        phases=frozenset({"spec", "map", "testgen", "review", "report"}),
        max_complexity="complex",
        tags=frozenset({"openrouter", "cloud", "cheap"}),
    ),
    ModelSpec(
        id="google/gemma-4-31b-it", name="Gemma 4 31B",
        provider="openrouter", family="gemma",
        cost_in=0.13, cost_out=0.38, context=262_144,
        swe_bench=0.48,  # * estimated; Gemma 4 family strong on code
        human_eval=0.87, # * estimated
        phases=frozenset({"spec", "map", "testgen", "review", "report"}),
        max_complexity="complex",
        tags=frozenset({"openrouter", "cloud", "cheap"}),
    ),
    ModelSpec(
        id="google/gemma-4-26b-a4b-it", name="Gemma 4 26B MoE",
        provider="openrouter", family="gemma",
        cost_in=0.08, cost_out=0.35, context=262_144,
        swe_bench=0.42,  # * MoE: strong knowledge, moderate reasoning
        human_eval=0.83, # * estimated
        phases=frozenset({"spec", "map", "testgen", "review", "report"}),
        max_complexity="moderate",
        tags=frozenset({"openrouter", "cloud", "cheap"}),
    ),
    ModelSpec(
        id="openai/gpt-4o-mini", name="GPT-4o Mini",
        provider="openrouter", family="gpt",
        cost_in=0.15, cost_out=0.60, context=128_000,
        swe_bench=0.43, human_eval=0.87,
        phases=frozenset({"spec", "map", "testgen", "review", "report"}),
        max_complexity="moderate",
        tags=frozenset({"openrouter", "cloud", "cheap", "copilot"}),
    ),
    ModelSpec(
        id="openai/gpt-4.1-mini", name="GPT-4.1 Mini",
        provider="openrouter", family="gpt",
        cost_in=0.40, cost_out=1.60, context=128_000,
        swe_bench=0.50,  # * 4.1 family improvement over 4o
        human_eval=0.90, # * estimated
        phases=frozenset({"spec", "map", "testgen", "review", "report"}),
        max_complexity="complex",
        tags=frozenset({"openrouter", "cloud", "medium", "copilot"}),
    ),
    ModelSpec(
        id="mistralai/mistral-small-3.1-24b-instruct", name="Mistral Small 3.1 24B",
        provider="openrouter", family="mistral",
        cost_in=0.35, cost_out=0.56, context=128_000,
        swe_bench=0.38, # * Mistral Small is capable but not SWE-focused
        human_eval=0.82,
        phases=frozenset({"map", "testgen", "review", "report"}),
        max_complexity="moderate",
        tags=frozenset({"openrouter", "cloud", "cheap"}),
    ),
    ModelSpec(
        id="devstral/devstral-small-2505", name="Devstral Small",
        provider="openrouter", family="mistral",
        cost_in=0.10, cost_out=0.30, context=131_072,
        swe_bench=0.468, human_eval=None,
        phases=frozenset({"map", "fill", "testgen", "review"}),
        max_complexity="complex",
        tags=frozenset({"openrouter", "cloud", "cheap"}),
    ),

    # ── Cloud: MiniMax ────────────────────────────────────────────────────
    ModelSpec(
        id="minimax/minimax-m1", name="MiniMax M1 (2.7)",
        provider="openrouter", family="minimax",
        cost_in=0.30, cost_out=1.10, context=1_000_000,
        swe_bench=0.56,   # * strong reasoning model, comparable to GPT-4.1-mini+
        human_eval=0.90,  # * estimated from MiniMax-01 benchmark reports
        phases=frozenset({"spec", "map", "testgen", "review", "report"}),
        max_complexity="complex",
        tags=frozenset({"openrouter", "cloud", "cheap", "long-context"}),
    ),
    ModelSpec(
        id="minimax/minimax-m1:free", name="MiniMax M1 (free)",
        provider="openrouter", family="minimax",
        cost_in=0.0, cost_out=0.0, context=1_000_000,
        swe_bench=0.56, human_eval=0.90,
        phases=frozenset({"spec", "map", "testgen", "review", "report"}),
        max_complexity="complex",
        tags=frozenset({"openrouter", "cloud", "free", "long-context"}),
    ),

    # ── Cloud: OpenRouter (free tier) ─────────────────────────────────────
    ModelSpec(
        id="google/gemma-4-26b-a4b-it:free", name="Gemma 4 26B MoE (free)",
        provider="openrouter", family="gemma",
        cost_in=0.0, cost_out=0.0, context=262_144,
        swe_bench=0.42, human_eval=0.83,
        phases=frozenset({"spec", "map", "testgen", "review", "report"}),
        max_complexity="moderate",
        tags=frozenset({"openrouter", "cloud", "free"}),
    ),
    ModelSpec(
        id="meta-llama/llama-3.3-70b-instruct:free", name="Llama 3.3 70B (free)",
        provider="openrouter", family="llama",
        cost_in=0.0, cost_out=0.0, context=131_072,
        swe_bench=0.41, human_eval=0.88,
        phases=frozenset({"spec", "map", "testgen", "review", "report"}),
        max_complexity="complex",
        tags=frozenset({"openrouter", "cloud", "free"}),
    ),

    # ── GitHub Models / Copilot (inference.ai.azure.com) ─────────────────
    ModelSpec(
        id="gpt-4o", name="GPT-4o (GitHub Models)",
        provider="github", family="gpt",
        cost_in=0.0, cost_out=0.0, context=128_000,  # free via GitHub token (rate-limited)
        swe_bench=0.49, human_eval=0.90,
        phases=frozenset(PHASES),  # capable of all phases
        max_complexity="complex",
        tags=frozenset({"github", "cloud", "free", "copilot"}),
    ),
    ModelSpec(
        id="gpt-4o-mini", name="GPT-4o Mini (GitHub Models)",
        provider="github", family="gpt",
        cost_in=0.0, cost_out=0.0, context=128_000,
        swe_bench=0.43, human_eval=0.87,
        phases=frozenset({"spec", "map", "fill", "testgen", "review", "report"}),
        max_complexity="moderate",
        tags=frozenset({"github", "cloud", "free", "copilot"}),
    ),
    ModelSpec(
        id="o3-mini", name="o3-mini (GitHub Models)",
        provider="github", family="gpt",
        cost_in=0.0, cost_out=0.0, context=200_000,
        swe_bench=0.69, human_eval=0.965,
        phases=frozenset({"spec", "map", "testgen", "review"}),
        max_complexity="expert",
        tags=frozenset({"github", "cloud", "free", "copilot", "reasoning"}),
    ),
    ModelSpec(
        id="Llama-3.3-70B-Instruct", name="Llama 3.3 70B (GitHub Models)",
        provider="github", family="llama",
        cost_in=0.0, cost_out=0.0, context=131_072,
        swe_bench=0.41, human_eval=0.88,
        phases=frozenset({"spec", "map", "testgen", "review", "report"}),
        max_complexity="complex",
        tags=frozenset({"github", "cloud", "free"}),
    ),

    # ── Local: B580 SYCL (llama-server port 8081) ─────────────────────────
    # Quant variants share base_model="rnj-1:8b". Router tries best quant
    # first, falls back to lower quant if VRAM doesn't fit.
    ModelSpec(
        id="rnj-1:8b", name="rnj-1 8B Q6_K (B580)",
        provider="llama-server", family="rnj",
        cost_in=0.0, cost_out=0.0, context=32_768,
        swe_bench=0.69,  # rnj.ai claimed; top local 8B on aider leaderboard
        human_eval=0.72, # * estimated
        local=True, vram_mb=10_500, tps_observed=32.0,
        params_b=8.0, quant="q6_k", base_model="rnj-1:8b",
        phases=frozenset({"spec", "map", "testgen", "review"}),
        max_complexity="complex",
        tags=frozenset({"local", "b580", "sycl"}),
    ),
    ModelSpec(
        id="rnj-1:8b-q4", name="rnj-1 8B Q4_K_M (B580 8GB)",
        provider="llama-server", family="rnj",
        cost_in=0.0, cost_out=0.0, context=4_096,
        swe_bench=0.65,  # * slight degradation from quantisation
        human_eval=0.69, # * estimated
        local=True, vram_mb=6_500, tps_observed=38.0,
        params_b=8.0, quant="q4_k_m", base_model="rnj-1:8b",
        phases=frozenset({"spec", "map", "testgen"}),
        max_complexity="moderate",
        tags=frozenset({"local", "b580", "sycl", "8gb"}),
    ),

    # ── Local: Ollama CPU ──────────────────────────────────────────────────
    ModelSpec(
        id="qwen2.5-coder:1.5b", name="Qwen2.5-Coder 1.5B",
        provider="ollama", family="qwen",
        cost_in=0.0, cost_out=0.0, context=32_768,
        swe_bench=None, human_eval=0.372,
        local=True, ram_mb=1_300, tps_observed=43.0,
        params_b=1.5, quant="q4_k_m", base_model="qwen2.5-coder",
        phases=frozenset({"fill", "report"}),
        max_complexity="simple",
        tags=frozenset({"local", "cpu", "tiny"}),
    ),
    ModelSpec(
        id="qwen2.5-coder:3b", name="Qwen2.5-Coder 3B",
        provider="ollama", family="qwen",
        cost_in=0.0, cost_out=0.0, context=32_768,
        swe_bench=None, human_eval=0.457,
        local=True, ram_mb=2_200, tps_observed=30.0,
        params_b=3.0, quant="q4_k_m", base_model="qwen2.5-coder",
        phases=frozenset({"fill", "testgen", "report"}),
        max_complexity="moderate",
        tags=frozenset({"local", "cpu", "small"}),
    ),
    ModelSpec(
        id="qwen3:1.7b", name="Qwen3 1.7B",
        provider="ollama", family="qwen",
        cost_in=0.0, cost_out=0.0, context=32_768,
        swe_bench=None, human_eval=0.40,  # * estimated
        local=True, ram_mb=1_400, tps_observed=40.0,
        params_b=1.7, quant="q4_k_m", base_model="qwen3",
        phases=frozenset({"fill", "report"}),
        max_complexity="simple",
        tags=frozenset({"local", "cpu", "tiny"}),
    ),
    ModelSpec(
        id="rnj-1:8b-cpu", name="rnj-1 8B (CPU / Ollama)",
        provider="ollama", family="rnj",
        cost_in=0.0, cost_out=0.0, context=32_768,
        swe_bench=0.69, human_eval=0.72,
        local=True, ram_mb=5_200, tps_observed=8.0,  # much slower on CPU
        params_b=8.0, quant="q4_k_m", base_model="rnj-1:8b",
        phases=frozenset({"fill", "map", "testgen"}),
        max_complexity="complex",
        tags=frozenset({"local", "cpu", "medium"}),
    ),
    ModelSpec(
        id="qwen3-coder:30b", name="Qwen3-Coder 30B MoE",
        provider="ollama", family="qwen",
        cost_in=0.0, cost_out=0.0, context=32_768,
        swe_bench=None, human_eval=0.65,  # * MoE 30B/3B-active
        local=True, ram_mb=6_000, tps_observed=18.0,
        params_b=30.0, quant="q4_k_m", base_model="qwen3-coder",
        phases=frozenset({"fill", "map", "testgen", "review"}),
        max_complexity="complex",
        tags=frozenset({"local", "cpu", "medium"}),
    ),
    ModelSpec(
        id="devstral:24b", name="Devstral 24B",
        provider="ollama", family="mistral",
        cost_in=0.0, cost_out=0.0, context=131_072,
        swe_bench=0.468, human_eval=None,
        local=True, ram_mb=16_000, tps_observed=6.0,
        params_b=24.0, quant="q4_k_m", base_model="devstral",
        phases=frozenset({"map", "fill", "testgen", "review"}),
        max_complexity="complex",
        tags=frozenset({"local", "cpu", "large"}),
    ),
]

# Index by id for fast lookup
_REGISTRY_BY_ID: dict[str, ModelSpec] = {m.id: m for m in REGISTRY}


def get(model_id: str) -> ModelSpec | None:
    return _REGISTRY_BY_ID.get(model_id)


def all_models() -> list[ModelSpec]:
    return list(REGISTRY)


# ---------------------------------------------------------------------------
# Phase → complexity → minimum benchmark thresholds
# ---------------------------------------------------------------------------
#
# These thresholds define the *minimum* capability bar for a model to be
# considered for a given phase at a given complexity level.
# Below threshold → model is skipped (will produce poor results).
#
# Thresholds are (swe_bench_min, human_eval_min).
# None means "no minimum" (use any model for this phase/complexity).

_PHASE_THRESHOLDS: dict[str, dict[str, tuple[float | None, float | None]]] = {
    "spec": {
        "trivial":  (None,  None),
        "simple":   (0.30,  None),
        "moderate": (0.38,  None),
        "complex":  (0.45,  None),
        "expert":   (0.60,  None),
    },
    "map": {
        "trivial":  (None,  0.37),
        "simple":   (0.30,  0.50),
        "moderate": (0.40,  0.65),
        "complex":  (0.45,  0.72),
        "expert":   (0.60,  0.80),
    },
    "fill": {
        "trivial":  (None,  0.35),
        "simple":   (None,  0.37),
        "moderate": (None,  0.45),
        "complex":  (None,  0.60),
        "expert":   (None,  0.72),
    },
    "testgen": {
        "trivial":  (None,  0.40),
        "simple":   (None,  0.50),
        "moderate": (0.38,  0.60),
        "complex":  (0.42,  0.72),
        "expert":   (0.55,  0.85),
    },
    "review": {
        "trivial":  (0.30,  None),
        "simple":   (0.35,  None),
        "moderate": (0.40,  None),
        "complex":  (0.45,  None),
        "expert":   (0.60,  None),
    },
    "report": {
        "trivial":  (None,  None),
        "simple":   (None,  None),
        "moderate": (None,  0.40),
        "complex":  (None,  0.50),
        "expert":   (None,  0.60),
    },
}

BUDGET_COST_CAPS: dict[str, float] = {
    "free":    0.0,
    "cheap":   0.002,   # < $0.002 per map call
    "medium":  0.020,
    "premium": 999.0,
}


def _complexity_index(c: str) -> int:
    return COMPLEXITY_LEVELS.index(c) if c in COMPLEXITY_LEVELS else 2


def _meets_threshold(model: ModelSpec, phase: str, complexity: str) -> bool:
    """Return True if model meets minimum benchmark bar for this phase+complexity."""
    thresh = _PHASE_THRESHOLDS.get(phase, {}).get(complexity, (None, None))
    swe_min, he_min = thresh

    if swe_min is not None and model.swe_bench is not None:
        if model.swe_bench < swe_min:
            return False
    if he_min is not None and model.human_eval is not None:
        if model.human_eval < he_min:
            return False
    if _complexity_index(model.max_complexity) < _complexity_index(complexity):
        return False
    return True


def _score_model(
    model: ModelSpec,
    phase: str,
    complexity: str,
    prefer_local: bool,
    hardware: "HardwareSetup | None" = None,
) -> float:
    """
    Score a model candidate for routing. Higher = better choice.

    Balances: benchmark quality, cost (lower = better), speed, local preference.
    When hardware is provided, GPU-backed local models score higher than CPU.
    """
    # Benchmark score for this phase
    if phase in ("map", "spec", "review", "testgen"):
        bench = model.swe_bench or (model.human_eval or 0.0) * 0.5
    else:  # fill, report
        bench = model.human_eval or (model.swe_bench or 0.0) * 0.7

    # Cost score: 0 cost = 1.0, scaling down with price
    cost_score = 1.0 / (1.0 + model.cost_per_map_call * 500)

    # Speed score: use observed tps, or endpoint speed if hardware available
    speed = model.tps_observed
    if speed is None and hardware is not None and model.local:
        ep = hardware.best_endpoint_for(model)
        if ep and ep.tps_observed:
            speed = ep.tps_observed
        elif ep and ep.device:
            # Rough heuristic: GPU speed_score → t/s proxy
            speed = ep.device.speed_score * 4.0  # scale to ~t/s range
    if speed is None:
        speed = 50.0 if not model.local else 20.0
    speed_score = min(speed / 50.0, 1.0)

    # Local preference (and GPU-on-hardware bonus)
    local_bonus = 0.0
    if prefer_local and model.local:
        local_bonus = 0.15
    if hardware is not None and model.local:
        ep = hardware.best_endpoint_for(model)
        if ep and ep.is_gpu:
            local_bonus += 0.10  # extra bump for GPU-backed local inference

    return bench * 0.5 + cost_score * 0.25 + speed_score * 0.15 + local_bonus


# ---------------------------------------------------------------------------
# Performance tracker (runtime, updated by dev_loop)
# ---------------------------------------------------------------------------

@dataclass
class PhaseOutcome:
    model_id: str
    phase: str
    complexity: str
    success: bool
    latency_s: float
    tokens_out: int


class PerformanceTracker:
    """
    Records per-model, per-phase, per-complexity outcomes at runtime.

    Used by ModelRouter to:
    - Skip models with high failure rates
    - Update observed t/s
    - Escalate after N consecutive failures
    """

    def __init__(self) -> None:
        # key: (model_id, phase, complexity) → list of outcomes
        self._history: dict[tuple, list[PhaseOutcome]] = {}

    def record(self, outcome: PhaseOutcome) -> None:
        key = (outcome.model_id, outcome.phase, outcome.complexity)
        self._history.setdefault(key, []).append(outcome)

    def failure_rate(self, model_id: str, phase: str, complexity: str,
                     window: int = 5) -> float:
        key = (model_id, phase, complexity)
        recent = self._history.get(key, [])[-window:]
        if not recent:
            return 0.0
        return sum(1 for o in recent if not o.success) / len(recent)

    def consecutive_failures(self, model_id: str, phase: str,
                              complexity: str) -> int:
        key = (model_id, phase, complexity)
        outcomes = self._history.get(key, [])
        count = 0
        for o in reversed(outcomes):
            if not o.success:
                count += 1
            else:
                break
        return count

    def observed_tps(self, model_id: str) -> float | None:
        """Average observed tokens/sec across all phases for this model."""
        rates = []
        for key, outcomes in self._history.items():
            if key[0] != model_id:
                continue
            for o in outcomes:
                if o.latency_s > 0 and o.tokens_out > 0:
                    rates.append(o.tokens_out / o.latency_s)
        return sum(rates) / len(rates) if rates else None

    def summary(self) -> dict:
        result = {}
        for (mid, phase, comp), outcomes in self._history.items():
            key = f"{mid}/{phase}/{comp}"
            successes = sum(1 for o in outcomes if o.success)
            result[key] = {
                "total": len(outcomes),
                "success_rate": successes / len(outcomes),
                "avg_latency": sum(o.latency_s for o in outcomes) / len(outcomes),
            }
        return result


# ---------------------------------------------------------------------------
# Model router
# ---------------------------------------------------------------------------

class ModelRouter:
    """
    Select the best available model for a given phase and complexity.

    Priority order:
    1. Meets benchmark threshold for phase+complexity
    2. Available (in available_providers)
    3. Within budget
    4. Fits available hardware (VRAM/RAM check via HardwareSetup)
    5. Not recently failing (from tracker)
    6. Highest composite score (quality × cost × speed × local preference)

    When a local model doesn't fit available hardware, the router
    automatically falls back through lower-quant variants of the same
    base_model before giving up or using cloud ("club until it fits").
    """

    def __init__(
        self,
        available_providers: set[str] | None = None,
        budget: str = "medium",
        prefer_local: bool = True,
        tracker: PerformanceTracker | None = None,
        escalate_after_failures: int = 2,
        hardware: "HardwareSetup | None" = None,
    ) -> None:
        """
        Parameters
        ----------
        available_providers: Set of provider strings to restrict to.
            e.g. {"anthropic"} for Anthropic-only, {"ollama","llama-server"}
            for local-only. None = all providers.
        budget: "free" | "cheap" | "medium" | "premium"
        prefer_local: Boost score of local models.
        tracker:  Optional PerformanceTracker for dynamic escalation.
        escalate_after_failures: Skip model after this many consecutive failures.
        hardware: HardwareSetup describing available devices and endpoints.
            If provided, local models are filtered by VRAM/RAM fit.
            If None, all local models are considered (no hardware check).
        """
        self.available_providers = available_providers
        self.budget = budget
        self.prefer_local = prefer_local
        self.tracker = tracker or PerformanceTracker()
        self.escalate_after = escalate_after_failures
        self.hardware = hardware

    def _hardware_fits(self, model: ModelSpec) -> bool:
        """Return True if model fits available hardware (or no hardware declared)."""
        if self.hardware is None:
            return True
        return self.hardware.can_fit(model)

    def select(
        self,
        phase: str,
        complexity: str,
        *,
        exclude_ids: set[str] | None = None,
    ) -> ModelSpec | None:
        """
        Return the best available model for this phase and complexity.

        For local models that don't fit available hardware, automatically
        tries lower-quant variants of the same base_model before skipping
        ("club until it fits"). Cloud models are unaffected.

        Returns None if no model meets the criteria.
        """
        cost_cap = BUDGET_COST_CAPS.get(self.budget, 999.0)
        exclude = exclude_ids or set()

        candidates = []
        for model in REGISTRY:
            # Provider filter
            if (self.available_providers is not None
                    and model.provider not in self.available_providers):
                continue

            # Phase support
            if phase not in model.phases:
                continue

            # Explicit exclusion
            if model.id in exclude:
                continue

            # Budget
            if model.cost_per_map_call > cost_cap:
                continue

            # Benchmark threshold
            if not _meets_threshold(model, phase, complexity):
                continue

            # Hardware fit — for local models, check VRAM/RAM
            if model.local and not self._hardware_fits(model):
                continue

            # Dynamic: skip if consecutive failures exceed threshold
            if self.tracker.consecutive_failures(model.id, phase, complexity) >= self.escalate_after:
                continue

            score = _score_model(model, phase, complexity, self.prefer_local,
                                 hardware=self.hardware)
            candidates.append((score, model))

        if not candidates:
            return None

        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]

    def explain_hardware_fit(self, phase: str, complexity: str) -> list[dict]:
        """
        Show why each local model candidate was accepted or rejected by hardware.
        Useful for debugging "why is nothing running on my GPU?"
        """
        if self.hardware is None:
            return [{"note": "No hardware declared — all models pass fit check"}]

        results = []
        for model in REGISTRY:
            if not model.local:
                continue
            if phase not in model.phases:
                continue
            fits = self._hardware_fits(model)
            needed = model.vram_mb or model.ram_mb or 0
            # Determine whether fit is GPU-native or CPU-fallback
            fit_path = "n/a"
            if model.vram_mb and fits:
                gpu_fit = any(d.vram_mb >= model.vram_mb
                              for d in self.hardware.devices)
                fit_path = "gpu" if gpu_fit else "cpu-fallback (slow)"
            elif model.ram_mb and fits:
                fit_path = "cpu"

            results.append({
                "model": model.name,
                "id": model.id,
                "quant": model.quant,
                "needed_mb": needed,
                "fits": fits,
                "fit_path": fit_path,
                "reason": (
                    f"needs {needed}MB {'VRAM' if model.vram_mb else 'RAM'}, "
                    f"have {self.hardware.total_vram_mb}MB VRAM / "
                    f"{self.hardware.ram_mb}MB RAM"
                    + (f" [{fit_path}]" if fits else " [does not fit]")
                ),
            })
        return results

    def select_suite(self, complexity: str) -> dict[str, ModelSpec | None]:
        """
        Select a full model suite for all phases at a given complexity.

        Returns {phase: ModelSpec} dict. Ensures the review model differs
        from the map model where possible (independent perspective).
        """
        suite: dict[str, ModelSpec | None] = {}
        for phase in PHASES:
            suite[phase] = self.select(phase, complexity)

        # Ensure reviewer differs from map model if possible
        if (suite.get("map") and suite.get("review")
                and suite["map"] and suite["review"]
                and suite["map"].id == suite["review"].id):
            map_id = suite["map"].id
            alt = self.select("review", complexity, exclude_ids={map_id})
            if alt:
                suite["review"] = alt

        return suite

    def explain(self, phase: str, complexity: str) -> list[tuple[float, ModelSpec]]:
        """Return ranked candidates with scores for debugging/display."""
        cost_cap = BUDGET_COST_CAPS.get(self.budget, 999.0)
        results = []
        for model in REGISTRY:
            if (self.available_providers is not None
                    and model.provider not in self.available_providers):
                continue
            if phase not in model.phases:
                continue
            if model.cost_per_map_call > cost_cap:
                continue
            passes_threshold = _meets_threshold(model, phase, complexity)
            score = _score_model(model, phase, complexity, self.prefer_local)
            results.append((score, model, passes_threshold))
        results.sort(key=lambda x: -x[0])
        return [(s, m) for s, m, ok in results if ok]


# ---------------------------------------------------------------------------
# Preset configurations for common setups
# ---------------------------------------------------------------------------

def router_for_setup(setup: str, hardware: "HardwareSetup | None" = None, **kwargs) -> ModelRouter:
    """
    Return a pre-configured ModelRouter for common hardware/subscription setups.

    setup options:
      "local_only"       — No internet, uses Ollama + llama-server
      "local_b580"       — B580 SYCL for map, Ollama CPU for fill
      "openrouter_free"  — Free tier only (rate-limited, use sparingly)
      "openrouter_cheap" — Paid but cheap (< $0.002 per call)
      "anthropic"        — Anthropic API only
      "copilot"          — GitHub Copilot models (GPT family)
      "best_local_first" — Prefer local, cloud fallback for hard phases
    """
    presets: dict[str, dict] = {
        "local_only": dict(
            available_providers={"ollama"},
            budget="free", prefer_local=True,
        ),
        "local_b580": dict(
            available_providers={"ollama", "llama-server"},
            budget="free", prefer_local=True,
        ),
        "openrouter_free": dict(
            available_providers={"openrouter"},
            budget="free", prefer_local=False,
        ),
        "openrouter_cheap": dict(
            available_providers={"openrouter"},
            budget="cheap", prefer_local=False,
        ),
        "anthropic": dict(
            available_providers={"anthropic"},
            budget="medium", prefer_local=False,
        ),
        "copilot": dict(
            available_providers={"openrouter"},
            budget="medium", prefer_local=False,
            # Could filter by tags={"copilot"} if needed
        ),
        "github": dict(
            available_providers={"github"},
            budget="free", prefer_local=False,
        ),
        "best_local_first": dict(
            available_providers={"ollama", "llama-server", "openrouter"},
            budget="cheap", prefer_local=True,
        ),
    }
    config = presets.get(setup, presets["best_local_first"])
    config.update(kwargs)
    if hardware is not None:
        config["hardware"] = hardware
    return ModelRouter(**config)


# ---------------------------------------------------------------------------
# CLI: print the routing table
# ---------------------------------------------------------------------------

def print_routing_table(router: ModelRouter | None = None) -> None:
    """Print the full routing matrix for all phases × complexities."""
    if router is None:
        router = ModelRouter()

    print(f"\n{'='*100}")
    print(f"  MODEL ROUTING TABLE  (budget={router.budget}, "
          f"providers={router.available_providers or 'all'}, "
          f"prefer_local={router.prefer_local})")
    print(f"{'='*100}")
    header = f"  {'Phase':10s} {'Complexity':10s} {'Model':35s} {'Provider':12s} "
    header += f"{'SWE':5s} {'HE':5s} {'$/call':8s} {'t/s':5s}"
    print(header)
    print("  " + "-"*98)

    for phase in PHASES:
        for complexity in COMPLEXITY_LEVELS:
            m = router.select(phase, complexity)
            if m:
                swe = f"{m.swe_bench:.2f}" if m.swe_bench else "  — "
                he  = f"{m.human_eval:.2f}" if m.human_eval else "  — "
                cost = "$0 local" if m.free else f"${m.cost_per_map_call:.5f}"
                tps = f"{m.tps_observed:.0f}" if m.tps_observed else "  — "
                print(f"  {phase:10s} {complexity:10s} {m.name:35s} "
                      f"{m.provider:12s} {swe:5s} {he:5s} {cost:8s} {tps:5s}")
            else:
                print(f"  {phase:10s} {complexity:10s} {'(no model available)':35s}")

    print(f"{'='*100}\n")


def print_complexity_suite(task: str, router: ModelRouter | None = None) -> None:
    """Estimate complexity and print the full model suite for a task."""
    if router is None:
        router = ModelRouter()
    complexity = estimate_complexity(task)
    suite = router.select_suite(complexity)

    print(f"\n  Task complexity: {complexity.upper()}")
    print(f"  {'Phase':10s} {'Model':35s} {'SWE':5s} {'HE':5s} {'Cost/call':10s}")
    print("  " + "-"*70)
    for phase, m in suite.items():
        if m:
            swe  = f"{m.swe_bench:.2f}" if m.swe_bench else "  — "
            he   = f"{m.human_eval:.2f}" if m.human_eval else "  — "
            cost = "$0" if m.free else f"${m.cost_per_map_call:.5f}"
            print(f"  {phase:10s} {m.name:35s} {swe:5s} {he:5s} {cost:10s}")
        else:
            print(f"  {phase:10s} (none available)")
    print()


if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="codeclub model routing table")
    parser.add_argument("--setup", default="best_local_first",
                        choices=list(router_for_setup.__doc__.split("setup options:")[1]
                                     .split("\n")[1:8]),
                        help="Hardware/provider setup preset")
    parser.add_argument("--budget", default="cheap",
                        choices=["free","cheap","medium","premium"])
    parser.add_argument("--task", default=None,
                        help="Show model suite for a specific task (estimates complexity)")
    args = parser.parse_args()

    r = router_for_setup("best_local_first", budget=args.budget)
    print_routing_table(r)

    if args.task:
        print_complexity_suite(args.task, r)
