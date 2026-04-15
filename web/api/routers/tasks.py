from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..database import get_db, get_setting, log_activity, set_setting
from ..models import (
    BulkTaskActionResponse,
    PhaseInfo,
    PipelineStatusResponse,
    TaskBulkRequest,
    TaskCreate,
    TaskList,
    TaskResponse,
    TaskStatus,
    TaskUpdate,
)

router = APIRouter()
STREAM_GRACE_PERIOD_S = 30


@dataclass
class TaskStreamState:
    task_id: str
    subscribers: list[asyncio.Queue[tuple[str, dict[str, Any]]]] = field(default_factory=list)
    history: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    done: bool = False


TASK_STREAMS: dict[str, TaskStreamState] = {}


def get_or_create_task_stream(task_id: str, *, reset: bool = False) -> TaskStreamState:
    state = TASK_STREAMS.get(task_id)
    if state is None:
        state = TaskStreamState(task_id=task_id)
        TASK_STREAMS[task_id] = state
    elif reset:
        state.history.clear()
        state.done = False
    return state


async def _cleanup_task_stream(task_id: str, state: TaskStreamState) -> None:
    await asyncio.sleep(STREAM_GRACE_PERIOD_S)
    if TASK_STREAMS.get(task_id) is state and state.done:
        TASK_STREAMS.pop(task_id, None)


def _broadcast_stream_event(
    state: TaskStreamState, event_type: str, payload: dict[str, Any]
) -> None:
    state.history.append((event_type, payload))
    for queue in list(state.subscribers):
        queue.put_nowait((event_type, payload))
    if event_type == "done":
        state.done = True
        asyncio.create_task(_cleanup_task_stream(state.task_id, state))


def start_task_stream(task_id: str) -> TaskStreamState:
    return get_or_create_task_stream(task_id, reset=True)


def broadcast_task_event(task_id: str, event_type: str, payload: dict[str, Any]) -> None:
    state = get_or_create_task_stream(task_id)
    _broadcast_stream_event(state, event_type, payload)


def _row_to_task(row: aiosqlite.Row) -> TaskResponse:
    d = dict(row)
    d["phases"] = json.loads(d.pop("phases_json") or "[]")
    d["phases"] = [PhaseInfo(**p) if isinstance(p, dict) else p for p in d["phases"]]
    d["review_json"] = json.loads(d["review_json"]) if d.get("review_json") else None
    d["ledger_json"] = json.loads(d["ledger_json"]) if d.get("ledger_json") else None
    d["git_enabled"] = bool(d["git_enabled"])
    return TaskResponse(**d)


async def _get_task_or_404(
    db: aiosqlite.Connection, task_id: str
) -> aiosqlite.Row:
    cursor = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return row


@router.get("/", response_model=TaskList)
async def list_tasks(
    status: TaskStatus | None = Query(None),
    db: aiosqlite.Connection = Depends(get_db),
):
    if status:
        cursor = await db.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY priority ASC, created_at DESC",
            (status.value,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM tasks ORDER BY priority ASC, created_at DESC"
        )
    rows = await cursor.fetchall()
    tasks = [_row_to_task(r) for r in rows]
    return TaskList(tasks=tasks, total=len(tasks))


def _dedupe_task_ids(task_ids: list[str]) -> list[str]:
    return list(dict.fromkeys(task_ids))


async def _get_existing_task_ids(
    db: aiosqlite.Connection, task_ids: list[str]
) -> set[str]:
    if not task_ids:
        return set()
    placeholders = ", ".join("?" for _ in task_ids)
    cursor = await db.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})",
        task_ids,
    )
    rows = await cursor.fetchall()
    return {row["id"] for row in rows}


async def _validate_bulk_task_ids(
    db: aiosqlite.Connection, task_ids: list[str]
) -> list[str]:
    ids = _dedupe_task_ids(task_ids)
    if not ids:
        return []
    existing = await _get_existing_task_ids(db, ids)
    missing = [task_id for task_id in ids if task_id not in existing]
    if missing:
        raise HTTPException(
            status_code=404,
            detail={"message": "Some tasks were not found", "task_ids": missing},
        )
    return ids


