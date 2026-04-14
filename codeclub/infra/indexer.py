"""
indexer.py — Auto-discover available models from provider APIs.

Queries OpenRouter, Anthropic, and GitHub/Copilot endpoints to find
what's actually available right now. Returns normalized dicts that
can be compared against the curated REGISTRY.

Usage:
    from codeclub.infra.indexer import index_openrouter, index_all

    models = index_openrouter()          # live API call
    all_models = index_all()             # all providers
    print_index(all_models)              # formatted table

Design: Data over LLM. HTTP calls + JSON parsing. No inference.
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class IndexedModel:
    """A model discovered from a provider API."""
    id: str
    name: str
    provider: str
    family: str
    cost_in: float          # USD per million tokens
    cost_out: float
    context: int
    free: bool = False
    tags: set[str] = field(default_factory=set)


# ═══════════════════════════════════════════════════════════════════════════════
# OpenRouter
# ═══════════════════════════════════════════════════════════════════════════════

# Families we care about for code generation
_CODE_FAMILIES = {
    "gpt-5", "gpt-4", "gpt-3", "claude", "gemma", "llama", "qwen",
    "devstral", "mistral", "deepseek", "minimax", "codex", "phi",
}


def _guess_family(model_id: str) -> str:
    """Extract family name from model ID."""
    low = model_id.lower()
    for f in sorted(_CODE_FAMILIES, key=len, reverse=True):
        if f in low:
            return f
    return "other"


def index_openrouter(
    *,
    max_cost_in: float = 20.0,
    include_free: bool = True,
    timeout: int = 15,
) -> list[IndexedModel]:
    """
    Fetch available models from OpenRouter /api/v1/models.

    Filters to coding-relevant families and reasonable pricing.
    """
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError) as e:
        print(f"  [indexer] OpenRouter fetch failed: {e}")
        return []

    results: list[IndexedModel] = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        name = m.get("name", "")
        low = (mid + name).lower()

        if not any(f in low for f in _CODE_FAMILIES):
            continue

        pricing = m.get("pricing", {})
        cost_in = float(pricing.get("prompt", 0)) * 1_000_000
        cost_out = float(pricing.get("completion", 0)) * 1_000_000

        if cost_in > max_cost_in:
            continue

        is_free = cost_in == 0 and cost_out == 0
        if is_free and not include_free:
            continue

        ctx = m.get("context_length", 0) or 0
        family = _guess_family(mid)

        tags = {"openrouter", "cloud"}
        if is_free:
            tags.add("free")
        elif cost_in < 0.50:
            tags.add("cheap")
        elif cost_in < 3.00:
            tags.add("medium")
        else:
            tags.add("premium")

        results.append(IndexedModel(
            id=mid,
            name=name,
            provider="openrouter",
            family=family,
            cost_in=round(cost_in, 3),
            cost_out=round(cost_out, 3),
            context=ctx,
            free=is_free,
            tags=tags,
        ))

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Anthropic — known model list (no public discovery API)
# ═══════════════════════════════════════════════════════════════════════════════

# Anthropic doesn't have a public /models endpoint. These are the known models.
# Updated from https://docs.anthropic.com/en/docs/about-claude/models

_ANTHROPIC_MODELS = [
    ("claude-opus-4-6",    "Claude Opus 4.6",    5.00, 25.00, 200_000, "premium"),
    ("claude-sonnet-4-6",  "Claude Sonnet 4.6",  3.00, 15.00, 200_000, "medium"),
    ("claude-sonnet-4-5",  "Claude Sonnet 4.5",  3.00, 15.00, 200_000, "medium"),
    ("claude-haiku-4-5",   "Claude Haiku 4.5",   1.00,  5.00, 200_000, "cheap"),
]


def index_anthropic(*, verify: bool = False, timeout: int = 10) -> list[IndexedModel]:
    """
    Return known Anthropic models. Optionally verify API key works.
    """
    results = []
    for mid, name, ci, co, ctx, tier in _ANTHROPIC_MODELS:
        results.append(IndexedModel(
            id=mid, name=name, provider="anthropic", family="claude",
            cost_in=ci, cost_out=co, context=ctx,
            tags={"anthropic", "cloud", tier},
        ))

    if verify:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if key:
            try:
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages",
                    method="POST",
                    headers={
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    data=json.dumps({
                        "model": "claude-haiku-4-5",
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    }).encode(),
                )
                with urllib.request.urlopen(req, timeout=timeout):
                    for r in results:
                        r.tags.add("verified")
            except Exception:
                pass  # key invalid or rate limited

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# GitHub Copilot SDK — known models
# ═══════════════════════════════════════════════════════════════════════════════

# Models available through the Copilot SDK (free with Copilot subscription).
# These route through api.githubcopilot.com.

_COPILOT_SDK_MODELS = [
    ("gpt-5.4",           "GPT-5.4",            "gpt",    1_000_000),
    ("gpt-5",             "GPT-5",              "gpt",      400_000),
    ("gpt-4.1",           "GPT-4.1",            "gpt",    1_000_000),
    ("claude-sonnet-4.6", "Claude Sonnet 4.6",  "claude",   200_000),
    ("claude-sonnet-4.5", "Claude Sonnet 4.5",  "claude",   200_000),
    ("gpt-4o",            "GPT-4o",             "gpt",      128_000),
    ("o3-mini",           "o3-mini",            "gpt",      200_000),
    ("o4-mini",           "o4-mini",            "gpt",      200_000),
    ("gemini-2.5-pro",    "Gemini 2.5 Pro",     "gemini",   200_000),
]


def index_copilot_sdk() -> list[IndexedModel]:
    """Return models available through GitHub Copilot SDK (all free)."""
    results = []
    for mid, name, family, ctx in _COPILOT_SDK_MODELS:
        results.append(IndexedModel(
            id=mid, name=name, provider="copilot-sdk", family=family,
            cost_in=0.0, cost_out=0.0, context=ctx, free=True,
            tags={"copilot-sdk", "cloud", "free", "copilot"},
        ))
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# GitHub Models marketplace
# ═══════════════════════════════════════════════════════════════════════════════

_GITHUB_MODELS = [
    ("gpt-5.4",                    "GPT-5.4",              "gpt",      1_000_000),
    ("gpt-5",                      "GPT-5",                "gpt",        400_000),
    ("gpt-4.1",                    "GPT-4.1",              "gpt",      1_000_000),
    ("gpt-4.1-mini",               "GPT-4.1 Mini",         "gpt",      1_000_000),
    ("gpt-4.1-nano",               "GPT-4.1 Nano",         "gpt",      1_000_000),
    ("gpt-4o",                     "GPT-4o",               "gpt",        128_000),
    ("gpt-4o-mini",                "GPT-4o Mini",          "gpt",        128_000),
    ("o3-mini",                    "o3-mini",              "gpt",        200_000),
    ("o4-mini",                    "o4-mini",              "gpt",        200_000),
    ("Llama-3.3-70B-Instruct",     "Llama 3.3 70B",        "llama",      131_072),
    ("DeepSeek-R1",                "DeepSeek R1",          "deepseek",   128_000),
]


def index_github_models() -> list[IndexedModel]:
    """Return models available through GitHub Models (free with rate limits)."""
    results = []
    for mid, name, family, ctx in _GITHUB_MODELS:
        results.append(IndexedModel(
            id=mid, name=name, provider="github", family=family,
            cost_in=0.0, cost_out=0.0, context=ctx, free=True,
            tags={"github", "cloud", "free"},
        ))
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Combined index
# ═══════════════════════════════════════════════════════════════════════════════

def index_all(*, include_openrouter: bool = True, timeout: int = 15) -> list[IndexedModel]:
    """
    Index all providers. OpenRouter is a live API call; others are known lists.

    Returns deduplicated list sorted by provider then cost.
    """
    models: list[IndexedModel] = []

    if include_openrouter:
        models.extend(index_openrouter(timeout=timeout))

    models.extend(index_anthropic())
    models.extend(index_copilot_sdk())
    models.extend(index_github_models())

    # Sort: provider, then cost
    models.sort(key=lambda m: (m.provider, m.cost_in, m.id))
    return models


def print_index(models: list[IndexedModel]) -> None:
    """Pretty-print indexed models."""
    current_provider = ""
    for m in models:
        if m.provider != current_provider:
            current_provider = m.provider
            print(f"\n{'─'*70}")
            print(f"  {current_provider.upper()}")
            print(f"{'─'*70}")
        cost = "FREE" if m.free else f"${m.cost_in:.2f}/${m.cost_out:.2f}"
        ctx = f"{m.context:>10,}" if m.context else "       n/a"
        print(f"  {m.id:55s} {cost:>15s}  ctx={ctx}")


# ═══════════════════════════════════════════════════════════════════════════════
# Diff: compare indexed models against the curated REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════

def diff_registry(indexed: list[IndexedModel]) -> dict:
    """
    Compare indexed models against the curated REGISTRY.

    Returns a dict with:
      - missing: models available from providers but not in REGISTRY
      - stale_pricing: models where indexed price differs from REGISTRY
      - matched: models that match
    """
    from codeclub.infra.models import REGISTRY

    registry_ids = {m.id: m for m in REGISTRY}
    indexed_ids = {m.id: m for m in indexed}

    missing = []
    stale = []
    matched = []

    for im in indexed:
        rm = registry_ids.get(im.id)
        if rm is None:
            missing.append(im)
        elif abs(rm.cost_in - im.cost_in) > 0.01 or abs(rm.cost_out - im.cost_out) > 0.01:
            stale.append({"indexed": im, "registry": rm})
        else:
            matched.append(im)

    return {"missing": missing, "stale_pricing": stale, "matched": matched}


def print_diff(diff: dict) -> None:
    """Pretty-print a registry diff."""
    missing = diff["missing"]
    stale = diff["stale_pricing"]
    matched = diff["matched"]

    print(f"\n  Registry diff: {len(matched)} matched, {len(missing)} missing, {len(stale)} stale pricing\n")

    if missing:
        print("  MISSING (available but not in REGISTRY):")
        for m in missing[:20]:
            cost = "FREE" if m.free else f"${m.cost_in:.2f}/${m.cost_out:.2f}"
            print(f"    {m.id:50s} {cost:>15s}  [{m.provider}]")
        if len(missing) > 20:
            print(f"    ... and {len(missing) - 20} more")

    if stale:
        print("\n  STALE PRICING (registry price differs from live):")
        for s in stale:
            im, rm = s["indexed"], s["registry"]
            print(f"    {im.id:50s} registry=${rm.cost_in:.2f}/${rm.cost_out:.2f}  "
                  f"live=${im.cost_in:.2f}/${im.cost_out:.2f}")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Index available models from provider APIs")
    parser.add_argument("--diff", action="store_true", help="Compare against REGISTRY")
    parser.add_argument("--provider", choices=["openrouter", "anthropic", "copilot", "github", "all"],
                        default="all", help="Which provider to index")
    parser.add_argument("--timeout", type=int, default=15)
    args = parser.parse_args()

    if args.provider == "all":
        models = index_all(timeout=args.timeout)
    elif args.provider == "openrouter":
        models = index_openrouter(timeout=args.timeout)
    elif args.provider == "anthropic":
        models = index_anthropic()
    elif args.provider == "copilot":
        models = index_copilot_sdk()
    elif args.provider == "github":
        models = index_github_models()
    else:
        models = []

    print_index(models)

    if args.diff:
        d = diff_registry(models)
        print_diff(d)
