"""
codeclub MCP server — exposes routing, classification, and compression
as tools for any MCP-compatible client (Copilot CLI, Claude Desktop, etc).

Usage (stdio transport — what Copilot CLI uses):
    python -m codeclub.mcp_server

Install in Copilot CLI:
    /mcp add codeclub -- python -m codeclub.mcp_server

Or with full path:
    /mcp add codeclub -- python /path/to/codeclub/codeclub/mcp_server.py
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

# Ensure repo root is on sys.path so tournament etc. are importable.
_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

logger = logging.getLogger("codeclub.mcp")

# ---------------------------------------------------------------------------
# Lazy imports — fail gracefully with clear messages
# ---------------------------------------------------------------------------

_tournament = None
_compressor = None


def _ensure_tournament():
    global _tournament
    if _tournament is not None:
        return _tournament
    try:
        import tournament as _t

        _tournament = _t
        return _t
    except ImportError as exc:
        raise RuntimeError(
            "tournament.py not found — run from the codeclub repo root"
        ) from exc


def _ensure_compressor():
    global _compressor
    if _compressor is not None:
        return _compressor
    try:
        from codeclub.compress.compressor import compress

        _compressor = compress
        return compress
    except ImportError as exc:
        raise RuntimeError(
            "codeclub.compress not importable — check installation"
        ) from exc


# ---------------------------------------------------------------------------
# MCP server definition
# ---------------------------------------------------------------------------

server = Server("codeclub")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="classify_task",
            description=(
                "Classify a coding/sysadmin/cloud task. Returns category, "
                "subcategory, difficulty, clarity, confidence, and suggested "
                "model routing — all without calling an LLM."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task description to classify",
                    },
                },
                "required": ["task"],
            },
        ),
        Tool(
            name="estimate_cost",
            description=(
                "Estimate tokens, cost, and wallclock time for a task. "
                "Returns a full routing plan with recommended model, "
                "alternatives, and cost comparison."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task description to estimate",
                    },
                    "difficulty": {
                        "type": "integer",
                        "description": "Override difficulty (0-100). Auto-detected if omitted.",
                    },
                    "clarity": {
                        "type": "integer",
                        "description": "Override clarity (0-100). Auto-detected if omitted.",
                    },
                },
                "required": ["task"],
            },
        ),
        Tool(
            name="compress_context",
            description=(
                "Compress code/text context to reduce tokens. Uses "
                "tree-sitter stubbing for code and structural compression "
                "for prose. Returns the compressed text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text or code to compress",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "code", "prose"],
                        "description": "Compression mode (default: auto)",
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="route_model",
            description=(
                "Pick the best model for a task given difficulty and clarity "
                "coordinates. Returns the recommended model with reasoning."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "difficulty": {
                        "type": "integer",
                        "description": "Task difficulty (0-100)",
                    },
                    "clarity": {
                        "type": "integer",
                        "description": "Task clarity (0-100)",
                    },
                },
                "required": ["difficulty", "clarity"],
            },
        ),
        Tool(
            name="list_profiles",
            description=(
                "List all available task profiles with their token/cost "
                "estimates. Useful for understanding what the system knows "
                "about different task types."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Filter by category (coding, sysadmin, cloud, debug, cross-codebase)",
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "classify_task":
            return _handle_classify(arguments)
        elif name == "estimate_cost":
            return _handle_estimate(arguments)
        elif name == "compress_context":
            return _handle_compress(arguments)
        elif name == "route_model":
            return _handle_route(arguments)
        elif name == "list_profiles":
            return _handle_list_profiles(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return [TextContent(type="text", text=f"Error: {exc}")]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _handle_classify(args: dict) -> list[TextContent]:
    t = _ensure_tournament()
    task = args["task"]
    classification = t.classify_request_adaptive(task)

    result = {
        "category": classification.category,
        "subcategory": classification.subcategory,
        "confidence": round(classification.confidence, 3),
        "confidence_tier": classification.confidence_tier,
        "suggested_profile": classification.suggested_profile,
        "signals": classification.signals,
        "needs_model_escalation": classification.needs_model_classification,
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_estimate(args: dict) -> list[TextContent]:
    t = _ensure_tournament()
    task = args["task"]

    classification, coord, profile = t.classify_and_estimate(task)

    estimate: dict = {
        "classification": {
            "category": classification.category,
            "subcategory": classification.subcategory,
            "confidence": round(classification.confidence, 3),
        },
        "coordinates": {
            "difficulty": coord.difficulty,
            "clarity": coord.clarity,
        },
    }

    if profile:
        tokens = profile.total_tokens(coord)
        estimate["profile"] = {
            "key": classification.suggested_profile or "unknown",
            "category": profile.category,
            "estimated_tokens": tokens,
            "estimated_cost_usd": round(tokens * 0.00000015, 4),
            "gather_rounds": profile.gather_rounds,
            "iterations": profile.iterations,
            "wallclock_overhead_s": profile.total_wallclock_overhead_s(),
            "estimated_wallclock_s": round(profile.total_wallclock_s(), 1),
        }

    # Override coordinates if user provided them
    d = args.get("difficulty", coord.difficulty)
    c = args.get("clarity", coord.clarity)
    if d != coord.difficulty or c != coord.clarity:
        coord = t.SmashCoord(difficulty=d, clarity=c)
        estimate["coordinates_override"] = {"difficulty": d, "clarity": c}

    # Add routing summary and reasoning
    tokens = profile.total_tokens(coord) if profile else None
    cost = round(tokens * 0.00000015, 6) if tokens else None
    wallclock = round(profile.total_wallclock_s(), 1) if profile else None
    estimate["summary"] = t.format_routing_summary(
        classification, coord,
        estimated_tokens=tokens,
        estimated_cost=cost,
    )
    estimate["reasoning"] = t.format_routing_reasoning(
        classification, coord, profile,
        estimated_tokens=tokens,
        estimated_cost=cost,
        estimated_time_s=wallclock,
    )

    return [TextContent(type="text", text=json.dumps(estimate, indent=2))]


def _handle_compress(args: dict) -> list[TextContent]:
    compress = _ensure_compressor()
    text = args["text"]
    mode = args.get("mode", "auto")

    compressed = compress(text, mode=mode)

    # Token count approximation (4 chars ≈ 1 token)
    orig_tokens = len(text) // 4
    comp_tokens = len(compressed) // 4
    savings = (1 - comp_tokens / max(orig_tokens, 1)) * 100

    result = {
        "compressed": compressed,
        "original_chars": len(text),
        "compressed_chars": len(compressed),
        "approx_original_tokens": orig_tokens,
        "approx_compressed_tokens": comp_tokens,
        "savings_pct": round(savings, 1),
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_route(args: dict) -> list[TextContent]:
    t = _ensure_tournament()
    d = args["difficulty"]
    c = args["clarity"]

    coord = t.SmashCoord(difficulty=d, clarity=c)

    # Use the routing engine to find the best model
    model = t.pick_model(coord) if hasattr(t, "pick_model") else None

    result = {"difficulty": d, "clarity": c}

    if model:
        result["recommended_model"] = str(model)
    else:
        # Fallback: provide heuristic guidance
        if d <= 20 and c >= 60:
            rec = "Small model (1.5-8B) — simple, clear task"
        elif d <= 40 and c >= 40:
            rec = "Mid-tier model (14-32B) — moderate task"
        elif d <= 60:
            rec = "Large model (70B+ or cloud) — complex task"
        else:
            rec = "Frontier model (GPT-4o, Claude Sonnet, etc) — hard task"

        if c < 40:
            rec += " ⚠️ Low clarity — consider clarifying the spec first"

        result["recommendation"] = rec

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_list_profiles(args: dict) -> list[TextContent]:
    t = _ensure_tournament()
    profiles = t.TASK_PROFILES
    category = args.get("category")

    # Default coord for token estimation
    default_coord = t.SmashCoord(difficulty=50, clarity=50)

    rows = []
    for key, prof in sorted(profiles.items()):
        if category and not key.startswith(category):
            continue
        rows.append({
            "key": key,
            "category": prof.category,
            "tokens_at_d50_c50": prof.total_tokens(default_coord),
            "gather_rounds": prof.gather_rounds,
            "iterations": prof.iterations,
            "wallclock_s": round(prof.total_wallclock_s(), 1),
        })

    result = {"count": len(rows), "profiles": rows}
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
