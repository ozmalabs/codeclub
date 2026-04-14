"""
Token counter using tiktoken (cl100k_base — same encoding used by GPT-4/Claude-style BPE).

Provides utilities to measure compression ratios in terms of real token cost.
"""

from __future__ import annotations

import tiktoken

_ENC = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_ENC.encode(text))


def compression_stats(original: str, compressed: str) -> dict:
    orig_tokens = count_tokens(original)
    comp_tokens = count_tokens(compressed)
    saved = orig_tokens - comp_tokens
    ratio = saved / orig_tokens if orig_tokens else 0.0
    return {
        "original_tokens": orig_tokens,
        "compressed_tokens": comp_tokens,
        "tokens_saved": saved,
        "compression_ratio": ratio,
        "original_bytes": len(original.encode()),
        "compressed_bytes": len(compressed.encode()),
        "byte_ratio": 1 - len(compressed.encode()) / len(original.encode()) if original else 0.0,
    }


def print_stats(label: str, original: str, compressed: str) -> None:
    stats = compression_stats(original, compressed)
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Tokens:  {stats['original_tokens']:>5} → {stats['compressed_tokens']:>5}"
          f"  (saved {stats['tokens_saved']:>4}, {stats['compression_ratio']:.1%})")
    print(f"  Bytes:   {stats['original_bytes']:>5} → {stats['compressed_bytes']:>5}"
          f"  (ratio   {stats['byte_ratio']:.1%})")
