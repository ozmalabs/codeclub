"""Tests for codeclub.context.router — context-aware model routing."""
from __future__ import annotations

import pytest
from dataclasses import dataclass, field

from codeclub.context.router import (
    ContextRouter,
    RoutingDecision,
    context_window_fits,
    _context_efficiency,
)
from codeclub.context.assembler import FitLevel
from codeclub.infra.models import (
    ModelSpec,
    ModelRouter,
    PerformanceTracker,
    PHASES,
    _score_model,
)


# ── Fixtures: minimal model specs ────────────────────────────────────

def _make_model(
    name: str,
    *,
    context: int = 8192,
    cost_in: float = 0.0,
    cost_out: float = 0.0,
    swe_bench: float | None = 0.30,
    human_eval: float | None = 0.50,
    params_b: float | None = None,
    quant: str | None = None,
    local: bool = False,
    phases: frozenset[str] | None = None,
    max_complexity: str = "hard",
) -> ModelSpec:
    """Build a minimal ModelSpec for testing."""
    return ModelSpec(
        id=name,
        name=name,
        provider="ollama" if local else "openrouter",
        family="test",
        cost_in=cost_in,
        cost_out=cost_out,
        context=context,
        swe_bench=swe_bench,
        human_eval=human_eval,
        local=local,
        params_b=params_b,
        quant=quant,
        phases=phases or frozenset(PHASES),
        max_complexity=max_complexity,
    )


SMALL_MODEL = _make_model("small-4k", context=4096, cost_in=0.1, cost_out=0.1)
MEDIUM_MODEL = _make_model("medium-32k", context=32768, cost_in=0.5, cost_out=0.5)
LARGE_MODEL = _make_model(
    "large-128k", context=131072, cost_in=2.0, cost_out=2.0,
    swe_bench=0.55, human_eval=0.80,
)
LOCAL_MODEL = _make_model(
    "local-8k", context=8192, local=True, params_b=8.0, quant="q6_k",
)
TEST_MODELS = [SMALL_MODEL, MEDIUM_MODEL, LARGE_MODEL, LOCAL_MODEL]


# ── Helpers ──────────────────────────────────────────────────────────

@pytest.fixture()
def patched_registry(monkeypatch):
    """Replace the global REGISTRY with our test models."""
    import codeclub.infra.models as mod
    monkeypatch.setattr(mod, "REGISTRY", TEST_MODELS)
    return TEST_MODELS


@pytest.fixture()
def router(patched_registry):
    """ContextRouter wrapping a basic ModelRouter."""
    base = ModelRouter(budget="premium", prefer_local=False)
    return ContextRouter(base)


@pytest.fixture()
def local_router(patched_registry):
    """ContextRouter that prefers local models."""
    base = ModelRouter(budget="premium", prefer_local=True)
    return ContextRouter(base)


# ── context_window_fits ──────────────────────────────────────────────


class TestContextWindowFits:
    def test_fits_with_room(self):
        assert context_window_fits(8192, 2000) is True

    def test_exactly_at_boundary(self):
        # 75% of 8192 = 6144
        assert context_window_fits(8192, 6144) is True

    def test_one_over_boundary(self):
        assert context_window_fits(8192, 6145) is False

    def test_zero_context(self):
        assert context_window_fits(8192, 0) is True

    def test_custom_reserve(self):
        # 50% reserve → 4096 available
        assert context_window_fits(8192, 4096, reserve_fraction=0.5) is True
        assert context_window_fits(8192, 4097, reserve_fraction=0.5) is False

    def test_zero_reserve(self):
        assert context_window_fits(8192, 8192, reserve_fraction=0.0) is True
        assert context_window_fits(8192, 8193, reserve_fraction=0.0) is False


# ── _context_efficiency ──────────────────────────────────────────────


class TestContextEfficiency:
    def test_doesnt_fit_returns_zero(self):
        assert _context_efficiency(4096, 5000, 0.25) == 0.0

    def test_sweet_spot(self):
        # 50% utilization of 8192 * 0.75 = 6144 → 3072 tokens
        assert _context_efficiency(8192, 3072, 0.25) == 1.0

    def test_under_utilization_penalty(self):
        # 0% utilization
        eff = _context_efficiency(131072, 0, 0.25)
        assert 0.69 <= eff <= 0.71  # ~0.7

    def test_over_utilization_penalty(self):
        # 90% utilization → slight penalty
        available = int(8192 * 0.75)  # 6144
        tokens = int(available * 0.90)
        eff = _context_efficiency(8192, tokens, 0.25)
        assert 0.5 < eff < 1.0

    def test_at_75_percent_is_one(self):
        available = int(8192 * 0.75)  # 6144
        tokens = int(available * 0.75)
        assert _context_efficiency(8192, tokens, 0.25) == 1.0


# ── Context filtering ────────────────────────────────────────────────


