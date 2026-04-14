"""
repomix_lite.py — Minimal file-packing inspired by Repomix.

Repomix packs an entire codebase into a single AI-friendly file so the model
has full context without needing tool calls.  Our lite version:
  - Concatenates selected files with slim XML-style headers (matches Repomix format)
  - Strips trailing whitespace and collapses excessive blank lines
  - Optionally strips Python comments and docstrings
  - Reports per-file and total token counts

This is the "packaging" layer — apply treefrag/symbol compression on top.
"""

from __future__ import annotations

import re
import ast
from pathlib import Path


# ---------------------------------------------------------------------------
# File cleaning (reduces noise before the model sees content)
# ---------------------------------------------------------------------------

def strip_trailing_whitespace(code: str) -> str:
    return "\n".join(line.rstrip() for line in code.splitlines())


def collapse_blank_lines(code: str, *, max_consecutive: int = 1) -> str:
    """Collapse runs of blank lines to at most max_consecutive blank lines."""
    pattern = r"\n{" + str(max_consecutive + 2) + r",}"
    return re.sub(pattern, "\n" * (max_consecutive + 1), code)


def strip_python_comments(code: str) -> str:
    """Remove standalone comment lines (lines where the first non-space char is #)."""
    lines = []
    for line in code.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def strip_python_docstrings(code: str) -> str:
    """
    Remove module/class/function docstrings from Python source.

    Uses the AST to find docstring nodes, then removes them from the source.
    Non-parseable code is returned unchanged.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    lines = code.splitlines(keepends=True)
    to_remove: list[tuple[int, int]] = []  # (start_line_0indexed, end_line_0indexed)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            start = first.lineno - 1
            end = first.end_lineno - 1
            to_remove.append((start, end))

    for start, end in sorted(to_remove, reverse=True):
        lines[start: end + 1] = []

    return "".join(lines)


def clean(
    code: str,
    *,
    strip_comments: bool = False,
    strip_docstrings: bool = False,
    collapse_blanks: bool = True,
) -> str:
    """Apply cleaning passes to a file."""
    result = strip_trailing_whitespace(code)
    if strip_comments:
        result = strip_python_comments(result)
    if strip_docstrings:
        result = strip_python_docstrings(result)
    if collapse_blanks:
        result = collapse_blank_lines(result)
    return result


# ---------------------------------------------------------------------------
# File packing (Repomix-style)
# ---------------------------------------------------------------------------

REPOMIX_HEADER = """\
This file is a packed representation of the repository's contents.
It was created by repomix-lite, a minimal implementation of Repomix-style packing.
"""


def pack_files(
    files: dict[str, str],
    *,
    strip_comments: bool = False,
    strip_docstrings: bool = False,
    collapse_blanks: bool = True,
    include_summary: bool = True,
) -> str:
    """
    Pack multiple files into a single Repomix-format string.

    Args:
        files: {relative_path: file_content}
        strip_comments: remove standalone comment lines
        strip_docstrings: remove docstrings
        collapse_blanks: collapse multiple blank lines
        include_summary: include a table of contents header

    Returns:
        Single packed string suitable for LLM context.
    """
    parts: list[str] = [REPOMIX_HEADER]

    if include_summary:
        parts.append("<file_summary>\n")
        for path in files:
            parts.append(f"  <file path=\"{path}\" />\n")
        parts.append("</file_summary>\n\n")

    parts.append("<files>\n")

    for path, content in files.items():
        cleaned = clean(
            content,
            strip_comments=strip_comments,
            strip_docstrings=strip_docstrings,
            collapse_blanks=collapse_blanks,
        )
        parts.append(f'<file path="{path}">\n')
        parts.append(cleaned)
        if not cleaned.endswith("\n"):
            parts.append("\n")
        parts.append("</file>\n\n")

    parts.append("</files>\n")
    return "".join(parts)


def pack_paths(
    paths: list[str | Path],
    *,
    root: str | Path | None = None,
    **kwargs,
) -> str:
    """
    Load files from disk and pack them.

    Args:
        paths: list of file paths
        root: optional root path to strip from file names
        **kwargs: passed to pack_files
    """
    files: dict[str, str] = {}
    root_path = Path(root) if root else None

    for p in paths:
        path = Path(p)
        key = str(path.relative_to(root_path)) if root_path else path.name
        try:
            files[key] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass

    return pack_files(files, **kwargs)
