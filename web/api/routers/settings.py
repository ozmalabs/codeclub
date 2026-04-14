from fastapi import APIRouter, Depends
import aiosqlite

from ..database import get_db, set_setting
from ..models import SettingsResponse, SettingsUpdate

router = APIRouter()

PRESETS = [
    "local_only",
    "local_b580",
    "copilot",
    "anthropic",
    "openrouter_cheap",
    "best_local_first",
]


@router.get("/", response_model=SettingsResponse)
async def get_settings(db: aiosqlite.Connection = Depends(get_db)):
    cur = await db.execute("SELECT key, value FROM app_settings")
    rows = await cur.fetchall()
    return SettingsResponse(settings={r["key"]: r["value"] for r in rows})


@router.put("/", response_model=SettingsResponse)
async def update_settings(
    body: SettingsUpdate, db: aiosqlite.Connection = Depends(get_db)
):
    for key, value in body.settings.items():
        await set_setting(db, key, value)
    await db.commit()
    cur = await db.execute("SELECT key, value FROM app_settings")
    rows = await cur.fetchall()
    return SettingsResponse(settings={r["key"]: r["value"] for r in rows})


@router.get("/presets")
async def list_presets():
    return PRESETS
