"""Tests for codeclub.claude_code_mcp — Claude Code routing + compression."""

import json
import pytest
from codeclub.claude_code_mcp import (
    _tier_from_coords,
    _apply_budget_hint,
    _context_strategy,
    _compress_for_model,
    _handle_pick_model,
    _handle_classify,
    _handle_compress,
    _handle_estimate_cost,
    MODELS,
)


# ── Routing (uses same classify_and_estimate as rest of codeclub) ────────

class TestTierFromCoords:
    """Threshold mapping: difficulty × clarity → haiku / sonnet / opus."""

    def test_low_difficulty_high_clarity_haiku(self):
        assert _tier_from_coords(20, 70) == "haiku"

    def test_low_difficulty_low_clarity_escalates(self):
        assert _tier_from_coords(20, 30) == "sonnet"

    def test_mid_difficulty_sonnet(self):
        assert _tier_from_coords(50, 60) == "sonnet"

    def test_mid_difficulty_low_clarity_escalates(self):
        assert _tier_from_coords(50, 30) == "opus"

    def test_high_difficulty_opus(self):
        assert _tier_from_coords(80, 70) == "opus"

    def test_boundary_haiku_ceiling(self):
        assert _tier_from_coords(35, 50) == "haiku"
        assert _tier_from_coords(36, 50) == "sonnet"

    def test_boundary_sonnet_ceiling(self):
        assert _tier_from_coords(65, 50) == "sonnet"
        assert _tier_from_coords(66, 50) == "opus"


class TestEndToEndRouting:
    """Full pipeline: task string → classify_and_estimate → tier."""

    def _route(self, task):
        result = _handle_pick_model({"task": task})
        return json.loads(result[0].text)

    @pytest.mark.parametrize("task", [
        "add a docstring to this function",
        "rename a variable to snake_case",
        "format this file",
        "write type hints for this module",
    ])
    def test_simple_tasks_route_to_haiku(self, task):
        data = self._route(task)
        assert data["tier"] == "haiku", f"{task!r} → {data['tier']} (d={data['task_coordinates']['difficulty']})"

    @pytest.mark.parametrize("task", [
        "design a consensus protocol for distributed transaction log with Byzantine fault tolerance",
    ])
    def test_hard_tasks_route_to_opus(self, task):
        data = self._route(task)
        assert data["tier"] == "opus", f"{task!r} → {data['tier']} (d={data['task_coordinates']['difficulty']})"

    def test_difficulty_spread(self):
        """Verify the classifier produces a meaningful spread, not all d=35."""
        simple = self._route("add a docstring")["task_coordinates"]["difficulty"]
        hard = self._route("design a distributed consensus protocol with Byzantine fault tolerance")["task_coordinates"]["difficulty"]
        assert hard - simple >= 30, f"Spread too narrow: simple={simple}, hard={hard}"


class TestBudgetHint:
    def test_auto_passthrough(self):
        assert _apply_budget_hint("sonnet", "auto") == "sonnet"
        assert _apply_budget_hint("opus", None) == "opus"

    def test_force_down(self):
        assert _apply_budget_hint("opus", "haiku") == "haiku"

    def test_force_up(self):
        assert _apply_budget_hint("haiku", "opus") == "opus"

    def test_invalid_ignored(self):
        assert _apply_budget_hint("sonnet", "gpt-5") == "sonnet"


# ── Context strategy ─────────────────────────────────────────────────────

class TestContextStrategy:
    def test_small_context_full(self):
        result = _context_strategy(5000, "haiku")
        assert result["action"] == "full"

    def test_large_context_compress(self):
        result = _context_strategy(200_000, "opus")
        assert result["action"] == "compress"
        assert result["extra_cost_if_uncompressed_usd"] > 0

    def test_threshold_boundary(self):
        # Just under threshold (40k tokens * 3.5 chars/token = 140k chars)
        result = _context_strategy(139_000, "sonnet")
        assert result["action"] == "full"
        result = _context_strategy(141_000, "sonnet")
        assert result["action"] == "compress"


