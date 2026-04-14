"""Run history — list and inspect pipeline execution attempts."""
from fastapi import APIRouter, Depends, HTTPException, Query
import aiosqlite
import json

from ..database import get_db
from ..models import RunResponse, PhaseInfo

router = APIRouter()


def _row_to_run(row: aiosqlite.Row) -> RunResponse:
    phases = json.loads(row["phases_json"] or "[]")
    return RunResponse(
        id=row["id"],
        task_id=row["task_id"],
        attempt=row["attempt"],
        status=row["status"],
        phases=[PhaseInfo(**p) for p in phases],
        code_snapshot=row["code_snapshot"],
        test_output=row["test_output"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        cost_usd=row["cost_usd"],
        elapsed_s=row["elapsed_s"],
        created_at=row["created_at"],
    )


@router.get("/")
async def list_runs(
    task_id: str | None = Query(None),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[RunResponse]:
    if task_id:
        cursor = await db.execute(
            "SELECT * FROM runs WHERE task_id = ? ORDER BY created_at DESC LIMIT 50",
            (task_id,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT 50"
        )
    rows = await cursor.fetchall()
    return [_row_to_run(r) for r in rows]


@router.get("/{run_id}")
async def get_run(
    run_id: int,
    db: aiosqlite.Connection = Depends(get_db),
) -> RunResponse:
    cursor = await db.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    return _row_to_run(row)
