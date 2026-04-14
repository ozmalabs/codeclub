"""Tournament fights — Phase 5."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def list_tournaments():
    return {"status": "not_implemented", "phase": 5}
