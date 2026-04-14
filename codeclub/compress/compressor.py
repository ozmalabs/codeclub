"""
Compressor: symbol-substitution layer.

Applies only verified token-saving substitutions from the symbol table.
For structural compression (removing bodies, deduplication) see tree.py.
For file packing see repomix.py.
For combined strategies see pipeline.py.
"""

from __future__ import annotations

from .symbol_table import PYTHON_ENCODE, WALLET_ALIASES


def compress_python(code: str, *, domain: str = "generic") -> str:
    """
    Apply Python type-hint compressions.

    domain="wallet" also applies wallet/payment-domain identifier aliases.
    """
    result = code
    for english, compressed in PYTHON_ENCODE:
        result = result.replace(english, compressed)
    if domain == "wallet":
        for original, alias in WALLET_ALIASES:
            result = result.replace(original, alias)
    return result


def compress(text: str, *, mode: str = "auto", domain: str = "generic") -> str:
    """
    Compress text.

    mode="auto"   – detect Python by code signals; fall back to no-op
    mode="python" – force Python symbol compression
    mode="none"   – no-op (useful for benchmarking structural strategies alone)
    """
    if mode == "auto":
        code_signals = ("def ", "class ", "import ", "return ", "async def ")
        mode = "python" if any(s in text for s in code_signals) else "none"

    if mode == "python":
        return compress_python(text, domain=domain)
    if mode == "none":
        return text
    raise ValueError(f"Unknown mode: {mode!r}")
