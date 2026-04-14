"""
adaptive.py — Adaptive fit precision tracking.

Learns the optimal fit level per intent category from observed outcomes.
When tight fits cause errors, padding is increased. When generous fits
always succeed, padding can be decreased.

The adaptation is gradual and bounded — it nudges the effective padding
up or down, not the fit level itself. The user's chosen fit level is
respected as the baseline; adaptation adjusts within that level's range.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("codeclub.adaptive")


@dataclass
class FitOutcome:
    """Record of one request's fit performance."""
    intent: str                    # classified intent
    fit_level: str                 # fit level used (tight, balanced, etc.)
    context_tokens: int            # assembled context size
    budget_tokens: int             # budget we were working within
    success: bool                  # did the model succeed?
    error_type: str | None = None  # "context_insufficient", "quality_low", None
    timestamp: float = field(default_factory=time.time)


class AdaptiveFitTracker:
    """
    Tracks fit outcomes and computes adjusted padding per intent.

    Maintains a rolling window of outcomes per intent. If failure rate
    at a given fit level exceeds a threshold, increases padding for
    that intent. If success rate is very high, suggests tighter fit.

    The tracker persists state to a JSON file so it survives restarts.

    Usage:
        tracker = AdaptiveFitTracker()

        # After each request:
        tracker.record(FitOutcome(
            intent="debug", fit_level="tight",
            context_tokens=2000, budget_tokens=8000,
            success=False, error_type="context_insufficient",
        ))

        # Before assembling context:
        adjustment = tracker.get_adjustment("debug", "tight")
        effective_padding = base_padding * (1.0 + adjustment)
    """

    def __init__(
        self,
        state_path: str | Path = "adaptive_fit.json",
        window_size: int = 20,
        increase_threshold: float = 0.3,    # if >30% failures, increase padding
        decrease_threshold: float = 0.95,   # if >95% success, can decrease
        max_adjustment: float = 0.5,        # max ±50% padding adjustment
        adjustment_step: float = 0.05,      # each failure/success moves by 5%
    ):
        self.state_path = Path(state_path)
        self.window_size = window_size
        self.increase_threshold = increase_threshold
        self.decrease_threshold = decrease_threshold
        self.max_adjustment = max_adjustment
        self.adjustment_step = adjustment_step

        # State: {intent: {fit_level: {"adjustment": float, "outcomes": [...]}}}
        self._state: dict = {}
        self._load()

    def record(self, outcome: FitOutcome) -> None:
        """Record a fit outcome and update adjustments."""
        key = outcome.intent
        level = outcome.fit_level

        if key not in self._state:
            self._state[key] = {}
        if level not in self._state[key]:
            self._state[key][level] = {"adjustment": 0.0, "outcomes": []}

        entry = self._state[key][level]
        entry["outcomes"].append({
            "success": outcome.success,
            "error_type": outcome.error_type,
            "tokens": outcome.context_tokens,
            "budget": outcome.budget_tokens,
            "ts": outcome.timestamp,
        })

        # Keep only the rolling window
        entry["outcomes"] = entry["outcomes"][-self.window_size:]

        # Recompute adjustment
        outcomes = entry["outcomes"]
        if len(outcomes) >= 3:  # need minimum data
            failure_rate = sum(1 for o in outcomes if not o["success"]) / len(outcomes)
            success_rate = 1.0 - failure_rate

            current = entry["adjustment"]

            if failure_rate > self.increase_threshold:
                # Too many failures — increase padding
                entry["adjustment"] = min(
                    current + self.adjustment_step,
                    self.max_adjustment,
                )
                logger.info(
                    "Adaptive: %s/%s padding increased to %+.0f%% (fail_rate=%.0f%%)",
                    key, level, entry["adjustment"] * 100, failure_rate * 100,
                )
            elif success_rate >= self.decrease_threshold and current > 0:
                # Very high success — can tighten
                entry["adjustment"] = max(
                    current - self.adjustment_step,
                    -self.max_adjustment,
                )
                logger.info(
                    "Adaptive: %s/%s padding decreased to %+.0f%% (success_rate=%.0f%%)",
                    key, level, entry["adjustment"] * 100, success_rate * 100,
                )

        self._save()

    def get_adjustment(self, intent: str, fit_level: str) -> float:
        """
        Get the padding adjustment for an intent/fit_level pair.

        Returns a multiplier adjustment: -0.5 to +0.5.
        The caller applies it as: effective_padding = base_padding * (1.0 + adjustment)

        For example, if base padding for "balanced" is 0.25 and adjustment is +0.10,
        effective padding becomes 0.25 * 1.10 = 0.275.
        """
        if intent in self._state and fit_level in self._state[intent]:
            return self._state[intent][fit_level].get("adjustment", 0.0)
        return 0.0

    def get_stats(self) -> dict:
        """Summary of all tracked intents and their adjustments."""
        result = {}
        for intent, levels in self._state.items():
            result[intent] = {}
            for level, data in levels.items():
                outcomes = data.get("outcomes", [])
                n = len(outcomes)
                successes = sum(1 for o in outcomes if o["success"])
                result[intent][level] = {
                    "adjustment": data.get("adjustment", 0.0),
                    "total": n,
                    "success_rate": successes / n if n else 0,
                    "last_outcome": outcomes[-1] if outcomes else None,
                }
        return result

    def reset(self) -> None:
        """Clear all adaptive state."""
        self._state = {}
        self._save()

    def _load(self) -> None:
        """Load state from disk."""
        if self.state_path.exists():
            try:
                self._state = json.loads(self.state_path.read_text())
            except (json.JSONDecodeError, OSError):
                logger.warning("Failed to load adaptive state, starting fresh")
                self._state = {}

    def _save(self) -> None:
        """Persist state to disk."""
        try:
            self.state_path.write_text(json.dumps(self._state, indent=2))
        except OSError:
            logger.warning("Failed to save adaptive state")
