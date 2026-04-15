"""Tournament API — results, task catalog, runner, leaderboard, and SSE."""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiosqlite
import tournament as tournament_module
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from tournament import TASKS, FightResult, build_contenders, check_endpoints, run_task

from ..database import DB_PATH, get_db, log_activity
from ..models import (
    FightResultResponse,
    TournamentLeaderboardEntry,
    TournamentResultResponse,
    TournamentStartRequest,
    TournamentStartResponse,
    TournamentTaskResponse,
)

router = APIRouter()


@dataclass
class TournamentRunState:
    run_id: str
    loop: asyncio.AbstractEventLoop
    history: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    subscribers: list[asyncio.Queue[tuple[str, dict[str, Any]]]] = field(default_factory=list)
    done: bool = False


ACTIVE_RUNS: dict[str, TournamentRunState] = {}
_FIGHT_STREAM_LOCAL = threading.local()


def _patched_print_fight_result(result: FightResult) -> None:
    callback = getattr(_FIGHT_STREAM_LOCAL, "callback", None)
    if callback:
        try:
            callback(result)
        except Exception:
            pass
    _ORIGINAL_PRINT_FIGHT_RESULT(result)


if not getattr(tournament_module, "_web_api_fight_patch", False):
    _ORIGINAL_PRINT_FIGHT_RESULT = tournament_module._print_fight_result
    tournament_module._web_api_original_print_fight_result = _ORIGINAL_PRINT_FIGHT_RESULT
    tournament_module._print_fight_result = _patched_print_fight_result
    tournament_module._web_api_fight_patch = True
else:
    _ORIGINAL_PRINT_FIGHT_RESULT = tournament_module._web_api_original_print_fight_result


def _row_to_result(row: aiosqlite.Row) -> TournamentResultResponse:
    return TournamentResultResponse(**dict(row))


def _fight_to_payload(result: FightResult, fitness: float | None = None) -> FightResultResponse:
    model = result.model or result.fill_model
    return FightResultResponse(
        task_id=result.task_id,
        mode=result.mode,
        model=model,
        mapper=result.map_model or None,
        quality=result.quality,
        tests_passed=result.tests_passed,
        tests_total=result.tests_total,
        elapsed_s=result.elapsed_s,
        cost_usd=result.cost,
        energy_j=result.energy_j,
        smash_fit=result.smash_fit,
        smash_measured=result.smash_measured,
        fitness=fitness,
    )


def _broadcast_event(state: TournamentRunState, event: str, payload: dict[str, Any]) -> None:
    state.history.append((event, payload))
    for queue in list(state.subscribers):
        queue.put_nowait((event, payload))
    if event == "done":
        state.done = True


async def _log_activity_event(
    event: str,
    entity_id: str,
    detail: dict[str, Any] | None = None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await log_activity(db, event, "tournament", entity_id, detail)
        await db.commit()


async def _insert_fight_result_aio(result: FightResult, fitness: float) -> None:
    """Insert a fight result using aiosqlite (safe for concurrent async access)."""
    model = result.model or result.fill_model
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO tournament_results (
                task_id, mode, model, mapper, quality, tests_passed, tests_total,
                elapsed_s, cost_usd, energy_j, smash_fit, smash_measured, fitness,
                final_code, test_details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.task_id,
                result.mode,
                model,
                result.map_model or None,
                result.quality,
                result.tests_passed,
                result.tests_total,
                result.elapsed_s,
                result.cost,
                result.energy_j,
                result.smash_fit,
                result.smash_measured,
                fitness,
                result.final_code or None,
                json.dumps(result.test_details) if result.test_details else None,
            ),
        )
        await db.commit()


def _insert_fight_result_async(
    result: FightResult, fitness: float, loop: asyncio.AbstractEventLoop
) -> None:
    """Schedule an async DB insert from a background thread."""
    asyncio.run_coroutine_threadsafe(_insert_fight_result_aio(result, fitness), loop)


def _run_tournament_sync(
    run_id: str,
    task_ids: list[str],
    optimize: str,
    quick: bool,
    loop: asyncio.AbstractEventLoop,
) -> dict[str, int]:
    state = ACTIVE_RUNS[run_id]
    weights = tournament_module.PRESETS[optimize]
    contenders = check_endpoints(build_contenders())
    if not contenders:
        raise RuntimeError("No reachable tournament contenders found")

    total_fights = 0
    champions = 0

    def emit_fight(result: FightResult) -> None:
        payload = _fight_to_payload(result).model_dump()
        loop.call_soon_threadsafe(_broadcast_event, state, "fight", payload)

    _FIGHT_STREAM_LOCAL.callback = emit_fight
    try:
        for task_id in task_ids:
            loop.call_soon_threadsafe(
                _broadcast_event, state, "task",
                {"task_id": task_id, "status": "running"},
            )
            scored = run_task(TASKS[task_id], contenders, weights, quick=quick)
            for result, fitness in scored:
                total_fights += 1
                if result.quality >= 1.0:
                    champions += 1
                _insert_fight_result_async(result, fitness, loop)
            loop.call_soon_threadsafe(
                _broadcast_event, state, "task",
                {"task_id": task_id, "status": "done", "results": len(scored)},
            )
            asyncio.run_coroutine_threadsafe(
                _log_activity_event(
                    "tournament_task_completed",
                    run_id,
                    {"run_id": run_id, "task_id": task_id, "results": len(scored)},
                ),
                loop,
            )
    finally:
        _FIGHT_STREAM_LOCAL.callback = None

    return {"champions": champions, "total_fights": total_fights}


