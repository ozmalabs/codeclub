"""
codeclub MCP server for Claude Code.

Stripped-down routing for the three-model Claude world:
  haiku   — fast, cheap, surprisingly capable
  sonnet  — balanced default
  opus    — hard problems, architecture, security

One context switch: compress if the context is large and the task doesn't
need it all; send full if the model is free-tier or the task benefits from
breadth.

Install:
    claude mcp add codeclub -- python -m codeclub.claude_code_mcp

Or from repo root:
    claude mcp add codeclub -- uv run python -m codeclub.claude_code_mcp
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

logger = logging.getLogger("codeclub.claude_code_mcp")

# ---------------------------------------------------------------------------
# Model table — the three Claude Code models
# ---------------------------------------------------------------------------

MODELS = {
    "haiku": {
        "id": "claude-haiku-4-5-20251001",
        "display": "Haiku 4.5",
        "cost_in":  0.80,   # USD / 1M input tokens
        "cost_out": 4.00,
        "context":  200_000,
        "max_difficulty": 55,   # set generously — haiku is capable
        "min_clarity":    50,   # needs reasonable spec
        "sweet_spot": "Refactors, formatting, docstrings, simple CRUD, test stubs, "
                      "short scripts, file operations, regex, config edits",
    },
    "sonnet": {
        "id": "claude-sonnet-4-6",
        "display": "Sonnet 4.6",
        "cost_in":  3.00,
        "cost_out": 15.00,
        "context":  200_000,
        "max_difficulty": 80,
        "min_clarity":    30,   # handles vague specs well
        "sweet_spot": "Feature implementation, debugging, API design, "
                      "multi-file refactors, code review, migrations, test suites",
    },
    "opus": {
        "id": "claude-opus-4-6",
        "display": "Opus 4.6",
        "cost_in":  15.00,
        "cost_out": 75.00,
        "context":  200_000,
        "max_difficulty": 100,
        "min_clarity":    0,    # handles ambiguity well
        "sweet_spot": "Hard architecture decisions, security review, distributed systems, "
                      "novel algorithms, ambiguous specs, critical path refactors",
    },
}

# Cost relative to haiku — used in pick_model response to give quick intuition
_RELATIVE_COST = {"haiku": "1×", "sonnet": "4×", "opus": "19×"}

# Context compression threshold: if estimated context exceeds this many tokens,
# recommend compressing before spawning a sub-agent on a per-token-billed model.
_COMPRESS_THRESHOLD_TOKENS = 40_000

# Chars-per-token approximation (conservative for mixed code+prose)
_CHARS_PER_TOKEN = 3.5


# ---------------------------------------------------------------------------
# Routing logic
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Routing logic — uses the same classify_and_estimate as the rest of codeclub
# ---------------------------------------------------------------------------
#
# Haiku is very capable.  Thresholds are generous toward haiku.
#   d ≤ 35 → haiku   (simple coding, formatting, docs, tests, basic features)
#   d ≤ 65 → sonnet  (multi-file refactors, debugging, moderate design)
#   d > 65 → opus    (security, distributed, architecture, ambiguous specs)
#
# Low clarity bumps up one tier: a vague spec needs more reasoning.
_HAIKU_CEILING = 35
_SONNET_CEILING = 65
_CLARITY_ESCALATION_THRESHOLD = 35


def _tier_from_coords(difficulty: int, clarity: int) -> str:
    """Map difficulty × clarity to haiku / sonnet / opus."""
    # Low clarity = vague spec, needs more reasoning → escalate one tier
    escalate = clarity < _CLARITY_ESCALATION_THRESHOLD

    if difficulty <= _HAIKU_CEILING:
        return "sonnet" if escalate else "haiku"
    if difficulty <= _SONNET_CEILING:
        return "opus" if escalate else "sonnet"
    return "opus"


def _apply_budget_hint(tier: str, budget: str | None) -> str:
    """Apply an optional budget override."""
    if not budget or budget == "auto":
        return tier
    order = ["haiku", "sonnet", "opus"]
    if budget not in order:
        return tier
    return budget


def _context_strategy(context_chars: int, tier: str) -> dict:
    """
    One binary decision: compress or send full.

    Opus/sonnet are per-token billed so compression matters above the threshold.
    Haiku is cheap enough that full context is almost always fine too, but we
    flag it if the context is very large to avoid unnecessarily slow responses.
    """
    estimated_tokens = int(context_chars / _CHARS_PER_TOKEN)
    over_threshold = estimated_tokens > _COMPRESS_THRESHOLD_TOKENS

    if not over_threshold:
        return {
            "action": "full",
            "estimated_context_tokens": estimated_tokens,
            "rationale": "Context is small — send as-is.",
        }

    # Large context: always recommend compression
    cost_per_1m = MODELS[tier]["cost_in"]
    extra_cost = round((estimated_tokens - _COMPRESS_THRESHOLD_TOKENS) / 1_000_000 * cost_per_1m, 4)
    return {
        "action": "compress",
        "estimated_context_tokens": estimated_tokens,
        "tokens_over_threshold": estimated_tokens - _COMPRESS_THRESHOLD_TOKENS,
        "extra_cost_if_uncompressed_usd": extra_cost,
        "rationale": (
            f"Context is ~{estimated_tokens:,} tokens. "
            f"Use compress_context to stub function bodies before sending. "
            f"Keeping full context costs ~${extra_cost} extra on {MODELS[tier]['display']}."
        ),
        "tip": "call compress_context with mode='code' if the context is mostly code",
    }


# ---------------------------------------------------------------------------
# Lazy imports
# ---------------------------------------------------------------------------

_tournament = None


def _ensure_tournament():
    global _tournament
    if _tournament is not None:
        return _tournament
    try:
        import tournament as t
        _tournament = t
        return t
    except ImportError as exc:
        raise RuntimeError("tournament.py not found — run from the codeclub repo root") from exc


def _compress_for_model(text: str, mode: str) -> str:
    """
    Structural compression suitable for direct model consumption.

    Uses tree-sitter stubbing + compact passes only.  Deliberately skips
    the CJK symbol-table substitutions (PYTHON_ENCODE) — those are designed
    for a paired encode/decode pipeline and produce unreadable output when
    sent to a model without the decoder context.
    """
    from codeclub.compress.tree import stub_functions, _detect_language
    from codeclub.compress.compact import compact

    _py_signals = ("def ", "class ", "import ", "return ", "async def ")
    _js_signals = ("function ", "const ", "let ", "var ", "export ", "=> {", "=> (")
    is_python = any(s in text for s in _py_signals)
    is_js = any(s in text for s in _js_signals)

    if mode == "prose" or (mode == "auto" and not is_python and not is_js):
        return text  # prose compression not yet implemented; no-op

    # Detect language from content signals
    language = "python"
    if is_js and not is_python:
        language = "javascript"
    elif is_js and is_python:
        # Ambiguous — JS arrow functions / const are a strong signal
        if "=>" in text or "require(" in text:
            language = "javascript"

    # mode == "code" or auto-detected as code
    stubbed, _ = stub_functions(text, language=language)
    return compact(stubbed, strip_sections=True, collapse_sigs=True)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("codeclub-claude-code")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="pick_model",
            description=(
                "Classify a task and pick the best Claude model (haiku / sonnet / opus). "
                "Returns the model ID, routing rationale, estimated cost, and a context "
                "strategy (compress vs full) when you provide the context size. "
                "Use this before spawning a sub-agent to avoid defaulting to the most "
                "expensive model for everything."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "What the sub-agent needs to do",
                    },
                    "context_chars": {
                        "type": "integer",
                        "description": (
                            "Approximate character count of the context you plan to send "
                            "(e.g. len(file_contents)). Used to decide whether to compress. "
                            "Omit if context is small or unknown."
                        ),
                    },
                    "budget": {
                        "type": "string",
                        "enum": ["auto", "haiku", "sonnet", "opus"],
                        "description": (
                            "Optional budget hint. 'haiku' forces the cheapest tier, "
                            "'opus' forces the strongest. 'auto' (default) picks by task."
                        ),
                    },
                },
                "required": ["task"],
            },
        ),
        Tool(
            name="compress_context",
            description=(
                "Compress code or prose to reduce token count before sending to a model. "
                "For code: replaces function bodies with '...' stubs, keeping signatures "
                "and docstrings (70–95% reduction). For prose: structural compression. "
                "Returns compressed text and savings percentage."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Code or text to compress",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "code", "prose"],
                        "description": "Compression mode (default: auto-detect)",
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="estimate_cost",
            description=(
                "Show cost estimates across all three Claude models for a task. "
                "Useful when you want to see the haiku/sonnet/opus tradeoff before deciding."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task to estimate cost for",
                    },
                },
                "required": ["task"],
            },
        ),
        Tool(
            name="run_dev_loop",
            description=(
                "Run the full codeclub dev loop: spec → generate → test → fix → review → report. "
                "Takes a natural language task description and produces working, tested, reviewed code. "
                "Uses the Anthropic API for all phases (routed by difficulty). "
                "Returns the generated code, test results, review verdict, and cost ledger."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "What to build (natural language)",
                    },
                    "context": {
                        "type": "string",
                        "description": "Existing code or stubs to build on (optional)",
                    },
                    "setup": {
                        "type": "string",
                        "description": (
                            "Router preset: anthropic (default), copilot, "
                            "openrouter_cheap, best_local_first"
                        ),
                    },
                    "max_iterations": {
                        "type": "integer",
                        "description": "Max test→fix cycles (default: 3)",
                    },
                    "stack": {
                        "type": "string",
                        "description": (
                            "Library stack hint: web-api, cli, data, library, async-service. "
                            "Auto-detected from task if omitted."
                        ),
                    },
                },
                "required": ["task"],
            },
        ),
        Tool(
            name="classify_task",
            description=(
                "Classify a task and return its difficulty, clarity, category, "
                "and suggested profile. Raw classification data — use this when "
                "you want to understand the routing decision or inspect the coordinates."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task to classify",
                    },
                },
                "required": ["task"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "pick_model":
            return _handle_pick_model(arguments)
        elif name == "compress_context":
            return _handle_compress(arguments)
        elif name == "estimate_cost":
            return _handle_estimate_cost(arguments)
        elif name == "run_dev_loop":
            return _handle_dev_loop(arguments)
        elif name == "classify_task":
            return _handle_classify(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return [TextContent(type="text", text=f"Error: {exc}")]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_pick_model(args: dict) -> list[TextContent]:
    task = args["task"]
    context_chars = args.get("context_chars")
    budget_hint = args.get("budget", "auto")

    # Classify using the same engine as the rest of codeclub
    t = _ensure_tournament()
    classification, coord, profile = t.classify_and_estimate(task)
    d, c = coord.difficulty, coord.clarity

    # Route
    auto_tier = _tier_from_coords(d, c)
    tier = _apply_budget_hint(auto_tier, budget_hint)
    model = MODELS[tier]

    # Token/cost estimate
    token_estimate = profile.total_tokens(coord) if profile else None
    cost_estimate = None
    if token_estimate:
        cost_estimate = round(
            token_estimate / 1_000_000 * (model["cost_in"] + model["cost_out"] * 0.3), 5
        )

    # Budget override note
    override_note = None
    if budget_hint and budget_hint != "auto" and budget_hint != auto_tier:
        order = ["haiku", "sonnet", "opus"]
        direction = "down" if order.index(budget_hint) < order.index(auto_tier) else "up"
        override_note = (
            f"Budget hint '{budget_hint}' overrides auto-route '{auto_tier}' "
            f"(forced {direction})"
        )

    category = getattr(classification, "category", "unknown")
    subcategory = getattr(classification, "subcategory", "")
    label = f"{category}/{subcategory}" if subcategory else category

    result: dict = {
        "model_id": model["id"],
        "tier": tier,
        "display": model["display"],
        "relative_cost": _RELATIVE_COST[tier],
        "task_coordinates": {"difficulty": d, "clarity": c},
        "classification": label,
        "rationale": (
            f"d={d} c={c} ({label}). "
            f"{'Low clarity — escalated. ' if c < _CLARITY_ESCALATION_THRESHOLD else ''}"
            f"{MODELS[tier]['display']}."
        ),
        "sweet_spot": model["sweet_spot"],
    }

    if token_estimate:
        result["estimated_tokens"] = token_estimate
        result["estimated_cost_usd"] = cost_estimate

    if override_note:
        result["override_note"] = override_note

    # Context strategy
    if context_chars is not None:
        result["context_strategy"] = _context_strategy(context_chars, tier)
    else:
        result["context_strategy"] = {
            "action": "unknown",
            "tip": "Pass context_chars=len(your_context) to get a compress/full recommendation",
        }

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_compress(args: dict) -> list[TextContent]:
    text = args["text"]
    mode = args.get("mode", "auto")

    compressed = _compress_for_model(text, mode)

    orig_tokens = int(len(text) / _CHARS_PER_TOKEN)
    comp_tokens = int(len(compressed) / _CHARS_PER_TOKEN)
    savings = round((1 - comp_tokens / max(orig_tokens, 1)) * 100, 1)

    result = {
        "compressed": compressed,
        "original_chars": len(text),
        "compressed_chars": len(compressed),
        "approx_original_tokens": orig_tokens,
        "approx_compressed_tokens": comp_tokens,
        "savings_pct": savings,
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_estimate_cost(args: dict) -> list[TextContent]:
    task = args["task"]
    t = _ensure_tournament()
    _, coord, profile = t.classify_and_estimate(task)

    auto_tier = _tier_from_coords(coord.difficulty, coord.clarity)
    token_estimate = profile.total_tokens(coord) if profile else None

    rows = []
    for tier_name, model in MODELS.items():
        if token_estimate:
            cost = round(token_estimate / 1_000_000 * (model["cost_in"] + model["cost_out"] * 0.3), 5)
        else:
            cost = None
        row = {
            "tier": tier_name,
            "model_id": model["id"],
            "relative_cost": _RELATIVE_COST[tier_name],
            "cost_in_per_1m": model["cost_in"],
            "cost_out_per_1m": model["cost_out"],
            "recommended": tier_name == auto_tier,
        }
        if cost is not None:
            row["estimated_cost_usd"] = cost
        rows.append(row)

    result = {
        "recommended_tier": auto_tier,
        "estimated_tokens": token_estimate,
        "models": rows,
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_dev_loop(args: dict) -> list[TextContent]:
    from codeclub.dev.loop import run as dev_loop_run
    from codeclub.infra.models import router_for_setup

    task = args["task"]
    context = args.get("context", "")
    setup = args.get("setup", "anthropic")
    max_iterations = args.get("max_iterations", 3)
    stack = args.get("stack")

    router = router_for_setup(setup)
    loop_result = dev_loop_run(
        task,
        context=context,
        router=router,
        max_fix_iterations=max_iterations,
        stack=stack,
        verbose=False,
    )

    # Build a structured response
    result: dict = {
        "task": task,
        "passed": loop_result.passed,
        "approved": loop_result.approved,
        "iterations": loop_result.iterations,
        "total_time_s": round(loop_result.total_time_s, 1),
        "complexity": loop_result.complexity,
        "models": {
            "map": loop_result.map_model,
            "fill": loop_result.fill_model,
            "review": loop_result.review_model,
        },
    }

    # Code
    if loop_result.final_code:
        result["code"] = loop_result.final_code

    # Test results
    if loop_result.test_results:
        last_test = loop_result.test_results[-1]
        result["tests"] = {
            "passed": last_test.passed,
            "output": last_test.stdout[:2000] if hasattr(last_test, "stdout") else "",
            "test_code": loop_result.tests[:2000] if loop_result.tests else "",
        }

    # Review
    if loop_result.review:
        result["review"] = {
            "approved": loop_result.review.approved,
            "summary": loop_result.review.summary if hasattr(loop_result.review, "summary") else "",
            "issues": loop_result.review.issues if hasattr(loop_result.review, "issues") else [],
        }

    # Report
    if loop_result.report:
        result["report"] = loop_result.report

    # Ledger
    if loop_result.ledger:
        result["ledger"] = loop_result.ledger.summary()

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_classify(args: dict) -> list[TextContent]:
    t = _ensure_tournament()
    task = args["task"]
    classification, coord, profile = t.classify_and_estimate(task)

    tier = _tier_from_coords(coord.difficulty, coord.clarity)

    result = {
        "category": classification.category,
        "subcategory": classification.subcategory,
        "confidence": round(classification.confidence, 3),
        "suggested_profile": classification.suggested_profile,
        "coordinates": {"difficulty": coord.difficulty, "clarity": coord.clarity},
        "routed_tier": tier,
        "routed_model": MODELS[tier]["id"],
    }

    if profile:
        result["profile"] = {
            "estimated_tokens": profile.total_tokens(coord),
            "gather_rounds": profile.gather_rounds,
            "iterations": profile.iterations,
        }

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main():
    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_options)


def main():
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