async def _get_tasks_with_statuses(
    db: aiosqlite.Connection, task_ids: list[str], statuses: tuple[str, ...]
) -> list[aiosqlite.Row]:
    if not task_ids:
        return []
    placeholders = ", ".join("?" for _ in task_ids)
    status_placeholders = ", ".join("?" for _ in statuses)
    cursor = await db.execute(
        f"SELECT * FROM tasks WHERE id IN ({placeholders}) AND status IN ({status_placeholders})",
        (*task_ids, *statuses),
    )
    return await cursor.fetchall()


@router.post("/", response_model=TaskResponse, status_code=201)
async def create_task(
    body: TaskCreate,
    db: aiosqlite.Connection = Depends(get_db),
):
    task_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO tasks
           (id, title, description, setup, stack, language, budget,
            git_enabled, priority, max_fix_rounds, map_model, fill_model,
            review_model, phases_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id,
            body.title,
            body.description,
            body.setup,
            body.stack,
            body.language,
            body.budget,
            int(body.git_enabled),
            body.priority,
            body.max_fix_rounds,
            body.map_model,
            body.fill_model,
            body.review_model,
            "[]",
        ),
    )
    await log_activity(db, "task_created", "task", task_id, {"title": body.title})
    await db.commit()
    return _row_to_task(await _get_task_or_404(db, task_id))


@router.post("/bulk/run", response_model=BulkTaskActionResponse)
async def bulk_run_tasks(
    body: TaskBulkRequest,
    db: aiosqlite.Connection = Depends(get_db),
):
    task_ids = await _validate_bulk_task_ids(db, body.task_ids)
    if not task_ids:
        return BulkTaskActionResponse(count=0, task_ids=[])

    eligible_rows = await _get_tasks_with_statuses(db, task_ids, ("pending", "failed"))
    queued_ids = [row["id"] for row in eligible_rows]
    if not queued_ids:
        return BulkTaskActionResponse(count=0, task_ids=[])

    placeholders = ", ".join("?" for _ in queued_ids)
    await db.execute(
        f"UPDATE tasks SET status = 'queued' "
        f"WHERE id IN ({placeholders})",
        queued_ids,
    )
    for task_id in queued_ids:
        await log_activity(db, "task_queued", "task", task_id, {"bulk": True})
    await db.commit()
    return BulkTaskActionResponse(count=len(queued_ids), task_ids=queued_ids)


@router.post("/bulk/cancel", response_model=BulkTaskActionResponse)
async def bulk_cancel_tasks(
    body: TaskBulkRequest,
    db: aiosqlite.Connection = Depends(get_db),
):
    task_ids = await _validate_bulk_task_ids(db, body.task_ids)
    if not task_ids:
        return BulkTaskActionResponse(count=0, task_ids=[])

    cancellable_rows = await _get_tasks_with_statuses(db, task_ids, ("queued", "running"))
    cancelled_ids = [row["id"] for row in cancellable_rows]
    if not cancelled_ids:
        return BulkTaskActionResponse(count=0, task_ids=[])

    placeholders = ", ".join("?" for _ in cancelled_ids)
    await db.execute(
        f"UPDATE tasks SET status = 'cancelled' "
        f"WHERE id IN ({placeholders})",
        cancelled_ids,
    )
    for row in cancellable_rows:
        await log_activity(db, "task_cancelled", "task", row["id"], {"bulk": True})
        if row["status"] == "queued":
            broadcast_task_event(row["id"], "done", {"status": "cancelled", "quality": 0.0, "cost": None})
        else:
            broadcast_task_event(row["id"], "log", {"message": "Cancellation requested"})
    await db.commit()
    return BulkTaskActionResponse(count=len(cancelled_ids), task_ids=cancelled_ids)


@router.post("/bulk/delete", response_model=BulkTaskActionResponse)
async def bulk_delete_tasks(
    body: TaskBulkRequest,
    db: aiosqlite.Connection = Depends(get_db),
):
    task_ids = await _validate_bulk_task_ids(db, body.task_ids)
    if not task_ids:
        return BulkTaskActionResponse(count=0, task_ids=[])

    placeholders = ", ".join("?" for _ in task_ids)
    await db.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", task_ids)
    for task_id in task_ids:
        await log_activity(db, "task_deleted", "task", task_id, {"bulk": True})
        TASK_STREAMS.pop(task_id, None)
    await db.commit()
    return BulkTaskActionResponse(count=len(task_ids), task_ids=task_ids)