async def _run_tournament_background(
    run_id: str,
    task_ids: list[str],
    optimize: str,
    quick: bool,
) -> None:
    state = ACTIVE_RUNS[run_id]
    try:
        summary = await asyncio.to_thread(
            _run_tournament_sync,
            run_id,
            task_ids,
            optimize,
            quick,
            state.loop,
        )
        await _log_activity_event(
            "tournament_completed",
            run_id,
            {"run_id": run_id, **summary, "optimize": optimize, "quick": quick},
        )
        _broadcast_event(state, "done", {"status": "completed", **summary})
    except Exception as exc:
        await _log_activity_event(
            "tournament_failed",
            run_id,
            {"run_id": run_id, "error": str(exc), "optimize": optimize, "quick": quick},
        )
        _broadcast_event(
            state,
            "done",
            {"status": "failed", "error": str(exc), "champions": 0, "total_fights": 0},
        )
    finally:
        # Clean up after all subscribers disconnect (or immediately if none)
        await asyncio.sleep(30)  # grace period for late SSE connections
        ACTIVE_RUNS.pop(run_id, None)


@router.get("/", response_model=list[TournamentResultResponse])
@router.get("/results", response_model=list[TournamentResultResponse])
@router.get("/results/", response_model=list[TournamentResultResponse], include_in_schema=False)
async def list_tournaments(
    task_id: str | None = Query(None),
    db: aiosqlite.Connection = Depends(get_db),
):
    if task_id:
        cursor = await db.execute(
            "SELECT * FROM tournament_results WHERE task_id = ? ORDER BY created_at DESC",
            (task_id,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM tournament_results ORDER BY created_at DESC"
        )
    rows = await cursor.fetchall()
    return [_row_to_result(row) for row in rows]


@router.post("/start", response_model=TournamentStartResponse)
@router.post("/start/", response_model=TournamentStartResponse, include_in_schema=False)
async def start_tournament(
    body: TournamentStartRequest,
    db: aiosqlite.Connection = Depends(get_db),
):
    if body.task_id:
        if body.task_id not in TASKS:
            raise HTTPException(status_code=404, detail="Tournament task not found")
        task_ids = [body.task_id]
    else:
        task_ids = sorted(TASKS)

    run_id = str(uuid.uuid4())
    ACTIVE_RUNS[run_id] = TournamentRunState(
        run_id=run_id,
        loop=asyncio.get_running_loop(),
    )

    await log_activity(
        db,
        "tournament_started",
        "tournament",
        run_id,
        {
            "run_id": run_id,
            "task_id": body.task_id,
            "optimize": body.optimize,
            "quick": body.quick,
            "tasks": task_ids,
        },
    )
    await db.commit()

    asyncio.create_task(
        _run_tournament_background(run_id, task_ids, body.optimize, body.quick)
    )
    return TournamentStartResponse(status="started", run_id=run_id)


@router.get("/tasks", response_model=list[TournamentTaskResponse])
@router.get("/tasks/", response_model=list[TournamentTaskResponse], include_in_schema=False)
async def list_tournament_tasks():
    return [
        TournamentTaskResponse(
            id=task.id,
            name=task.name,
            lang=task.lang,
            base_difficulty=task.base_difficulty,
            num_tests=len(task.tests),
            description=task.description,
        )
        for task in sorted(TASKS.values(), key=lambda task: task.id)
    ]


@router.get("/leaderboard", response_model=list[TournamentLeaderboardEntry])
@router.get(
    "/leaderboard/",
    response_model=list[TournamentLeaderboardEntry],
    include_in_schema=False,
)
async def get_leaderboard(db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute(
        """
        SELECT model, COUNT(*) AS wins, AVG(fitness) AS avg_fitness
        FROM tournament_results
        WHERE quality = 1.0
        GROUP BY model
        ORDER BY wins DESC, avg_fitness DESC, model ASC
        """
    )
    rows = await cursor.fetchall()
    return [TournamentLeaderboardEntry(**dict(row)) for row in rows]


@router.get("/stream/{run_id}")
@router.get("/stream/{run_id}/", include_in_schema=False)
async def stream_tournament(run_id: str):
    state = ACTIVE_RUNS.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Tournament run not found")

    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    state.subscribers.append(queue)

    async def event_generator():
        try:
            for event, payload in state.history:
                yield f"event: {event}\ndata: {json.dumps(payload)}\n\n"
            if state.done:
                return

            while True:
                event, payload = await queue.get()
                yield f"event: {event}\ndata: {json.dumps(payload)}\n\n"
                if event == "done":
                    return
        finally:
            if queue in state.subscribers:
                state.subscribers.remove(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
