"""
Heuristic request classifier for the dynamic context system.

Classifies incoming user messages into intent categories that determine
what context to assemble.  Pure pattern matching + keyword scoring — no
model call needed.  Also estimates task clarity.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


# ── Intent taxonomy ──────────────────────────────────────────────────

class Intent(str, Enum):
    NEW_TASK = "new_task"
    FOLLOW_UP = "follow_up"
    DEBUG = "debug"
    QUESTION = "question"
    REFACTOR = "refactor"
    REVIEW = "review"
    EXPLORE = "explore"
    CONTINUE = "continue"
    PIVOT = "pivot"
    META = "meta"


@dataclass
class Classification:
    """Result of classifying a user request."""

    intent: Intent
    confidence: float
    clarity: int
    file_refs: list[str]
    symbol_refs: list[str]
    is_pivot: bool
    keywords: dict[str, int] = field(default_factory=dict)


# ── Keyword patterns per intent ──────────────────────────────────────

# Each entry maps Intent -> list of (pattern, weight) tuples.
# Patterns are compiled once at module load.

_KEYWORD_RULES: dict[Intent, list[tuple[re.Pattern[str], int]]] = {
    Intent.NEW_TASK: [
        (re.compile(r"\b(?:build|create|implement|write|make|add)\b", re.I), 3),
        (re.compile(r"\bnew\b", re.I), 2),
        (re.compile(r"^(?:build|create|implement|write|make|add)\b", re.I), 2),
        (re.compile(r"\bfrom scratch\b", re.I), 3),
    ],
    Intent.FOLLOW_UP: [
        (re.compile(r"\b(?:fix|change|update|modify|adjust|tweak)\b", re.I), 3),
        (re.compile(r"\b(?:also|then|next|now)\b", re.I), 1),
        (re.compile(r"\b(?:run|execute|rerun|re-run)\b", re.I), 2),
    ],
    Intent.DEBUG: [
        (re.compile(r"\b(?:error|failing|broken|bug|crash|traceback|exception)\b", re.I), 4),
        (re.compile(r"\b(?:doesn't work|not working|doesn't compile)\b", re.I), 3),
        (re.compile(r"\b(?:doesn't? make sense|wrong|incorrect|unexpected)\b", re.I), 3),
        (re.compile(r"(?:Error|Exception|Traceback):", re.I), 5),
        (re.compile(r"File \"[^\"]+\", line \d+", re.I), 5),
    ],
    Intent.QUESTION: [
        (re.compile(r"\?\s*$", re.M), 4),
        (re.compile(r"^(?:how|what|why|where|when|can|is|does|explain)\b", re.I), 3),
        (re.compile(r"\b(?:explain|describe|tell me about)\b", re.I), 2),
    ],
    Intent.REFACTOR: [
        (re.compile(r"\b(?:rename|extract|clean\s*up|reorganize|restructure|split|merge)\b", re.I), 4),
        (re.compile(r"\b(?:refactor|move|dedup(?:licate)?)\b", re.I), 4),
    ],
    Intent.REVIEW: [
        (re.compile(r"\b(?:review|audit|examine)\b", re.I), 4),
        (re.compile(r"\b(?:look at|check|inspect)\b", re.I), 2),
        (re.compile(r"\b(?:PR|pull request|diff)\b", re.I), 2),
    ],
    Intent.EXPLORE: [
        (re.compile(r"\b(?:find|search|grep|locate)\b", re.I), 3),
        (re.compile(r"\b(?:where is|show me|list)\b", re.I), 3),
        (re.compile(r"\b(?:what files|which files)\b", re.I), 3),
    ],
    Intent.CONTINUE: [],  # handled specially — short affirmative check
    Intent.PIVOT: [
        (re.compile(r"^(?:actually|instead)\b", re.I), 5),
        (re.compile(r"\b(?:forget that|let'?s switch|different topic|new thing)\b", re.I), 5),
        (re.compile(r"\b(?:never\s*mind|scratch that|on second thought)\b", re.I), 4),
    ],
    Intent.META: [
        (re.compile(r"\b(?:summary|status|progress|history)\b", re.I), 4),
        (re.compile(r"\b(?:what have we done|session|recap)\b", re.I), 4),
        (re.compile(r"\bhow many\b", re.I), 2),
    ],
}

_CONTINUE_PHRASES = {
    "yes", "ok", "okay", "do it", "go", "continue", "keep going",
    "next", "proceed", "sure", "yep", "yeah", "go ahead",
    "sounds good", "let's go", "ship it", "lgtm", "yup",
}

_CONTINUE_MAX_LEN = 30

# ── Reference extraction ─────────────────────────────────────────────

_FILE_RE = re.compile(
    r"(?:^|[\s`\"'(])"                    # boundary
    r"((?:[\w./-]+/)*[\w.-]+\.[a-zA-Z]{1,10})"  # path with extension
    r"(?=$|[\s`\"'),;:])",                 # boundary
    re.M,
)

_VERSION_RE = re.compile(r"^[vV]?\d+\.\d+(?:\.\d+)*$")
_NUMBER_RE = re.compile(r"^\d+\.\d+$")

_CAMEL_RE = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b")
_BACKTICK_RE = re.compile(r"`([^`\s][^`]*?)`")
_EXPLICIT_SYM_RE = re.compile(
    r"\bthe\s+(\w+)\s+(?:class|function|method|module|variable)\b", re.I,
)


def extract_file_refs(message: str) -> list[str]:
    """Extract file path references from a message."""
    seen: set[str] = set()
    result: list[str] = []
    for m in _FILE_RE.finditer(message):
        path = m.group(1)
        if _VERSION_RE.match(path) or _NUMBER_RE.match(path):
            continue
        if path.startswith(".") and "/" not in path and len(path) < 4:
            continue
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def extract_symbol_refs(message: str) -> list[str]:
    """Extract code symbol references from a message."""
    seen: set[str] = set()
    result: list[str] = []

    for m in _CAMEL_RE.finditer(message):
        sym = m.group(1)
        if sym not in seen:
            seen.add(sym)
            result.append(sym)

    for m in _BACKTICK_RE.finditer(message):
        inner = m.group(1).strip()
        # Skip paths (already captured by file extraction)
        if "/" in inner or (inner.count(".") == 1 and not inner[0].isupper()):
            continue
        if inner and inner not in seen:
            seen.add(inner)
            result.append(inner)

    for m in _EXPLICIT_SYM_RE.finditer(message):
        sym = m.group(1)
        if sym not in seen:
            seen.add(sym)
            result.append(sym)

    return result


def _has_error_traces(message: str) -> bool:
    """Return True if the message contains error/stack trace patterns."""
    return bool(
        re.search(r"(?:Error|Exception|Traceback):", message)
        or re.search(r'File "[^"]+", line \d+', message)
        or re.search(r"^\s+at\s+\S+\s+\(", message, re.M)
    )


# ── Clarity estimation ───────────────────────────────────────────────

_ALGO_RE = re.compile(
    r"\b(?:binary search|token bucket|leaky bucket|rate limit|trie|"
    r"bloom filter|hash map|linked list|BFS|DFS|dijkstra|"
    r"merge sort|quick sort|dynamic programming|memoiz|"
    r"pub.?sub|observer pattern|factory pattern|singleton|"
    r"dependency injection)\b",
    re.I,
)

_ACCEPTANCE_RE = re.compile(
    r"\b(?:should return|must return|expected output|test case|"
    r"given .+ when .+ then|assert|raises?)\b",
    re.I,
)

_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```|`[^`]+`")

_TECH_TERMS = re.compile(
    r"\b(?:async|await|generator|decorator|middleware|endpoint|"
    r"schema|migration|ORM|SQL|API|REST|gRPC|protobuf|"
    r"websocket|callback|promise|mutex|semaphore|thread|"
    r"coroutine|iterator|generic|interface|abstract|"
    r"serialize|deserialize|encode|decode|hash|encrypt|"
    r"queue|stack|heap|cach(?:e|ing)|index|shard|replica|"
    r"database|query|queries)\b",
    re.I,
)

_ASPIRATIONAL_RE = re.compile(
    r"\b(?:amazing|perfect|best possible|beautiful|elegant|world.?class|"
    r"state.?of.?the.?art|cutting.?edge|incredible|awesome)\b",
    re.I,
)

_VAGUE_RE = re.compile(
    r"^(?:make me (?:a|some) (?:thing|stuff)|build something|"
    r"do something|help me|make it (?:good|better|nice)|"
    r"write some code)\s*$",
    re.I,
)

_NOUN_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")


def estimate_clarity(message: str) -> int:
    """Estimate the spec clarity of a user request (0–100)."""
    score = 30
    words = message.split()
    word_count = len(words)

    if extract_symbol_refs(message):
        score += 15
    if _ALGO_RE.search(message):
        score += 10
    if _ACCEPTANCE_RE.search(message):
        score += 10
    if extract_file_refs(message):
        score += 10
    if _CODE_BLOCK_RE.search(message):
        score += 8
    # Specific data types / return values
    if re.search(r"\b(?:returns?|->)\s+\w+", message, re.I):
        score += 5
    if len(_TECH_TERMS.findall(message)) > 3:
        score += 5

    # Penalties
    has_specifics = bool(
        extract_file_refs(message)
        or extract_symbol_refs(message)
        or _TECH_TERMS.findall(message)
    )
    if word_count < 20 and not has_specifics:
        score -= 10
    if _ASPIRATIONAL_RE.search(message):
        score -= 15
    if _VAGUE_RE.match(message.strip()):
        score -= 20
    if not _NOUN_RE.search(message):
        score -= 10

    return max(5, min(95, score))


# ── Pivot detection ──────────────────────────────────────────────────

_STOP_WORDS = frozenset(
    "the a an and or but in on at to for of is it that this "
    "with from by as be are was were do does did i we you they "
    "my our your can will would should have has had not".split()
)


def _extract_topic_words(text: str) -> set[str]:
    """Extract meaningful topic words from text."""
    words = set(re.findall(r"\b[a-zA-Z]{3,}\b", text.lower()))
    return words - _STOP_WORDS


def _detect_pivot(
    message: str,
    recent_context: list[dict] | None,
) -> bool:
    """Detect whether the message is a topic pivot."""
    lower = message.lstrip().lower()
    if re.match(r"^(?:actually|instead|forget that|never\s*mind|scratch that)\b", lower):
        return True

    if not recent_context:
        return False

    recent_text = " ".join(
        t.get("content", "") for t in recent_context[-4:]
    )
    recent_topics = _extract_topic_words(recent_text)
    current_topics = _extract_topic_words(message)

    if not recent_topics or not current_topics:
        return False

    overlap = len(recent_topics & current_topics)
    max_possible = min(len(recent_topics), len(current_topics))
    if max_possible == 0:
        return False

    return (overlap / max_possible) < 0.20


# ── Scoring and classification ───────────────────────────────────────

def _score_intents(message: str) -> dict[str, int]:
    """Score every intent against the message, return {intent_value: score}."""
    scores: dict[str, int] = {}
    for intent, rules in _KEYWORD_RULES.items():
        total = 0
        for pattern, weight in rules:
            total += len(pattern.findall(message)) * weight
        scores[intent.value] = total
    return scores


def _check_continue(message: str) -> bool:
    """Return True if the message is a short affirmative continuation."""
    stripped = message.strip().lower().rstrip("!. ")
    if len(stripped) > _CONTINUE_MAX_LEN:
        return False
    return stripped in _CONTINUE_PHRASES


def classify(
    message: str,
    recent_context: list[dict] | None = None,
) -> Classification:
    """Classify a user message into an intent category.

    Args:
        message: The user's message text.
        recent_context: Optional recent turns [{role, content}] for
                        pivot and continuation detection.

    Returns:
        Classification with intent, confidence, clarity, and refs.
    """
    if not message or not message.strip():
        return Classification(
            intent=Intent.FOLLOW_UP,
            confidence=0.1,
            clarity=5,
            file_refs=[],
            symbol_refs=[],
            is_pivot=False,
        )

    file_refs = extract_file_refs(message)
    symbol_refs = extract_symbol_refs(message)
    has_traces = _has_error_traces(message)
    is_pivot = _detect_pivot(message, recent_context)

    # Short affirmative → continue
    if _check_continue(message):
        return Classification(
            intent=Intent.CONTINUE,
            confidence=0.95,
            clarity=estimate_clarity(message),
            file_refs=file_refs,
            symbol_refs=symbol_refs,
            is_pivot=False,
            keywords={"continue": 1},
        )

    scores = _score_intents(message)

    # Boost follow_up if recent file refs overlap
    if recent_context and file_refs:
        recent_text = " ".join(t.get("content", "") for t in recent_context[-4:])
        for f in file_refs:
            if f in recent_text:
                scores[Intent.FOLLOW_UP.value] += 3
                break

    # Boost debug when error traces are present
    if has_traces:
        scores[Intent.DEBUG.value] += 6

    # Force pivot when detected
    if is_pivot:
        scores[Intent.PIVOT.value] += 6

    sorted_intents = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_name, best_score = sorted_intents[0]
    second_name, second_score = sorted_intents[1] if len(sorted_intents) > 1 else ("", 0)

    # Tie-breaking: if top two within 20%, prefer safer option
    if best_score > 0 and second_score > 0:
        ratio = second_score / best_score
        if ratio >= 0.80:
            # Prefer debug when traces present
            if has_traces and Intent.DEBUG.value in (best_name, second_name):
                best_name = Intent.DEBUG.value
                best_score = max(scores[Intent.DEBUG.value], best_score)
            # Prefer follow_up over new_task
            elif {best_name, second_name} == {Intent.FOLLOW_UP.value, Intent.NEW_TASK.value}:
                best_name = Intent.FOLLOW_UP.value
                best_score = max(scores[Intent.FOLLOW_UP.value], best_score)

    # Confidence from score magnitude
    total = sum(scores.values()) or 1
    confidence = round(min(1.0, best_score / max(total, 1)), 2)

    # Default to follow_up when unsure
    if confidence < 0.3 or best_score == 0:
        best_name = Intent.FOLLOW_UP.value
        confidence = max(confidence, 0.15)

    intent = Intent(best_name)
    clarity = estimate_clarity(message)

    return Classification(
        intent=intent,
        confidence=confidence,
        clarity=clarity,
        file_refs=file_refs,
        symbol_refs=symbol_refs,
        is_pivot=is_pivot,
        keywords={k: v for k, v in scores.items() if v > 0},
    )


# ── Episode boundary detection ───────────────────────────────────────

_EPISODE_SHIFT_PAIRS: set[tuple[str, str]] = {
    (Intent.DEBUG.value, Intent.NEW_TASK.value),
    (Intent.QUESTION.value, Intent.NEW_TASK.value),
    (Intent.REVIEW.value, Intent.NEW_TASK.value),
    (Intent.EXPLORE.value, Intent.NEW_TASK.value),
    (Intent.META.value, Intent.NEW_TASK.value),
}


def should_start_new_episode(
    classification: Classification,
    current_episode_intent: str | None = None,
    current_episode_age_s: float = 0,
    idle_threshold_s: float = 300,
) -> bool:
    """Determine whether this message should start a new episode.

    Triggers:
    - classification.is_pivot is True
    - Intent is new_task and current episode intent differs
    - Idle gap exceeds threshold
    - Significant intent shift (e.g. debug → new_task)
    """
    if classification.is_pivot:
        return True

    if current_episode_age_s >= idle_threshold_s:
        return True

    cur = classification.intent.value
    if current_episode_intent is None:
        return True

    if cur == Intent.NEW_TASK.value and current_episode_intent != Intent.NEW_TASK.value:
        return True

    if (current_episode_intent, cur) in _EPISODE_SHIFT_PAIRS:
        return True

    return False
