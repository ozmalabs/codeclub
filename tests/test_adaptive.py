"""Tests for adaptive fit precision tracking."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeclub.context.adaptive import AdaptiveFitTracker, FitOutcome


@pytest.fixture()
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "adaptive_fit.json"


@pytest.fixture()
def tracker(state_path: Path) -> AdaptiveFitTracker:
    return AdaptiveFitTracker(state_path=state_path)


def _outcome(
    intent: str = "debug",
    fit_level: str = "tight",
    success: bool = True,
    error_type: str | None = None,
    context_tokens: int = 2000,
    budget_tokens: int = 8000,
) -> FitOutcome:
    """Helper to build FitOutcome with sensible defaults."""
    return FitOutcome(
        intent=intent,
        fit_level=fit_level,
        context_tokens=context_tokens,
        budget_tokens=budget_tokens,
        success=success,
        error_type=error_type,
    )


class TestRecordAndAdjustment:
    """Recording outcomes updates adjustments correctly."""

    def test_unknown_intent_returns_zero(self, tracker: AdaptiveFitTracker) -> None:
        assert tracker.get_adjustment("nonexistent", "tight") == 0.0

    def test_unknown_fit_level_returns_zero(self, tracker: AdaptiveFitTracker) -> None:
        tracker.record(_outcome(intent="debug", fit_level="tight"))
        assert tracker.get_adjustment("debug", "balanced") == 0.0

    def test_fewer_than_three_outcomes_no_adjustment(
        self, tracker: AdaptiveFitTracker
    ) -> None:
        """Minimum 3 outcomes required before adjustment changes."""
        tracker.record(_outcome(success=False))
        tracker.record(_outcome(success=False))
        assert tracker.get_adjustment("debug", "tight") == 0.0

    def test_failure_rate_exceeding_threshold_increases_padding(
        self, state_path: Path
    ) -> None:
        tracker = AdaptiveFitTracker(
            state_path=state_path,
            increase_threshold=0.3,
            adjustment_step=0.05,
        )
        # 2 failures + 1 success = 66% failure rate > 30%
        tracker.record(_outcome(success=False))
        tracker.record(_outcome(success=False))
        tracker.record(_outcome(success=True))
        assert tracker.get_adjustment("debug", "tight") == pytest.approx(0.05)

    def test_high_success_rate_decreases_padding(self, state_path: Path) -> None:
        tracker = AdaptiveFitTracker(
            state_path=state_path,
            decrease_threshold=0.95,
            adjustment_step=0.05,
        )
        # Prime with a positive adjustment first
        tracker._state["debug"] = {
            "tight": {"adjustment": 0.10, "outcomes": []},
        }
        # 20 successes → 100% success rate > 95%
        for _ in range(20):
            tracker.record(_outcome(success=True))
        assert tracker.get_adjustment("debug", "tight") < 0.10

    def test_adjustment_bounded_by_max(self, state_path: Path) -> None:
        tracker = AdaptiveFitTracker(
            state_path=state_path,
            increase_threshold=0.3,
            adjustment_step=0.1,
            max_adjustment=0.5,
        )
        # Record many failures to push adjustment to the limit
        for _ in range(30):
            tracker.record(_outcome(success=False))
        assert tracker.get_adjustment("debug", "tight") <= 0.5

    def test_decrease_bounded_by_negative_max(self, state_path: Path) -> None:
        tracker = AdaptiveFitTracker(
            state_path=state_path,
            decrease_threshold=0.95,
            adjustment_step=0.1,
            max_adjustment=0.5,
        )
        # Start with positive adjustment, record many successes
        tracker._state["debug"] = {
            "tight": {"adjustment": 0.5, "outcomes": []},
        }
        for _ in range(50):
            tracker.record(_outcome(success=True))
        assert tracker.get_adjustment("debug", "tight") >= -0.5


class TestRollingWindow:
    """Rolling window keeps only latest N outcomes."""

    def test_window_trims_old_outcomes(self, state_path: Path) -> None:
        window = 5
        tracker = AdaptiveFitTracker(state_path=state_path, window_size=window)
        for _ in range(10):
            tracker.record(_outcome())
        outcomes = tracker._state["debug"]["tight"]["outcomes"]
        assert len(outcomes) == window

    def test_old_failures_fall_out_of_window(self, state_path: Path) -> None:
        """Old failures that leave the window stop affecting adjustment."""
        tracker = AdaptiveFitTracker(
            state_path=state_path,
            window_size=5,
            increase_threshold=0.3,
            decrease_threshold=0.95,
            adjustment_step=0.05,
        )
        # 3 failures → triggers increase
        for _ in range(3):
            tracker.record(_outcome(success=False))
        assert tracker.get_adjustment("debug", "tight") > 0.0

        # 5 successes push all failures out; adjustment still rose during
        # the transition (failures still in window), but once the window
        # is pure successes the decrease branch fires.
        for _ in range(5):
            tracker.record(_outcome(success=True))
        peak = tracker.get_adjustment("debug", "tight")

        # One more success — window is 100% success, decrease triggers
        tracker.record(_outcome(success=True))
        assert tracker.get_adjustment("debug", "tight") < peak


class TestPersistence:
    """State persists across tracker instances."""

    def test_state_survives_restart(self, state_path: Path) -> None:
        t1 = AdaptiveFitTracker(
            state_path=state_path,
            increase_threshold=0.3,
            adjustment_step=0.05,
        )
        for _ in range(3):
            t1.record(_outcome(success=False))
        saved_adj = t1.get_adjustment("debug", "tight")

        t2 = AdaptiveFitTracker(state_path=state_path)
        assert t2.get_adjustment("debug", "tight") == pytest.approx(saved_adj)

    def test_state_file_is_valid_json(self, state_path: Path) -> None:
        tracker = AdaptiveFitTracker(state_path=state_path)
        tracker.record(_outcome())
        data = json.loads(state_path.read_text())
        assert "debug" in data

    def test_corrupt_file_starts_fresh(self, state_path: Path) -> None:
        state_path.write_text("{bad json!!!")
        tracker = AdaptiveFitTracker(state_path=state_path)
        assert tracker._state == {}


class TestReset:
    """reset() clears all state."""

    def test_reset_clears_adjustments(self, tracker: AdaptiveFitTracker) -> None:
        for _ in range(5):
            tracker.record(_outcome(success=False))
        tracker.reset()
        assert tracker.get_adjustment("debug", "tight") == 0.0

    def test_reset_clears_persisted_state(
        self, tracker: AdaptiveFitTracker, state_path: Path
    ) -> None:
        tracker.record(_outcome())
        tracker.reset()
        data = json.loads(state_path.read_text())
        assert data == {}


class TestGetStats:
    """get_stats returns correct summary."""

    def test_stats_empty_tracker(self, tracker: AdaptiveFitTracker) -> None:
        assert tracker.get_stats() == {}

    def test_stats_reflect_recorded_outcomes(
        self, tracker: AdaptiveFitTracker
    ) -> None:
        tracker.record(_outcome(success=True))
        tracker.record(_outcome(success=False, error_type="context_insufficient"))
        stats = tracker.get_stats()
        entry = stats["debug"]["tight"]
        assert entry["total"] == 2
        assert entry["success_rate"] == pytest.approx(0.5)
        assert entry["last_outcome"]["error_type"] == "context_insufficient"

    def test_stats_includes_adjustment(self, state_path: Path) -> None:
        tracker = AdaptiveFitTracker(
            state_path=state_path,
            increase_threshold=0.3,
            adjustment_step=0.05,
        )
        for _ in range(3):
            tracker.record(_outcome(success=False))
        stats = tracker.get_stats()
        assert stats["debug"]["tight"]["adjustment"] == pytest.approx(0.05)


class TestIndependentTracking:
    """Multiple intents and fit levels are tracked independently."""

    def test_different_intents_independent(self, state_path: Path) -> None:
        tracker = AdaptiveFitTracker(
            state_path=state_path,
            increase_threshold=0.3,
            adjustment_step=0.05,
        )
        # Failures for debug
        for _ in range(3):
            tracker.record(_outcome(intent="debug", success=False))
        # Successes for continue
        for _ in range(3):
            tracker.record(_outcome(intent="continue", success=True))

        assert tracker.get_adjustment("debug", "tight") > 0.0
        assert tracker.get_adjustment("continue", "tight") == 0.0

    def test_different_fit_levels_independent(self, state_path: Path) -> None:
        tracker = AdaptiveFitTracker(
            state_path=state_path,
            increase_threshold=0.3,
            adjustment_step=0.05,
        )
        # Failures for tight
        for _ in range(3):
            tracker.record(
                _outcome(intent="debug", fit_level="tight", success=False)
            )
        # Successes for balanced
        for _ in range(3):
            tracker.record(
                _outcome(intent="debug", fit_level="balanced", success=True)
            )

        assert tracker.get_adjustment("debug", "tight") > 0.0
        assert tracker.get_adjustment("debug", "balanced") == 0.0
