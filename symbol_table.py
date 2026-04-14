"""
Symbol tables for token-efficient prompt compression.

Every substitution here has been validated against cl100k_base (tiktoken)
to produce a POSITIVE token delta.  CJK characters are only used when they
tokenise to fewer BPE tokens than the original ASCII — most CJK in cl100k_base
costs 2–3 tokens, so we use them only where measurements confirm savings.

Order matters for encoding: longer/more-specific patterns MUST come first.
Decoding is sorted by key length (longest first) to avoid partial matches.

Strategy origins:
  - Type-hint compressions: validated against cl100k_base BPE
  - Codebase-specific aliases: inspired by Stingy Context identifier aliasing
  - Output style: Caveman / 文言文 (caveman saves ~75% output tokens)
"""

# ---------------------------------------------------------------------------
# Universal Python type-annotation compressions
# All entries verified: original_tokens > compressed_tokens
# ---------------------------------------------------------------------------
PYTHON_ENCODE: list[tuple[str, str]] = [
    # Longest patterns first to prevent partial-match interference
    ("dict[str, Any] | None", "不?"),    # 7 → 2 tokens  (+5 saved)
    ("dict[str, Any]",        "不"),     # 5 → 1 token   (+4 saved)
    ("str | None",            "串?"),    # 3 → 2 tokens  (+1 saved)
    ("int | None",            "三?"),    # 3 → 2 tokens  (+1 saved)
    ("bool | None",           "业?"),    # 3 → 2 tokens  (+1 saved)
    # Module-level boilerplate
    ("from __future__ import annotations", "中annotations"),  # 6 → 2 (+4 saved)
    ("@abc.abstractmethod",   "@不方"),  # 4 → 3 tokens  (+1 saved)
]

# Decode: MUST sort by key length descending so "不?" decodes before "不"
PYTHON_DECODE: list[tuple[str, str]] = sorted(
    [(cjk, eng) for eng, cjk in PYTHON_ENCODE],
    key=lambda x: len(x[0]),
    reverse=True,
)

# ---------------------------------------------------------------------------
# Example domain-specific identifier aliases (wallet/payment provider domain)
# Inspired by Stingy Context's TREEFRAG dictionary / identifier aliasing.
# Short aliases chosen to be single tokens in cl100k_base.
# ---------------------------------------------------------------------------
WALLET_ALIASES: list[tuple[str, str]] = [
    # Longest first to prevent partial-match interference
    ("normalize_wallet_provider_name", "nwpn"),  # 4 → 3 tokens (+1)
    ("destination_provider_name",      "dpn"),   # 3 → 2 tokens (+1)
    ("wallet_provider_name",           "wpn"),   # 3 → 2 tokens (+1)
    ("fallback_provider_name",         "fpn"),   # 3 → 2 tokens (+1)
    ("source_provider_name",           "spn"),   # 3 → 2 tokens (+1)
    ("WalletProviderError",            "WPE"),   # 3 → 2 tokens (+1)
    ("WalletTransaction",              "WT"),    # 2 → 1 token  (+1)
    ("WalletProvider",                 "WP"),    # 2 → 1 token  (+1)
    ("WalletBalance",                  "WB"),    # 2 → 1 token  (+1)
    ("meta_data",                      "md"),    # 2 → 1 token  (+1)
    ("transaction_id",                 "tid"),   # 2 → 1 token  (+1)
]

WALLET_ALIASES_DECODE: list[tuple[str, str]] = sorted(
    [(alias, orig) for orig, alias in WALLET_ALIASES],
    key=lambda x: len(x[0]),
    reverse=True,
)
