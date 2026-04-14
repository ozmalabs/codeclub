"""
Clarity uplift layer for the dynamic context system.

Before routing a task to a model, check if the task's spec clarity is below
the empirically-measured cliff (~40c).  If so, calculate whether it's more
efficient to first clarify the spec (using a cheap model), then route the
clearer task to a smaller model — vs sending it directly to a big expensive
model.

Empirical basis (141 benchmark fights):
  - clarity 20-30 → 0% quality across ALL models (even frontier)
  - clarity 50-55 → 80-100% quality on mid-tier models
  - cliff between 30-50c — below it, tokens are wasted regardless of model
  - every task uplifted from c≈25 to c≈55 went from 0% to 80-100%
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum


class UpliftPreference(str, Enum):
    """User preference for the speed/efficiency tradeoff."""
    SPEED = "speed"             # skip uplift, pay for bigger model (one round trip)
    EFFICIENCY = "efficiency"   # always uplift when ROI is positive (two round trips)
    BALANCED = "balanced"       # uplift when savings > 2x clarification cost


# The clarity cliff — empirically measured sigmoid parameters
CLARITY_SIGMOID_K = 0.25       # steepness
CLARITY_CLIFF_OFFSET = 20      # cliff is min_clarity - this


@dataclass
class UpliftDecision:
    """Result of the uplift decision analysis."""
    should_uplift: bool
    reason: str

    # Economics
    estimated_clarity: int
    target_clarity: int
    direct_success_prob: float
    uplifted_success_prob: float

    # Cost estimates (relative, not absolute — model costs vary)
    direct_cost_factor: float
    uplift_cost_factor: float

    # The numbers that made the decision
    roi: float


@dataclass
class UpliftResult:
    """Result of actually performing the clarity uplift."""
    original_message: str
    uplifted_message: str
    original_clarity: int
    uplifted_clarity: int
    uplift_tokens: int
    method: str                    # "decompose" | "restructure" | "expand"


# ── Sigmoid probability model ────────────────────────────────────────


def estimate_success_probability(clarity: int, min_clarity: int = 55) -> float:
    """
    Estimate probability of task success given spec clarity.

    Uses the empirically calibrated sigmoid from SmashRange.fit().
    This is the clarity component of the SmashRange heuristic,
    extracted for standalone use.
    """
    cliff = min_clarity - CLARITY_CLIFF_OFFSET
    return 1.0 / (1.0 + math.exp(-CLARITY_SIGMOID_K * (clarity - cliff)))


# ── Uplift decision ──────────────────────────────────────────────────


def decide_uplift(
    clarity: int,
    preference: UpliftPreference = UpliftPreference.BALANCED,
    *,
    min_clarity: int = 55,
    uplift_token_cost: float = 15.0,
    direct_model_cost_factor: float = 10.0,
    small_model_cost_factor: float = 1.0,
    uplift_clarity_boost: int = 30,
) -> UpliftDecision:
    """
    Decide whether to uplift (clarify) a vague task before routing.

    Args:
        clarity: Estimated current spec clarity (0-100)
        preference: User's speed/efficiency preference
        min_clarity: Model's min_clarity threshold (from SmashRange)
        uplift_token_cost: Relative cost of the clarification pass
            (same units as model cost factors)
        direct_model_cost_factor: Relative cost of model that handles low clarity
        small_model_cost_factor: Relative cost of model after clarity uplift
        uplift_clarity_boost: How many clarity points the uplift adds (empirical ~30)

    Cost model:
        direct_cost = direct_model_cost × (1 / direct_success_prob)
        uplift_cost = uplift_token_cost × small_model_cost + small_model_cost / uplifted_prob
        ROI = (direct_cost - uplift_cost) / uplift_cost
    """
    direct_prob = estimate_success_probability(clarity, min_clarity)
    uplifted_clarity = min(95, clarity + uplift_clarity_boost)
    uplifted_prob = estimate_success_probability(uplifted_clarity, min_clarity)

    direct_expected_cost = direct_model_cost_factor / max(direct_prob, 0.05)
    uplift_expected_cost = (
        uplift_token_cost * small_model_cost_factor
        + small_model_cost_factor / max(uplifted_prob, 0.1)
    )

    roi = (direct_expected_cost - uplift_expected_cost) / max(uplift_expected_cost, 0.01)

    # Apply preference thresholds
    if preference == UpliftPreference.SPEED:
        should_uplift = roi > 5.0 and direct_prob < 0.1
    elif preference == UpliftPreference.EFFICIENCY:
        should_uplift = roi > 0.0
    else:  # BALANCED
        should_uplift = roi > 1.0

    # Build explanation
    if should_uplift:
        reason = (
            f"Clarity {clarity}→{uplifted_clarity} boosts P(success) from "
            f"{direct_prob:.0%} to {uplifted_prob:.0%}; ROI={roi:.1f}x "
            f"({preference.value} mode)"
        )
    else:
        if direct_prob > 0.8:
            reason = f"Clarity {clarity} already sufficient (P={direct_prob:.0%})"
        elif roi <= 0:
            reason = f"Uplift ROI negative ({roi:.1f}x); direct send cheaper"
        else:
            reason = (
                f"ROI={roi:.1f}x below {preference.value} threshold; "
                f"sending directly (P={direct_prob:.0%})"
            )

    return UpliftDecision(
        should_uplift=should_uplift,
        reason=reason,
        estimated_clarity=clarity,
        target_clarity=uplifted_clarity,
        direct_success_prob=round(direct_prob, 4),
        uplifted_success_prob=round(uplifted_prob, 4),
        direct_cost_factor=round(direct_expected_cost, 2),
        uplift_cost_factor=round(uplift_expected_cost, 2),
        roi=round(roi, 4),
    )


# ── Expand heuristic (no model needed) ───────────────────────────────

_EXPAND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\brate\s*limit(?:er|ing)?\b", re.I),
        "RateLimiter class with token bucket algorithm, configurable rate and "
        "burst, thread-safe, acquire() and try_acquire() methods",
    ),
    (
        re.compile(r"\bcache\b", re.I),
        "LRU cache with get/put, configurable max size, O(1) operations, "
        "optional TTL expiry",
    ),
    (
        re.compile(r"\bparser?\b", re.I),
        "Parser class with parse() method returning AST, error handling with "
        "line numbers, configurable grammar",
    ),
    (
        re.compile(r"\bqueue\b", re.I),
        "thread-safe Queue with enqueue/dequeue, configurable max capacity, "
        "blocking and non-blocking modes",
    ),
    (
        re.compile(r"\blogger?\b", re.I),
        "structured Logger with configurable log levels, formatters, "
        "file and stdout handlers, context fields",
    ),
    (
        re.compile(r"\bpoolr?\b", re.I),
        "resource Pool with acquire/release, configurable size, "
        "health checking, timeout on acquire",
    ),
    (
        re.compile(r"\bretry\b", re.I),
        "retry decorator with exponential backoff, configurable max retries "
        "and jitter, per-exception filtering",
    ),
    (
        re.compile(r"\bcircuit\s*breaker\b", re.I),
        "CircuitBreaker with closed/open/half-open states, configurable "
        "failure threshold and recovery timeout",
    ),
    (
        re.compile(r"\bscheduler\b", re.I),
        "task Scheduler supporting cron expressions and intervals, "
        "with cancel/pause/resume per job",
    ),
    (
        re.compile(r"\bmiddleware\b", re.I),
        "middleware chain with before/after hooks, error handling, "
        "request/response transformation, ordered execution",
    ),
]


def _expand_heuristic(message: str) -> str:
    """Keyword-based expansion for common patterns."""
    expansions: list[str] = []
    for pattern, expansion in _EXPAND_PATTERNS:
        if pattern.search(message):
            expansions.append(expansion)

    if not expansions:
        return message

    parts = [message.rstrip(".") + "."]
    parts.append("Specifically: " + "; ".join(expansions) + ".")
    return " ".join(parts)


# ── Restructure prompt (for model-based uplift) ──────────────────────

_RESTRUCTURE_PROMPT = """\
You are a spec writer. Rewrite this vague request into a clear, specific \
technical specification. Do NOT implement it — just clarify what needs to \
be built.

