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
_models_mod = None

# Session state: client-seeded models.  Empty until set_available_models is
# called.  Each entry is a ModelSpec (matched from registry or synthesised).
_seeded_models: list = []       # list[ModelSpec]
_seeded_model_ids: set = set()  # quick lookup


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


def _ensure_models():
    global _models_mod
    if _models_mod is not None:
        return _models_mod
    from codeclub.infra import models as _m
    _models_mod = _m
    return _m


# ---------------------------------------------------------------------------
# Model matching — fuzzy match client IDs against the known registry
# ---------------------------------------------------------------------------

def _normalise_id(raw: str) -> str:
    """Normalise a model ID for fuzzy matching: lowercase, dots→dashes."""
    return raw.lower().strip().replace(".", "-").replace("_", "-")


def _match_registry(client_id: str, registry: list) -> "ModelSpec | None":
    """Try to find a registry ModelSpec matching a client-provided ID."""
    norm = _normalise_id(client_id)
    # Exact normalised match
    for m in registry:
        if _normalise_id(m.id) == norm:
            return m
    # Substring match (e.g. client sends "claude-opus-4.6", registry has
    # "claude-opus-4-6")
    for m in registry:
        if norm in _normalise_id(m.id) or _normalise_id(m.id) in norm:
            return m
    # Name match
    for m in registry:
        if norm in _normalise_id(m.name) or _normalise_id(m.name) in norm:
            return m
    return None


def _synthesise_model(client_id: str, meta: dict) -> "ModelSpec":
    """
    Create a ModelSpec from client-provided metadata for an unknown model.

    The client might only send {"id": "some-model"} or might include
    cost_in, cost_out, context, etc.  We fill gaps with heuristics based
    on the model name.
    """
    m = _ensure_models()

    name = meta.get("name", client_id)
    norm = _normalise_id(client_id)

    # Heuristic tier from name
    if any(k in norm for k in ("opus", "o1", "o3", "gpt-5")):
        tier, complexity = "premium", "expert"
        default_cost_in, default_cost_out = 5.0, 25.0
    elif any(k in norm for k in ("sonnet", "gpt-4", "gpt-5-mini")):
        tier, complexity = "medium", "expert"
        default_cost_in, default_cost_out = 3.0, 15.0
    elif any(k in norm for k in ("haiku", "gpt-4-1", "mini", "flash")):
        tier, complexity = "cheap", "complex"
        default_cost_in, default_cost_out = 0.4, 1.6
    else:
        tier, complexity = "medium", "moderate"
        default_cost_in, default_cost_out = 1.0, 5.0

    # Detect provider from name patterns
    if any(k in norm for k in ("claude", "sonnet", "opus", "haiku")):
        provider, family = "anthropic", "claude"
    elif any(k in norm for k in ("gpt", "o1", "o3", "codex")):
        provider, family = "openai", "gpt"
    elif "gemini" in norm or "gemma" in norm:
        provider, family = "google", "gemma"
    else:
        provider, family = "unknown", "unknown"

    return m.ModelSpec(
        id=client_id,
        name=name,
        provider=meta.get("provider", provider),
        family=meta.get("family", family),
        cost_in=meta.get("cost_in", default_cost_in),
        cost_out=meta.get("cost_out", default_cost_out),
        context=meta.get("context", 200_000),
        swe_bench=meta.get("swe_bench"),
        human_eval=meta.get("human_eval"),
        local=False,
        phases=frozenset(meta.get("phases", m.PHASES)),
        max_complexity=meta.get("max_complexity", complexity),
        tags=frozenset(meta.get("tags", {"cloud", tier, "client-seeded"})),
    )


def _build_seeded_router() -> "ModelRouter | None":
    """
    Build a ModelRouter constrained to client-seeded models.

    Temporarily injects seeded models into the registry so the router's
    standard select() logic works, then restores the original.
    """
    if not _seeded_models:
        return None

    m = _ensure_models()
    original = m.REGISTRY[:]
    try:
        # Replace registry with seeded set only
        m.REGISTRY.clear()
        m.REGISTRY.extend(_seeded_models)
        providers = {model.provider for model in _seeded_models}
        router = m.ModelRouter(
            available_providers=providers,
            budget="premium",   # don't filter by cost — client chose these
            prefer_local=False,
        )
        return router
    finally:
        m.REGISTRY.clear()
        m.REGISTRY.extend(original)


