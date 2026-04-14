"""Hardware detection & endpoints — Phase 2."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def get_hardware():
    return {"status": "not_implemented", "phase": 2}
