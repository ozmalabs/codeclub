"""
expander.py — Round-trip expansion of LLM-edited compressed code back to full source.

The problem
-----------
We compress a file to stubs (signatures + "...") before sending to the LLM.
The LLM edits the compressed file.  We need to produce a valid full source file by:

  1. For each stubbed function where the LLM kept "..." → restore the original body.
  2. For each stubbed function where the LLM replaced "..." with new code → use new code.
  3. For signature-only edits (LLM changed the def line) → update the signature in place.

How it works
------------
The SourceMap (produced by stub_functions()) records for every stub:
  - its original line range in the full source
  - its compressed line range in the stub file

expand() takes:
  - original_code: the full source before compression
  - source_map: the SourceMap from stub_functions()
  - llm_output: the LLM's modified version of the compressed file

It returns the reconstructed full source.

For cases the LLM edited entirely (returned a full, non-stub file), expand() falls
through gracefully and returns llm_output unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tree import SourceMap, StubEntry


# ---------------------------------------------------------------------------
# Core expansion
# ---------------------------------------------------------------------------

_STUB_PLACEHOLDER = re.compile(r"^\s*\.\.\.\s*$")


def expand(original_code: str, source_map: "SourceMap", llm_output: str) -> str:
    """
    Reconstruct the full source file after the LLM edited the compressed stub file.

    Parameters
    ----------
    original_code:
        The complete source file before compression (same as source_map.original_code).
    source_map:
        The SourceMap returned by stub_functions().
    llm_output:
        The LLM's modified version of the compressed stub file.

    Returns
    -------
    str
        Fully reconstructed source with original bodies restored where the LLM
        did not change them, and new LLM-written bodies inserted where it did.
    """
    if not source_map.stubs:
        # Nothing was stubbed — LLM output is already the full file
        return llm_output

    orig_lines = original_code.splitlines(keepends=True)
    comp_lines = llm_output.splitlines(keepends=True)

    # Parse the LLM's compressed output into function slots
    llm_slots = _parse_llm_slots(comp_lines, source_map)

    # Build the result by splicing original lines and LLM changes
    result_lines: list[str] = []
    orig_cursor = 0

    # Process stubs in source order
    for stub in sorted(source_map.stubs, key=lambda s: s.orig_start):
        # Copy original lines up to this function's start
        result_lines.extend(orig_lines[orig_cursor: stub.orig_start])

        llm_slot = llm_slots.get(stub.name)

        if llm_slot is None:
            # LLM removed this function entirely — keep original
            result_lines.extend(orig_lines[stub.orig_start: stub.orig_end + 1])
        else:
            sig_lines, body_lines, kept_stub = llm_slot
            if kept_stub:
                # LLM kept "..." unchanged — restore the entire original function.
                # We use the original, not sig+body separately, to avoid duplication
                # of docstrings that appear in both the stub sig and the original body.
                result_lines.extend(orig_lines[stub.orig_start: stub.orig_end + 1])
            else:
                # LLM wrote new code — use the full LLM slot as-is
                result_lines.extend(sig_lines)
                result_lines.extend(body_lines)

        orig_cursor = stub.orig_end + 1

    # Copy remainder of original file
    result_lines.extend(orig_lines[orig_cursor:])

    return "".join(result_lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class _Slot:
    """Parsed function slot from the LLM's compressed output."""
    sig_lines: list[str]
    body_lines: list[str]
    kept_stub: bool  # True if body is just "..."


