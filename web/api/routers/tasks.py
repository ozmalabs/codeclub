from __future__ import annotations

import asyncio
import json
import uuid

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..database import get_db, log_activity
from ..models import (
    PhaseInfo,
    TaskCreate,
    TaskList,
    TaskResponse,
    TaskStatus,
    TaskUpdate,
)

router = APIRouter()


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
    await _get_task_or_404(db, task_id)
    await db.execute(
        "UPDATE tasks SET status = 'cancelled' WHERE id = ?", (task_id,)
    )
    await log_activity(db, "task_cancelled", "task", task_id)
    await db.commit()
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
    await _get_task_or_404(db, task_id)

    async def event_generator():
        data = json.dumps({"status": "not_implemented"})
        yield f"data: {data}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