# ── Compression ──────────────────────────────────────────────────────────

class TestCompressForModel:
    def test_stubs_function_bodies(self):
        code = "def foo(x: int) -> str:\n    result = str(x)\n    return result\n"
        compressed = _compress_for_model(code, "code")
        assert "..." in compressed
        assert "result = str(x)" not in compressed
        assert "def foo" in compressed

    def test_no_cjk_in_output(self):
        code = "def bar() -> str | None:\n    return None\n"
        compressed = _compress_for_model(code, "code")
        assert "\u4e32" not in compressed  # 串
        assert "str | None" in compressed

    def test_preserves_signatures(self):
        code = "def baz(a: int, b: str = 'x') -> dict:\n    return {a: b}\n"
        compressed = _compress_for_model(code, "code")
        assert "def baz(a: int, b: str = 'x') -> dict:" in compressed

    def test_prose_passthrough(self):
        text = "This is just prose with no code in it."
        assert _compress_for_model(text, "prose") == text

    def test_auto_detects_code(self):
        code = "def hello():\n    print('hi')\n"
        assert "..." in _compress_for_model(code, "auto")

    def test_auto_detects_prose(self):
        text = "Just a regular sentence about nothing."
        assert _compress_for_model(text, "auto") == text


# ── Tool handlers (integration) ─────────────────────────────────────────

class TestPickModelHandler:
    def _call(self, **kwargs):
        return json.loads(_handle_pick_model(kwargs)[0].text)

    def test_returns_model_id(self):
        data = self._call(task="rename a variable")
        assert data["model_id"] in {m["id"] for m in MODELS.values()}

    def test_includes_coordinates(self):
        data = self._call(task="rename a variable")
        assert "difficulty" in data["task_coordinates"]
        assert "clarity" in data["task_coordinates"]

    def test_context_strategy_included(self):
        data = self._call(task="rename a variable", context_chars=1000)
        assert data["context_strategy"]["action"] == "full"

    def test_budget_override_noted(self):
        data = self._call(task="design a distributed system", budget="haiku")
        assert data["tier"] == "haiku"
        assert "override" in data.get("override_note", "").lower()


class TestEstimateCostHandler:
    def _call(self, **kwargs):
        return json.loads(_handle_estimate_cost(kwargs)[0].text)

    def test_returns_all_three_models(self):
        data = self._call(task="implement a feature")
        assert len(data["models"]) == 3
        tiers = {m["tier"] for m in data["models"]}
        assert tiers == {"haiku", "sonnet", "opus"}

    def test_one_recommended(self):
        data = self._call(task="implement a feature")
        recommended = [m for m in data["models"] if m["recommended"]]
        assert len(recommended) == 1


class TestCompressHandler:
    def _call(self, **kwargs):
        return json.loads(_handle_compress(kwargs)[0].text)

    def test_reports_savings(self):
        code = "def f():\n    x = 1\n    y = 2\n    return x + y\n"
        data = self._call(text=code)
        assert "savings_pct" in data
        assert data["approx_compressed_tokens"] <= data["approx_original_tokens"]

    def test_no_cjk_in_compressed_output(self):
        code = "def g() -> str | None:\n    return None\n"
        data = self._call(text=code)
        assert "\u4e32" not in data["compressed"]


class TestClassifyHandler:
    def _call(self, **kwargs):
        return json.loads(_handle_classify(kwargs)[0].text)

    def test_returns_coordinates(self):
        data = self._call(task="add a docstring")
        assert "difficulty" in data["coordinates"]
        assert "clarity" in data["coordinates"]

    def test_returns_routed_tier(self):
        data = self._call(task="add a docstring")
        assert data["routed_tier"] in {"haiku", "sonnet", "opus"}
        assert data["routed_model"] in {m["id"] for m in MODELS.values()}

    def test_returns_category(self):
        data = self._call(task="build a REST API")
        assert "category" in data
        assert "subcategory" in data

    def test_returns_profile(self):
        data = self._call(task="implement a feature")
        assert "estimated_tokens" in data.get("profile", {})