Vague request: {message}

Rewrite as a clear spec with:
1. What to build (specific class/function names)
2. Key methods/interface
3. Core algorithm or approach
4. Edge cases to handle
5. What "done" looks like

Be concise. 100 words max."""


# ── Uplift execution ─────────────────────────────────────────────────


def _estimate_clarity_lazy(message: str) -> int:
    """Import and call estimate_clarity lazily to avoid circular deps."""
    from codeclub.context.classifier import estimate_clarity
    return estimate_clarity(message)


def uplift_spec(
    message: str,
    call_fn: callable | None = None,
    method: str = "restructure",
) -> UpliftResult:
    """
    Uplift a vague task spec into a clearer one.

    Three methods:
      - "restructure": Use a lightweight prompt to restructure the request
        into specific requirements (requires call_fn for model-based uplift).
      - "decompose": Use codeclub's spec.py decompose() — full structured spec.
      - "expand": Simple keyword expansion heuristic (no model, fastest).

    Args:
        message: The vague user message.
        call_fn: LLM call function for model-based uplift (optional).
            Signature: call_fn(prompt: str) -> str
        method: Which uplift method to use.
    """
    original_clarity = _estimate_clarity_lazy(message)

    if method == "expand":
        uplifted = _expand_heuristic(message)
        uplifted_clarity = _estimate_clarity_lazy(uplifted)
        return UpliftResult(
            original_message=message,
            uplifted_message=uplifted,
            original_clarity=original_clarity,
            uplifted_clarity=uplifted_clarity,
            uplift_tokens=0,
            method="expand",
        )

    if method == "restructure":
        if call_fn is None:
            # Fall back to expand if no model available
            return uplift_spec(message, call_fn=None, method="expand")

        prompt = _RESTRUCTURE_PROMPT.format(message=message)
        uplifted = call_fn(prompt)
        uplift_tokens = len(prompt.split()) + len(uplifted.split())
        uplifted_clarity = _estimate_clarity_lazy(uplifted)
        return UpliftResult(
            original_message=message,
            uplifted_message=uplifted,
            original_clarity=original_clarity,
            uplifted_clarity=uplifted_clarity,
            uplift_tokens=uplift_tokens,
            method="restructure",
        )

    if method == "decompose":
        from spec import decompose

        parts = decompose(message)
        uplifted = "\n".join(
            f"- {p}" if isinstance(p, str) else str(p) for p in parts
        )
        uplifted_clarity = _estimate_clarity_lazy(uplifted)
        return UpliftResult(
            original_message=message,
            uplifted_message=uplifted,
            original_clarity=original_clarity,
            uplifted_clarity=uplifted_clarity,
            uplift_tokens=0,
            method="decompose",
        )

    raise ValueError(f"Unknown uplift method: {method!r}")
