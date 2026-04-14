"""Model registry — Phase 2."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def list_models():
    return {"status": "not_implemented", "phase": 2}
