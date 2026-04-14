"""
Context assembler for the dynamic context system.

Given a classified request (intent + refs) and a session store, assemble
the minimal context needed for the LLM.  Replaces "send everything" with
"send only what's relevant".
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .classifier import Classification, Intent
    from .store import SessionStore

# ── Lazy imports with graceful fallback ──────────────────────────────

_count_tokens_fn = None
_stub_functions_fn = None
_compact_fn = None
_compress_fn = None


def _get_count_tokens():
    global _count_tokens_fn
    if _count_tokens_fn is None:
        try:
            from codeclub.compress.tokens import count_tokens
            _count_tokens_fn = count_tokens
        except Exception:
            _count_tokens_fn = lambda text: len(text) // 4
    return _count_tokens_fn


def _get_stub_functions():
    global _stub_functions_fn
    if _stub_functions_fn is None:
        try:
            from codeclub.compress.tree import stub_functions
            _stub_functions_fn = stub_functions
        except Exception:
            _stub_functions_fn = None
    return _stub_functions_fn


def _get_compact():
    global _compact_fn
    if _compact_fn is None:
        try:
            from codeclub.compress.compact import compact
            _compact_fn = compact
        except Exception:
            _compact_fn = None
    return _compact_fn


def _get_compress():
    global _compress_fn
    if _compress_fn is None:
        try:
            from codeclub.compress.compressor import compress
            _compress_fn = compress
        except Exception:
            _compress_fn = None
    return _compress_fn


# ── Fit precision levels ─────────────────────────────────────────────


class FitLevel(str, Enum):
    MINIMAL = "minimal"     # exact matches only, 0% padding
    TIGHT = "tight"         # direct refs + 1 hop, 10% padding
    BALANCED = "balanced"   # topic cluster + related, 25% padding
    GENEROUS = "generous"   # wide semantic search, 50% padding
    FULL = "full"           # everything, no filtering


FIT_PADDING: dict[FitLevel, float] = {
    FitLevel.MINIMAL: 0.0,
    FitLevel.TIGHT: 0.10,
    FitLevel.BALANCED: 0.25,
    FitLevel.GENEROUS: 0.50,
    FitLevel.FULL: 1.0,
}

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".tox",
              ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
              ".eggs", "*.egg-info"}


# ── Result dataclass ─────────────────────────────────────────────────


@dataclass
class AssembledContext:
    """The assembled context ready to send to the model."""

    system_prompt: str
    context_blocks: list[str]
    user_message: str

    # Metadata
    intent: str
    fit_level: str
    total_tokens: int
    sources: list[str]
    budget_tokens: int

    @property
    def full_context(self) -> str:
        """Combined context blocks as a single string."""
        return "\n\n".join(b for b in self.context_blocks if b.strip())

    def as_messages(self) -> list[dict]:
        """Convert to OpenAI messages format."""
        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        if self.context_blocks:
            messages.append({"role": "system", "content": self.full_context})
        messages.append({"role": "user", "content": self.user_message})
        return messages


# ── Intent dispatch table ────────────────────────────────────────────

_INTENT_ASSEMBLERS: dict[str, callable] = {}


def _register(intent_name: str):
    """Decorator to register an assembler for an intent."""
    def decorator(fn):
        _INTENT_ASSEMBLERS[intent_name] = fn
        return fn
    return decorator


# ── Per-intent assemblers ────────────────────────────────────────────


@_register("new_task")
def _assemble_new_task(
    classification: Classification,
    store: SessionStore,
    budget: int,
    fit: FitLevel,
    repo_root: str | Path | None,
) -> tuple[list[str], list[str]]:
    """New task: repo structure + relevant stubs by keyword."""
    blocks: list[str] = []
    sources: list[str] = []

    if repo_root:
        structure = _repo_structure(repo_root, max_depth=2)
        blocks.append(f"<repo_structure>\n{structure}\n</repo_structure>")
        sources.append("repo_structure")

    if classification.file_refs:
        code = _read_and_stub_files(classification.file_refs, repo_root)
        blocks.append(f"<code>\n{code}\n</code>")
        sources.append(f"code:{','.join(classification.file_refs)}")

    return blocks, sources


@_register("follow_up")
def _assemble_follow_up(
    classification: Classification,
    store: SessionStore,
    budget: int,
    fit: FitLevel,
    repo_root: str | Path | None,
) -> tuple[list[str], list[str]]:
    """Follow-up: files from current episode + recent turns."""
    blocks: list[str] = []
    sources: list[str] = []

    ep = store.active_episode()
    if ep:
        turns = store.episode_turns(ep["id"], limit=_turn_limit(fit))
        if turns:
            turn_text = _format_turns(turns)
            blocks.append(
                f"<recent_conversation>\n{turn_text}\n</recent_conversation>",
            )
            sources.append(f"turns:{len(turns)}")

        files = store.files_touched(ep["id"])
        if files and repo_root:
            code = _read_and_stub_files(
                list(files), repo_root,
                full_bodies=classification.file_refs,
            )
            blocks.append(f"<code>\n{code}\n</code>")
            sources.append(f"code:{len(files)}files")

        decisions = store.active_decisions(ep["id"])
        if decisions:
            dec_text = "\n".join(f"- {d['decision']}" for d in decisions)
            blocks.append(f"<decisions>\n{dec_text}\n</decisions>")
            sources.append(f"decisions:{len(decisions)}")

    return blocks, sources


@_register("debug")
def _assemble_debug(
    classification: Classification,
    store: SessionStore,
    budget: int,
    fit: FitLevel,
    repo_root: str | Path | None,
) -> tuple[list[str], list[str]]:
    """Debug: failing files + tests + error trace + recent changes."""
    blocks: list[str] = []
    sources: list[str] = []

    ep = store.active_episode()
    if ep:
        errors = store.episode_artifacts(ep["id"], artifact_type="error")
        if errors:
            latest = errors[-1]
            blocks.append(f"<error>\n{latest['content']}\n</error>")
            sources.append("error_trace")

        tests = store.episode_artifacts(ep["id"], artifact_type="test_result")
        if tests:
            latest = tests[-1]
            blocks.append(
                f"<test_results>\n{latest['content']}\n</test_results>",
            )
            sources.append("test_results")

    if classification.file_refs and repo_root:
        code = _read_files_full(classification.file_refs, repo_root)
        blocks.append(f"<code>\n{code}\n</code>")
        sources.append(f"code_full:{','.join(classification.file_refs)}")

    if ep:
        turns = store.episode_turns(ep["id"], limit=2)
        if turns:
            blocks.append(
                f"<recent_conversation>\n{_format_turns(turns)}\n</recent_conversation>",
            )
            sources.append(f"turns:{len(turns)}")

    return blocks, sources


@_register("question")
def _assemble_question(
    classification: Classification,
    store: SessionStore,
    budget: int,
    fit: FitLevel,
    repo_root: str | Path | None,
) -> tuple[list[str], list[str]]:
    """Question: relevant stubs + decisions."""
    blocks: list[str] = []
    sources: list[str] = []

    if classification.symbol_refs or classification.file_refs:
        query = " ".join(classification.symbol_refs + classification.file_refs)
        try:
            results = store.search(query, limit=5)
        except Exception:
            results = []
        if results:
            search_text = "\n".join(r["content"][:500] for r in results)
            blocks.append(
                f"<relevant_context>\n{search_text}\n</relevant_context>",
            )
            sources.append(f"search:{len(results)}")

    if classification.file_refs and repo_root:
        code = _read_and_stub_files(classification.file_refs, repo_root)
        blocks.append(f"<code>\n{code}\n</code>")
        sources.append(f"code_stubs:{len(classification.file_refs)}")

    ep = store.active_episode()
    if ep:
        decisions = store.active_decisions(ep["id"])
        if decisions:
            dec_text = "\n".join(
                f"- {d['decision']}: {d.get('rationale', '')}" for d in decisions
            )
            blocks.append(f"<decisions>\n{dec_text}\n</decisions>")
            sources.append(f"decisions:{len(decisions)}")

    return blocks, sources


@_register("refactor")
def _assemble_refactor(
    classification: Classification,
    store: SessionStore,
    budget: int,
    fit: FitLevel,
    repo_root: str | Path | None,
) -> tuple[list[str], list[str]]:
    """Refactor: target files full + dependents as stubs."""
    blocks: list[str] = []
    sources: list[str] = []

    # Target files get full bodies (need to see the code to refactor)
    if classification.file_refs and repo_root:
        code = _read_files_full(classification.file_refs, repo_root)
        blocks.append(f"<code>\n{code}\n</code>")
        sources.append(f"code_full:{','.join(classification.file_refs)}")

    # Files touched in the episode as stubs (dependents / related)
    ep = store.active_episode()
    if ep:
        files = store.files_touched(ep["id"])
        stub_files = [f for f in files if f not in set(classification.file_refs)]
        if stub_files and repo_root:
            code = _read_and_stub_files(stub_files, repo_root)
            blocks.append(f"<related_code>\n{code}\n</related_code>")
            sources.append(f"stubs:{len(stub_files)}files")

        decisions = store.active_decisions(ep["id"])
        if decisions:
            dec_text = "\n".join(f"- {d['decision']}" for d in decisions)
            blocks.append(f"<decisions>\n{dec_text}\n</decisions>")
            sources.append(f"decisions:{len(decisions)}")

    return blocks, sources


@_register("continue")
def _assemble_continue(
    classification: Classification,
    store: SessionStore,
    budget: int,
    fit: FitLevel,
    repo_root: str | Path | None,
) -> tuple[list[str], list[str]]:
    """Continue: same context as last turn."""
    blocks: list[str] = []
    sources: list[str] = []

    ep = store.active_episode()
    if ep:
        turns = store.episode_turns(ep["id"], limit=1)
        if turns:
            blocks.append(
                f"<recent_conversation>\n{_format_turns(turns)}\n</recent_conversation>",
            )
            sources.append("last_turn")

    return blocks, sources


@_register("pivot")
def _assemble_pivot(
    classification: Classification,
    store: SessionStore,
    budget: int,
    fit: FitLevel,
    repo_root: str | Path | None,
) -> tuple[list[str], list[str]]:
    """Pivot: clean slate, maybe repo structure."""
    blocks: list[str] = []
    sources: list[str] = []

    if repo_root:
        structure = _repo_structure(repo_root, max_depth=2)
        blocks.append(f"<repo_structure>\n{structure}\n</repo_structure>")
        sources.append("repo_structure")

    return blocks, sources


@_register("meta")
def _assemble_meta(
    classification: Classification,
    store: SessionStore,
    budget: int,
    fit: FitLevel,
    repo_root: str | Path | None,
) -> tuple[list[str], list[str]]:
    """Meta: episode summaries + session stats."""
    blocks: list[str] = []
    sources: list[str] = []

    episodes = store.list_episodes(limit=20)
    if episodes:
        ep_text = "\n".join(
            f"- {e['topic']} ({e['intent']}, "
            f"{'active' if not e.get('closed_at') else 'closed'})"
            for e in episodes
        )
        blocks.append(f"<session_history>\n{ep_text}\n</session_history>")
        sources.append(f"episodes:{len(episodes)}")

    stats = store.session_stats()
    blocks.append(f"<session_stats>\n{_format_stats(stats)}\n</session_stats>")
    sources.append("stats")

    return blocks, sources


# ── Main entry point ─────────────────────────────────────────────────


def assemble(
    classification: Classification,
    message: str,
    store: SessionStore,
    *,
    fit: FitLevel = FitLevel.BALANCED,
    budget_tokens: int = 8192,
    repo_root: str | Path | None = None,
    system_prompt: str = "",
) -> AssembledContext:
    """
    Assemble context for a classified request.

    Pulls relevant context from the session store and code index
    based on the intent category and fit precision level.
    """
    count_fn = _get_count_tokens()

    # 1. Resolve effective budget with fit-level padding
    padding = FIT_PADDING.get(fit, 0.25)
    effective_budget = int(budget_tokens * (1 + padding))

    # 2. Dispatch to intent-specific assembler
    intent_name = classification.intent.value if hasattr(classification.intent, "value") else str(classification.intent)
    assembler_fn = _INTENT_ASSEMBLERS.get(intent_name, _assemble_new_task)
    blocks, sources = assembler_fn(classification, store, effective_budget, fit, repo_root)

    # 3. Account for system prompt + user message in the budget
    reserved = count_fn(system_prompt) + count_fn(message)
    remaining_budget = max(0, effective_budget - reserved)

    # 4. Trim blocks to fit within remaining budget
    blocks = _trim_to_budget(blocks, remaining_budget, count_fn)

    # 5. If still over budget, apply compression pipeline
    total_block_tokens = sum(count_fn(b) for b in blocks)
    if total_block_tokens > remaining_budget:
        blocks = _compress_blocks(blocks)
        blocks = _trim_to_budget(blocks, remaining_budget, count_fn)

    # 6. Compute final token count
    total_tokens = reserved + sum(count_fn(b) for b in blocks)

    return AssembledContext(
        system_prompt=system_prompt,
        context_blocks=blocks,
        user_message=message,
        intent=intent_name,
        fit_level=fit.value,
        total_tokens=total_tokens,
        sources=sources,
        budget_tokens=budget_tokens,
    )


# ── Helper functions ─────────────────────────────────────────────────


def _turn_limit(fit: FitLevel) -> int:
    """How many recent turns to include based on fit level."""
    return {
        FitLevel.MINIMAL: 1,
        FitLevel.TIGHT: 2,
        FitLevel.BALANCED: 3,
        FitLevel.GENEROUS: 5,
        FitLevel.FULL: 50,
    }[fit]


def _repo_structure(repo_root: str | Path, max_depth: int = 2) -> str:
    """Generate a tree-like repo structure listing."""
    root = Path(repo_root)
    lines: list[str] = []

    def _walk(directory: Path, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        except PermissionError:
            return
        for entry in entries:
            if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                continue
            if entry.is_dir():
                lines.append(f"{prefix}{entry.name}/")
                _walk(entry, prefix + "  ", depth + 1)
            else:
                lines.append(f"{prefix}{entry.name}")

    lines.append(f"{root.name}/")
    _walk(root, "  ", 1)
    return "\n".join(lines)


def _read_and_stub_files(
    file_paths: list[str],
    repo_root: str | Path | None,
    full_bodies: list[str] | None = None,
) -> str:
    """
    Read files and stub them (remove function bodies, keep signatures).
    Files in *full_bodies* are returned without stubbing.
    Uses codeclub.compress.tree.stub_functions for Python files.
    """
    full_set = set(full_bodies) if full_bodies else set()
    stub_fn = _get_stub_functions()
    parts: list[str] = []

    for fpath in file_paths:
        content = _safe_read(fpath, repo_root)
        if content is None:
            continue

        if fpath in full_set or stub_fn is None or not fpath.endswith(".py"):
            parts.append(f"# {fpath}\n{content}")
        else:
            stubbed, _smap = stub_fn(content)
            parts.append(f"# {fpath}\n{stubbed}")

    return "\n\n".join(parts)


def _read_files_full(file_paths: list[str], repo_root: str | Path | None) -> str:
    """Read files and return full content."""
    parts: list[str] = []
    for fpath in file_paths:
        content = _safe_read(fpath, repo_root)
        if content is None:
            continue
        parts.append(f"# {fpath}\n{content}")
    return "\n\n".join(parts)


def _safe_read(file_path: str, repo_root: str | Path | None) -> str | None:
    """Safely read a file, returning None on failure."""
    candidates = [Path(file_path)]
    if repo_root:
        candidates.insert(0, Path(repo_root) / file_path)
    for p in candidates:
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
    return None


def _format_turns(turns: list[dict]) -> str:
    """Format turns for inclusion in context."""
    parts: list[str] = []
    for t in turns:
        role = t.get("role", "unknown")
        content = t.get("content", "")
        parts.append(f"{role}: {content}")
    return "\n".join(parts)


def _format_stats(stats: dict) -> str:
    """Format session stats for display."""
    return "\n".join(f"{k}: {v}" for k, v in stats.items())


def _trim_to_budget(
    blocks: list[str],
    budget_tokens: int,
    count_fn: callable,
) -> list[str]:
    """
    Trim context blocks to fit within token budget.
    Removes blocks from the end (least important last) until within budget.
    """
    if not blocks:
        return blocks

    total = sum(count_fn(b) for b in blocks)
    if total <= budget_tokens:
        return blocks

    result = list(blocks)
    while result and sum(count_fn(b) for b in result) > budget_tokens:
        result.pop()
    return result


def _compress_blocks(blocks: list[str]) -> list[str]:
    """Apply compact + symbol compression to all blocks."""
    compact_fn = _get_compact()
    compress_fn = _get_compress()

    result: list[str] = []
    for block in blocks:
        text = block
        if compact_fn is not None:
            try:
                text = compact_fn(text)
            except Exception:
                pass
        if compress_fn is not None:
            try:
                text = compress_fn(text)
            except Exception:
                pass
        result.append(text)
    return result
