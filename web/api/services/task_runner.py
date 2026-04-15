"""
Background task runner — polls for queued tasks and executes them
via codeclub's dev loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from codeclub.dev.loop import run as dev_loop_run
from codeclub.infra.models import router_for_setup
from tournament import classify_and_estimate

from web.api.database import DB_PATH, get_setting, log_activity
from web.api.models import PhaseInfo, PhaseStatus
from web.api.routers.tasks import broadcast_task_event, start_task_stream

log = logging.getLogger(__name__)


async def _connect() -> aiosqlite.Connection:
    """Open a standalone aiosqlite connection (not tied to FastAPI DI)."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _phase_payload(
    phase: str,
    status: PhaseStatus,
    *,
    started_at: str | None = None,
    elapsed_s: float | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return PhaseInfo(
        phase=phase,
        status=status,
        started_at=started_at,
        elapsed_s=elapsed_s,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        error=error,
    ).model_dump()


def _build_phase_payloads(started_at: str, result) -> list[dict[str, Any]]:
    phases: list[dict[str, Any]] = []
    if result.ledger:
        for phase in result.ledger.phases:
            phases.append(
                _phase_payload(
                    phase.phase,
                    PhaseStatus.done if phase.success else PhaseStatus.failed,
                    started_at=started_at,
                    elapsed_s=phase.wall_s,
                    tokens_in=phase.tokens_in,
                    tokens_out=phase.tokens_out,
                    error=phase.error or None,
                )
            )
    else:
        phases.append(
            _phase_payload("spec", PhaseStatus.done, started_at=started_at)
        )

    if result.test_results:
        phases.append(
            _phase_payload(
                "test",
                PhaseStatus.done if result.passed else PhaseStatus.failed,
                started_at=started_at,
            )
        )
    else:
        phases.append(
            _phase_payload("test", PhaseStatus.skipped, started_at=started_at)
        )

    if result.review is None:
        phases.append(
            _phase_payload("review", PhaseStatus.skipped, started_at=started_at)
        )
    elif not any(phase["phase"] == "review" for phase in phases):
        phases.append(
            _phase_payload("review", PhaseStatus.done, started_at=started_at)
        )

    return phases


def _emit_result_events(task_id: str, started_at: str, result) -> None:
    broadcast_task_event(
        task_id,
        "log",
        {
            "message": (
                f"Dev loop completed in {result.total_time_s:.2f}s "
                f"after {result.iterations + 1} iteration(s)"
            )
        },
    )
    for phase in _build_phase_payloads(started_at, result):
        broadcast_task_event(task_id, "phase", phase)

    if result.test_results:
        for index, test_result in enumerate(result.test_results, start=1):
            if test_result.failed_tests:
                for test_name in test_result.failed_tests:
                    error = next(
                        (
                            message
                            for message in test_result.errors
                            if test_name in message
                        ),
                        test_result.errors[0] if test_result.errors else None,
                    )
                    broadcast_task_event(
                        task_id,
                        "test",
                        {"name": test_name, "passed": False, "error": error},
                    )
            else:
                broadcast_task_event(
                    task_id,
                    "test",
                    {
                        "name": f"iteration_{index}",
                        "passed": test_result.passed,
                        "error": None if test_result.passed else (
                            test_result.errors[0] if test_result.errors else None
                        ),
                    },
                )

    if result.final_code:
        broadcast_task_event(task_id, "code", {"code": result.final_code})

    if result.review:
        broadcast_task_event(
            task_id,
            "review",
            {
                "verdict": result.review.verdict,
                "summary": result.review.summary,
                "issues": result.review.issues,
                "approved": result.review.approved,
                "score": result.review.score,
            },
        )


def _run_dev_loop_sync(
    *,
    task_id: str,
    description: str,
    setup: str,
    stack: str | None,
    max_fix_rounds: int,
    router,
    loop: asyncio.AbstractEventLoop,
    started_at: str,
):
    loop.call_soon_threadsafe(
        broadcast_task_event,
        task_id,
        "log",
        {"message": f"Starting dev loop with setup '{setup}'"},
    )
    try:
        result = dev_loop_run(
            description,
            router=router,
            max_fix_iterations=max_fix_rounds,
            run_review=True,
            verbose=False,
            stack=stack,
        )
    except Exception as exc:
        loop.call_soon_threadsafe(
            broadcast_task_event,
            task_id,
            "error",
            {"message": str(exc)},
        )
        raise

    loop.call_soon_threadsafe(_emit_result_events, task_id, started_at, result)
    return result