def _parse_llm_slots(
    comp_lines: list[str],
    source_map: "SourceMap",
) -> dict[str, tuple[list[str], list[str], bool]]:
    """
    Scan the LLM's output and extract per-function slots.

    Returns {name: (sig_lines, body_lines, kept_stub)}.
    We locate each function by matching its comp_start line content,
    then read until comp_end (with tolerance for line-count drift).
    """
    slots: dict[str, tuple[list[str], list[str], bool]] = {}

    for stub in source_map.stubs:
        # Locate this stub's start line in the LLM output
        comp_start = _find_line_in_comp(comp_lines, stub, source_map.original_code)
        if comp_start == -1:
            continue

        # Scan forward from comp_start to find the "..." placeholder
        stub_line_idx = -1
        for ci in range(comp_start, min(comp_start + 40, len(comp_lines))):
            if _STUB_PLACEHOLDER.match(comp_lines[ci]):
                stub_line_idx = ci
                break

        if stub_line_idx != -1:
            # "..." found — check whether the LLM fully replaced the stub body.
            # A replaced stub has "..." removed and new code written in its place.
            # When "..." is still present, the LLM kept the stub → restore original body.
            #
            # Heuristic: look at the lines right after "...". If the next non-blank
            # line is another function/class definition at the *same* indent level as
            # comp_start, then "..." is a genuine kept stub. Otherwise the LLM may
            # have inserted code after "..." (rare but possible).
            after = comp_lines[stub_line_idx + 1: stub_line_idx + 6] if stub_line_idx + 1 < len(comp_lines) else []
            fn_indent = _get_indent(comp_lines[comp_start] if comp_start < len(comp_lines) else "")

            # Count non-blank, non-closing-brace lines between "..." and the next function
            inserted_lines = []
            for line in after:
                stripped = line.strip()
                if not stripped or stripped in ("}", "};", "];"):
                    continue
                if (stripped.startswith("def ") or stripped.startswith("async def ")
                        or stripped.startswith("class ") or stripped.startswith("const ")
                        or stripped.startswith("function ") or stripped.startswith("export ")
                        or stripped.startswith("// ") or stripped.startswith("# ")):
                    break  # hit a new function — stop
                inserted_lines.append(line)

            if inserted_lines:
                # LLM replaced "..." with real code — capture new body
                body_end = _find_body_end(comp_lines, stub_line_idx + 1, fn_indent)
                sig_lines = comp_lines[comp_start: stub_line_idx]
                body_lines = comp_lines[stub_line_idx: body_end + 1]
                slots[stub.name] = (sig_lines, body_lines, False)
            else:
                # "..." kept unchanged — restore original body
                sig_lines = comp_lines[comp_start: stub_line_idx]
                slots[stub.name] = (sig_lines, [], True)
        else:
            # No "..." found — LLM wrote a complete function body
            body_end = _find_body_end(comp_lines, comp_start + 1, _get_indent(
                comp_lines[comp_start] if comp_start < len(comp_lines) else ""
            ))
            sig_lines = comp_lines[comp_start: comp_start + 1]
            body_lines = comp_lines[comp_start + 1: body_end + 1]
            slots[stub.name] = (sig_lines, body_lines, False)

    return slots


def _find_line_in_comp(comp_lines: list[str], stub: "StubEntry", original_code: str) -> int:
    """Find the compressed line index for this stub's function start."""
    orig_lines = original_code.splitlines(keepends=True)
    if stub.orig_start >= len(orig_lines):
        return stub.comp_start

    # Use the signature's first line content as a fingerprint
    target = orig_lines[stub.orig_start].rstrip("\n").rstrip()

    # Search near expected location first (±10 lines for drift)
    search_start = max(0, stub.comp_start - 5)
    search_end = min(len(comp_lines), stub.comp_start + 15)

    for ci in range(search_start, search_end):
        if comp_lines[ci].rstrip("\n").rstrip() == target:
            return ci

    # Wider search if needed
    for ci in range(len(comp_lines)):
        if comp_lines[ci].rstrip("\n").rstrip() == target:
            return ci

    return stub.comp_start


def _find_body_start(orig_lines: list[str], fn_start: int) -> int:
    """Find the first body line after a function signature."""
    for i in range(fn_start, len(orig_lines)):
        if orig_lines[i].rstrip().endswith(":"):
            return i + 1
    return fn_start + 1


def _find_body_end(lines: list[str], from_idx: int, fn_indent: str) -> int:
    """Find the last line of a function body (stops when indentation returns to fn level)."""
    end = from_idx
    for i in range(from_idx, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if not stripped:  # blank line — could be inside or outside body
            end = i
            continue
        line_indent = _get_indent(line)
        if len(line_indent) <= len(fn_indent) and stripped:
            break
        end = i
    return end


def _get_indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _is_next_function_or_class(lines: list[str]) -> bool:
    """Check if the next non-blank line starts a new function/class."""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        return (
            stripped.startswith("def ")
            or stripped.startswith("async def ")
            or stripped.startswith("class ")
            or stripped.startswith("const ")
            or stripped.startswith("function ")
            or stripped.startswith("export ")
        )
    return False


# ---------------------------------------------------------------------------
# Convenience: apply a symbol-table round-trip
# ---------------------------------------------------------------------------

def expand_symbols(compressed: str, decode_table: dict[str, str]) -> str:
    """
    Reverse symbol-table compression: replace all encoded aliases with originals.
    Used after the LLM produces output on compressed code.

    Applies longest-key-first to avoid partial matches.
    """
    result = compressed
    for encoded, original in sorted(decode_table.items(), key=lambda x: len(x[0]), reverse=True):
        result = result.replace(encoded, original)
    return result
