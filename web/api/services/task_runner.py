"""
Background task runner — polls for queued tasks and executes them
via codeclub's dev loop.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiosqlite

from codeclub.dev.loop import run as dev_loop_run
from codeclub.infra.models import router_for_setup
from tournament import classify_and_estimate

from web.api.database import DB_PATH, log_activity

log = logging.getLogger(__name__)


async def _connect() -> aiosqlite.Connection:
    """Open a standalone aiosqlite connection (not tied to FastAPI DI)."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


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

        # Mark running
        await db.execute(
            "UPDATE tasks SET status = 'running', started_at = datetime('now') WHERE id = ?",
            (task_id,),
        )
        await db.commit()
        await log_activity(db, "task_started", "task", task_id, {"title": row["title"]})
        await db.commit()

        try:
            # Classify for difficulty/clarity metadata
            _cls, coord, _profile = classify_and_estimate(row["description"])

            # Build a model router from the task's setup preset
            setup = row["setup"] or "best_local_first"
            router = router_for_setup(setup)

            # Run dev loop in a thread (synchronous)
            result = await asyncio.to_thread(
                dev_loop_run,
                row["description"],
                router=router,
                max_fix_iterations=row["max_fix_rounds"] or 3,
                run_review=True,
                verbose=False,
                stack=row["stack"],
            )

            status = "done" if result.passed else "failed"
            test_output = (
                result.test_results[-1].output
                if result.test_results
                else None
            )
            review_json = (
                json.dumps({"approved": result.approved, "report": result.report})
                if hasattr(result, "approved")
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
                "  review_json = ?, ledger_json = ?, fix_rounds = ?,"
                "  complexity = ?, completed_at = datetime('now') "
                "WHERE id = ?",
                (
                    status,
                    result.final_code,
                    test_output,
                    review_json,
                    ledger_json,
                    result.iterations,
                    f"d={coord.difficulty} c={coord.clarity}",
                    task_id,
                ),
            )

            await db.execute(
                "INSERT INTO runs (task_id, attempt, status, code_snapshot, test_output, elapsed_s) "
                "VALUES (?, 1, ?, ?, ?, ?)",
                (task_id, status, result.final_code, test_output, result.total_time_s),
            )
            await db.commit()

            await log_activity(db, f"task_{status}", "task", task_id, {
                "iterations": result.iterations,
                "passed": result.passed,
                "time_s": result.total_time_s,
            })
            await db.commit()

        except Exception as e:
            log.exception("Task %s failed", task_id)
            await db.execute(
                "UPDATE tasks SET status = 'failed', error = ?, completed_at = datetime('now') WHERE id = ?",
                (str(e), task_id),
            )
            await db.commit()
            await log_activity(db, "task_failed", "task", task_id, {"error": str(e)})
            await db.commit()
