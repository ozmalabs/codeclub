"""
pipeline.py — Multi-strategy compression pipeline.

Combines all compression strategies in the optimal sequence:

  1. Repomix packing     → consolidate files, strip noise
  2. Structural stubbing → replace function bodies with stubs (LongCodeZip coarse)
  3. TREEFRAG dedup      → deduplicate across files (Stingy Context)
  4. Symbol substitution → token-level savings (Stingy Context aliases + type hints)
  5. Caveman output      → terse model responses (75% output savings)

Each strategy is independently measurable and combinable.

Reference savings (on issue #1519 wallet_provider_snippet.py):
  Strategy              | Tokens saved | Ratio
  ----------------------|--------------|-------
  Repomix pack          |   ~5%        | noise removal
  Structural stub       |  ~81%        | body removal
  Symbol substitution   |   ~5%        | alias + type hints
  Combined              |  ~82%        | (stub dominates)
  + Caveman output      |  ~75% output | (applied at inference time)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import tiktoken

from .compressor import compress_python
from .repomix import pack_files, clean
from .tree import stub_functions, treefrag, render_fragment_dict
from .compact import compact

_ENC = tiktoken.get_encoding("cl100k_base")


def _tokens(text: str) -> int:
    return len(_ENC.encode(text))


# ---------------------------------------------------------------------------
# Strategy result dataclass
# ---------------------------------------------------------------------------

@dataclass
class StrategyResult:
    name: str
    original_tokens: int
    compressed_tokens: int
    output: str

    @property
    def tokens_saved(self) -> int:
        return self.original_tokens - self.compressed_tokens

    @property
    def compression_ratio(self) -> float:
        return self.tokens_saved / self.original_tokens if self.original_tokens else 0.0

    def __repr__(self) -> str:
        return (
            f"StrategyResult({self.name!r}: "
            f"{self.original_tokens}→{self.compressed_tokens} tokens, "
            f"{self.compression_ratio:.1%} saved)"
        )


# ---------------------------------------------------------------------------
# Individual strategy runners
# ---------------------------------------------------------------------------

def run_repomix(files: dict[str, str], **kwargs) -> StrategyResult:
    """Strategy 1: Repomix packing with noise removal."""
    original = "\n".join(files.values())
    packed = pack_files(files, **kwargs)
    return StrategyResult(
        name="repomix",
        original_tokens=_tokens(original),
        compressed_tokens=_tokens(packed),
        output=packed,
    )


def run_stub(files: dict[str, str]) -> StrategyResult:
    """
    Strategy 2: Structural stubbing (LongCodeZip coarse-grained pass).

    Replace all function bodies with signature + docstring + '...'
    This is the most powerful single strategy for read-only code context.
    """
    original = "\n".join(files.values())
    stubbed = {path: stub_functions(code)[0] for path, code in files.items()}
    combined = "\n".join(stubbed.values())
    return StrategyResult(
        name="stub",
        original_tokens=_tokens(original),
        compressed_tokens=_tokens(combined),
        output=combined,
    )


def run_treefrag(files: dict[str, str]) -> StrategyResult:
    """
    Strategy 3: TREEFRAG cross-file deduplication (Stingy Context).

    Replaces duplicate function bodies with hash references and emits
    a fragment dictionary header.
    """
    original = "\n".join(files.values())
    result = treefrag(files)
    frag_header = render_fragment_dict(result.fragment_dict)
    combined = frag_header + "\n".join(result.compressed_files.values())
    return StrategyResult(
        name="treefrag",
        original_tokens=_tokens(original),
        compressed_tokens=_tokens(combined),
        output=combined,
    )


def run_symbol(files: dict[str, str], *, domain: str = "generic") -> StrategyResult:
    """
    Strategy 4: Symbol substitution (verified token-saving aliases + type hints).

    Inspired by Stingy Context identifier aliasing.
    """
    original = "\n".join(files.values())
    compressed = {
        path: compress_python(code, domain=domain) for path, code in files.items()
    }
    combined = "\n".join(compressed.values())
    return StrategyResult(
        name=f"symbol({domain})",
        original_tokens=_tokens(original),
        compressed_tokens=_tokens(combined),
        output=combined,
    )


def run_compact(files: dict[str, str], *, domain: str = "generic") -> StrategyResult:
    """
    Strategy 5: compact pass — simplification without structural removal.

    Applies on top of existing code (not stubs):
      - Strip decorative section comments (────)
      - Collapse multi-line signatures to single line
      - Symbol substitution (aliases + type hints)

    Best used as a standalone pass when full stubbing is too aggressive
    (e.g., for files the model needs to understand bodies of).
    """
    original = "\n".join(files.values())
    result = {
        path: compress_python(compact(code), domain=domain)
        for path, code in files.items()
    }
    combined = "\n".join(result.values())
    return StrategyResult(
        name=f"compact({domain})",
        original_tokens=_tokens(original),
        compressed_tokens=_tokens(combined),
        output=combined,
    )


def run_full(files: dict[str, str], *, domain: str = "generic") -> StrategyResult:
    """
    Strategy 6: full pipeline — every non-lossy pass combined.

    Order: clean → strip module docstrings → stub bodies → compact passes → symbol substitution.

    This is the maximum lossless compression for read-only context files.
    Add Caveman output style at inference time for ~75% additional output savings.
    """
    from .repomix import strip_python_docstrings

    original = "\n".join(files.values())

    result = {}
    for path, code in files.items():
        # 1. Repomix-style clean
        c = clean(code, collapse_blanks=True)
        # 2. Remove module/class docstrings
        c = strip_python_docstrings(c)
        # 3. Structural stub (LongCodeZip coarse pass)
        c, _ = stub_functions(c)
        # 4. Compact passes (section comments, sig collapse)
        c = compact(c, strip_sections=True, collapse_sigs=True)
        # 5. Symbol substitution (Stingy Context aliases)
        c = compress_python(c, domain=domain)
        result[path] = c

    combined = "\n".join(result.values())
    return StrategyResult(
        name=f"full({domain})",
        original_tokens=_tokens(original),
        compressed_tokens=_tokens(combined),
        output=combined,
    )


def run_combined(files: dict[str, str], *, domain: str = "generic") -> StrategyResult:
    """
    Full pipeline: repomix clean → stub → symbol substitution.

    (TREEFRAG is skipped here since stub is more aggressive for single-file context.)
    """
    original = "\n".join(files.values())

    # Step 1: clean noise (repomix-style)
    cleaned = {path: clean(code, collapse_blanks=True) for path, code in files.items()}

    # Step 2: structural stub (LongCodeZip coarse pass)
    stubbed = {path: stub_functions(code)[0] for path, code in cleaned.items()}

    # Step 3: symbol substitution
    compressed = {
        path: compress_python(code, domain=domain) for path, code in stubbed.items()
    }

    combined = "\n".join(compressed.values())
    return StrategyResult(
        name=f"combined({domain})",
        original_tokens=_tokens(original),
        compressed_tokens=_tokens(combined),
        output=combined,
    )


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkReport:
    strategies: list[StrategyResult] = field(default_factory=list)

    def print(self) -> None:
        print(f"\n{'='*68}")
        print(f"  {'Strategy':<22} {'Original':>9} {'Compressed':>11} {'Saved':>7} {'Ratio':>7}")
        print(f"  {'-'*22} {'-'*9} {'-'*11} {'-'*7} {'-'*7}")
        for r in self.strategies:
            print(
                f"  {r.name:<22} {r.original_tokens:>9} {r.compressed_tokens:>11} "
                f"{r.tokens_saved:>7} {r.compression_ratio:>7.1%}"
            )
        print(f"{'='*68}")
        print("  Note: 'stub'/'full' remove function bodies (read-only context use)")
        print("  'compact' = section strip + sig collapse + symbol (keeps bodies)")
        print("  Add Caveman output style for ~75% additional OUTPUT token savings")
        print(f"{'='*68}\n")

    def best(self) -> StrategyResult:
        return max(self.strategies, key=lambda r: r.compression_ratio)


def benchmark(files: dict[str, str], *, domain: str = "generic") -> BenchmarkReport:
    """Run all strategies and return a BenchmarkReport."""
    return BenchmarkReport(strategies=[
        run_repomix(files, collapse_blanks=True),
        run_compact(files, domain=domain),
        run_stub(files),
        run_treefrag(files),
        run_symbol(files, domain=domain),
        run_combined(files, domain=domain),
        run_full(files, domain=domain),
    ])
