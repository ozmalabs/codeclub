from fastapi import APIRouter, Depends
import aiosqlite
import json

from ..database import get_db
from ..models import DashboardResponse

router = APIRouter()


@router.get("/", response_model=DashboardResponse)
async def get_dashboard(db: aiosqlite.Connection = Depends(get_db)):
    cur = await db.execute(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('pending','queued')"
    )
    queue_depth = (await cur.fetchone())[0]

    cur = await db.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
    )
    active_runs = (await cur.fetchone())[0]

    cur = await db.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'done' AND completed_at >= date('now')"
    )
    completed_today = (await cur.fetchone())[0]

    cur = await db.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'failed' AND completed_at >= date('now')"
    )
    failed_today = (await cur.fetchone())[0]

    cur = await db.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE created_at >= date('now')"
    )
    total_cost_today = (await cur.fetchone())[0]

    cur = await db.execute(
        "SELECT id, event, entity_type, entity_id, detail, created_at "
        "FROM activity_log ORDER BY id DESC LIMIT 20"
    )
    rows = await cur.fetchall()
    recent_activity = [
        {
            "id": r["id"],
            "event": r["event"],
            "entity_type": r["entity_type"],
            "entity_id": r["entity_id"],
            "detail": json.loads(r["detail"]) if r["detail"] else None,
            "created_at": r["created_at"],
        }
        for r in rows
    ]

    return DashboardResponse(
        queue_depth=queue_depth,
        active_runs=active_runs,
        completed_today=completed_today,
        failed_today=failed_today,
        total_cost_today=total_cost_today,
        hardware=None,
        recent_activity=recent_activity,
    )
