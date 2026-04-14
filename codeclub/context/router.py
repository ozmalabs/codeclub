"""
Context-aware model routing.

Adds context size as a third routing axis for Club Smash:
    task(difficulty, clarity) + context_tokens → best model

Smaller assembled context enables cheaper/faster models.
Integrates fit precision, clarity uplift, and SmashRange.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeclub.infra.models import ModelSpec, ModelRouter

from .assembler import FitLevel


# ── Helpers ──────────────────────────────────────────────────────────


def context_window_fits(
    model_context: int,
    assembled_tokens: int,
    reserve_fraction: float = 0.25,
) -> bool:
    """
    Check if assembled context fits in model's window with room for output.

    Reserve 25% of context window for model output by default.
    """
    available = int(model_context * (1.0 - reserve_fraction))
    return assembled_tokens <= available


def _context_efficiency(
    model_context: int,
    assembled_tokens: int,
    reserve: float,
) -> float:
    """
    Score how efficiently a model's context window is used.

    Perfect = assembled_tokens uses 30-75% of available window.
    Too small (using <10% of window) = wasteful, mild penalty.
    Too large (doesn't fit) = 0.
    """
    available = int(model_context * (1.0 - reserve))
    if assembled_tokens > available:
        return 0.0

    utilization = assembled_tokens / max(available, 1)

    # Sweet spot: 30-75% utilization
    if 0.30 <= utilization <= 0.75:
        return 1.0
    elif utilization < 0.30:
        # Under-utilizing — mild penalty (model is bigger than needed)
        return 0.7 + utilization  # 0.7 at 0%, 1.0 at 30%
    else:
        # 75-100% — getting tight, slight concern
        return 1.0 - (utilization - 0.75) * 2  # 1.0 at 75%, 0.5 at 100%


def _get_smash_fitness(model: ModelSpec, difficulty: int, clarity: int) -> float:
    """Get SmashRange fitness for a model at given coordinates."""
    try:
        from tournament import SmashCoord, build_contenders
        contenders = {c.name: c for c in build_contenders()}
        contender = contenders.get(model.name) or contenders.get(model.id)
        if contender:
            return contender.smash.fit(SmashCoord(difficulty, clarity))
    except ImportError:
        pass

    # Fallback: estimate from params
    try:
        from tournament import estimate_smash_range, SmashCoord
        if model.params_b:
            smash = estimate_smash_range(
                model.params_b,
                quant=model.quant or "",
            )
            return smash.fit(SmashCoord(difficulty, clarity))
    except ImportError:
        pass

    return 0.5  # no info, neutral score


# ── Routing decision ─────────────────────────────────────────────────


@dataclass
class RoutingDecision:
    """Full routing decision with explanation."""

    model: ModelSpec | None          # selected model (None if nothing fits)
    phase: str                       # which phase this is for

    # The inputs that drove the decision
    context_tokens: int              # assembled context size
    estimated_difficulty: int        # task difficulty (0-100)
    estimated_clarity: int           # task clarity (0-100)
    fit_level: str                   # fit precision used

    # Why this model was chosen
    context_fits: bool               # True if context fits model's window
    smash_fit: float                 # SmashRange fitness score (0-1)
    score: float                     # overall routing score

    reason: str                      # human-readable explanation

    # Alternatives considered
    candidates_considered: int       # how many models were evaluated
    candidates_rejected_context: int  # rejected because context too large

    # Cost comparison
    estimated_cost_factor: float     # relative cost (1.0 = cheapest option)
    model_downgraded: bool           # True if context savings enabled a cheaper model


# ── Context router ───────────────────────────────────────────────────


class ContextRouter:
    """
    Model router that considers context size alongside difficulty/clarity.

    Wraps the standard ModelRouter and adds:
    1. Context window filtering (reject models where context doesn't fit)
    2. SmashRange fitness scoring (from tournament.py or estimated from params)
    3. Context-size bonus (smaller context → can use cheaper models → bonus)
    4. Uplift-aware routing (if uplift was performed, adjust clarity)

    Usage::

        from codeclub.context.router import ContextRouter
        from codeclub.infra.models import ModelRouter

        base_router = ModelRouter(prefer_local=True)
        ctx_router = ContextRouter(base_router)

        decision = ctx_router.select(
            phase="fill",
            complexity="simple",
            context_tokens=2000,
            difficulty=30,
            clarity=55,
        )
        print(decision.model.name, decision.reason)
    """

    def __init__(
        self,
        base_router: ModelRouter,
        output_reserve: float = 0.25,
    ) -> None:
        self.base_router = base_router
        self.output_reserve = output_reserve

    # ── Main entry point ─────────────────────────────────────────────

    def select(
        self,
        phase: str,
        complexity: str,
        *,
        context_tokens: int = 0,
        difficulty: int = 50,
        clarity: int = 65,
        fit_level: FitLevel = FitLevel.BALANCED,
        exclude_ids: set[str] | None = None,
    ) -> RoutingDecision:
        """
        Select the best model considering context size.

        Flow:
        1. Get candidates from base router (respects provider, budget, hardware)
        2. Filter by context window (model.context must fit assembled_tokens)
        3. Score by SmashRange fitness × context efficiency
        4. Return best with full explanation
        """
        from codeclub.infra.models import REGISTRY, BUDGET_COST_CAPS, _score_model

        cost_cap = BUDGET_COST_CAPS.get(self.base_router.budget, 999.0)
        exclude = exclude_ids or set()

        scored: list[tuple[float, ModelSpec, float, float, float]] = []
        rejected_context = 0
        total_considered = 0

        for model in REGISTRY:
            # --- Provider filter ---
            if (self.base_router.available_providers is not None
                    and model.provider not in self.base_router.available_providers):
                continue

            # --- Phase support ---
            if phase not in model.phases:
                continue

            # --- Explicit exclusion ---
            if model.id in exclude:
                continue

            # --- Budget ---
            if model.cost_per_map_call > cost_cap:
                continue

            # --- Hardware fit (local models) ---
            if model.local and not self.base_router._hardware_fits(model):
                continue

            total_considered += 1

            # --- Context window filter ---
            if context_tokens > 0 and not context_window_fits(
                model.context, context_tokens, self.output_reserve
            ):
                rejected_context += 1
                continue

            # --- Dynamic failure check ---
            tracker = self.base_router.tracker
            if tracker.consecutive_failures(
                model.id, phase, complexity
            ) >= self.base_router.escalate_after:
                continue

            # --- Compute composite score ---
            base_score = _score_model(
                model, phase, complexity, self.base_router.prefer_local,
                hardware=self.base_router.hardware,
            )
            smash_fit = _get_smash_fitness(model, difficulty, clarity)
            ctx_eff = (
                _context_efficiency(model.context, context_tokens, self.output_reserve)
                if context_tokens > 0
                else 1.0
            )

            # Weighted combination: base drives selection, smash and ctx_eff
            # refine it. Weights: base 50%, smash 30%, context 20%.
            combined = base_score * 0.50 + smash_fit * 0.30 + ctx_eff * 0.20
            scored.append((combined, model, base_score, smash_fit, ctx_eff))

        # --- Pick winner ---
        if not scored:
            return RoutingDecision(
                model=None,
                phase=phase,
                context_tokens=context_tokens,
                estimated_difficulty=difficulty,
                estimated_clarity=clarity,
                fit_level=fit_level.value if isinstance(fit_level, FitLevel) else str(fit_level),
                context_fits=False,
                smash_fit=0.0,
                score=0.0,
                reason="No model fits: all candidates rejected by context, budget, or phase",
                candidates_considered=total_considered,
                candidates_rejected_context=rejected_context,
                estimated_cost_factor=0.0,
                model_downgraded=False,
            )

        scored.sort(key=lambda t: -t[0])
        combined, winner, base_score, smash_fit, ctx_eff = scored[0]

        # Determine if a cheaper model was enabled by small context
        cheapest_cost = min(m.cost_per_map_call for _, m, *_ in scored)
        winner_cost = winner.cost_per_map_call
        cost_factor = (winner_cost / cheapest_cost) if cheapest_cost > 0 else 1.0

        # Did context savings let us pick something cheaper than the base
        # router would have selected?
        base_pick = self.base_router.select(phase, complexity, exclude_ids=exclude_ids)
        downgraded = (
            base_pick is not None
            and winner.id != base_pick.id
            and winner.cost_per_map_call < base_pick.cost_per_map_call
        )

        reason_parts = [f"{winner.name} (score={combined:.3f})"]
        if smash_fit >= 0.8:
            reason_parts.append(f"strong smash fit {smash_fit:.2f}")
        elif smash_fit < 0.4:
            reason_parts.append(f"weak smash fit {smash_fit:.2f}")
        if ctx_eff >= 0.9:
            reason_parts.append("good context utilization")
        elif ctx_eff < 0.75:
            reason_parts.append(f"context efficiency {ctx_eff:.2f}")
        if downgraded:
            reason_parts.append("context savings enabled cheaper model")

        return RoutingDecision(
            model=winner,
            phase=phase,
            context_tokens=context_tokens,
            estimated_difficulty=difficulty,
            estimated_clarity=clarity,
            fit_level=fit_level.value if isinstance(fit_level, FitLevel) else str(fit_level),
            context_fits=True,
            smash_fit=round(smash_fit, 4),
            score=round(combined, 4),
            reason="; ".join(reason_parts),
            candidates_considered=total_considered,
            candidates_rejected_context=rejected_context,
            estimated_cost_factor=round(cost_factor, 4),
            model_downgraded=downgraded,
        )

    # ── Suite selection ───────────────────────────────────────────────

    def select_suite(
        self,
        complexity: str,
        *,
        context_tokens: int = 0,
        difficulty: int = 50,
        clarity: int = 65,
        fit_level: FitLevel = FitLevel.BALANCED,
    ) -> dict[str, RoutingDecision]:
        """Select a full model suite for all phases, context-aware."""
        from codeclub.infra.models import PHASES

        suite: dict[str, RoutingDecision] = {}
        for phase in PHASES:
            suite[phase] = self.select(
                phase, complexity,
                context_tokens=context_tokens,
                difficulty=difficulty,
                clarity=clarity,
                fit_level=fit_level,
            )
        return suite

    # ── Explain / debug ──────────────────────────────────────────────

    def explain(
        self,
        phase: str,
        complexity: str,
        *,
        context_tokens: int = 0,
        difficulty: int = 50,
        clarity: int = 65,
    ) -> list[dict]:
        """
        Show all candidates with their scores broken down.
        Useful for debugging routing decisions.

        Returns list of dicts with: model, base_score, smash_fit,
        context_efficiency, combined_score, context_fits, rejected_reason.
        """
        from codeclub.infra.models import REGISTRY, BUDGET_COST_CAPS, _score_model

        cost_cap = BUDGET_COST_CAPS.get(self.base_router.budget, 999.0)
        results: list[dict] = []

        for model in REGISTRY:
            entry: dict = {
                "model": model.name,
                "model_id": model.id,
                "context_window": model.context,
            }

            # Provider filter
            if (self.base_router.available_providers is not None
                    and model.provider not in self.base_router.available_providers):
                entry.update(rejected_reason="provider not available",
                             base_score=0, smash_fit=0, context_efficiency=0,
                             combined_score=0, context_fits=False)
                results.append(entry)
                continue

            # Phase support
            if phase not in model.phases:
                entry.update(rejected_reason=f"phase '{phase}' not supported",
                             base_score=0, smash_fit=0, context_efficiency=0,
                             combined_score=0, context_fits=False)
                results.append(entry)
                continue

            # Budget
            if model.cost_per_map_call > cost_cap:
                entry.update(rejected_reason="over budget",
                             base_score=0, smash_fit=0, context_efficiency=0,
                             combined_score=0, context_fits=False)
                results.append(entry)
                continue

            fits = context_window_fits(
                model.context, context_tokens, self.output_reserve
            ) if context_tokens > 0 else True

            base_score = _score_model(
                model, phase, complexity, self.base_router.prefer_local,
                hardware=self.base_router.hardware,
            )
            smash_fit = _get_smash_fitness(model, difficulty, clarity)
            ctx_eff = (
                _context_efficiency(model.context, context_tokens, self.output_reserve)
                if context_tokens > 0
                else 1.0
            )

            combined = base_score * 0.50 + smash_fit * 0.30 + ctx_eff * 0.20

            rejected_reason = None if fits else "context too large"

            entry.update(
                base_score=round(base_score, 4),
                smash_fit=round(smash_fit, 4),
                context_efficiency=round(ctx_eff, 4),
                combined_score=round(combined, 4),
                context_fits=fits,
                rejected_reason=rejected_reason,
            )
            results.append(entry)

        results.sort(key=lambda d: -d["combined_score"])
        return results
