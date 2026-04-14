from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from codeclub.dev.loop import make_call_fn, make_copilot_sdk_fn
from codeclub.infra.models import ModelSpec, router_for_setup


def test_make_copilot_sdk_fn_uses_sdk_and_returns_text(monkeypatch):
    calls: dict[str, object] = {}

    class FakeSubprocessConfig:
        def __init__(self, **kwargs):
            calls["config_kwargs"] = kwargs

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def send_and_wait(self, prompt: str, *, timeout: int):
            calls["prompt"] = prompt
            calls["timeout"] = timeout
            return SimpleNamespace(data=SimpleNamespace(content={"text": "sdk ok"}))

    class FakeClient:
        def __init__(self, config):
            calls["client_config"] = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def create_session(self, **kwargs):
            calls["session_kwargs"] = kwargs
            return FakeSession()

    class FakePermissionHandler:
        @staticmethod
        def approve_all(request, invocation):
            return {"kind": "approved"}

    fake_copilot = types.ModuleType("copilot")
    fake_copilot.CopilotClient = FakeClient
    fake_copilot.SubprocessConfig = FakeSubprocessConfig

    fake_copilot_session = types.ModuleType("copilot.session")
    fake_copilot_session.PermissionHandler = FakePermissionHandler

    monkeypatch.setitem(sys.modules, "copilot", fake_copilot)
    monkeypatch.setitem(sys.modules, "copilot.session", fake_copilot_session)

    fn = make_copilot_sdk_fn(
        "gpt-5",
        github_token="token-123",
        cli_path="/usr/bin/copilot",
        cwd="/tmp/codeclub",
        timeout=42,
    )

    assert fn("Build a rate limiter") == "sdk ok"
    assert calls["config_kwargs"] == {
        "cwd": "/tmp/codeclub",
        "log_level": "error",
        "cli_path": "/usr/bin/copilot",
        "github_token": "token-123",
        "use_logged_in_user": False,
    }
    assert calls["session_kwargs"] == {
        "on_permission_request": FakePermissionHandler.approve_all,
        "model": "gpt-5",
        "working_directory": "/tmp/codeclub",
    }
    assert calls["prompt"] == "Build a rate limiter"
    assert calls["timeout"] == 42


def test_make_call_fn_routes_copilot_sdk_provider(monkeypatch):
    sentinel = object()

    def fake_make_copilot_sdk_fn(model_id: str, **kwargs):
        assert model_id == "gpt-5.4"  # prefix stripped
        assert kwargs["cwd"]
        return sentinel

    monkeypatch.setattr("codeclub.dev.loop.make_copilot_sdk_fn", fake_make_copilot_sdk_fn)

    model = ModelSpec(
        id="copilot:gpt-5.4",
        name="GPT-5.4",
        provider="copilot-sdk",
        family="gpt",
    )

    assert make_call_fn(model) is sentinel


def test_router_for_setup_copilot_uses_copilot_sdk_provider():
    router = router_for_setup("copilot")
    assert router.available_providers == {"copilot-sdk"}
