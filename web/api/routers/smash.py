"""Smash efficiency maps — live data from tournament engine."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Query

_repo = str(Path(__file__).resolve().parent.parent.parent.parent)
if _repo not in sys.path:
    sys.path.insert(0, _repo)

router = APIRouter()


def _build_contenders():
    """Build Contender list from the model registry."""
    from codeclub.infra.models import REGISTRY
    from tournament import Contender, estimate_smash_range

    contenders = []
    for spec in REGISTRY:
        if not spec.params_b or spec.params_b <= 0:
            continue
        sr = estimate_smash_range(spec.params_b, quant=spec.quant)
        contenders.append(Contender(
            name=spec.name,
            kind="cloud" if not spec.local else "local",
            model_id=spec.id,
            cost_input=spec.cost_in,
            cost_output=spec.cost_out,
            params_b=spec.params_b,
            quant=spec.quant,
            context_k=spec.context // 1000 if spec.context >= 1000 else spec.context,
            is_local=spec.local,
            smash=sr,
        ))
    return contenders


@router.get("/")
async def get_smash_overview():
    """Return models with their smash ranges."""
    try:
        from codeclub.infra.models import REGISTRY
        from tournament import estimate_smash_range
    except ImportError as exc:
        return {"models": [], "error": str(exc)}

    models = []
    for spec in REGISTRY:
        if not spec.params_b or spec.params_b <= 0:
            continue
        try:
            sr = estimate_smash_range(spec.params_b, quant=spec.quant)
            models.append({
                "model_name": spec.name,
                "model_id": spec.id,
                "smash": {
                    "low": sr.low,
                    "sweet": sr.sweet,
                    "high": sr.high,
                    "min_clarity": sr.min_clarity,
                },
            })
        except Exception:
            continue

    return {"models": models, "count": len(models)}


@router.get("/route")
async def route_task(
    difficulty: int = Query(..., ge=0, le=100),
    clarity: int = Query(..., ge=0, le=100),
):
    """Route a task to the best model given difficulty and clarity."""
    try:
        from tournament import SmashCoord, recommend_routing
    except ImportError as exc:
        return {"error": str(exc)}

    coord = SmashCoord(difficulty=difficulty, clarity=clarity)

    try:
        contenders = _build_contenders()
    except ImportError as exc:
        return {"error": str(exc)}

    rec = recommend_routing(coord, contenders)

    # Build lookup from model name to contender for smash data
    by_name = {c.name: c for c in contenders}

    recommended = []
    for est in rec.all_estimates:
        c = by_name.get(est.model, None)
        entry = {
            "name": est.model,
            "quality": round(est.quality, 3),
            "time_s": round(est.time_s, 2),
            "cost_usd": round(est.cost_usd, 6),
            "value_eff": round(est.value_eff, 3),
            "speed_eff": round(est.speed_eff, 3),
            "compound_eff": round(est.compound_eff, 3),
            "is_local": est.is_local,
        }
        if c:
            entry["model_id"] = c.model_id
            entry["smash"] = {
                "low": c.smash.low,
                "sweet": c.smash.sweet,
                "high": c.smash.high,
                "min_clarity": c.smash.min_clarity,
            }
            entry["club"] = c.club
            entry["kind"] = c.kind
        recommended.append(entry)

    # Sort by compound efficiency descending
    recommended.sort(key=lambda r: r.get("compound_eff", 0), reverse=True)

    best = {}
    if rec.best_compound:
        best["compound"] = rec.best_compound.model
    if rec.best_value:
        best["value"] = rec.best_value.model
    if rec.best_speed:
        best["speed"] = rec.best_speed.model

    return {
        "coord": {"difficulty": difficulty, "clarity": clarity},
        "best": best,
        "recommended_models": recommended,
    }
