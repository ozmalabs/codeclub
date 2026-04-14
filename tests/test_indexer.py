"""Tests for codeclub.infra.indexer — model auto-discovery."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codeclub.infra.indexer import (
    IndexedModel,
    _guess_family,
    diff_registry,
    index_all,
    index_anthropic,
    index_copilot_sdk,
    index_github_models,
    index_openrouter,
)


# ── _guess_family ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model_id,expected", [
    ("openai/gpt-5.4", "gpt-5"),
    ("openai/gpt-4o-mini", "gpt-4"),
    ("anthropic/claude-sonnet-4.6", "claude"),
    ("meta-llama/llama-3.3-70b", "llama"),
    ("deepseek/deepseek-chat-v3", "deepseek"),
    ("google/gemma-4-26b", "gemma"),
    ("qwen/qwen3-coder-30b", "qwen"),
    ("minimax/minimax-m2.5", "minimax"),
    ("unknown/model-xyz", "other"),
])
def test_guess_family(model_id, expected):
    assert _guess_family(model_id) == expected


# ── index_openrouter ─────────────────────────────────────────────────────────

_FAKE_OPENROUTER_RESPONSE = {
    "data": [
        {
            "id": "openai/gpt-5.4",
            "name": "GPT-5.4",
            "pricing": {"prompt": "0.0000025", "completion": "0.000015"},
            "context_length": 1_000_000,
        },
        {
            "id": "meta-llama/llama-3.3-70b-instruct:free",
            "name": "Llama 3.3 70B (free)",
            "pricing": {"prompt": "0", "completion": "0"},
            "context_length": 131_072,
        },
        {
            "id": "some-vendor/unrelated-model",
            "name": "Unrelated Chat Model",
            "pricing": {"prompt": "0.001", "completion": "0.002"},
            "context_length": 32_000,
        },
    ]
}


def _mock_urlopen(response_data):
    """Create a mock for urllib.request.urlopen that returns JSON data."""
    class FakeResponse:
        def __init__(self):
            self._data = json.dumps(response_data).encode()
        def read(self):
            return self._data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    return patch("codeclub.infra.indexer.urllib.request.urlopen", return_value=FakeResponse())


def test_index_openrouter_parses_models():
    with _mock_urlopen(_FAKE_OPENROUTER_RESPONSE):
        models = index_openrouter()

    ids = {m.id for m in models}
    assert "openai/gpt-5.4" in ids
    assert "meta-llama/llama-3.3-70b-instruct:free" in ids
    # unrelated-model should be filtered out (not in _CODE_FAMILIES)
    assert "some-vendor/unrelated-model" not in ids


def test_index_openrouter_pricing():
    with _mock_urlopen(_FAKE_OPENROUTER_RESPONSE):
        models = index_openrouter()

    gpt = next(m for m in models if "gpt-5.4" in m.id)
    assert gpt.cost_in == 2.5
    assert gpt.cost_out == 15.0
    assert not gpt.free

    llama = next(m for m in models if "llama" in m.id)
    assert llama.free
    assert llama.cost_in == 0.0


def test_index_openrouter_tags():
    with _mock_urlopen(_FAKE_OPENROUTER_RESPONSE):
        models = index_openrouter()

    gpt = next(m for m in models if "gpt-5.4" in m.id)
    assert "openrouter" in gpt.tags
    assert "medium" in gpt.tags  # $2.50 is in medium range

    llama = next(m for m in models if "llama" in m.id)
    assert "free" in llama.tags


def test_index_openrouter_excludes_free_when_asked():
    with _mock_urlopen(_FAKE_OPENROUTER_RESPONSE):
        models = index_openrouter(include_free=False)

    ids = {m.id for m in models}
    assert "meta-llama/llama-3.3-70b-instruct:free" not in ids


def test_index_openrouter_handles_network_error():
    with patch("codeclub.infra.indexer.urllib.request.urlopen", side_effect=OSError("no network")):
        models = index_openrouter()
    assert models == []


# ── index_anthropic ──────────────────────────────────────────────────────────

def test_index_anthropic_returns_known_models():
    models = index_anthropic()
    ids = {m.id for m in models}
    assert "claude-opus-4-6" in ids
    assert "claude-sonnet-4-6" in ids
    assert "claude-haiku-4-5" in ids
    assert all(m.provider == "anthropic" for m in models)
    assert all(m.family == "claude" for m in models)


def test_index_anthropic_pricing():
    models = index_anthropic()
    opus = next(m for m in models if m.id == "claude-opus-4-6")
    assert opus.cost_in == 5.0
    assert opus.cost_out == 25.0


# ── index_copilot_sdk ───────────────────────────────────────────────────────

def test_index_copilot_sdk_returns_models():
    models = index_copilot_sdk()
    assert len(models) > 0
    assert all(m.provider == "copilot-sdk" for m in models)
    assert all(m.free for m in models)
    assert all(m.cost_in == 0.0 for m in models)

    ids = {m.id for m in models}
    assert "gpt-5.4" in ids
    assert "claude-sonnet-4.6" in ids


# ── index_github_models ─────────────────────────────────────────────────────

def test_index_github_models_returns_models():
    models = index_github_models()
    assert len(models) > 0
    assert all(m.provider == "github" for m in models)
    assert all(m.free for m in models)

    ids = {m.id for m in models}
    assert "gpt-5.4" in ids


# ── index_all ────────────────────────────────────────────────────────────────

def test_index_all_without_openrouter():
    models = index_all(include_openrouter=False)
    providers = {m.provider for m in models}
    assert "openrouter" not in providers
    assert "anthropic" in providers
    assert "copilot-sdk" in providers
    assert "github" in providers


def test_index_all_sorted_by_provider_then_cost():
    models = index_all(include_openrouter=False)
    prev_key = ("", 0.0, "")
    for m in models:
        key = (m.provider, m.cost_in, m.id)
        assert key >= prev_key, f"Out of order: {key} < {prev_key}"
        prev_key = key


# ── diff_registry ────────────────────────────────────────────────────────────

def test_diff_registry_finds_missing():
    indexed = [
        IndexedModel(
            id="brand-new-model", name="Brand New", provider="openrouter",
            family="gpt", cost_in=1.0, cost_out=5.0, context=100_000,
        ),
    ]
    result = diff_registry(indexed)
    assert len(result["missing"]) == 1
    assert result["missing"][0].id == "brand-new-model"


def test_diff_registry_finds_stale_pricing():
    from codeclub.infra.models import REGISTRY

    # Find a model that's in the registry with known pricing
    reg_model = next((m for m in REGISTRY if m.cost_in > 0), None)
    if reg_model is None:
        pytest.skip("No paid models in REGISTRY")

    indexed = [
        IndexedModel(
            id=reg_model.id, name=reg_model.name, provider=reg_model.provider,
            family=reg_model.family, cost_in=reg_model.cost_in + 1.0,
            cost_out=reg_model.cost_out, context=128_000,
        ),
    ]
    result = diff_registry(indexed)
    assert len(result["stale_pricing"]) == 1


def test_diff_registry_matches():
    from codeclub.infra.models import REGISTRY

    reg_model = next((m for m in REGISTRY if m.cost_in > 0), None)
    if reg_model is None:
        pytest.skip("No paid models in REGISTRY")

    indexed = [
        IndexedModel(
            id=reg_model.id, name=reg_model.name, provider=reg_model.provider,
            family=reg_model.family, cost_in=reg_model.cost_in,
            cost_out=reg_model.cost_out, context=128_000,
        ),
    ]
    result = diff_registry(indexed)
    assert len(result["matched"]) == 1
    assert len(result["missing"]) == 0
    assert len(result["stale_pricing"]) == 0
