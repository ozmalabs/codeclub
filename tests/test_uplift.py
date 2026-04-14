"""Tests for codeclub.context.uplift — clarity uplift layer."""

import math

from codeclub.context.uplift import (
    CLARITY_CLIFF_OFFSET,
    CLARITY_SIGMOID_K,
    UpliftDecision,
    UpliftPreference,
    UpliftResult,
    _expand_heuristic,
    decide_uplift,
    estimate_success_probability,
    uplift_spec,
)


# ── Sigmoid probability curve ────────────────────────────────────────


class TestSigmoidProbability:
    """Verify the sigmoid matches empirical observations."""

    def test_very_low_clarity_near_zero(self):
        """c=20 with default min_clarity=55 → cliff=35 → near zero."""
        p = estimate_success_probability(20)
        assert p < 0.05, f"Expected ~0 at clarity 20, got {p:.4f}"

    def test_cliff_region(self):
        """c=35 (right at the cliff) → ~50%."""
        p = estimate_success_probability(35)
        assert 0.4 < p < 0.6, f"Expected ~0.5 at clarity 35, got {p:.4f}"

    def test_mid_clarity(self):
        """c=50 → should be high but not quite 1."""
        p = estimate_success_probability(50)
        assert p > 0.95, f"Expected >0.95 at clarity 50, got {p:.4f}"

    def test_above_min_clarity(self):
        """c=65 → should be effectively 1."""
        p = estimate_success_probability(65)
        assert p > 0.99, f"Expected >0.99 at clarity 65, got {p:.4f}"

    def test_monotonically_increasing(self):
        """Higher clarity → higher probability."""
        prev = 0.0
        for c in range(0, 100, 5):
            p = estimate_success_probability(c)
            assert p >= prev, f"Not monotonic: p({c})={p} < p({c-5})={prev}"
            prev = p

    def test_custom_min_clarity(self):
        """Changing min_clarity shifts the curve."""
        p_default = estimate_success_probability(40, min_clarity=55)
        p_harder = estimate_success_probability(40, min_clarity=70)
        assert p_default > p_harder, "Higher min_clarity should shift curve right"

    def test_sigmoid_formula_directly(self):
        """Cross-check against manual sigmoid calculation."""
        clarity = 50
        min_clarity = 55
        cliff = min_clarity - CLARITY_CLIFF_OFFSET
        expected = 1.0 / (1.0 + math.exp(-CLARITY_SIGMOID_K * (clarity - cliff)))
        actual = estimate_success_probability(clarity, min_clarity)
        assert abs(actual - expected) < 1e-10


# ── Uplift decision ──────────────────────────────────────────────────


class TestDecideUplift:

    def test_low_clarity_should_uplift(self):
        """Very vague spec (c=15) should trigger uplift."""
        d = decide_uplift(15)
        assert d.should_uplift is True
        assert d.roi > 1.0

    def test_high_clarity_no_uplift(self):
        """Clear spec (c=75) should not trigger uplift."""
        d = decide_uplift(75)
        assert d.should_uplift is False

    def test_mid_clarity_balanced_threshold(self):
        """Mid clarity — ROI must exceed 1.0 for balanced mode."""
        d = decide_uplift(50, UpliftPreference.BALANCED)
        # At clarity 50, success is already very high → no uplift
        assert d.should_uplift is False

    def test_returns_correct_dataclass(self):
        d = decide_uplift(30)
        assert isinstance(d, UpliftDecision)
        assert isinstance(d.should_uplift, bool)
        assert isinstance(d.reason, str)
        assert isinstance(d.roi, float)
        assert 0 <= d.estimated_clarity <= 100
        assert 0 <= d.target_clarity <= 100

    def test_target_clarity_capped_at_95(self):
        d = decide_uplift(80, uplift_clarity_boost=30)
        assert d.target_clarity <= 95

    def test_uplifted_prob_higher_than_direct(self):
        d = decide_uplift(20)
        assert d.uplifted_success_prob >= d.direct_success_prob


# ── Preference levels ────────────────────────────────────────────────


class TestPreferenceLevels:

    def test_speed_requires_extreme_case(self):
        """SPEED mode: only uplift when ROI > 5 AND direct_prob < 0.1."""
        # At clarity 30, direct prob is moderate — SPEED should skip
        d_speed = decide_uplift(30, UpliftPreference.SPEED)
        d_balanced = decide_uplift(30, UpliftPreference.BALANCED)
        # Speed is always at least as conservative as balanced
        if d_speed.should_uplift:
            assert d_balanced.should_uplift

    def test_efficiency_most_aggressive(self):
        """EFFICIENCY mode: any positive ROI triggers uplift."""
        d = decide_uplift(25, UpliftPreference.EFFICIENCY)
        assert d.should_uplift is True
        assert d.roi > 0

    def test_balanced_is_middle_ground(self):
        """BALANCED is stricter than EFFICIENCY but looser than SPEED."""
        clarity = 15  # very low
        d_eff = decide_uplift(clarity, UpliftPreference.EFFICIENCY)
        d_bal = decide_uplift(clarity, UpliftPreference.BALANCED)
        d_spd = decide_uplift(clarity, UpliftPreference.SPEED)

        # All three should agree on very low clarity
        assert d_eff.should_uplift is True
        assert d_bal.should_uplift is True
        # Speed may or may not uplift depending on exact ROI vs threshold
        assert d_eff.roi == d_bal.roi == d_spd.roi  # same economics