@router.get("/pipeline/status", response_model=PipelineStatusResponse)
async def pipeline_status(db: aiosqlite.Connection = Depends(get_db)):
    paused = (await get_setting(db, "pipeline_paused")) == "true"
    cursor = await db.execute("SELECT COUNT(*) AS count FROM tasks WHERE status = 'queued'")
    row = await cursor.fetchone()
    return PipelineStatusResponse(paused=paused, queue_depth=row["count"])


@router.post("/pipeline/pause", response_model=PipelineStatusResponse)
async def pause_pipeline(db: aiosqlite.Connection = Depends(get_db)):
    await set_setting(db, "pipeline_paused", "true")
    await log_activity(db, "pipeline_paused", "pipeline", "tasks")
    await db.commit()
    return await pipeline_status(db)


@router.post("/pipeline/resume", response_model=PipelineStatusResponse)
async def resume_pipeline(db: aiosqlite.Connection = Depends(get_db)):
    await set_setting(db, "pipeline_paused", "false")
    await log_activity(db, "pipeline_resumed", "pipeline", "tasks")
    await db.commit()
    return await pipeline_status(db)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    return _row_to_task(await _get_task_or_404(db, task_id))


@router.put("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: str,
    body: TaskUpdate,
    db: aiosqlite.Connection = Depends(get_db),
):
    await _get_task_or_404(db, task_id)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return _row_to_task(await _get_task_or_404(db, task_id))
    if "git_enabled" in updates:
        updates["git_enabled"] = int(updates["git_enabled"])
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    await db.execute(
        f"UPDATE tasks SET {set_clause} WHERE id = ?",
        (*updates.values(), task_id),
    )
    await db.commit()
    return _row_to_task(await _get_task_or_404(db, task_id))


@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    await _get_task_or_404(db, task_id)
    await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    await db.commit()
    TASK_STREAMS.pop(task_id, None)
    return {"ok": True}


@router.post("/{task_id}/run", response_model=TaskResponse)
async def run_task(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    row = await _get_task_or_404(db, task_id)
    if row["status"] not in ("pending", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot queue task with status '{row['status']}'",
        )
    await db.execute(
        "UPDATE tasks SET status = 'queued' WHERE id = ?", (task_id,)
    )
    await log_activity(db, "task_queued", "task", task_id)
    await db.commit()
    return _row_to_task(await _get_task_or_404(db, task_id))


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    row = await _get_task_or_404(db, task_id)
    await db.execute(
        "UPDATE tasks SET status = 'cancelled' WHERE id = ?", (task_id,)
    )
    await log_activity(db, "task_cancelled", "task", task_id)
    await db.commit()
    if row["status"] == "queued":
        broadcast_task_event(task_id, "done", {"status": "cancelled", "quality": 0.0, "cost": None})
    elif row["status"] == "running":
        broadcast_task_event(task_id, "log", {"message": "Cancellation requested"})
    return _row_to_task(await _get_task_or_404(db, task_id))


@router.post("/{task_id}/retry", response_model=TaskResponse)
async def retry_task(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    row = await _get_task_or_404(db, task_id)
    if row["status"] != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot retry task with status '{row['status']}'",
        )
    await db.execute(
        "UPDATE tasks SET status = 'pending', fix_rounds = fix_rounds + 1 WHERE id = ?",
        (task_id,),
    )
    await log_activity(db, "task_retried", "task", task_id)
    await db.commit()
    return _row_to_task(await _get_task_or_404(db, task_id))


@router.get("/{task_id}/stream")
async def stream_task(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    row = await _get_task_or_404(db, task_id)
    existing_state = TASK_STREAMS.get(task_id)

    if existing_state is None and row["status"] in ("done", "failed", "cancelled"):
        review = json.loads(row["review_json"]) if row["review_json"] else None
        ledger = json.loads(row["ledger_json"]) if row["ledger_json"] else None

        async def terminal_event_generator():
            if row["error"]:
                yield f"event: error\ndata: {json.dumps({'message': row['error']})}\n\n"
            quality = (review or {}).get("score", 1.0 if row["status"] == "done" else 0.0)
            cost = (ledger or {}).get("total_cost_usd") or (ledger or {}).get("cost_usd") or 0.0
            yield (
                "event: done\n"
                f"data: {json.dumps({'status': row['status'], 'quality': quality, 'cost': cost})}\n\n"
            )

        return StreamingResponse(
            terminal_event_generator(),
            media_type="text/event-stream",
        )

    state = existing_state or get_or_create_task_stream(task_id)
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