# ---------------------------------------------------------------------------
# MCP server definition
# ---------------------------------------------------------------------------

server = Server("codeclub")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="set_available_models",
            description=(
                "Seed the routing engine with the models YOUR client can "
                "actually use. Call this before route_model so recommendations "
                "are executable. Accepts a list of model IDs with optional "
                "metadata (cost, context window). Models are matched against "
                "the known registry; unknowns get heuristic specs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "models": {
                        "type": "array",
                        "description": (
                            "List of available models. Each item is either a "
                            "string (model ID) or an object with id + optional "
                            "fields: name, cost_in, cost_out, context, provider, "
                            "max_complexity, phases."
                        ),
                        "items": {
                            "oneOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "name": {"type": "string"},
                                        "cost_in": {"type": "number", "description": "USD per 1M input tokens"},
                                        "cost_out": {"type": "number", "description": "USD per 1M output tokens"},
                                        "context": {"type": "integer", "description": "Context window size"},
                                        "provider": {"type": "string"},
                                        "max_complexity": {"type": "string", "enum": ["trivial", "simple", "moderate", "complex", "expert"]},
                                    },
                                    "required": ["id"],
                                },
                            ],
                        },
                    },
                },
                "required": ["models"],
            },
        ),
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
                "coordinates. Returns per-phase model assignments (spec, map, "
                "fill, review, etc) with costs. If set_available_models was "
                "called, picks ONLY from those models. Otherwise uses the "
                "full registry with an optional setup preset."
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
                    "setup": {
                        "type": "string",
                        "description": (
                            "Router preset: local_only, local_b580, "
                            "openrouter_free, openrouter_cheap, anthropic, "
                            "copilot, github, best_local_first (default)"
                        ),
                    },
                },
                "required": ["difficulty", "clarity"],
            },
        ),
        Tool(
            name="list_models",
            description=(
                "List all available models for a given setup/preset. "
                "Shows per-phase assignments at each complexity level "
                "with costs. Useful for understanding what models will "
                "be used and optimising for cost."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "setup": {
                        "type": "string",
                        "description": (
                            "Router preset: local_only, local_b580, "
                            "openrouter_free, openrouter_cheap, anthropic, "
                            "copilot, github, best_local_first (default)"
                        ),
                    },
                },
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
        if name == "set_available_models":
            return _handle_set_models(arguments)
        elif name == "classify_task":
            return _handle_classify(arguments)
        elif name == "estimate_cost":
            return _handle_estimate(arguments)
        elif name == "compress_context":
            return _handle_compress(arguments)
        elif name == "route_model":
            return _handle_route(arguments)
        elif name == "list_models":
            return _handle_list_models(arguments)
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
        # Use seeded models for cost if available
        if _seeded_models:
            m = _ensure_models()
            cheapest = min(_seeded_models, key=lambda x: x.cost_in)
            costliest = max(_seeded_models, key=lambda x: x.cost_in)
            avg_rate = (cheapest.cost_in + costliest.cost_in) / 2 / 1_000_000
            cost_usd = round(tokens * avg_rate, 4)
            cost_note = f"based on seeded models (cheapest: {cheapest.id}, costliest: {costliest.id})"
        else:
            cost_usd = round(tokens * 0.00000015, 4)
            cost_note = "default rate ($0.15/1M tokens)"

        estimate["profile"] = {
            "key": classification.suggested_profile or "unknown",
            "category": profile.category,
            "estimated_tokens": tokens,
            "estimated_cost_usd": cost_usd,
            "cost_note": cost_note,
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


def _handle_set_models(args: dict) -> list[TextContent]:
    """Seed the engine with the client's available models."""
    global _seeded_models, _seeded_model_ids
    m = _ensure_models()
    raw_list = args["models"]

    matched, synthesised, failed = [], [], []
    new_models = []
    new_ids = set()

    for item in raw_list:
        if isinstance(item, str):
            client_id, meta = item, {}
        else:
            client_id = item["id"]
            meta = {k: v for k, v in item.items() if k != "id"}

        # Try matching against known registry
        spec = _match_registry(client_id, m.REGISTRY)
        if spec:
            matched.append({"client_id": client_id, "matched": spec.id,
                            "name": spec.name, "cost_in": spec.cost_in,
                            "cost_out": spec.cost_out})
            new_models.append(spec)
            new_ids.add(spec.id)
        else:
            # Synthesise from metadata + heuristics
            try:
                spec = _synthesise_model(client_id, meta)
                synthesised.append({"client_id": client_id, "name": spec.name,
                                    "cost_in": spec.cost_in,
                                    "cost_out": spec.cost_out,
                                    "tier": spec.max_complexity})
                new_models.append(spec)
                new_ids.add(spec.id)
            except Exception as e:
                failed.append({"client_id": client_id, "error": str(e)})

    _seeded_models = new_models
    _seeded_model_ids = new_ids

    result = {
        "seeded": len(new_models),
        "matched_from_registry": matched,
        "synthesised": synthesised,
    }
    if failed:
        result["failed"] = failed

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_route(args: dict) -> list[TextContent]:
    m = _ensure_models()
    t = _ensure_tournament()
    d = args["difficulty"]
    c = args["clarity"]
    setup = args.get("setup", "best_local_first")

    # Determine complexity from difficulty
    complexity = m.estimate_complexity(
        f"difficulty-{d}"  # estimate_complexity expects a task string
    ) if hasattr(m, "estimate_complexity") else "moderate"
    # Direct mapping is more reliable
    if d <= 15:
        complexity = "trivial"
    elif d <= 30:
        complexity = "simple"
    elif d <= 50:
        complexity = "moderate"
    elif d <= 70:
        complexity = "complex"
    else:
        complexity = "expert"

    # Build router: seeded models take priority, else use setup preset
    if _seeded_models:
        original = m.REGISTRY[:]
        try:
            m.REGISTRY.clear()
            m.REGISTRY.extend(_seeded_models)
            providers = {model.provider for model in _seeded_models}
            router = m.ModelRouter(
                available_providers=providers,
                budget="premium",
                prefer_local=False,
            )
            suite = router.select_suite(complexity)
        finally:
            m.REGISTRY.clear()
            m.REGISTRY.extend(original)
    else:
        router = m.router_for_setup(setup)
        suite = router.select_suite(complexity)

    # Build response with per-phase assignments
    phases = {}
    total_cost_per_1k_calls = 0.0
    for phase, model in sorted(suite.items()):
        if model is None:
            phases[phase] = {"model": None, "reason": "no model available"}
        else:
            avg_tokens = 2000  # rough estimate per phase call
            phase_cost = (model.cost_in + model.cost_out) * avg_tokens / 1_000_000
            total_cost_per_1k_calls += phase_cost * 1000
            phases[phase] = {
                "model_id": model.id,
                "model_name": model.name,
                "cost_in": model.cost_in,
                "cost_out": model.cost_out,
                "local": model.local,
                "max_complexity": model.max_complexity,
            }

    # Strategy advice
    if c < 40:
        strategy = "Low clarity — use a strong model to clarify the spec first, then route fill to cheaper models"
    elif d <= 30:
        strategy = "Simple task — smaller/cheaper models handle all phases well"
    elif d <= 60:
        strategy = "Moderate task — use mid-tier for spec/map, cheap for fill"
    else:
        strategy = "Hard task — frontier for spec/map/review, mid-tier for fill"

    result = {
        "difficulty": d,
        "clarity": c,
        "complexity": complexity,
        "source": "seeded_models" if _seeded_models else f"preset:{setup}",
        "strategy": strategy,
        "phases": phases,
    }

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _handle_list_models(args: dict) -> list[TextContent]:
    m = _ensure_models()
    setup = args.get("setup", "best_local_first")

    if _seeded_models:
        models = _seeded_models
        source = "seeded_models"
    else:
        router = m.router_for_setup(setup)
        # Get all models the router would consider (all complexities, all phases)
        seen = set()
        models = []
        for complexity in m.COMPLEXITY_LEVELS:
            suite = router.select_suite(complexity)
            for model in suite.values():
                if model and model.id not in seen:
                    seen.add(model.id)
                    models.append(model)
        source = f"preset:{setup}"

    rows = []
    for model in sorted(models, key=lambda x: (x.cost_in, x.id)):
        rows.append({
            "id": model.id,
            "name": model.name,
            "provider": model.provider,
            "cost_in": model.cost_in,
            "cost_out": model.cost_out,
            "context": model.context,
            "local": model.local,
            "max_complexity": model.max_complexity,
            "phases": sorted(model.phases),
        })

    result = {
        "source": source,
        "count": len(rows),
        "models": rows,
    }
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
