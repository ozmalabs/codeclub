"""Model registry — live data from codeclub engine."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter

_repo = str(Path(__file__).resolve().parent.parent.parent.parent)
if _repo not in sys.path:
    sys.path.insert(0, _repo)

router = APIRouter()


@router.get("/")
async def list_models():
    """Return all known models with their capabilities."""
    try:
        from codeclub.infra.models import REGISTRY, ModelSpec
        from tournament import SmashRange, estimate_smash_range
    except ImportError as exc:
        return {"models": [], "count": 0, "error": str(exc)}

    models = []
    for spec in REGISTRY:
        entry = {
            "id": spec.id,
            "name": spec.name,
            "provider": spec.provider,
            "family": spec.family,
            "params_b": spec.params_b,
            "quant": spec.quant,
            "cost_input": spec.cost_in,
            "cost_output": spec.cost_out,
            "context": spec.context,
            "local": spec.local,
            "phases": spec.phases,
            "tags": spec.tags,
        }
        # Compute smash range from model parameters
        if (spec.params_b or 0) > 0:
            try:
                sr = estimate_smash_range(spec.params_b, quant=spec.quant)
                entry["smash"] = {
                    "low": sr.low,
                    "sweet": sr.sweet,
                    "high": sr.high,
                    "min_clarity": sr.min_clarity,
                }
            except Exception:
                entry["smash"] = None
        else:
            entry["smash"] = None
        models.append(entry)

    return {"models": models, "count": len(models)}
