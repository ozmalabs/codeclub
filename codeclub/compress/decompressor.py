"""
Decompressor: reverses symbol compressions back to original Python.

Uses the reverse symbol tables with longest-key-first ordering to prevent
partial-match errors (e.g. decoding "不" before "不?" would corrupt output).
"""

from __future__ import annotations

from .symbol_table import PYTHON_DECODE, WALLET_ALIASES_DECODE


def decompress_python(code: str, *, domain: str = "generic") -> str:
    result = code
    if domain == "wallet":
        for alias, original in WALLET_ALIASES_DECODE:
            result = result.replace(alias, original)
    for compressed, english in PYTHON_DECODE:
        result = result.replace(compressed, english)
    return result


def decompress(text: str, *, mode: str = "auto", domain: str = "generic") -> str:
    """
    Decompress text.  mode and domain must match what was used during compression.
    """
    if mode == "auto":
        # Heuristic: presence of verified CJK markers means Python mode was used
        mode = "python" if any(c in text for c in ("不", "串?", "三?", "业?", "中annotations", "@不方")) else "none"

    if mode == "python":
        return decompress_python(text, domain=domain)
    if mode == "none":
        return text
    raise ValueError(f"Unknown mode: {mode!r}")
