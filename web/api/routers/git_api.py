"""Git integration — Phase 4."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def list_repos():
    return {"status": "not_implemented", "phase": 4}
