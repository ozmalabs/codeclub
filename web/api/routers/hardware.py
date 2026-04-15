"""Hardware detection — live probe."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter

_repo = str(Path(__file__).resolve().parent.parent.parent.parent)
if _repo not in sys.path:
    sys.path.insert(0, _repo)

router = APIRouter()


@router.get("/")
async def get_hardware():
    """Detect hardware and probe endpoints."""
    try:
        from codeclub.infra.hardware import HardwareSetup
    except ImportError as exc:
        return {
            "has_gpu": False,
            "total_vram_mb": 0,
            "ram_mb": 0,
            "endpoints": [],
            "error": str(exc),
        }

    hw = HardwareSetup.detect()
    hw.probe()

    endpoints = []
    for ep in hw.alive_endpoints():
        endpoints.append({
            "url": ep.url,
            "provider": ep.provider,
            "model_id": ep.model_id or "",
            "alive": ep.alive,
            "response_ms": ep.tps_observed,
            "is_gpu": ep.device is not None,
        })

    return {
        "has_gpu": hw.has_gpu,
        "total_vram_mb": hw.total_vram_mb,
        "ram_mb": hw.ram_mb,
        "endpoints": endpoints,
    }
