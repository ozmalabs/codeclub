"""
proxy.py — OpenAI-compatible context proxy.

Sits between any LLM client and the model API.  Intercepts requests,
assembles minimal context, routes to the best model, streams responses.

Usage:
    python -m codeclub.context.proxy --port 8300 --upstream http://localhost:11434/v1

    # Then point your client at http://localhost:8300/v1/chat/completions
    # instead of the model API directly.

The proxy is transparent — clients send standard OpenAI API requests
and get standard responses back.  The magic happens in between.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import AsyncIterator

# Optional heavy deps — importable even if not installed.
try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

try:
    from fastapi import FastAPI, Request, Response
    from fastapi.responses import StreamingResponse
except ImportError:
    FastAPI = None  # type: ignore[assignment,misc]

try:
    import uvicorn
except ImportError:
    uvicorn = None  # type: ignore[assignment]

# Internal deps — graceful fallback when modules are missing or incomplete.
try:
    from .store import SessionStore
except ImportError:
    SessionStore = None  # type: ignore[assignment,misc]

try:
    from .classifier import (
        classify as _classify,
        estimate_clarity,
        should_start_new_episode as _should_start_new_episode,
        Intent,
    )
except ImportError:
    _classify = None  # type: ignore[assignment]
    estimate_clarity = None  # type: ignore[assignment]
    _should_start_new_episode = None  # type: ignore[assignment]
    Intent = None  # type: ignore[assignment]

try:
    from .assembler import assemble as _assemble, AssembledContext, FitLevel
except ImportError:
    _assemble = None  # type: ignore[assignment]
    AssembledContext = None  # type: ignore[assignment]
    FitLevel = None  # type: ignore[assignment,misc]

try:
    from .uplift import (
        decide_uplift as _decide_uplift,
        uplift_spec as _uplift_spec,
        UpliftPreference,
        UpliftDecision,
    )
except ImportError:
    _decide_uplift = None  # type: ignore[assignment]
    _uplift_spec = None  # type: ignore[assignment]
    UpliftPreference = None  # type: ignore[assignment,misc]
    UpliftDecision = None  # type: ignore[assignment,misc]

try:
    from .router import ContextRouter, context_window_fits
except ImportError:
    ContextRouter = None  # type: ignore[assignment,misc]
    context_window_fits = None  # type: ignore[assignment]

try:
    from .compaction import CompactionWorker
except ImportError:
    CompactionWorker = None  # type: ignore[assignment,misc]

try:
    from .adaptive import AdaptiveFitTracker, FitOutcome
except ImportError:
    AdaptiveFitTracker = None  # type: ignore[assignment,misc]
    FitOutcome = None  # type: ignore[assignment,misc]


logger = logging.getLogger("codeclub.proxy")


# ---------------------------------------------------------------------------
# Thin wrappers that degrade gracefully when optional modules are absent
# ---------------------------------------------------------------------------

def _safe_classify(message: str, recent_context: list[dict] | None = None):
    """Classify intent, or return a minimal stand-in."""
    if _classify is not None:
        return _classify(message, recent_context=recent_context)

    # Fallback: pretend everything is a follow-up with moderate clarity.
    from types import SimpleNamespace

    return SimpleNamespace(
        intent=SimpleNamespace(value="follow_up"),
        confidence=0.5,
        clarity=50,
        file_refs=[],
        symbol_refs=[],
        is_pivot=False,
        keywords={},
    )


def _safe_should_start_new_episode(
    classification,
    current_episode_intent: str | None = None,
    current_episode_age_s: float = 0,
) -> bool:
    if _should_start_new_episode is not None:
        return _should_start_new_episode(
            classification,
            current_episode_intent=current_episode_intent,
            current_episode_age_s=current_episode_age_s,
        )
    return current_episode_age_s > 300


def _safe_decide_uplift(clarity: int, pref):
    """Decide whether to uplift, or skip if the module is absent."""
    if _decide_uplift is not None:
        return _decide_uplift(clarity, pref)

    from types import SimpleNamespace

    return SimpleNamespace(should_uplift=False, reason="uplift module unavailable")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def _require(name: str, obj):
    """Raise a clear error if a required dependency is missing."""
    if obj is None:
        raise ImportError(
            f"{name} is required to run the context proxy.  "
            f"Install it with:  pip install {name}"
        )


def create_app(
    upstream_url: str = "http://localhost:11434/v1",
    db_path: str = "context_session.db",
    repo_root: str | None = None,
    default_fit: str = "balanced",
    default_preference: str = "balanced",
) -> FastAPI:
    """Create the proxy FastAPI app."""
    _require("fastapi", FastAPI)
    _require("httpx", httpx)

    app = FastAPI(title="codeclub context proxy", version="0.1.0")

    # Session store — fall back to a no-op if unavailable
    store = SessionStore(db_path) if SessionStore is not None else None

    fit_level = FitLevel(default_fit) if FitLevel is not None else None
    uplift_pref = (
        UpliftPreference(default_preference)
        if UpliftPreference is not None
        else None
    )

    # Stats tracking
    stats = {
        "requests": 0,
        "tokens_original": 0,
        "tokens_assembled": 0,
        "uplifts_performed": 0,
        "uplifts_skipped": 0,
        "episodes_created": 0,
        "model_downgrades": 0,
    }

    # Context-aware router (optional — needs codeclub.infra.models)
    ctx_router = None
    try:
        from codeclub.infra.models import ModelRouter
        base_router = ModelRouter(prefer_local=True)
        if ContextRouter is not None:
            ctx_router = ContextRouter(base_router)
            logger.info("Context-aware routing enabled")
    except ImportError:
        logger.info("ModelRouter not available — routing passthrough only")

    # Background compaction worker (optional)
    compaction_worker = None
    if CompactionWorker is not None and store is not None:
        compaction_worker = CompactionWorker(store, check_interval_s=120.0)
        compaction_worker.start()

    # Adaptive fit tracker (optional)
    adaptive = None
    if AdaptiveFitTracker is not None:
        state_dir = Path(db_path).parent
        adaptive = AdaptiveFitTracker(state_path=state_dir / "adaptive_fit.json")
        logger.info("Adaptive fit tracking enabled")

    @app.on_event("shutdown")
    async def _shutdown():
        if compaction_worker is not None:
            compaction_worker.stop()

    # ------------------------------------------------------------------
    # Main proxy endpoint
    # ------------------------------------------------------------------

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        """
        Intercept chat completions.

        Flow:
        1. Parse incoming request
        2. Classify intent + estimate clarity
        3. Check episode boundary
        4. Decide on clarity uplift
        5. Assemble minimal context
        6. Forward to upstream model
        7. Stream response back
        8. Index the exchange in session store
        """
        body = await request.json()
        messages = body.get("messages", [])
        model = body.get("model", "")
        stream = body.get("stream", False)

        # Allow clients to override fit / preference per-request.
        req_fit = request.headers.get("X-Context-Fit", default_fit)
        try:
            fit = FitLevel(req_fit) if FitLevel is not None else None
        except ValueError:
            fit = fit_level

        req_pref = request.headers.get("X-Context-Preference", default_preference)
        try:
            pref = (
                UpliftPreference(req_pref)
                if UpliftPreference is not None
                else None
            )
        except ValueError:
            pref = uplift_pref

        # FULL fit or missing assembler → pass through unmodified.
        if (FitLevel is not None and fit == FitLevel.FULL) or _assemble is None:
            return await _forward_raw(body, stream)

        # Find the last user message.
        user_msg = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_msg = msg.get("content", "")
                break

        if not user_msg:
            return await _forward_raw(body, stream)

        # 1. Classify
        recent = [
            {"role": m["role"], "content": m.get("content", "")}
            for m in messages[-6:]
        ]
        classification = _safe_classify(user_msg, recent_context=recent)
        clarity = classification.clarity

        # 2. Episode management
        ep_id: str | None = None
        if store is not None:
            current_ep = store.active_episode()
            if current_ep and _safe_should_start_new_episode(
                classification,
                current_episode_intent=current_ep.get("intent"),
                current_episode_age_s=time.time() - current_ep["updated_at"],
            ):
                store.close_episode(current_ep["id"])
                current_ep = None

            if not current_ep:
                ep_id = store.create_episode(
                    topic=user_msg[:80],
                    intent=classification.intent.value,
                )
                stats["episodes_created"] += 1
            else:
                ep_id = current_ep["id"]

            # 3. Store user turn
            turn_id = store.add_turn(
                ep_id, "user", user_msg,
                intent=classification.intent.value,
            )
            for f in classification.file_refs:
                store.add_code_ref(ep_id, f, turn_id=turn_id, ref_type="read")

        # 4. Clarity uplift check
        uplift_decision = _safe_decide_uplift(clarity, pref)
        effective_message = user_msg

        if uplift_decision.should_uplift and _uplift_spec is not None:
            result = _uplift_spec(user_msg, method="expand")
            effective_message = result.uplifted_message
            clarity = result.uplifted_clarity
            stats["uplifts_performed"] += 1
            logger.info(
                "Clarity uplift: %d -> %d (%s)",
                result.original_clarity,
                result.uplifted_clarity,
                uplift_decision.reason,
            )
        else:
            stats["uplifts_skipped"] += 1

        # 5. Assemble context (with adaptive adjustment)
        budget = _model_budget(model)

        # Apply adaptive padding adjustment if available
        if adaptive is not None and fit is not None:
            adj = adaptive.get_adjustment(
                classification.intent.value, fit.value,
            )
            if adj != 0.0:
                budget = int(budget * (1.0 + adj))
                logger.debug("Adaptive adjustment: %+.0f%% → budget=%d", adj * 100, budget)

        assembled = _assemble(
            classification,
            effective_message,
            store,
            fit=fit,
            budget_tokens=budget,
            repo_root=repo_root,
        )

        stats["requests"] += 1
        original_tokens = sum(len(m.get("content", "")) // 4 for m in messages)
        stats["tokens_original"] += original_tokens
        stats["tokens_assembled"] += assembled.total_tokens

        logger.info(
            "req=%d intent=%s clarity=%d fit=%s tokens=%d->%d sources=%s",
            stats["requests"],
            classification.intent.value,
            clarity,
            fit.value if fit is not None else "?",
            original_tokens,
            assembled.total_tokens,
            ",".join(assembled.sources),
        )

        # 6. Context-aware routing (optional model override)
        routed_model = model
        if ctx_router is not None and not request.headers.get("X-Context-Model"):
            try:
                from codeclub.infra.models import estimate_complexity
                complexity = estimate_complexity(effective_message)
                routing = ctx_router.select(
                    phase="fill",
                    complexity=complexity,
                    context_tokens=assembled.total_tokens,
                    difficulty=50,
                    clarity=clarity,
                    fit_level=fit or FitLevel.BALANCED,
                )
                if routing.model is not None and routing.model.id != model:
                    logger.info(
                        "Route override: %s -> %s (ctx=%d, smash=%.2f)",
                        model, routing.model.id,
                        assembled.total_tokens, routing.smash_fit,
                    )
                    routed_model = routing.model.id
                    if routing.model_downgraded:
                        stats["model_downgrades"] += 1
            except Exception:
                logger.debug("Routing decision failed, keeping original model", exc_info=True)

        # 7. Build forwarded request
        forwarded_body = dict(body)
        forwarded_body["messages"] = assembled.as_messages()
        if routed_model != model:
            forwarded_body["model"] = routed_model

        # 8. Forward and stream
        if stream:
            return StreamingResponse(
                _stream_and_index(forwarded_body, ep_id, store, stats),
                media_type="text/event-stream",
            )

        response_data = await _forward_and_get(forwarded_body)

        # Index assistant response
        assistant_msg = ""
        success = True
        choices = response_data.get("choices", [])
        if choices:
            assistant_msg = (
                choices[0].get("message", {}).get("content", "")
            )
        if assistant_msg and store is not None and ep_id is not None:
            store.add_turn(ep_id, "assistant", assistant_msg)

        # Check for context-insufficient signals in the response
        error_type = None
        if response_data.get("error"):
            success = False
            error_type = "api_error"
        elif assistant_msg and any(
            phrase in assistant_msg.lower()
            for phrase in ["i need to see", "could you share", "please provide",
                           "i don't have enough context", "without seeing the"]
        ):
            success = False
            error_type = "context_insufficient"

        # Record adaptive outcome
        if adaptive is not None and FitOutcome is not None and fit is not None:
            adaptive.record(FitOutcome(
                intent=classification.intent.value,
                fit_level=fit.value,
                context_tokens=assembled.total_tokens,
                budget_tokens=budget,
                success=success,
                error_type=error_type,
            ))

        return response_data

    # ------------------------------------------------------------------
    # Session management endpoints
    # ------------------------------------------------------------------

    @app.get("/v1/session/episodes")
    async def list_episodes():
        if store is None:
            return {"episodes": []}
        return {"episodes": store.list_episodes()}

    @app.get("/v1/session/stats")
    async def session_stats():
        db_stats = store.session_stats() if store is not None else {}
        return {
            **db_stats,
            "proxy": stats,
            "tokens_saved": stats["tokens_original"] - stats["tokens_assembled"],
            "compression_ratio": (
                1 - stats["tokens_assembled"] / max(stats["tokens_original"], 1)
            ),
        }

    @app.get("/v1/session/fit-stats")
    async def fit_stats():
        """Per-intent adaptive fit analytics."""
        if adaptive is not None:
            return adaptive.stats()
        return {"message": "adaptive tracking not available"}

    @app.post("/v1/session/reset")
    async def reset_session():
        if store is not None:
            store.reset(archive=True)
        for k in stats:
            stats[k] = 0
        return {"status": "reset"}

    @app.get("/health")
    async def health():
        return {"status": "ok", "upstream": upstream_url}

    # ------------------------------------------------------------------
    # Internal helpers (closures — capture upstream_url)
    # ------------------------------------------------------------------

    async def _forward_raw(body: dict, stream: bool):
        """Forward request without modification (FULL fit level)."""
        async with httpx.AsyncClient(timeout=120) as client:
            if stream:
                return StreamingResponse(
                    _raw_stream(body, client),
                    media_type="text/event-stream",
                )
            resp = await client.post(
                f"{upstream_url}/chat/completions",
                json=body,
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=dict(resp.headers),
            )

    async def _raw_stream(
        body: dict, client: httpx.AsyncClient
    ) -> AsyncIterator[bytes]:
        async with client.stream(
            "POST", f"{upstream_url}/chat/completions", json=body
        ) as resp:
            async for chunk in resp.aiter_bytes():
                yield chunk

    async def _forward_and_get(body: dict) -> dict:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{upstream_url}/chat/completions",
                json=body,
            )
            return resp.json()

    async def _stream_and_index(
        body: dict,
        ep_id: str | None,
        store: SessionStore | None,
        stats: dict,
    ) -> AsyncIterator[bytes]:
        """Stream response while collecting it for indexing."""
        collected: list[str] = []
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", f"{upstream_url}/chat/completions", json=body
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk
                    # Try to extract content from SSE chunks.
                    try:
                        text = chunk.decode("utf-8", errors="ignore")
                        for line in text.split("\n"):
                            if line.startswith("data: ") and line != "data: [DONE]":
                                data = json.loads(line[6:])
                                delta = (
                                    data.get("choices", [{}])[0]
                                    .get("delta", {})
                                )
                                content = delta.get("content", "")
                                if content:
                                    collected.append(content)
                    except (json.JSONDecodeError, IndexError, KeyError):
                        pass

        full_response = "".join(collected)
        if full_response and store is not None and ep_id is not None:
            store.add_turn(ep_id, "assistant", full_response)

    return app


# ---------------------------------------------------------------------------
# Model budget lookup
# ---------------------------------------------------------------------------

def _model_budget(model_name: str) -> int:
    """Get context-window budget for a model."""
    try:
        from codeclub.infra.models import get as _model_get

        spec = _model_get(model_name)
        if spec is not None:
            return spec.context
    except ImportError:
        pass

    # Heuristic fallbacks by name prefix.
    _DEFAULTS = {
        "gpt-4": 128_000,
        "gpt-5": 1_000_000,
        "claude": 200_000,
        "gemini": 1_000_000,
        "llama": 128_000,
        "qwen": 32_000,
    }
    lower = model_name.lower()
    for prefix, ctx in _DEFAULTS.items():
        if prefix in lower:
            return ctx
    return 8192


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="codeclub context proxy — dynamic context assembly for LLM APIs",
    )
    parser.add_argument("--port", type=int, default=8300, help="Port (default: 8300)")
    parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    parser.add_argument(
        "--upstream",
        default="http://localhost:11434/v1",
        help="Upstream LLM API URL (default: Ollama)",
    )
    parser.add_argument("--db", default="context_session.db", help="Session DB path")
    parser.add_argument("--repo", default=None, help="Repository root for code context")
    parser.add_argument(
        "--fit",
        default="balanced",
        choices=["minimal", "tight", "balanced", "generous", "full"],
        help="Default fit precision (default: balanced)",
    )
    parser.add_argument(
        "--preference",
        default="balanced",
        choices=["speed", "efficiency", "balanced"],
        help="Default uplift preference (default: balanced)",
    )
    parser.add_argument("--log-level", default="INFO", help="Log level")

    args = parser.parse_args()

    _require("fastapi", FastAPI)
    _require("httpx", httpx)
    _require("uvicorn", uvicorn)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    app = create_app(
        upstream_url=args.upstream,
        db_path=args.db,
        repo_root=args.repo,
        default_fit=args.fit,
        default_preference=args.preference,
    )

    logger.info(
        "codeclub context proxy starting on %s:%d -> %s (fit=%s, pref=%s)",
        args.host,
        args.port,
        args.upstream,
        args.fit,
        args.preference,
    )

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
