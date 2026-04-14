"""Smash efficiency map — Phase 2."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def get_smash():
    return {"status": "not_implemented", "phase": 2}
