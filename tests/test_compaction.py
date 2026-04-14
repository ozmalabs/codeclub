"""Tests for codeclub.context.compaction."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from codeclub.context.compaction import (
    CompactionWorker,
    _build_summary_prompt,
    _heuristic_summary,
    compact_all,
    compact_episode,
    extract_decisions,
    prune_turns,
)
from codeclub.context.store import SessionStore


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def store() -> SessionStore:
    """In-memory session store."""
    s = SessionStore(":memory:")
    yield s
    s.close()


def _make_episode(store: SessionStore, *, topic: str = "auth module", close: bool = False):
    """Create an episode with turns, decisions, and artifacts. Returns episode id."""
    eid = store.create_episode(topic=topic, intent="new_task")
    store.add_turn(eid, "user", "Create an auth module with JWT")
    store.add_turn(eid, "assistant", "Sure, I'll create auth.py with JWT tokens.")
    store.add_turn(eid, "user", "Add token refresh too")
    store.add_turn(eid, "assistant", "Done. Added refresh_token() method.")
    store.add_decision(eid, "Use JWT for auth", rationale="Industry standard")
    store.add_decision(eid, "Use bcrypt for hashing", rationale="Secure and battle-tested")
    store.add_artifact(eid, "code", "def auth(): ...", name="auth.py")
    store.add_artifact(eid, "test_result", "PASSED test_auth", name="test_auth")
    if close:
        store.close_episode(eid)
    return eid


# ── _heuristic_summary ───────────────────────────────────────────────


class TestHeuristicSummary:
    def test_basic_summary(self):
        turns = [
            {"role": "user", "content": "Build an auth module"},
            {"role": "assistant", "content": "I'll create JWT-based auth."},
            {"role": "user", "content": "Add refresh tokens"},
        ]
        decisions = [{"decision": "Use JWT for auth"}]
        artifacts = [{"artifact_type": "code", "name": "auth.py"}]

        result = _heuristic_summary("auth module", turns, decisions, artifacts)

        assert "Episode: auth module" in result
        assert "3 turns" in result
        assert "2 from user" in result
        assert "Started with: Build an auth module" in result
        assert "Ended with: Add refresh tokens" in result
        assert "Use JWT for auth" in result
        assert "1 code(s)" in result

    def test_no_decisions_or_artifacts(self):
        turns = [{"role": "user", "content": "Hello"}]
        result = _heuristic_summary("greeting", turns, [], [])
        assert "Episode: greeting" in result
        assert "Decisions" not in result
        assert "Produced" not in result

    def test_single_user_turn_no_ended_with(self):
        turns = [
            {"role": "user", "content": "Just one message"},
            {"role": "assistant", "content": "Got it"},
        ]
        result = _heuristic_summary("single", turns, [], [])
        assert "Started with" in result
        assert "Ended with" not in result

    def test_multiple_artifact_types(self):
        artifacts = [
            {"artifact_type": "code", "name": "a.py"},
            {"artifact_type": "code", "name": "b.py"},
            {"artifact_type": "test_result", "name": "t1"},
        ]
        result = _heuristic_summary("mixed", [], [], artifacts)
        assert "2 code(s)" in result
        assert "1 test_result(s)" in result

    def test_long_content_truncated(self):
        long_msg = "x" * 500
        turns = [{"role": "user", "content": long_msg}]
        result = _heuristic_summary("long", turns, [], [])
        # First 200 chars used
        assert len(result) < 500


# ── _build_summary_prompt ────────────────────────────────────────────


class TestBuildSummaryPrompt:
    def test_includes_topic(self):
        prompt = _build_summary_prompt("auth module", [], [], [])
        assert "Episode topic: auth module" in prompt

    def test_includes_turns(self):
        turns = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        prompt = _build_summary_prompt("topic", turns, [], [])
        assert "user: Hello" in prompt
        assert "assistant: Hi there" in prompt

    def test_truncates_long_turns(self):
        turns = [{"role": "user", "content": "x" * 1000}]
        prompt = _build_summary_prompt("topic", turns, [], [])
        assert "..." in prompt
        # Should not contain full 1000 chars of content
        assert "x" * 600 not in prompt

    def test_includes_decisions(self):
        decisions = [{"decision": "Use JWT"}]
        prompt = _build_summary_prompt("topic", [], decisions, [])
        assert "Decisions made:" in prompt
        assert "- Use JWT" in prompt

    def test_includes_artifacts(self):
        artifacts = [{"artifact_type": "code", "name": "auth.py"}]
        prompt = _build_summary_prompt("topic", [], [], artifacts)
        assert "Artifacts:" in prompt
        assert "[code] auth.py" in prompt

    def test_no_decisions_block_when_empty(self):
        prompt = _build_summary_prompt("topic", [], [], [])
        assert "Decisions made:" not in prompt

    def test_unnamed_artifact(self):
        artifacts = [{"artifact_type": "error"}]
        prompt = _build_summary_prompt("topic", [], [], artifacts)
        assert "[error] unnamed" in prompt


# ── compact_episode ──────────────────────────────────────────────────


class TestCompactEpisode:
    def test_heuristic_when_no_call_fn(self, store):
        eid = _make_episode(store, close=True)
        summary = compact_episode(store, eid)

        assert "Episode: auth module" in summary
        ep = store.get_episode(eid)
        assert ep["summary"] == summary

    def test_calls_llm_when_provided(self, store):
        eid = _make_episode(store, close=True)
        mock_fn = MagicMock(return_value="LLM generated summary of the episode.")

        summary = compact_episode(store, eid, call_fn=mock_fn)

        assert summary == "LLM generated summary of the episode."
        mock_fn.assert_called_once()
        # Verify prompt was passed
        prompt_arg = mock_fn.call_args[0][0]
        assert "auth module" in prompt_arg

    def test_fallback_on_llm_failure(self, store):
        eid = _make_episode(store, close=True)
        failing_fn = MagicMock(side_effect=RuntimeError("API timeout"))

        summary = compact_episode(store, eid, call_fn=failing_fn)

        # Should fall back to heuristic
        assert "Episode: auth module" in summary
        ep = store.get_episode(eid)
        assert ep["summary"] is not None

    def test_strips_quotes_from_llm_response(self, store):
        eid = _make_episode(store, close=True)
        mock_fn = MagicMock(return_value='  "A quoted summary."  ')

        summary = compact_episode(store, eid, call_fn=mock_fn)
        assert summary == "A quoted summary."

    def test_raises_for_missing_episode(self, store):
        with pytest.raises(ValueError, match="not found"):
            compact_episode(store, "nonexistent-id")

    def test_stores_summary_in_episode(self, store):
        eid = _make_episode(store, close=True)
        compact_episode(store, eid)

        ep = store.get_episode(eid)
        assert ep["summary"] is not None
        assert len(ep["summary"]) > 0


# ── extract_decisions ────────────────────────────────────────────────


class TestExtractDecisions:
    def test_finds_lets_use_pattern(self):
        turns = [
            {"role": "assistant", "content": "Let's use SQLite for the database."},
        ]
        result = extract_decisions(turns)
        assert any("SQLite for the database" in d for d in result)

    def test_finds_going_with_pattern(self):
        turns = [
            {"role": "assistant", "content": "Going with a microservices architecture for this project."},
        ]
        result = extract_decisions(turns)
        assert any("microservices" in d for d in result)

    def test_finds_is_better_pattern(self):
        turns = [
            {"role": "assistant", "content": "The async approach is better because it scales."},
        ]
        result = extract_decisions(turns)
        assert any("async approach" in d for d in result)

    def test_finds_the_approach_is_pattern(self):
        turns = [
            {"role": "assistant", "content": "The approach is to use event sourcing for state management."},
        ]
        result = extract_decisions(turns)
        assert any("event sourcing" in d for d in result)

    def test_ignores_user_turns(self):
        turns = [
            {"role": "user", "content": "Let's use PostgreSQL for the database."},
        ]
        result = extract_decisions(turns)
        assert result == []

    def test_ignores_very_short_matches(self):
        turns = [
            {"role": "assistant", "content": "Let's use X."},
        ]
        result = extract_decisions(turns)
        # "X" is too short (len 1, below threshold of 10)
        assert result == []

    def test_ignores_very_long_matches(self):
        long = "a " * 150  # 300 chars
        turns = [
            {"role": "assistant", "content": f"Let's use {long}."},
        ]
        result = extract_decisions(turns)
        # Too long (> 200)
        assert result == []

    def test_multiple_decisions(self):
        turns = [
            {"role": "assistant", "content": (
                "Let's use JWT for authentication. "
                "Going with PostgreSQL for the database."
            )},
        ]
        result = extract_decisions(turns)
        assert len(result) >= 2


# ── prune_turns ──────────────────────────────────────────────────────


class TestPruneTurns:
    def test_prunes_old_closed_with_summary(self, store):
        eid = _make_episode(store, close=True)
        # Add a summary
        store.close_episode(eid, summary="Test summary")
        # Backdate the closed_at to make it old enough
        store.conn.execute(
            "UPDATE episodes SET closed_at = ? WHERE id = ?",
            (time.time() - 8 * 86400, eid),
        )
        store.conn.commit()

        turns_before = store.episode_turns(eid)
        assert len(turns_before) > 0

        pruned = prune_turns(store, max_age_days=7.0)

        assert pruned == len(turns_before)
        turns_after = store.episode_turns(eid)
        assert len(turns_after) == 0

        # Episode record and summary still exist
        ep = store.get_episode(eid)
        assert ep is not None
        assert ep["summary"] == "Test summary"

    def test_keeps_active_episodes(self, store):
        eid = _make_episode(store, close=False)

        pruned = prune_turns(store, max_age_days=0.0)
        assert pruned == 0
        assert len(store.episode_turns(eid)) > 0

    def test_keeps_episodes_without_summary(self, store):
        eid = _make_episode(store, close=True)
        # Backdate to make old
        store.conn.execute(
            "UPDATE episodes SET closed_at = ? WHERE id = ?",
            (time.time() - 30 * 86400, eid),
        )
        store.conn.commit()

        pruned = prune_turns(store, max_age_days=7.0, keep_if_no_summary=True)
        assert pruned == 0
        assert len(store.episode_turns(eid)) > 0

    def test_prunes_without_summary_when_flag_off(self, store):
        eid = _make_episode(store, close=True)
        store.conn.execute(
            "UPDATE episodes SET closed_at = ? WHERE id = ?",
            (time.time() - 30 * 86400, eid),
        )
        store.conn.commit()

        turns_before = len(store.episode_turns(eid))
        pruned = prune_turns(store, max_age_days=7.0, keep_if_no_summary=False)
        assert pruned == turns_before

    def test_keeps_recent_episodes(self, store):
        eid = _make_episode(store, close=True)
        store.close_episode(eid, summary="Summary")

        pruned = prune_turns(store, max_age_days=7.0)
        assert pruned == 0
        assert len(store.episode_turns(eid)) > 0

    def test_fts_entries_cleaned(self, store):
        eid = _make_episode(store, close=True)
        store.close_episode(eid, summary="Summary")
        store.conn.execute(
            "UPDATE episodes SET closed_at = ? WHERE id = ?",
            (time.time() - 10 * 86400, eid),
        )
        store.conn.commit()

        # Verify FTS entries exist before pruning
        fts_before = store.conn.execute(
            "SELECT COUNT(*) FROM session_fts WHERE source_type = 'turn'",
        ).fetchone()[0]
        assert fts_before > 0

        prune_turns(store, max_age_days=7.0)

        fts_after = store.conn.execute(
            "SELECT COUNT(*) FROM session_fts WHERE source_type = 'turn'",
        ).fetchone()[0]
        assert fts_after == 0


# ── compact_all ──────────────────────────────────────────────────────


class TestCompactAll:
    def test_compacts_multiple_episodes(self, store):
        eid1 = _make_episode(store, topic="auth", close=True)
        eid2 = _make_episode(store, topic="database", close=True)
        _make_episode(store, topic="active", close=False)  # should be skipped

        count = compact_all(store)

        assert count == 2
        assert store.get_episode(eid1)["summary"] is not None
        assert store.get_episode(eid2)["summary"] is not None

    def test_skips_already_compacted(self, store):
        eid = _make_episode(store, close=True)
        compact_episode(store, eid)

        count = compact_all(store)
        assert count == 0

    def test_with_call_fn(self, store):
        _make_episode(store, close=True)
        mock_fn = MagicMock(return_value="LLM summary")

        count = compact_all(store, call_fn=mock_fn)

        assert count == 1
        mock_fn.assert_called_once()


# ── CompactionWorker ─────────────────────────────────────────────────


class TestCompactionWorker:
    """Worker tests use file-backed stores so SQLite allows cross-thread access."""

    @pytest.fixture()
    def threadsafe_store(self, tmp_path):
        """File-backed store that works across threads."""
        db_path = tmp_path / "worker_test.db"
        s = SessionStore(db_path)
        # Allow the connection to be used from the daemon thread
        s.conn.close()
        s.conn = __import__("sqlite3").connect(str(db_path), check_same_thread=False)
        s.conn.row_factory = __import__("sqlite3").Row
        s.conn.execute("PRAGMA journal_mode=WAL")
        yield s
        s.close()

    def test_start_stop(self, threadsafe_store):
        worker = CompactionWorker(threadsafe_store, check_interval_s=0.1)
        worker.start()
        assert worker.stats["running"] is True
        worker.stop(timeout=2.0)
        assert worker.stats["running"] is False

    def test_idempotent_start(self, threadsafe_store):
        worker = CompactionWorker(threadsafe_store, check_interval_s=0.1)
        worker.start()
        worker.start()  # second start should be a no-op
        assert worker.stats["running"] is True
        worker.stop(timeout=2.0)

    def test_compacts_in_background(self, threadsafe_store):
        eid = _make_episode(threadsafe_store, close=True)

        worker = CompactionWorker(threadsafe_store, check_interval_s=0.05)
        worker.start()

        # Wait for compaction to happen
        deadline = time.time() + 3.0
        while time.time() < deadline:
            ep = threadsafe_store.get_episode(eid)
            if ep and ep.get("summary"):
                break
            time.sleep(0.05)

        worker.stop(timeout=2.0)

        ep = threadsafe_store.get_episode(eid)
        assert ep["summary"] is not None
        assert worker.stats["compacted"] >= 1

    def test_stats_accumulate(self, threadsafe_store):
        _make_episode(threadsafe_store, close=True)
        _make_episode(threadsafe_store, close=True)

        worker = CompactionWorker(threadsafe_store, check_interval_s=0.05)
        worker.start()

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if worker.stats["compacted"] >= 2:
                break
            time.sleep(0.05)

        worker.stop(timeout=2.0)
        assert worker.stats["compacted"] >= 2
