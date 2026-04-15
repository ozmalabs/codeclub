from fastapi import APIRouter, Depends
import aiosqlite
import json

from ..database import get_db, get_setting
from ..models import DashboardResponse
from .hardware import get_hardware

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

    # Tournament stats
    cur = await db.execute(
        "SELECT COUNT(*) FROM tournament_results WHERE created_at >= date('now')"
    )
    tournament_fights_today = (await cur.fetchone())[0]
    cur = await db.execute(
        "SELECT COUNT(*) FROM tournament_results WHERE created_at >= date('now') AND quality = 1.0"
    )
    tournament_champions_today = (await cur.fetchone())[0]

    # Pipeline paused
    paused = (await get_setting(db, "pipeline_paused")) == "true"

    # Hardware status
    hardware = await get_hardware()

    # Activity feed
    cur = await db.execute(
        "SELECT id, event, entity_type, entity_id, detail, created_at "
        "FROM activity_log ORDER BY id DESC LIMIT 20"
    )
    rows = await cur.fetchall()
    recent_activity = []
    for r in rows:
        # Keep detail as a JSON string for the frontend
        detail_str = r["detail"]
        recent_activity.append({
            "id": r["id"],
            "event": r["event"],
            "entity_type": r["entity_type"],
            "entity_id": r["entity_id"],
            "detail": detail_str,
            "created_at": r["created_at"],
        })

    # Map hardware to frontend-expected shape (hardware_status: list)
    hardware_status = []
    if hardware and hasattr(hardware, "endpoints"):
        for ep in hardware.endpoints:
            hardware_status.append({
                "name": ep.model_id if hasattr(ep, "model_id") else ep.url,
                "url": ep.url,
                "alive": ep.alive,
                "response_ms": ep.response_ms,
                "last_checked": None,
            })

    return DashboardResponse(
        queue_depth=queue_depth,
        active_runs=active_runs,
        completed_today=completed_today,
        failed_today=failed_today,
        total_cost_today=total_cost_today,
        hardware=None,
        hardware_status=hardware_status,
        recent_activity=recent_activity,
        tournament_fights_today=tournament_fights_today,
        tournament_champions_today=tournament_champions_today,
        pipeline_paused=paused,
    )