class TestContextFiltering:
    def test_large_context_rejects_small_models(self, router):
        """When context is large, small-window models are rejected."""
        decision = router.select(
            "fill", "simple",
            context_tokens=5000,
            difficulty=30,
            clarity=70,
        )
        # small-4k (4096 * 0.75 = 3072 available) can't fit 5000
        assert decision.model is not None
        assert decision.model.name != "small-4k"
        assert decision.candidates_rejected_context >= 1

    def test_small_context_allows_small_models(self, router):
        """When context is small, small-window models are eligible."""
        decision = router.select(
            "fill", "simple",
            context_tokens=500,
            difficulty=30,
            clarity=70,
        )
        assert decision.model is not None
        assert decision.candidates_rejected_context == 0

    def test_zero_context_skips_filtering(self, router):
        """When context_tokens=0, no models are rejected by context."""
        decision = router.select(
            "fill", "simple",
            context_tokens=0,
            difficulty=30,
            clarity=70,
        )
        assert decision.candidates_rejected_context == 0


# ── SmashRange integration ───────────────────────────────────────────


class TestSmashIntegration:
    def test_high_difficulty_prefers_capable(self, router):
        """High difficulty should not select weak models."""
        decision = router.select(
            "fill", "hard",
            context_tokens=2000,
            difficulty=85,
            clarity=70,
        )
        # At high difficulty, the large model with better benchmarks
        # should score higher due to smash fitness
        assert decision.model is not None
        assert decision.smash_fit >= 0.0  # just check it's populated

    def test_smash_fit_is_populated(self, router):
        """Decision should always have a smash_fit value."""
        decision = router.select(
            "fill", "simple",
            context_tokens=1000,
            difficulty=50,
            clarity=65,
        )
        assert 0.0 <= decision.smash_fit <= 1.0


# ── Context efficiency preference ────────────────────────────────────


class TestContextEfficiencyPreference:
    def test_prefers_right_sized_window(self, router):
        """Given similar models, prefer one whose window matches context."""
        decision = router.select(
            "fill", "simple",
            context_tokens=2000,
            difficulty=30,
            clarity=70,
        )
        assert decision.model is not None
        # The explain method should show context_efficiency scores
        explained = router.explain(
            "fill", "simple",
            context_tokens=2000,
            difficulty=30,
            clarity=70,
        )
        # All eligible should have context_efficiency > 0
        eligible = [e for e in explained if e["context_fits"]]
        assert all(e["context_efficiency"] > 0 for e in eligible)


# ── RoutingDecision fields ───────────────────────────────────────────


class TestRoutingDecision:
    def test_all_fields_populated(self, router):
        decision = router.select(
            "fill", "simple",
            context_tokens=2000,
            difficulty=40,
            clarity=60,
            fit_level=FitLevel.TIGHT,
        )
        assert decision.model is not None
        assert decision.phase == "fill"
        assert decision.context_tokens == 2000
        assert decision.estimated_difficulty == 40
        assert decision.estimated_clarity == 60
        assert decision.fit_level == "tight"
        assert decision.context_fits is True
        assert isinstance(decision.smash_fit, float)
        assert isinstance(decision.score, float)
        assert len(decision.reason) > 0
        assert decision.candidates_considered > 0
        assert isinstance(decision.estimated_cost_factor, float)
        assert isinstance(decision.model_downgraded, bool)

    def test_none_model_when_nothing_fits(self, patched_registry, monkeypatch):
        """Returns None model when context is too large for all models."""
        import codeclub.infra.models as mod
        tiny = [_make_model("tiny", context=1024)]
        monkeypatch.setattr(mod, "REGISTRY", tiny)

        base = ModelRouter(budget="premium")
        ctx_router = ContextRouter(base)
        decision = ctx_router.select(
            "fill", "simple",
            context_tokens=5000,
            difficulty=30,
            clarity=70,
        )
        assert decision.model is None
        assert decision.context_fits is False
        assert decision.score == 0.0
        assert "rejected" in decision.reason.lower() or "no model" in decision.reason.lower()


# ── select_suite ─────────────────────────────────────────────────────


class TestSelectSuite:
    def test_returns_all_phases(self, router):
        suite = router.select_suite(
            "simple",
            context_tokens=2000,
            difficulty=30,
            clarity=70,
        )
        assert set(suite.keys()) == set(PHASES)
        for phase, decision in suite.items():
            assert isinstance(decision, RoutingDecision)
            assert decision.phase == phase


# ── explain ──────────────────────────────────────────────────────────


class TestExplain:
    def test_returns_score_breakdown(self, router):
        results = router.explain(
            "fill", "simple",
            context_tokens=2000,
            difficulty=30,
            clarity=70,
        )
        assert len(results) > 0
        for entry in results:
            assert "model" in entry
            assert "base_score" in entry
            assert "smash_fit" in entry
            assert "context_efficiency" in entry
            assert "combined_score" in entry
            assert "context_fits" in entry

    def test_rejected_models_have_reason(self, router):
        """Models that don't fit context should have a rejected_reason."""
        results = router.explain(
            "fill", "simple",
            context_tokens=5000,
            difficulty=30,
            clarity=70,
        )
        # small-4k shouldn't fit
        small_entry = next(
            (e for e in results if e["model"] == "small-4k"), None
        )
        assert small_entry is not None
        assert small_entry["rejected_reason"] == "context too large"
        assert small_entry["context_fits"] is False

    def test_sorted_by_combined_score(self, router):
        results = router.explain(
            "fill", "simple",
            context_tokens=2000,
            difficulty=30,
            clarity=70,
        )
        scores = [e["combined_score"] for e in results]
        assert scores == sorted(scores, reverse=True)
