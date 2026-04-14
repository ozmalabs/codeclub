"""
compact.py — Token-aware code simplification passes.

These are lossless-for-context transformations: they reduce token count
while preserving enough information for an LLM to understand the code structure.
Appropriate for read-only reference context (not for files being edited).

Findings from BPE token analysis (cl100k_base):
  - Whitespace:     newlines and indentation are ~1 token each → minimising them helps
  - Signatures:     multi-line sigs use more tokens than single-line equivalents
  - Decorative:     section separator comments (────) cost tokens, carry no logic
  - Module docs:    long module docstrings cost 10–80+ tokens, rarely needed in context
  - Type annotations: `dict[str, Any] | None` = 7 tokens → compress to 2 with symbol table
  - `self`:         always first param — model knows this; removing saves 1 token per method

Pipeline order (cumulative savings on wallet_local.py 2478-token file):
  1. stub_functions   → 782 tokens  (68.4% saved)  — removes bodies
  2. strip module doc → 664 tokens  (73.2% saved)  — removes file-level docstring
  3. collapse sigs    → 612 tokens  (75.3% saved)  — multi-line → single-line sigs
  4. symbol compress  → 571 tokens  (77.0% saved)  — identifier + type aliases
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Pass 1: strip decorative section comments
# These are lines like:  # ── provisioning ────────────────────────────
# They help humans navigate but cost tokens and carry no semantic content.
# ---------------------------------------------------------------------------

_SECTION_COMMENT_RE = re.compile(
    r"^[ \t]*#[^\n]*\u2500{3,}[^\n]*$",  # U+2500 = BOX DRAWINGS LIGHT HORIZONTAL
    re.MULTILINE,
)


def strip_section_comments(code: str) -> str:
    """Remove decorative section separator comment lines."""
    result = _SECTION_COMMENT_RE.sub("", code)
    # Clean up any double-blank lines created by removals
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result


# ---------------------------------------------------------------------------
# Pass 2: collapse multi-line function signatures to single line
# Saves ~6% on typical stub outputs.
# Before: def foo(\n    self,\n    x: int,\n) -> str:
# After:  def foo( self, x: int, ) -> str:
# ---------------------------------------------------------------------------

def collapse_signatures(code: str) -> str:
    """
    Collapse multi-line function/method signatures onto a single line.

    A signature is multi-line when `def` ... `)` spans multiple lines before `:`.
    We join interior lines with a single space, keeping the same indentation level.
    """
    lines = code.split("\n")
    result: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]

        # Detect start of a multi-line def (doesn't end with : on this line)
        if (
            (stripped.startswith("def ") or stripped.startswith("async def "))
            and not line.rstrip().endswith(":")
        ):
            parts = [stripped.rstrip()]
            i += 1
            while i < len(lines) and not lines[i].rstrip().endswith(":"):
                inner = lines[i].strip()
                if inner:
                    parts.append(inner)
                i += 1
            if i < len(lines):
                parts.append(lines[i].strip())
            result.append(indent + " ".join(parts))
        else:
            result.append(line)

        i += 1

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Pass 3: remove `self` as explicit first parameter
# The LLM knows `self` is the first param of instance methods.
# Saves 1–2 tokens per method (the token `self` + optional `, `).
# NOT reversible without knowing which functions are methods.
# ---------------------------------------------------------------------------

_SELF_COMMA_RE = re.compile(r"\bdef (\w+)\(self, ")
_SELF_ONLY_RE = re.compile(r"\bdef (\w+)\(self\)")


def remove_self_param(code: str) -> str:
    """Remove explicit `self` parameter from method signatures (lossy but conventional)."""
    result = _SELF_COMMA_RE.sub(r"def \1(", code)
    result = _SELF_ONLY_RE.sub(r"def \1()", result)
    return result


# ---------------------------------------------------------------------------
# Pass 4: strip inline type annotations (aggressive — lossy for codegen)
# Only use for pure context reference where types are already documented elsewhere.
# ---------------------------------------------------------------------------

_ANNOTATION_RE = re.compile(r": (?:str|int|bool|float|bytes|Any|dict|list|tuple|None)\b")
_RETURN_ANNOTATION_RE = re.compile(r" -> [\w\[\], |]+(?=:)")


def strip_type_annotations(code: str) -> str:
    """
    Remove simple type annotations from function signatures.

    WARNING: lossy — only use when the type information is not critical
    for the task (e.g., when you just need to know function names/order).
    """
    result = _ANNOTATION_RE.sub("", code)
    result = _RETURN_ANNOTATION_RE.sub("", result)
    return result


# ---------------------------------------------------------------------------
# Combined compact pass (all non-lossy passes)
# ---------------------------------------------------------------------------

def compact(
    code: str,
    *,
    strip_sections: bool = True,
    collapse_sigs: bool = True,
    remove_self: bool = False,   # off by default — changes call convention
    strip_annotations: bool = False,  # off by default — lossy
) -> str:
    """
    Apply all token-reduction simplification passes in order.

    Args:
        strip_sections:   remove decorative section separator comments
        collapse_sigs:    collapse multi-line function signatures to one line
        remove_self:      remove `self` from instance method signatures
        strip_annotations: remove type annotations (lossy)
    """
    result = code
    if strip_sections:
        result = strip_section_comments(result)
    if collapse_sigs:
        result = collapse_signatures(result)
    if remove_self:
        result = remove_self_param(result)
    if strip_annotations:
        result = strip_type_annotations(result)
    return result