class TaskRunner:
    """Background loop that picks and runs queued tasks."""

    def __init__(self, poll_interval: float = 10.0):
        self._poll_interval = poll_interval
        self._running = True

    def stop(self):
        self._running = False

    async def run_loop(self):
        """Poll every N seconds for queued tasks. Runs as asyncio.create_task()."""
        log.info("Task runner started (poll every %.0fs)", self._poll_interval)
        while self._running:
            try:
                await self._tick()
            except Exception:
                log.exception("Task runner tick failed")
            await asyncio.sleep(self._poll_interval)
        log.info("Task runner stopped")

    async def _tick(self):
        """Single poll cycle — pick highest-priority queued task and run it."""
        db = await _connect()
        try:
            if (await get_setting(db, "pipeline_paused")) == "true":
                return
            await self._process_next(db)
        finally:
            await db.close()

    async def _process_next(self, db: aiosqlite.Connection):
        row = await db.execute_fetchone(
            "SELECT * FROM tasks WHERE status = 'queued' "
            "ORDER BY priority DESC, created_at ASC LIMIT 1"
        )
        if not row:
            return

        task_id = row["id"]
        loop = asyncio.get_running_loop()
        started_at = _now_iso()

        # Mark running
        cursor = await db.execute(
            "UPDATE tasks SET status = 'running', started_at = datetime('now') "
            "WHERE id = ? AND status = 'queued'",
            (task_id,),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return
        await db.commit()
        await log_activity(db, "task_started", "task", task_id, {"title": row["title"]})
        await db.commit()
        start_task_stream(task_id)
        broadcast_task_event(task_id, "log", {"message": "Task picked up by runner"})
        broadcast_task_event(
            task_id,
            "phase",
            _phase_payload("spec", PhaseStatus.running, started_at=started_at),
        )

        try:
            # Classify for difficulty/clarity metadata
            _cls, coord, _profile = classify_and_estimate(row["description"])

            # Build a model router from the task's setup preset
            setup = row["setup"] or "best_local_first"
            router = router_for_setup(setup)

            # Run dev loop in a thread (synchronous)
            result = await asyncio.to_thread(
                _run_dev_loop_sync,
                task_id=task_id,
                description=row["description"],
                setup=setup,
                stack=row["stack"],
                max_fix_rounds=row["max_fix_rounds"] or 3,
                router=router,
                loop=loop,
                started_at=started_at,
            )

            phases_json = json.dumps(_build_phase_payloads(started_at, result))
            existing_status = await db.execute_fetchone(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,),
            )
            cancelled = existing_status and existing_status["status"] == "cancelled"
            status = "cancelled" if cancelled else ("done" if result.passed else "failed")
            test_output = (
                result.test_results[-1].output
                if result.test_results
                else None
            )
            review_json = (
                json.dumps(
                    {
                        "approved": result.approved,
                        "verdict": result.review.verdict,
                        "summary": result.review.summary,
                        "issues": result.review.issues,
                        "score": result.review.score,
                        "report": result.report,
                    }
                )
                if result.review
                else None
            )
            ledger_json = (
                json.dumps(result.ledger.to_dict())
                if result.ledger and hasattr(result.ledger, "to_dict")
                else None
            )

            await db.execute(
                "UPDATE tasks SET "
                "  status = ?, final_code = ?, test_output = ?,"
                "  review_json = ?, ledger_json = ?, phases_json = ?,"
                "  fix_rounds = ?, complexity = ?, error = NULL,"
                "  completed_at = datetime('now') "
                "WHERE id = ?",
                (
                    status,
                    result.final_code,
                    test_output,
                    review_json,
                    ledger_json,
                    phases_json,
                    result.iterations,
                    f"d={coord.difficulty} c={coord.clarity}",
                    task_id,
                ),
            )

            await db.execute(
                "INSERT INTO runs (task_id, attempt, status, phases_json, code_snapshot, test_output, "
                "tokens_in, tokens_out, cost_usd, elapsed_s) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    (row["fix_rounds"] or 0) + 1,
                    status,
                    phases_json,
                    result.final_code,
                    test_output,
                    result.ledger.total_tokens_in if result.ledger else None,
                    result.ledger.total_tokens_out if result.ledger else None,
                    result.ledger.total_cost_usd() if result.ledger else None,
                    result.total_time_s,
                ),
            )
            await db.commit()

            await log_activity(db, f"task_{status}", "task", task_id, {
                "iterations": result.iterations,
                "passed": result.passed,
                "time_s": result.total_time_s,
            })
            await db.commit()
            broadcast_task_event(
                task_id,
                "done",
                {
                    "status": status,
                    "quality": (
                        result.review.score
                        if result.review
                        else (1.0 if result.passed else 0.0)
                    ),
                    "cost": (
                        result.ledger.total_cost_usd()
                        if result.ledger else 0.0
                    ),
                },
            )

        except Exception as e:
            log.exception("Task %s failed", task_id)
            await db.execute(
                "UPDATE tasks SET status = 'failed', error = ?, phases_json = ?, completed_at = datetime('now') "
                "WHERE id = ?",
                (
                    str(e),
                    json.dumps(
                        [
                            _phase_payload(
                                "spec",
                                PhaseStatus.failed,
                                started_at=started_at,
                                error=str(e),
                            )
                        ]
                    ),
                    task_id,
                ),
            )
            await db.commit()
            await log_activity(db, "task_failed", "task", task_id, {"error": str(e)})
            await db.commit()
            broadcast_task_event(
                task_id,
                "done",
                {"status": "failed", "quality": 0.0, "cost": None},
            )
