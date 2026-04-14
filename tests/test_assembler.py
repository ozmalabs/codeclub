"""Tests for codeclub.context.assembler."""
from __future__ import annotations

import os
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from codeclub.context.assembler import (
    AssembledContext,
    FitLevel,
    FIT_PADDING,
    assemble,
    _turn_limit,
    _repo_structure,
    _trim_to_budget,
    _format_turns,
    _format_stats,
    _read_and_stub_files,
    _read_files_full,
)
from codeclub.context.store import SessionStore


# ── Lightweight Classification stub ──────────────────────────────────
# The real classifier module doesn't exist yet; replicate its shape.


class _Intent:
    """Minimal Intent enum stand-in."""
    def __init__(self, value: str):
        self.value = value

    def __str__(self) -> str:
        return self.value


@dataclass
class _Classification:
    """Minimal Classification stand-in matching the expected interface."""
    intent: _Intent
    file_refs: list[str] = field(default_factory=list)
    symbol_refs: list[str] = field(default_factory=list)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """Create a small temporary repo structure."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(textwrap.dedent("""\
        def hello(name: str) -> str:
            \"\"\"Say hello.\"\"\"
            greeting = f"Hello, {name}!"
            formatted = greeting.upper()
            result = formatted + " welcome"
            log_entry = {"name": name, "greeting": result}
            print(log_entry)
            return result

        def add(a: int, b: int) -> int:
            return a + b
    """))
    (tmp_path / "src" / "utils.py").write_text(textwrap.dedent("""\
        import os

        def read_config(path: str) -> dict:
            with open(path) as f:
                return {}
    """))
    (tmp_path / "README.md").write_text("# My project\n")
    return tmp_path


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    """Create a fresh SessionStore in a temp directory."""
    db_path = tmp_path / "test_session.db"
    s = SessionStore(db_path=db_path)
    yield s
    s.close()


@pytest.fixture()
def populated_store(store: SessionStore) -> SessionStore:
    """Store pre-loaded with one episode, turns, decisions, and artifacts."""
    eid = store.create_episode(topic="auth module", intent="new_task")
    store.add_turn(eid, "user", "Create an auth module")
    store.add_turn(eid, "assistant", "Sure, I'll create auth.py with JWT.")
    store.add_turn(eid, "user", "Add token refresh too")
    store.add_code_ref(eid, "src/main.py", ref_type="read")
    store.add_code_ref(eid, "src/utils.py", ref_type="write")
    store.add_decision(eid, "Use JWT for auth", rationale="Industry standard")
    store.add_artifact(eid, "error", "Traceback: KeyError 'token'", name="err1")
    store.add_artifact(eid, "test_result", "FAILED test_auth::test_refresh", name="t1")
    return store


# ── FitLevel tests ───────────────────────────────────────────────────


class TestFitLevel:
    def test_enum_values(self):
        assert FitLevel.MINIMAL.value == "minimal"
        assert FitLevel.FULL.value == "full"

    def test_padding_mapping(self):
        assert FIT_PADDING[FitLevel.MINIMAL] == 0.0
        assert FIT_PADDING[FitLevel.BALANCED] == 0.25
        assert FIT_PADDING[FitLevel.FULL] == 1.0

    def test_is_str_enum(self):
        assert isinstance(FitLevel.TIGHT, str)
        assert FitLevel.TIGHT == "tight"


# ── Turn limit tests ─────────────────────────────────────────────────


class TestTurnLimit:
    def test_minimal_returns_1(self):
        assert _turn_limit(FitLevel.MINIMAL) == 1

    def test_balanced_returns_3(self):
        assert _turn_limit(FitLevel.BALANCED) == 3

    def test_full_returns_50(self):
        assert _turn_limit(FitLevel.FULL) == 50

    def test_all_levels_defined(self):
        for level in FitLevel:
            assert isinstance(_turn_limit(level), int)


# ── Repo structure tests ────────────────────────────────────────────


class TestRepoStructure:
    def test_generates_tree(self, tmp_repo: Path):
        tree = _repo_structure(tmp_repo, max_depth=2)
        assert tmp_repo.name in tree
        assert "src/" in tree
        assert "main.py" in tree
        assert "README.md" in tree

    def test_skips_hidden(self, tmp_repo: Path):
        (tmp_repo / ".git").mkdir()
        (tmp_repo / ".git" / "config").write_text("x")
        tree = _repo_structure(tmp_repo, max_depth=2)
        assert ".git" not in tree

    def test_skips_pycache(self, tmp_repo: Path):
        (tmp_repo / "__pycache__").mkdir()
        (tmp_repo / "__pycache__" / "mod.cpython-312.pyc").write_text("")
        tree = _repo_structure(tmp_repo, max_depth=2)
        assert "__pycache__" not in tree

    def test_max_depth_respected(self, tmp_repo: Path):
        deep = tmp_repo / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "deep.txt").write_text("x")
        tree = _repo_structure(tmp_repo, max_depth=1)
        assert "deep.txt" not in tree

    def test_empty_dir(self, tmp_path: Path):
        tree = _repo_structure(tmp_path, max_depth=2)
        assert tmp_path.name in tree


# ── File reading and stubbing ────────────────────────────────────────


class TestFileOps:
    def test_read_and_stub_files(self, tmp_repo: Path):
        result = _read_and_stub_files(
            ["src/main.py"], tmp_repo,
        )
        assert "# src/main.py" in result
        assert "def hello" in result
        # If tree-sitter is available, bodies are stubbed with "...".
        # If not (CI/minimal env), raw content is returned — both are valid.

    def test_read_and_stub_full_bodies(self, tmp_repo: Path):
        result = _read_and_stub_files(
            ["src/main.py"], tmp_repo,
            full_bodies=["src/main.py"],
        )
        assert "greeting = " in result  # body not stubbed

    def test_read_files_full(self, tmp_repo: Path):
        result = _read_files_full(["src/main.py"], tmp_repo)
        assert "# src/main.py" in result
        assert "greeting = " in result

    def test_missing_file_skipped(self, tmp_repo: Path):
        result = _read_and_stub_files(
            ["nonexistent.py", "src/main.py"], tmp_repo,
        )
        assert "nonexistent.py" not in result
        assert "src/main.py" in result

    def test_non_python_not_stubbed(self, tmp_repo: Path):
        result = _read_and_stub_files(["README.md"], tmp_repo)
        assert "# My project" in result


# ── Format helpers ───────────────────────────────────────────────────


class TestFormatters:
    def test_format_turns(self):
        turns = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = _format_turns(turns)
        assert "user: Hello" in result
        assert "assistant: Hi there" in result

    def test_format_stats(self):
        stats = {"episodes": 3, "turns": 10, "total_tokens": 500}
        result = _format_stats(stats)
        assert "episodes: 3" in result
        assert "turns: 10" in result


# ── Budget trimming ──────────────────────────────────────────────────


class TestTrimToBudget:
    def _cheap_count(self, text: str) -> int:
        return len(text) // 4

    def test_within_budget_unchanged(self):
        blocks = ["short", "text"]
        result = _trim_to_budget(blocks, 9999, self._cheap_count)
        assert result == blocks

    def test_over_budget_trims_from_end(self):
        blocks = ["a" * 100, "b" * 100, "c" * 100]
        result = _trim_to_budget(blocks, 60, self._cheap_count)
        assert len(result) < len(blocks)
        # First block should survive
        assert result[0] == "a" * 100

    def test_empty_blocks(self):
        assert _trim_to_budget([], 100, self._cheap_count) == []

    def test_zero_budget_returns_empty(self):
        blocks = ["some text here"]
        result = _trim_to_budget(blocks, 0, self._cheap_count)
        assert result == []


# ── AssembledContext ─────────────────────────────────────────────────


class TestAssembledContext:
    def test_full_context_joins_blocks(self):
        ctx = AssembledContext(
            system_prompt="sys",
            context_blocks=["block1", "block2"],
            user_message="hi",
            intent="new_task",
            fit_level="balanced",
            total_tokens=100,
            sources=["a"],
            budget_tokens=8192,
        )
        assert "block1" in ctx.full_context
        assert "block2" in ctx.full_context

    def test_full_context_skips_empty(self):
        ctx = AssembledContext(
            system_prompt="",
            context_blocks=["a", "", "  ", "b"],
            user_message="hi",
            intent="new_task",
            fit_level="balanced",
            total_tokens=50,
            sources=[],
            budget_tokens=8192,
        )
        # Empty / whitespace-only blocks excluded
        assert ctx.full_context == "a\n\nb"

    def test_as_messages_format(self):
        ctx = AssembledContext(
            system_prompt="You are helpful.",
            context_blocks=["<code>x</code>"],
            user_message="do stuff",
            intent="new_task",
            fit_level="balanced",
            total_tokens=50,
            sources=[],
            budget_tokens=8192,
        )
        msgs = ctx.as_messages()
        assert len(msgs) == 3
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "You are helpful."
        assert msgs[1]["role"] == "system"
        assert msgs[2]["role"] == "user"
        assert msgs[2]["content"] == "do stuff"

    def test_as_messages_no_system(self):
        ctx = AssembledContext(
            system_prompt="",
            context_blocks=[],
            user_message="hi",
            intent="question",
            fit_level="minimal",
            total_tokens=1,
            sources=[],
            budget_tokens=8192,
        )
        msgs = ctx.as_messages()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"


# ── Intent assembler integration tests ───────────────────────────────


class TestAssembleNewTask:
    def test_produces_repo_structure(self, store: SessionStore, tmp_repo: Path):
        c = _Classification(intent=_Intent("new_task"))
        ctx = assemble(c, "build me X", store, repo_root=tmp_repo)
        assert ctx.intent == "new_task"
        assert "repo_structure" in ctx.sources

    def test_includes_file_refs(self, store: SessionStore, tmp_repo: Path):
        c = _Classification(
            intent=_Intent("new_task"),
            file_refs=["src/main.py"],
        )
        ctx = assemble(c, "build me X", store, repo_root=tmp_repo)
        assert any("code" in s for s in ctx.sources)

    def test_no_repo_root(self, store: SessionStore):
        c = _Classification(intent=_Intent("new_task"))
        ctx = assemble(c, "hello", store)
        assert ctx.context_blocks == [] or "repo_structure" not in ctx.sources


class TestAssembleFollowUp:
    def test_includes_turns(self, populated_store: SessionStore):
        c = _Classification(intent=_Intent("follow_up"))
        ctx = assemble(c, "also do Y", populated_store)
        assert ctx.intent == "follow_up"
        assert any("turns" in s for s in ctx.sources)

    def test_includes_decisions(self, populated_store: SessionStore):
        c = _Classification(intent=_Intent("follow_up"))
        ctx = assemble(c, "also do Y", populated_store)
        assert any("decisions" in s for s in ctx.sources)

    def test_includes_files_touched(self, populated_store: SessionStore, tmp_repo: Path):
        c = _Classification(intent=_Intent("follow_up"))
        ctx = assemble(c, "also do Y", populated_store, repo_root=tmp_repo)
        assert any("code" in s for s in ctx.sources)

    def test_fit_level_affects_turns(self, populated_store: SessionStore):
        c = _Classification(intent=_Intent("follow_up"))
        ctx_min = assemble(c, "x", populated_store, fit=FitLevel.MINIMAL)
        ctx_gen = assemble(c, "x", populated_store, fit=FitLevel.GENEROUS)
        # Both should have turns, but generous may include more
        min_turns = [s for s in ctx_min.sources if s.startswith("turns:")]
        gen_turns = [s for s in ctx_gen.sources if s.startswith("turns:")]
        if min_turns and gen_turns:
            min_count = int(min_turns[0].split(":")[1])
            gen_count = int(gen_turns[0].split(":")[1])
            assert gen_count >= min_count

    def test_empty_store(self, store: SessionStore):
        c = _Classification(intent=_Intent("follow_up"))
        ctx = assemble(c, "continue", store)
        assert ctx.context_blocks == []


class TestAssembleDebug:
    def test_includes_error_trace(self, populated_store: SessionStore):
        c = _Classification(intent=_Intent("debug"))
        ctx = assemble(c, "fix the bug", populated_store)
        assert "error_trace" in ctx.sources

    def test_includes_test_results(self, populated_store: SessionStore):
        c = _Classification(intent=_Intent("debug"))
        ctx = assemble(c, "fix the bug", populated_store)
        assert "test_results" in ctx.sources

    def test_file_refs_full_bodies(self, populated_store: SessionStore, tmp_repo: Path):
        c = _Classification(
            intent=_Intent("debug"),
            file_refs=["src/main.py"],
        )
        ctx = assemble(c, "fix it", populated_store, repo_root=tmp_repo)
        assert any("code_full" in s for s in ctx.sources)

    def test_no_artifacts(self, store: SessionStore):
        eid = store.create_episode(topic="test", intent="debug")
        store.add_turn(eid, "user", "help")
        c = _Classification(intent=_Intent("debug"))
        ctx = assemble(c, "debug", store)
        assert "error_trace" not in ctx.sources


class TestAssembleQuestion:
    def test_basic_question(self, store: SessionStore):
        c = _Classification(intent=_Intent("question"))
        ctx = assemble(c, "what does X do?", store)
        assert ctx.intent == "question"

    def test_with_file_refs(self, store: SessionStore, tmp_repo: Path):
        c = _Classification(
            intent=_Intent("question"),
            file_refs=["src/main.py"],
        )
        ctx = assemble(c, "explain main.py", store, repo_root=tmp_repo)
        assert any("code_stubs" in s for s in ctx.sources)

    def test_with_decisions(self, populated_store: SessionStore):
        c = _Classification(intent=_Intent("question"))
        ctx = assemble(c, "why JWT?", populated_store)
        assert any("decisions" in s for s in ctx.sources)


class TestAssembleRefactor:
    def test_full_bodies_for_targets(self, populated_store: SessionStore, tmp_repo: Path):
        c = _Classification(
            intent=_Intent("refactor"),
            file_refs=["src/main.py"],
        )
        ctx = assemble(c, "refactor main", populated_store, repo_root=tmp_repo)
        assert any("code_full" in s for s in ctx.sources)

    def test_stubs_for_related(self, populated_store: SessionStore, tmp_repo: Path):
        c = _Classification(
            intent=_Intent("refactor"),
            file_refs=["src/main.py"],
        )
        ctx = assemble(c, "refactor main", populated_store, repo_root=tmp_repo)
        # src/utils.py was touched in the episode but not a target
        assert any("stubs" in s for s in ctx.sources)


class TestAssembleContinue:
    def test_last_turn(self, populated_store: SessionStore):
        c = _Classification(intent=_Intent("continue"))
        ctx = assemble(c, "go on", populated_store)
        assert "last_turn" in ctx.sources

    def test_empty_store(self, store: SessionStore):
        c = _Classification(intent=_Intent("continue"))
        ctx = assemble(c, "go on", store)
        assert ctx.context_blocks == []


class TestAssemblePivot:
    def test_repo_structure_only(self, store: SessionStore, tmp_repo: Path):
        c = _Classification(intent=_Intent("pivot"))
        ctx = assemble(c, "new topic", store, repo_root=tmp_repo)
        assert "repo_structure" in ctx.sources
        assert len(ctx.sources) == 1

    def test_no_repo(self, store: SessionStore):
        c = _Classification(intent=_Intent("pivot"))
        ctx = assemble(c, "new topic", store)
        assert ctx.context_blocks == []


class TestAssembleMeta:
    def test_includes_stats(self, store: SessionStore):
        c = _Classification(intent=_Intent("meta"))
        ctx = assemble(c, "session info", store)
        assert "stats" in ctx.sources

    def test_includes_episodes(self, populated_store: SessionStore):
        c = _Classification(intent=_Intent("meta"))
        ctx = assemble(c, "session info", populated_store)
        assert any("episodes" in s for s in ctx.sources)


# ── Budget and compression ───────────────────────────────────────────


class TestBudget:
    def test_system_prompt_counted(self, store: SessionStore):
        c = _Classification(intent=_Intent("new_task"))
        ctx = assemble(
            c, "hi", store,
            system_prompt="You are a helpful assistant." * 10,
            budget_tokens=50,
        )
        assert ctx.total_tokens > 0

    def test_fit_level_in_result(self, store: SessionStore):
        c = _Classification(intent=_Intent("new_task"))
        ctx = assemble(c, "hi", store, fit=FitLevel.TIGHT)
        assert ctx.fit_level == "tight"

    def test_budget_in_result(self, store: SessionStore):
        c = _Classification(intent=_Intent("new_task"))
        ctx = assemble(c, "hi", store, budget_tokens=4096)
        assert ctx.budget_tokens == 4096


class TestUnknownIntent:
    def test_falls_back_to_new_task(self, store: SessionStore, tmp_repo: Path):
        c = _Classification(intent=_Intent("unknown_intent_xyz"))
        ctx = assemble(c, "do something", store, repo_root=tmp_repo)
        # Should not crash — falls back to new_task assembler
        assert "repo_structure" in ctx.sources