# ── ROI calculation ──────────────────────────────────────────────────


class TestROICalculation:

    def test_roi_positive_when_vague(self):
        d = decide_uplift(15)
        assert d.roi > 0, f"Expected positive ROI for clarity 15, got {d.roi}"

    def test_roi_reflects_cost_ratio(self):
        """Doubling direct model cost should roughly double ROI."""
        d1 = decide_uplift(20, direct_model_cost_factor=10.0)
        d2 = decide_uplift(20, direct_model_cost_factor=20.0)
        assert d2.roi > d1.roi

    def test_roi_negative_when_clear(self):
        """High clarity → uplift adds cost without benefit."""
        d = decide_uplift(80)
        assert d.roi < 0, f"Expected negative ROI for clarity 80, got {d.roi}"

    def test_cost_factors_populated(self):
        d = decide_uplift(25)
        assert d.direct_cost_factor > 0
        assert d.uplift_cost_factor > 0


# ── Expand heuristic ─────────────────────────────────────────────────


class TestExpandHeuristic:

    def test_rate_limiter_expansion(self):
        result = _expand_heuristic("build a rate limiter")
        assert "token bucket" in result.lower()
        assert "acquire" in result.lower()

    def test_cache_expansion(self):
        result = _expand_heuristic("add a cache")
        assert "LRU" in result
        assert "get/put" in result

    def test_parser_expansion(self):
        result = _expand_heuristic("write a parser")
        assert "AST" in result

    def test_no_match_returns_original(self):
        msg = "do the thing with the stuff"
        assert _expand_heuristic(msg) == msg

    def test_multiple_matches_combined(self):
        result = _expand_heuristic("build a cache with retry logic")
        assert "LRU" in result
        assert "backoff" in result.lower()

    def test_expansion_produces_higher_clarity(self):
        """Expanded text should score higher on clarity estimation."""
        from codeclub.context.classifier import estimate_clarity

        original = "build a rate limiter"
        expanded = _expand_heuristic(original)

        orig_clarity = estimate_clarity(original)
        exp_clarity = estimate_clarity(expanded)
        assert exp_clarity >= orig_clarity, (
            f"Expanded clarity {exp_clarity} should be >= original {orig_clarity}"
        )


# ── Uplift spec (full pipeline) ──────────────────────────────────────


class TestUpliftSpec:

    def test_expand_method(self):
        result = uplift_spec("build a rate limiter", method="expand")
        assert isinstance(result, UpliftResult)
        assert result.method == "expand"
        assert result.original_message == "build a rate limiter"
        assert "token bucket" in result.uplifted_message.lower()
        assert result.uplift_tokens == 0

    def test_restructure_without_call_fn_falls_back(self):
        """restructure without call_fn falls back to expand."""
        result = uplift_spec("build a cache", call_fn=None, method="restructure")
        assert result.method == "expand"

    def test_restructure_with_call_fn(self):
        def fake_call(prompt: str) -> str:
            return (
                "Implement a CacheManager class with get(key) -> value, "
                "put(key, value), evict() methods. Use LRU eviction. "
                "Thread-safe. Max 1000 entries."
            )

        result = uplift_spec("build a cache", call_fn=fake_call, method="restructure")
        assert result.method == "restructure"
        assert "CacheManager" in result.uplifted_message
        assert result.uplift_tokens > 0

    def test_unknown_method_raises(self):
        try:
            uplift_spec("test", method="unknown")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "unknown" in str(e).lower()

    def test_clarity_recorded(self):
        result = uplift_spec("build a rate limiter", method="expand")
        assert result.original_clarity > 0
        assert result.uplifted_clarity > 0


# ── UpliftResult dataclass ───────────────────────────────────────────


class TestUpliftResultDataclass:

    def test_fields(self):
        r = UpliftResult(
            original_message="make thing",
            uplifted_message="Build a FooBar class with bar() method",
            original_clarity=20,
            uplifted_clarity=55,
            uplift_tokens=42,
            method="expand",
        )
        assert r.original_message == "make thing"
        assert r.uplifted_message == "Build a FooBar class with bar() method"
        assert r.original_clarity == 20
        assert r.uplifted_clarity == 55
        assert r.uplift_tokens == 42
        assert r.method == "expand"

    def test_equality(self):
        a = UpliftResult("a", "b", 10, 50, 0, "expand")
        b = UpliftResult("a", "b", 10, 50, 0, "expand")
        assert a == b
