"""
compaction.py — Background episode compaction.

When episodes close, a background job:
1. Summarises the episode (turns, decisions, artifacts)
2. Extracts key decisions and code refs
3. Stores the summary back in the session store
4. Optionally prunes old turns (configurable retention)

This is non-blocking — the system works without summaries.
Compaction improves retrieval quality over time.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .store import SessionStore

logger = logging.getLogger("codeclub.compaction")


# ---------------------------------------------------------------------------
# Episode summary generation
# ---------------------------------------------------------------------------

_SUMMARY_PROMPT = """Summarise this conversation episode in 3-5 sentences.
Focus on: what was discussed, what was decided, what was built/changed, and any open issues.

Episode topic: {topic}

Conversation:
{turns}

{decisions_block}
{artifacts_block}

Summary (3-5 sentences, factual, no fluff):"""


def _build_summary_prompt(
    topic: str,
    turns: list[dict],
    decisions: list[dict],
    artifacts: list[dict],
) -> str:
    """Build the compaction prompt for an episode."""
    turn_lines = []
    for t in turns:
        content = t["content"]
        if len(content) > 500:
            content = content[:500] + "..."
        turn_lines.append(f"{t['role']}: {content}")
    turns_text = "\n".join(turn_lines)

    decisions_block = ""
    if decisions:
        dec_lines = [f"- {d['decision']}" for d in decisions]
        decisions_block = "Decisions made:\n" + "\n".join(dec_lines)

    artifacts_block = ""
    if artifacts:
        art_lines = [f"- [{a['artifact_type']}] {a.get('name', 'unnamed')}" for a in artifacts]
        artifacts_block = "Artifacts:\n" + "\n".join(art_lines)

    return _SUMMARY_PROMPT.format(
        topic=topic,
        turns=turns_text,
        decisions_block=decisions_block,
        artifacts_block=artifacts_block,
    )


def _heuristic_summary(
    topic: str,
    turns: list[dict],
    decisions: list[dict],
    artifacts: list[dict],
) -> str:
    """
    Generate a summary WITHOUT a model call.

    Extracts key information heuristically:
    - First and last user messages (topic start + conclusion)
    - All decisions
    - Artifact types and names
    - Turn count and duration
    """
    parts = []

    parts.append(f"Episode: {topic}.")

    user_turns = [t for t in turns if t["role"] == "user"]
    if user_turns:
        parts.append(f"{len(turns)} turns ({len(user_turns)} from user).")

    if user_turns:
        first = user_turns[0]["content"][:200]
        parts.append(f"Started with: {first}")
        if len(user_turns) > 1:
            last = user_turns[-1]["content"][:200]
            parts.append(f"Ended with: {last}")

    if decisions:
        dec_text = "; ".join(d["decision"] for d in decisions[:5])
        parts.append(f"Decisions: {dec_text}.")

    if artifacts:
        by_type: dict[str, list[str]] = {}
        for a in artifacts:
            by_type.setdefault(a["artifact_type"], []).append(a.get("name", "unnamed"))
        art_parts = [f"{len(names)} {atype}(s)" for atype, names in by_type.items()]
        parts.append(f"Produced: {', '.join(art_parts)}.")

    return " ".join(parts)


def compact_episode(
    store: SessionStore,
    episode_id: str,
    call_fn: Callable | None = None,
) -> str:
    """
    Generate and store a summary for a closed episode.

    Args:
        store: Session store
        episode_id: Episode to compact
        call_fn: Optional LLM call function. If None, uses heuristic summary.

    Returns:
        The generated summary text.
    """
    episode = store.get_episode(episode_id)
    if not episode:
        raise ValueError(f"Episode {episode_id} not found")

    turns = store.episode_turns(episode_id)
    decisions = store.active_decisions(episode_id)
    artifacts = store.episode_artifacts(episode_id)

    if call_fn:
        prompt = _build_summary_prompt(
            episode["topic"], turns, decisions, artifacts,
        )
        try:
            summary = call_fn(prompt)
            summary = summary.strip().strip('"')
        except Exception as e:
            logger.warning("Model summary failed, falling back to heuristic: %s", e)
            summary = _heuristic_summary(
                episode["topic"], turns, decisions, artifacts,
            )
    else:
        summary = _heuristic_summary(
            episode["topic"], turns, decisions, artifacts,
        )

    store.close_episode(episode_id, summary=summary)
    logger.info("Compacted episode %s: %d chars", episode_id[:8], len(summary))
    return summary


# ---------------------------------------------------------------------------
# Turn pruning
# ---------------------------------------------------------------------------

def prune_turns(
    store: SessionStore,
    max_age_days: float = 7.0,
    keep_if_no_summary: bool = True,
) -> int:
    """
    Prune old turns from episodes that have summaries.

    Only prunes turns from closed episodes that have a summary.
    If keep_if_no_summary is True (default), episodes without summaries
    keep all their turns.

    Returns count of pruned turns.

    NOTE: accesses store.conn directly since there is no delete_turns()
    method yet. A proper store method should be added later.
    """
    cutoff = time.time() - (max_age_days * 86400)

    episodes = store.list_episodes(limit=1000)
    pruned = 0

    for ep in episodes:
        if not ep.get("closed_at"):
            continue  # never prune active episodes
        if ep["closed_at"] > cutoff:
            continue  # too recent
        if keep_if_no_summary and not ep.get("summary"):
            continue  # no summary, keep turns

        conn = store.conn

        # Delete FTS entries first (while turns still exist for the subquery)
        conn.execute(
            "DELETE FROM session_fts WHERE source_type = 'turn' "
            "AND source_id IN (SELECT CAST(id AS TEXT) FROM turns WHERE episode_id = ?)",
            (ep["id"],),
        )

        # Then delete the turns themselves
        cursor = conn.execute(
            "DELETE FROM turns WHERE episode_id = ?", (ep["id"],),
        )
        pruned += cursor.rowcount
        conn.commit()

    if pruned:
        logger.info("Pruned %d old turns", pruned)
    return pruned


# ---------------------------------------------------------------------------
# Decision extraction (heuristic)
# ---------------------------------------------------------------------------

_DECISION_PATTERNS = [
    r"(?:let'?s|we should|going with|decided on|chose|using|picking)\s+(.+?)(?:\.|$)",
    r"(\w[\w\s]+?)\s+(?:is better|is preferred|makes more sense)",
    r"(?:the approach|the plan|the strategy|the solution)\s+is\s+(.+?)(?:\.|$)",
]

_DECISION_RES = [re.compile(p, re.I | re.M) for p in _DECISION_PATTERNS]


def extract_decisions(turns: list[dict]) -> list[str]:
    """
    Extract likely decisions from conversation turns using heuristics.

    Best-effort — catches explicit decision language but misses implicit
    decisions. Good enough for v1.
    """
    decisions = []
    for t in turns:
        if t["role"] != "assistant":
            continue
        content = t["content"]
        for pattern in _DECISION_RES:
            for m in pattern.finditer(content):
                decision = m.group(1).strip()
                if 10 < len(decision) < 200:
                    decisions.append(decision)
    return decisions


# ---------------------------------------------------------------------------
# Background compaction worker
# ---------------------------------------------------------------------------

@dataclass
class CompactionWorker:
    """
    Background worker that compacts closed episodes.

    Runs in a daemon thread, checking for uncompacted closed episodes
    at a configurable interval.

    Usage:
        worker = CompactionWorker(store)
        worker.start()
        # ... later ...
        worker.stop()
    """
    store: SessionStore
    call_fn: Callable | None = None
    check_interval_s: float = 60.0
    prune_age_days: float = 7.0

    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _compacted_count: int = field(default=0, init=False)
    _pruned_count: int = field(default=0, init=False)

    def start(self) -> None:
        """Start the background compaction thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="compaction-worker",
        )
        self._thread.start()
        logger.info("Compaction worker started (interval=%ds)", self.check_interval_s)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the background compaction thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info(
            "Compaction worker stopped (compacted=%d, pruned=%d)",
            self._compacted_count, self._pruned_count,
        )

    @property
    def stats(self) -> dict:
        return {
            "running": self._thread.is_alive() if self._thread else False,
            "compacted": self._compacted_count,
            "pruned": self._pruned_count,
        }

    def _run(self) -> None:
        """Main worker loop."""
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("Compaction tick failed")
            self._stop_event.wait(self.check_interval_s)

    def _tick(self) -> None:
        """One compaction cycle: find uncompacted episodes, summarise them."""
        episodes = self.store.list_episodes(limit=100)

        for ep in episodes:
            if self._stop_event.is_set():
                break

            # Only compact closed episodes without summaries
            if not ep.get("closed_at"):
                continue
            if ep.get("summary"):
                continue

            try:
                compact_episode(self.store, ep["id"], self.call_fn)
                self._compacted_count += 1
            except Exception:
                logger.exception("Failed to compact episode %s", ep["id"][:8])

        # Periodic pruning
        pruned = prune_turns(self.store, max_age_days=self.prune_age_days)
        self._pruned_count += pruned


def compact_all(
    store: SessionStore,
    call_fn: Callable | None = None,
) -> int:
    """
    Compact all closed episodes that don't have summaries yet.
    Synchronous — use for one-off compaction or testing.

    Returns number of episodes compacted.
    """
    episodes = store.list_episodes(limit=1000)
    count = 0
    for ep in episodes:
        if not ep.get("closed_at"):
            continue
        if ep.get("summary"):
            continue
        compact_episode(store, ep["id"], call_fn)
        count += 1
    return count
