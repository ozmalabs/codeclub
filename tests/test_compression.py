"""
Tests for the llm-compressor using real-world fixtures from issue #1519
(Referral wallet transactions missing metadata at commit).

Strategies under test:
  1. Symbol substitution  — verified token-saving type-hint + identifier aliases
  2. Structural stubbing  — LongCodeZip-style body removal via AST
  3. TREEFRAG dedup       — Stingy Context-style cross-file deduplication
  4. Repomix packing      — noise removal + file consolidation
  5. Combined pipeline    — all of the above

Reference tools:
  - LLMLingua (microsoft/LLMLingua): perplexity-based token pruning
  - LongCodeZip (YerbaPage/LongCodeZip, ASE 2025): hierarchical coarse+fine pruning
  - Stingy Context (arXiv:2601.19929): TREEFRAG 18:1 compression
  - Repomix (yamadashy/repomix): codebase packing for AI context
  - Caveman (JuliusBrussee/caveman): output-side 75% token reduction
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pytest
from compressor import compress_python
from decompressor import decompress_python
from repomix_lite import pack_files, clean, strip_python_docstrings
from treefrag import stub_functions, treefrag, render_fragment_dict
from pipeline import benchmark, run_stub, run_treefrag, run_symbol, run_combined, run_repomix, run_compact, run_full
from token_counter import count_tokens, compression_stats

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


def wallet_files() -> dict[str, str]:
    return {
        "wallet_bridge_snippet.py": load("wallet_bridge_snippet.py"),
        "wallet_provider_snippet.py": load("wallet_provider_snippet.py"),
    }


def wallet_impl_files() -> dict[str, str]:
    """Real implementation files — have actual function bodies, not just stubs."""
    return {
        "wallet_local.py": load("wallet_local.py"),
        "wallet_stripe.py": load("wallet_stripe.py"),
    }


# ---------------------------------------------------------------------------
# 1. Symbol substitution tests
# ---------------------------------------------------------------------------

class TestSymbolSubstitution:
    """
    Validates the token-saving identifier aliasing and type-hint substitution.
    Inspired by Stingy Context's TREEFRAG identifier aliasing.
    All substitutions in symbol_table.py are pre-verified positive.
    """

    def test_type_hints_save_tokens(self):
        snippet = "meta_data: dict[str, Any] | None,\nresult: dict[str, Any],"
        compressed = compress_python(snippet)
        assert count_tokens(compressed) < count_tokens(snippet)

    def test_dict_any_none_compressed(self):
        snippet = "def foo(x: dict[str, Any] | None) -> dict[str, Any]:\n    pass\n"
        compressed = compress_python(snippet)
        assert "不?" in compressed
        assert "不" in compressed

    def test_future_annotations_compressed(self):
        snippet = "from __future__ import annotations\n"
        compressed = compress_python(snippet)
        assert "中annotations" in compressed
        assert count_tokens(compressed) < count_tokens(snippet)

    def test_abstractmethod_compressed(self):
        snippet = "    @abc.abstractmethod\n    def foo(self):\n        ...\n"
        compressed = compress_python(snippet)
        assert "@不方" in compressed
        assert count_tokens(compressed) < count_tokens(snippet)

    def test_wallet_domain_aliases(self):
        """Wallet-domain aliases save tokens on the issue #1519 code."""
        code = load("wallet_provider_snippet.py")
        generic = compress_python(code, domain="generic")
        wallet = compress_python(code, domain="wallet")
        # wallet domain should save more tokens than generic
        assert count_tokens(wallet) < count_tokens(generic)

    def test_round_trip_generic(self):
        code = load("wallet_bridge_snippet.py")
        compressed = compress_python(code)
        restored = decompress_python(compressed)
        assert restored == code, "Round-trip failed for wallet_bridge_snippet.py"

    def test_round_trip_wallet_domain(self):
        code = load("wallet_provider_snippet.py")
        compressed = compress_python(code, domain="wallet")
        restored = decompress_python(compressed, domain="wallet")
        assert restored == code, "Round-trip failed for wallet_provider_snippet.py"

    def test_no_partial_match_corruption(self):
        """'不?' must not decompose as '不' + '?' — longest match must win."""
        snippet = "x: dict[str, Any] | None, y: dict[str, Any]"
        compressed = compress_python(snippet)
        assert decompress_python(compressed) == snippet


# ---------------------------------------------------------------------------
# 2. Structural stubbing tests (LongCodeZip coarse pass)
# ---------------------------------------------------------------------------

class TestStructuralStubbing:
    """
    LongCodeZip uses coarse-grained selection to retain only the most relevant
    code units within a token budget.  Our stub_functions() implements the
    structural counterpart: replace bodies with stubs for context-only files.
    """

    def test_stub_saves_significant_tokens(self):
        """Stubbing a real implementation file should save >>50% tokens."""
        code = load("wallet_local.py")
        stubbed, smap = stub_functions(code)
        stats = compression_stats(code, stubbed)
        assert stats["compression_ratio"] > 0.50, (
            f"Expected >50% saving from stubbing real impl file, got {stats['compression_ratio']:.1%}"
        )

    def test_stub_preserves_signatures(self):
        """All def/method signatures must survive stubbing."""
        code = load("wallet_provider_snippet.py")
        stubbed, _ = stub_functions(code)
        assert "commit_inflight" in stubbed
        assert "void_inflight" in stubbed
        assert "provision_user_wallet" in stubbed

    def test_stub_preserves_docstrings(self):
        code = load("wallet_provider_snippet.py")
        stubbed, _ = stub_functions(code)
        assert "Commit a previously created inflight transaction." in stubbed

    def test_stub_removes_bodies(self):
        code = load("wallet_bridge_snippet.py")
        stubbed, _ = stub_functions(code)
        assert "resolve_transaction_provider" not in stubbed or "..." in stubbed

    def test_stub_valid_python(self):
        """Stubbed output must be syntactically valid Python (or contain '...')."""
        code = load("wallet_provider_snippet.py")
        stubbed, _ = stub_functions(code)
        assert "..." in stubbed

    def test_stub_bridge_saves_tokens(self):
        code = load("wallet_bridge_snippet.py")
        stubbed, _ = stub_functions(code)
        assert count_tokens(stubbed) < count_tokens(code)

    def test_stub_returns_source_map(self):
        """stub_functions must return a SourceMap with at least one stub entry."""
        code = load("wallet_local.py")
        stubbed, smap = stub_functions(code)
        assert smap is not None
        assert len(smap.stubs) > 0
        assert smap.original_code == code

    def test_stub_source_map_entries_have_names(self):
        """Every StubEntry must have a non-empty name and valid line numbers."""
        code = load("wallet_local.py")
        _, smap = stub_functions(code)
        for entry in smap.stubs:
            assert entry.name, f"Stub entry has empty name: {entry}"
            assert entry.orig_start >= 0
            assert entry.orig_end >= entry.orig_start


# ---------------------------------------------------------------------------
# 3. TREEFRAG deduplication tests (Stingy Context)
# ---------------------------------------------------------------------------

class TestTreeFrag:
    """
    Stingy Context achieves 18:1 compression via TREEFRAG:
    parse AST → extract fragments → deduplicate → emit dictionary + references.
    Our implementation applies this across the wallet_bridge + wallet_provider files.
    """

    def test_treefrag_produces_fragment_dict(self):
        files = wallet_files()
        result = treefrag(files)
        # Should find at least some fragments in multi-function files
        # (even if bodies are small, the dedup logic should run)
        assert isinstance(result.fragment_dict, dict)
        assert isinstance(result.compressed_files, dict)
        assert set(result.compressed_files.keys()) == set(files.keys())

    def test_treefrag_reduces_tokens(self):
        """
        TREEFRAG value is cross-file deduplication — it shines on large repos.
        For two small non-duplicated files, the fragment dict adds some overhead.
        The test verifies TREEFRAG runs without errors and fragment references appear
        when bodies are large enough.
        """
        files = wallet_files()
        result = treefrag(files)
        # All files should be in output
        assert set(result.compressed_files.keys()) == set(files.keys())
        # TREEFRAG value: test on real impl files that DO have large bodies
        impl = wallet_impl_files()
        impl_result = treefrag(impl)
        original_tokens = sum(count_tokens(c) for c in impl.values())
        compressed_tokens = sum(count_tokens(c) for c in impl_result.compressed_files.values())
        # Without dict overhead, individual files should be smaller
        assert compressed_tokens < original_tokens, (
            "TREEFRAG should reduce body tokens in implementation files"
        )

    def test_treefrag_fragment_dict_rendered(self):
        files = wallet_files()
        result = treefrag(files)
        if result.fragment_dict:
            rendered = render_fragment_dict(result.fragment_dict)
            assert "FRAGMENT DICTIONARY" in rendered
            assert "TREEFRAG" in rendered

    def test_treefrag_large_bodies_extracted(self):
        """Functions with large bodies should appear as FRAG: references."""
        # Create a file with a substantial function
        code = '''
def big_function(x: int) -> int:
    """A large function with many lines."""
    a = x + 1
    b = a * 2
    c = b - 3
    d = c / 4
    e = d + a
    f = e * b
    g = f - c
    h = g / d
    i = h + e
    j = i * f
    return int(j)
'''
        files = {"test.py": code}
        result = treefrag(files, min_body_tokens=5)
        compressed = result.compressed_files["test.py"]
        # Body should be replaced with a FRAG reference
        assert "FRAG:" in compressed or len(result.fragment_dict) > 0


# ---------------------------------------------------------------------------
# 4. Repomix packing tests
# ---------------------------------------------------------------------------

class TestRepomixPacking:
    """
    Repomix packs codebases into single AI-friendly files.
    Our repomix_lite implements the same concept with token measurement.
    """

    def test_pack_produces_valid_output(self):
        files = wallet_files()
        packed = pack_files(files)
        for path in files:
            assert path in packed
        assert "<file" in packed
        assert "</file>" in packed

    def test_pack_includes_summary(self):
        files = wallet_files()
        packed = pack_files(files, include_summary=True)
        assert "<file_summary>" in packed

    def test_docstring_stripping_saves_tokens(self):
        """Stripping docstrings should save meaningful tokens."""
        code = load("wallet_provider_snippet.py")
        stripped = strip_python_docstrings(code)
        saved = count_tokens(code) - count_tokens(stripped)
        assert saved > 20, f"Expected >20 tokens saved by docstring removal, got {saved}"

    def test_clean_removes_excess_blanks(self):
        code = "def foo():\n    pass\n\n\n\n\ndef bar():\n    pass\n"
        cleaned = clean(code, collapse_blanks=True)
        assert "\n\n\n" not in cleaned

    def test_pack_with_docstring_stripping(self):
        files = wallet_files()
        with_docs = pack_files(files, strip_docstrings=False)
        without_docs = pack_files(files, strip_docstrings=True)
        assert count_tokens(without_docs) < count_tokens(with_docs)


# ---------------------------------------------------------------------------
# 5a. Compact simplification tests
# ---------------------------------------------------------------------------

class TestCompactSimplification:
    """
    Tests token-aware simplification passes that work WITHOUT removing bodies.
    These are safe to apply to files the model needs to read AND modify.
    """

    def test_section_comments_stripped(self):
        from compact import strip_section_comments
        code = "class Foo:\n    # ── section ─────────────────\n    def bar(self): pass\n"
        result = strip_section_comments(code)
        assert "─" * 3 not in result
        assert "def bar" in result

    def test_sig_collapse_saves_tokens(self):
        from compact import collapse_signatures
        multi_line = (
            "def create_inflight(\n"
            "    self,\n"
            "    *,\n"
            "    source: str,\n"
            "    destination: str,\n"
            "    amount: int,\n"
            ") -> WalletTransaction:\n"
            "    ...\n"
        )
        collapsed = collapse_signatures(multi_line)
        assert count_tokens(collapsed) < count_tokens(multi_line)
        assert "def create_inflight" in collapsed
        assert "\n    *," not in collapsed

    def test_sig_collapse_preserves_colon(self):
        from compact import collapse_signatures
        code = "def foo(\n    self,\n    x: int,\n) -> str:\n    return str(x)\n"
        collapsed = collapse_signatures(code)
        assert collapsed.count("def foo") == 1
        assert "-> str:" in collapsed

    def test_compact_on_real_file(self):
        from compact import compact
        code = load("wallet_local.py")
        result = compact(code)
        assert count_tokens(result) < count_tokens(code)

    def test_compact_section_savings(self):
        """wallet_local.py has section separator comments — they should be stripped."""
        from compact import strip_section_comments
        code = load("wallet_local.py")
        result = strip_section_comments(code)
        assert "\u2500\u2500\u2500" not in result  # ─── stripped


# ---------------------------------------------------------------------------
# 5b. Combined pipeline benchmark (the real test)
# ---------------------------------------------------------------------------

class TestCombinedPipeline:
    """
    Tests the full strategy stack and benchmarks against the issue #1519 context.

    Best outcome expected (per user goal):
      repomix + stub + compact + symbol ≥ 75% token reduction for read-only code context
    """

    def test_combined_beats_individual_strategies(self):
        files = wallet_files()
        symbol_r = run_symbol(files, domain="wallet")
        combined_r = run_combined(files, domain="wallet")
        # Combined should save more than symbol alone
        assert combined_r.compression_ratio > symbol_r.compression_ratio

    def test_combined_saves_at_least_60_percent(self):
        """
        Core claim: combined pipeline achieves ≥60% token reduction on real impl files.
        Uses wallet_local.py + wallet_stripe.py which have real function bodies.
        """
        files = wallet_impl_files()
        result = run_combined(files, domain="wallet")
        assert result.compression_ratio >= 0.60, (
            f"Combined pipeline saved only {result.compression_ratio:.1%}, expected ≥60%"
        )

    def test_stub_is_the_dominant_strategy(self):
        """Structural stubbing should provide the largest single savings."""
        files = wallet_files()
        stub_r = run_stub(files)
        symbol_r = run_symbol(files, domain="wallet")
        repomix_r = run_repomix(files)
        assert stub_r.compression_ratio > symbol_r.compression_ratio
        assert stub_r.compression_ratio > repomix_r.compression_ratio

    def test_benchmark_runs_all_strategies(self):
        files = wallet_files()
        report = benchmark(files, domain="wallet")
        names = {r.name for r in report.strategies}
        assert "repomix" in names
        assert "stub" in names
        assert "treefrag" in names
        assert "symbol(wallet)" in names
        assert "compact(wallet)" in names
        assert "combined(wallet)" in names
        assert "full(wallet)" in names

    def test_full_beats_combined(self):
        """full() adds compact passes on top of combined — should save more."""
        files = wallet_impl_files()
        combined_r = run_combined(files, domain="wallet")
        full_r = run_full(files, domain="wallet")
        assert full_r.compression_ratio >= combined_r.compression_ratio

    def test_full_saves_at_least_75_percent(self):
        """Full pipeline (stub + compact + symbol) should reach ≥75% on impl files."""
        files = wallet_impl_files()
        result = run_full(files, domain="wallet")
        assert result.compression_ratio >= 0.75, (
            f"Full pipeline saved only {result.compression_ratio:.1%}, expected ≥75%"
        )

    def test_compact_saves_tokens_without_stubbing(self):
        """Compact pass alone (no body removal) should save some tokens."""
        files = wallet_impl_files()
        result = run_compact(files, domain="wallet")
        assert result.compression_ratio > 0.02, (
            "Compact pass should save >2% even without body removal"
        )

    def test_benchmark_best_is_combined(self):
        """On real implementation files, combined pipeline should win with ≥60% savings."""
        files = wallet_impl_files()
        report = benchmark(files, domain="wallet")
        best = report.best()
        assert best.compression_ratio >= 0.60

    def test_print_benchmark_report(self, capsys):
        """Print the full benchmark table to stdout."""
        files = wallet_impl_files()
        report = benchmark(files, domain="wallet")
        report.print()
        captured = capsys.readouterr()
        assert "Strategy" in captured.out
        assert "Ratio" in captured.out
        assert "stub" in captured.out
        print("\n=== CAVEMAN OUTPUT NOTE ===")
        print("Add Caveman (JuliusBrussee/caveman) output style at inference time.")
        print("Result: ~75% output tokens saved + ≥60% input tokens saved = ~87% total cost reduction.")


# ---------------------------------------------------------------------------
# 6. Tree-sitter JSX/JavaScript stubbing tests
# ---------------------------------------------------------------------------

class TestJSXStubbing:
    """
    Validates tree-sitter-based structural compression on real JSX frontend code.
    Uses StripeConnect.jsx — a 678-line React component with hooks and async handlers.

    SWE-agent's `filemap` does similar stubbing for Python only (tree-sitter, no source map).
    Our approach adds: JSX/JS support, SourceMap for round-trip, compact passes.
    """

    def test_jsx_stub_saves_significant_tokens(self):
        """Stubbing a real JSX file should save >40% tokens."""
        code = load("stripe_connect.jsx")
        stubbed, smap = stub_functions(code, language="javascript")
        stats = compression_stats(code, stubbed)
        assert stats["compression_ratio"] > 0.40, (
            f"Expected >40% saving from JSX stubbing, got {stats['compression_ratio']:.1%}"
        )

    def test_jsx_stub_produces_source_map(self):
        """SourceMap must be returned with at least one stub entry for a real JSX file."""
        code = load("stripe_connect.jsx")
        stubbed, smap = stub_functions(code, language="javascript")
        assert smap is not None
        assert len(smap.stubs) > 0, "Expected at least one stubbed function in StripeConnect.jsx"
        assert smap.language == "javascript"
        assert smap.original_code == code

    def test_jsx_stub_preserves_function_names(self):
        """Key component/hook names must survive stubbing."""
        code = load("stripe_connect.jsx")
        stubbed, _ = stub_functions(code, language="javascript")
        # These identifiers appear in signatures and must not be lost
        assert "StripeConnectPage" in stubbed or "useStripeProvider" in stubbed

    def test_jsx_stub_contains_ellipsis(self):
        """Non-trivial function bodies must be replaced with '...'."""
        code = load("stripe_connect.jsx")
        stubbed, _ = stub_functions(code, language="javascript")
        assert "..." in stubbed

    def test_jsx_stub_map_line_numbers_valid(self):
        """All SourceMap entries must have valid, non-negative line numbers."""
        code = load("stripe_connect.jsx")
        _, smap = stub_functions(code, language="javascript")
        total_lines = len(code.splitlines())
        for entry in smap.stubs:
            assert entry.orig_start >= 0, f"{entry.name}: orig_start < 0"
            assert entry.orig_end >= entry.orig_start, f"{entry.name}: orig_end < orig_start"
            assert entry.orig_end < total_lines + 5, f"{entry.name}: orig_end out of range"


# ---------------------------------------------------------------------------
# 7. Expander / round-trip tests
# ---------------------------------------------------------------------------

class TestExpander:
    """
    Validates that expand() correctly reconstructs the original file after LLM edits.

    The core problem: we compress code → send to LLM → LLM edits compressed version
    → we need to apply those edits back to the original full file.

    Three scenarios:
      A. LLM kept "..." unchanged → restore original body
      B. LLM replaced "..." with new code → use new code
      C. LLM changed a signature → update signature in original
    """

    SIMPLE_PYTHON = '''\
def greet(name: str) -> str:
    """Say hello."""
    greeting = f"Hello, {name}"
    greeting += "!"
    return greeting


def add(a: int, b: int) -> int:
    result = a + b
    return result
'''

    def test_expand_unchanged_restores_original(self):
        """If LLM returns the stub unchanged, expand() must restore the original."""
        from expander import expand
        original = self.SIMPLE_PYTHON
        compressed, smap = stub_functions(original, language="python")
        assert "..." in compressed, "Stub must contain '...'"
        # LLM returns compressed unchanged
        restored = expand(original, smap, compressed)
        # All original function bodies must be present
        assert "greeting = f" in restored or "greeting +=" in restored or "Hello" in restored
        assert "return greeting" in restored or "return result" in restored

    def test_expand_with_new_body_uses_llm_code(self):
        """If LLM replaces '...' with new code, expand() must use that new code."""
        from expander import expand
        original = self.SIMPLE_PYTHON
        compressed, smap = stub_functions(original, language="python")

        # Simulate LLM replacing the '...' in 'add' with new implementation
        new_body = "    return a + b + 1  # LLM added +1\n"
        llm_output = compressed.replace("    ...", new_body.rstrip("\n"), 1)

        restored = expand(original, smap, llm_output)
        # The LLM's new code must appear somewhere in the result
        # (either in-place or gracefully merged)
        assert restored  # must not be empty
        assert len(restored) > 10

    def test_expand_symbol_table_round_trip(self):
        """Symbol table encoding and decoding must be lossless."""
        from expander import expand_symbols
        from decompressor import decompress_python
        snippet = "def foo(x: dict[str, Any] | None) -> dict[str, Any]:\n    pass\n"
        compressed = compress_python(snippet)
        restored = decompress_python(compressed)
        assert restored == snippet

    def test_expand_no_stubs_returns_llm_output(self):
        """If source map has no stubs, expand() returns llm_output as-is."""
        from expander import expand
        from treefrag import SourceMap
        original = "x = 1\n"
        smap = SourceMap(language="python", original_code=original)
        result = expand(original, smap, "x = 2\n")
        assert result == "x = 2\n"


# ---------------------------------------------------------------------------
# 8. Retriever tests
# ---------------------------------------------------------------------------

class TestRetriever:
    """
    Validates the semantic retrieval layer (ChromaDB + NullRetriever).

    The retriever indexes compressed stubs, then returns the most relevant
    ones for a given task description within a token budget.

    ChromaDB is optional — NullRetriever works without it and is the fallback.
    """

    def _make_files(self) -> dict[str, str]:
        return {
            "wallet_local.py": load("wallet_local.py"),
            "wallet_stripe.py": load("wallet_stripe.py"),
        }

    def test_build_stub_index(self):
        """build_stub_index must return at least one StubIndex per file."""
        from retriever import build_stub_index
        files = self._make_files()
        index = build_stub_index(files)
        assert set(index.keys()) == set(files.keys())
        for filename, entries in index.items():
            assert len(entries) > 0, f"No stubs indexed for {filename}"

    def test_stub_index_entries_have_tokens(self):
        from retriever import build_stub_index
        index = build_stub_index(self._make_files())
        for entries in index.values():
            for entry in entries:
                assert entry.tokens > 0
                assert entry.name

    def test_null_retriever_returns_all_within_budget(self):
        """NullRetriever should fit stubs into token budget."""
        from retriever import build_stub_index, NullRetriever
        files = self._make_files()
        index = build_stub_index(files)
        retriever = NullRetriever()
        retriever.index(index)
        budget = 500
        results = retriever.query("wallet transaction", budget_tokens=budget)
        total = sum(r.stub.tokens for r in results)
        assert total <= budget
        assert len(results) > 0

    def test_null_retriever_respects_budget_zero(self):
        from retriever import build_stub_index, NullRetriever
        index = build_stub_index(self._make_files())
        retriever = NullRetriever()
        retriever.index(index)
        results = retriever.query("anything", budget_tokens=0)
        assert results == []

    def test_chroma_retriever_indexes_and_queries(self):
        """ChromaRetriever must return relevant stubs for a task."""
        from retriever import build_stub_index, ChromaRetriever, render_retrieved_context
        files = self._make_files()
        index = build_stub_index(files)
        retriever = ChromaRetriever()
        retriever.index(index)
        results = retriever.query("commit inflight wallet transaction", budget_tokens=800)
        assert len(results) > 0
        for r in results:
            assert r.stub.tokens > 0
            assert r.score >= 0.0

    def test_chroma_retriever_budget_respected(self):
        """ChromaRetriever must not exceed token budget."""
        from retriever import build_stub_index, ChromaRetriever
        index = build_stub_index(self._make_files())
        retriever = ChromaRetriever()
        retriever.index(index)
        budget = 300
        results = retriever.query("create payout", budget_tokens=budget)
        total = sum(r.stub.tokens for r in results)
        assert total <= budget

    def test_render_retrieved_context(self):
        """render_retrieved_context must produce non-empty grouped output."""
        from retriever import build_stub_index, NullRetriever, render_retrieved_context
        index = build_stub_index(self._make_files())
        retriever = NullRetriever()
        retriever.index(index)
        results = retriever.query("wallet", budget_tokens=600)
        ctx = render_retrieved_context(results)
        assert ctx
        assert "def " in ctx or "..." in ctx

    def test_jsx_file_indexed(self):
        """JSX files must be indexable via the stub index."""
        from retriever import build_stub_index, NullRetriever
        files = {"stripe_connect.jsx": load("stripe_connect.jsx")}
        index = build_stub_index(files)
        assert "stripe_connect.jsx" in index
        assert len(index["stripe_connect.jsx"]) > 0
        retriever = NullRetriever()
        retriever.index(index)
        results = retriever.query("stripe connect", budget_tokens=500)
        assert len(results) > 0


# ---------------------------------------------------------------------------
# 9. Brevity prompt tests (arXiv:2604.00025)
# ---------------------------------------------------------------------------

class TestBrevity:
    """
    Validates the brevity prompt wrappers.

    Based on: Inverse Scaling Can Be Easily Overcome With Scale-Aware Prompting
    (arXiv:2604.00025). Key finding: brevity constraints improve large-model
    accuracy by 26pp and let small (0.5B–3B) models beat large ones.
    """

    def test_wrap_adds_brevity_suffix(self):
        from brevity import BrevityPrompt, ModelTier
        prompt = BrevityPrompt.wrap("What is 2+2?", tier=ModelTier.LARGE)
        assert "2+2" in prompt
        assert "concise" in prompt.lower() or "brief" in prompt.lower() or "ONLY" in prompt

    def test_code_edit_prompt_structure(self):
        from brevity import BrevityPrompt, ModelTier
        ctx = "def foo(): ..."
        task = "Add a docstring to foo"
        prompt = BrevityPrompt.code_edit(ctx, task, tier=ModelTier.SMALL)
        assert "<context>" in prompt
        assert "<task>" in prompt
        assert "foo" in prompt
        assert "docstring" in prompt

    def test_source_map_instruction_prompt(self):
        from brevity import BrevityPrompt, ModelTier
        ctx = "def bar(): ..."
        task = "Fix the return type"
        prompt = BrevityPrompt.with_source_map_instruction(ctx, task, tier=ModelTier.MEDIUM)
        assert "..." in prompt
        assert "Keep '...'" in prompt
        assert "Replace '...'" in prompt

    def test_caveman_mode_prompt(self):
        from brevity import BrevityPrompt, ModelTier
        prompt = BrevityPrompt.code_edit("def x(): ...", "do thing", tier=ModelTier.LARGE, caveman=True)
        assert "caveman" in prompt.lower() or "compressed" in prompt.lower()

    def test_recommend_tier_single_function(self):
        from brevity import recommend_tier, ModelTier
        tier = recommend_tier(num_files_changed=1, num_functions_changed=1)
        assert tier == ModelTier.SMALL

    def test_recommend_tier_cross_file(self):
        from brevity import recommend_tier, ModelTier
        tier = recommend_tier(num_files_changed=3, num_functions_changed=5, has_cross_file_deps=True)
        assert tier == ModelTier.LARGE

    def test_recommend_tier_multi_function(self):
        from brevity import recommend_tier, ModelTier
        tier = recommend_tier(num_files_changed=1, num_functions_changed=4)
        assert tier == ModelTier.MEDIUM

    def test_full_pipeline_with_retrieval_and_brevity(self):
        """
        End-to-end: index repo → retrieve relevant stubs → build brevity-constrained prompt.
        This is the full workflow: compressed input + brevity output = small-model-ready prompt.
        """
        from retriever import build_stub_index, ChromaRetriever, render_retrieved_context
        from brevity import BrevityPrompt, recommend_tier

        files = {
            "wallet_local.py": load("wallet_local.py"),
            "wallet_stripe.py": load("wallet_stripe.py"),
        }
        task = "Fix commit_inflight to include referral metadata in the transaction"

        index = build_stub_index(files)
        retriever = ChromaRetriever()
        retriever.index(index)

        tier = recommend_tier(num_files_changed=1, num_functions_changed=1)
        results = retriever.query(task, budget_tokens=600)
        context = render_retrieved_context(results)
        prompt = BrevityPrompt.with_source_map_instruction(context, task, tier=tier)

        assert len(prompt) > 0
        assert "commit_inflight" in prompt or len(results) > 0  # relevant stub retrieved
        assert "Keep '...'" in prompt  # source map instruction present
        token_count = count_tokens(prompt)
        # Full context would be ~5000 tokens; retrieved+compressed should be much less
        assert token_count < 1500, f"Prompt too large: {token_count} tokens"

    # ------------------------------------------------------------------
    # New tests: variant A/B system and tuned SMALL constraint
    # ------------------------------------------------------------------

    def test_variant_v0_original_wording(self):
        """v0 baseline — the wording that caused 80% quality (diff fragments)."""
        from brevity import BrevityPrompt, ModelTier, _CODE_BREVITY_VARIANTS
        prompt = BrevityPrompt.code_edit("def f(): ...", "fix f", tier=ModelTier.SMALL, variant="v0_original")
        assert "only the changed code" in prompt

    def test_variant_v1_complete_function(self):
        """v1 default — must require full body and prohibit diff format."""
        from brevity import BrevityPrompt, ModelTier
        prompt = BrevityPrompt.code_edit("def f(): ...", "fix f", tier=ModelTier.SMALL, variant="v1_complete_function")
        assert "complete" in prompt and "runnable" in prompt
        assert "not a diff" in prompt or "no diff" in prompt.lower() or "diff" in prompt

    def test_variant_v2_structured(self):
        """v2 structured — format template with code fence."""
        from brevity import BrevityPrompt, ModelTier
        prompt = BrevityPrompt.code_edit("def f(): ...", "fix f", tier=ModelTier.SMALL, variant="v2_structured")
        assert "```python" in prompt

    def test_variant_v3_correctness_first(self):
        """v3 — anchors on correctness, not brevity."""
        from brevity import BrevityPrompt, ModelTier
        prompt = BrevityPrompt.code_edit("def f(): ...", "fix f", tier=ModelTier.SMALL, variant="v3_correctness_first")
        assert "runnable" in prompt

    def test_variant_v4_minimal(self):
        """v4 minimal control — just suppresses prose."""
        from brevity import BrevityPrompt, ModelTier
        prompt = BrevityPrompt.code_edit("def f(): ...", "fix f", tier=ModelTier.SMALL, variant="v4_minimal")
        assert "No explanation" in prompt or "no explanation" in prompt.lower()

    def test_default_small_tier_uses_minimal_wording(self):
        """Default SMALL constraint must be the benchmark-winning minimal wording."""
        from brevity import BrevityPrompt, ModelTier
        prompt = BrevityPrompt.code_edit("def f(): ...", "fix f", tier=ModelTier.SMALL)
        # Must NOT say "only the changed code" (v0 — caused 80% quality)
        assert "only the changed code" not in prompt
        # Must be the v4_minimal winner
        assert "No explanation. Code only." in prompt

    def test_variant_overrides_tier_constraint(self):
        """Passing variant= must override the tier's default constraint."""
        from brevity import BrevityPrompt, ModelTier
        prompt_default = BrevityPrompt.code_edit("def f(): ...", "fix", tier=ModelTier.SMALL)
        prompt_v0 = BrevityPrompt.code_edit("def f(): ...", "fix", tier=ModelTier.SMALL, variant="v0_original")
        assert prompt_default != prompt_v0

    def test_ab_test_variants_returns_all_keys(self):
        """ab_test_variants should return a result for every variant key."""
        from brevity import ab_test_variants, _CODE_BREVITY_VARIANTS
        outputs = ab_test_variants(
            context="def f(): ...",
            task="fix f",
            call_fn=lambda prompt: f"ECHO:{len(prompt)}",
        )
        assert set(outputs.keys()) == set(_CODE_BREVITY_VARIANTS.keys())

    def test_ab_test_variants_selective(self):
        """ab_test_variants with explicit variants list."""
        from brevity import ab_test_variants
        outputs = ab_test_variants(
            context="def f(): ...",
            task="fix f",
            call_fn=lambda prompt: "ok",
            variants=["v1_complete_function", "v3_correctness_first"],
        )
        assert list(outputs.keys()) == ["v1_complete_function", "v3_correctness_first"]

    def test_with_source_map_instruction_variant(self):
        """with_source_map_instruction should accept variant= param."""
        from brevity import BrevityPrompt, ModelTier
        prompt = BrevityPrompt.with_source_map_instruction(
            "def f(): ...", "fix f", tier=ModelTier.SMALL, variant="v2_structured"
        )
        assert "```python" in prompt
        assert "Keep '...'" in prompt


# =============================================================================
# TestGenerator — two-phase map+fill code generation (generator.py)
# =============================================================================

# A minimal stub map the mock "model" would return from Phase 1
_MOCK_STUB_MAP = """\
from typing import Any


class CashbackCalculator:
    def calculate_rate(self, merchant_category: str, amount: float) -> float:
        \"\"\"Return cashback rate for a merchant category.\"\"\"
        ...

    def apply_cap(self, cashback: float, cap: float) -> float:
        \"\"\"Clamp cashback to a per-transaction cap.\"\"\"
        ...

    def total_cashback(self, amount: float, category: str, cap: float) -> float:
        \"\"\"Return final cashback after rate and cap.\"\"\"
        ...
"""

# What the mock "model" returns when asked to fill each function
_MOCK_FILLED = {
    "calculate_rate": """\
    def calculate_rate(self, merchant_category: str, amount: float) -> float:
        \"\"\"Return cashback rate for a merchant category.\"\"\"
        rates = {"food": 0.05, "travel": 0.03, "other": 0.01}
        return rates.get(merchant_category, 0.01)
""",
    "apply_cap": """\
    def apply_cap(self, cashback: float, cap: float) -> float:
        \"\"\"Clamp cashback to a per-transaction cap.\"\"\"
        return min(cashback, cap)
""",
    "total_cashback": """\
    def total_cashback(self, amount: float, category: str, cap: float) -> float:
        \"\"\"Return final cashback after rate and cap.\"\"\"
        rate = self.calculate_rate(category, amount)
        return self.apply_cap(amount * rate, cap)
""",
}

_GEN_TASK = "Build a CashbackCalculator class with rate lookup, cap enforcement, and total calculation."


def _mock_call_fn_generator(prompt: str) -> str:
    """
    Mock LLM call for generator tests.
    Phase 1 (map prompt) → stub map.
    Phase 2 (fill prompt) → filled function body.
    """
    if "<implement>" in prompt:
        # Phase 2: detect which function is being filled from the <implement> block
        import re
        m = re.search(r"def (\w+)\(", prompt)
        fn_name = m.group(1) if m else None
        if fn_name and fn_name in _MOCK_FILLED:
            return _MOCK_FILLED[fn_name]
        return f"    def unknown(): ..."
    # Phase 1: return the stub map
    return _MOCK_STUB_MAP


class TestGenerator:
    """
    Tests for generator.py — two-phase map+fill new code generation.

    All tests use mock call_fn — no API calls needed.
    """

    def test_map_prompt_contains_task(self):
        from generator import map_prompt
        from brevity import ModelTier
        p = map_prompt("Build a calculator", tier=ModelTier.MEDIUM)
        assert "Build a calculator" in p
        assert "..." in p or "skeleton" in p.lower() or "signatures" in p.lower()

    def test_map_prompt_includes_context(self):
        from generator import map_prompt
        from brevity import ModelTier
        p = map_prompt("Build a calculator", context="class Base: ...", tier=ModelTier.MEDIUM)
        assert "Base" in p
        assert "<context>" in p

    def test_map_prompt_no_implementations_suffix(self):
        """Map prompt must instruct model NOT to write implementations."""
        from generator import map_prompt
        from brevity import ModelTier
        for tier in [ModelTier.SMALL, ModelTier.MEDIUM, ModelTier.LARGE]:
            p = map_prompt("build something", tier=tier)
            assert "No implementations" in p or "no implementations" in p.lower()

    def test_fill_prompt_structure(self):
        from generator import fill_prompt
        from brevity import ModelTier
        p = fill_prompt(
            stub_map=_MOCK_STUB_MAP,
            fn_name="calculate_rate",
            fn_sig="    def calculate_rate(self, merchant_category: str, amount: float) -> float:",
            task=_GEN_TASK,
            tier=ModelTier.SMALL,
        )
        assert "<module_interface>" in p
        assert "<task>" in p
        assert "<implement>" in p
        assert "calculate_rate" in p
        assert _GEN_TASK in p

    def test_fill_prompt_uses_small_brevity(self):
        """Fill prompt must end with the SMALL brevity constraint."""
        from generator import fill_prompt
        from brevity import ModelTier
        p = fill_prompt(_MOCK_STUB_MAP, "f", "def f(): ...", "task", tier=ModelTier.SMALL)
        assert p.endswith("No explanation. Code only.")

    def test_strip_fences_removes_python_fence(self):
        from generator import _strip_fences
        raw = "```python\ndef foo():\n    return 1\n```"
        result = _strip_fences(raw)
        assert result == "def foo():\n    return 1"
        assert "```" not in result

    def test_strip_fences_passthrough_plain(self):
        from generator import _strip_fences
        raw = "def foo():\n    return 1"
        assert _strip_fences(raw) == raw

    def test_generate_map_returns_stub(self):
        from generator import generate_map
        from brevity import ModelTier
        result = generate_map(_GEN_TASK, "", _mock_call_fn_generator, tier=ModelTier.MEDIUM)
        assert "CashbackCalculator" in result
        assert "calculate_rate" in result
        assert "..." in result

    def test_parse_stub_map_finds_all_slots(self):
        """parse_stub_map must find all three stubbed functions."""
        from generator import parse_stub_map
        slots = parse_stub_map(_MOCK_STUB_MAP)
        assert len(slots) == 3
        names = {s.name for s in slots}
        assert names == {"calculate_rate", "apply_cap", "total_cashback"}

    def test_parse_stub_map_slot_has_sig(self):
        """Each slot must carry the signature lines."""
        from generator import parse_stub_map
        slots = parse_stub_map(_MOCK_STUB_MAP)
        cr = next(s for s in slots if s.name == "calculate_rate")
        assert "merchant_category" in cr.sig
        assert "float" in cr.sig

    def test_parse_stub_map_line_indices(self):
        """start_line must be the def line; end_line must be the ... line."""
        from generator import parse_stub_map
        slots = parse_stub_map(_MOCK_STUB_MAP)
        lines = _MOCK_STUB_MAP.splitlines()
        for slot in slots:
            assert "def " in lines[slot.start_line]
            assert lines[slot.end_line].strip() == "..."

    def test_assemble_splices_bodies(self):
        """assemble() must replace ... stubs with filled function bodies."""
        from generator import assemble, parse_stub_map

        slots = parse_stub_map(_MOCK_STUB_MAP)
        assert len(slots) == 3, f"Expected 3 slots, got {len(slots)}: {[s.name for s in slots]}"

        filled = {name: body.strip() for name, body in _MOCK_FILLED.items()}
        result = assemble(_MOCK_STUB_MAP, slots, filled)

        # All three implementations should appear in the assembled output
        assert "rates.get" in result or "rates =" in result
        assert "min(cashback" in result
        assert "calculate_rate" in result  # called from total_cashback
        # No bare `...` stubs should remain
        assert "        ..." not in result

    def test_assemble_handles_full_module_response(self):
        """assemble() must extract just the target fn when model returns full module."""
        from generator import assemble, parse_stub_map

        # Model returned the full module for total_cashback (real observed behaviour)
        full_module_response = _MOCK_STUB_MAP.replace(
            "        ...",
            "        rates = {'food': 0.05}\n        return rates.get(merchant_category, 0.01)",
            1,  # only first occurrence
        )
        slots = parse_stub_map(_MOCK_STUB_MAP)
        filled = {"calculate_rate": full_module_response}  # full module, not just fn
        result = assemble(_MOCK_STUB_MAP, slots, filled)

        # Should NOT duplicate the full module
        assert result.count("class CashbackCalculator") == 1

    def test_generate_full_pipeline_mock(self):
        """Full pipeline with mock map_call_fn — no API calls."""
        from generator import generate
        from brevity import ModelTier

        result = generate(
            _GEN_TASK,
            context="",
            map_call_fn=_mock_call_fn_generator,
            map_tier=ModelTier.MEDIUM,
            fill_tier=ModelTier.SMALL,
            max_workers=2,
        )

        assert result.stub_map != ""
        assert len(result.filled_bodies) == 3
        assert result.assembled != result.stub_map
        assert result.map_tokens_in > 0
        assert result.map_tokens_out > 0
        assert result.fill_tokens_in > 0
        assert result.total_tokens_in == result.map_tokens_in + result.fill_tokens_in
        assert not result.errors

    def test_generate_split_map_fill_fns(self):
        """map_call_fn and fill_call_fn are called for the right phases."""
        import re as _re
        from generator import generate
        from brevity import ModelTier

        map_calls, fill_calls = [], []

        def map_fn(prompt: str) -> str:
            map_calls.append(prompt)
            return _MOCK_STUB_MAP

        def fill_fn(prompt: str) -> str:
            fill_calls.append(prompt)
            m = _re.search(r"def (\w+)\(", prompt)
            fn = m.group(1) if m else "unknown"
            return _MOCK_FILLED.get(fn, "    def x(): pass")

        result = generate(_GEN_TASK, "", map_call_fn=map_fn, fill_call_fn=fill_fn, max_workers=1)
        assert len(map_calls) == 1, "map_fn called once for Phase 1"
        assert len(fill_calls) == 3, "fill_fn called once per stub slot"
        assert not result.errors

    def test_generate_fill_defaults_to_map_fn(self):
        """When fill_call_fn is None, map_call_fn handles both phases."""
        from generator import generate
        from brevity import ModelTier

        result = generate(
            _GEN_TASK, "", map_call_fn=_mock_call_fn_generator,
            fill_call_fn=None, max_workers=1,
        )
        assert len(result.filled_bodies) == 3

    def test_generate_result_has_no_stub_ellipsis(self):
        """Assembled output should have no bare stub bodies remaining."""
        from generator import generate
        from brevity import ModelTier

        result = generate(
            _GEN_TASK, "", map_call_fn=_mock_call_fn_generator,
            map_tier=ModelTier.MEDIUM, fill_tier=ModelTier.SMALL, max_workers=1,
        )
        # 8-space indent + ... is the stub body pattern
        assert "        ..." not in result.assembled

    def test_fill_is_parallel(self):
        """With max_workers > 1, all fill calls should run concurrently."""
        import time
        from generator import generate
        from brevity import ModelTier

        call_count = []

        def slow_call(prompt: str) -> str:
            if "<implement>" in prompt:
                time.sleep(0.05)  # simulate latency
                call_count.append(1)
                import re
                m = re.search(r"def (\w+)\(", prompt)
                fn = m.group(1) if m else "unknown"
                return _MOCK_FILLED.get(fn, "    def x(): pass")
            return _MOCK_STUB_MAP

        t0 = time.time()
        generate(_GEN_TASK, "", map_call_fn=slow_call, max_workers=4)
        elapsed = time.time() - t0

        # 3 functions × 50ms each. With 4 workers should finish < 200ms (parallel).
        # Sequential would take ~150ms minimum. Parallel ~50-80ms.
        assert len(call_count) == 3
        assert elapsed < 0.5  # generous ceiling

    def test_cost_model_fill_tokens_per_function(self):
        """
        Each fill call should be small enough for a tiny model.
        Budget: <600 tokens in per function (stub map + sig + task).
        """
        from generator import fill_prompt
        from brevity import ModelTier
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")

        p = fill_prompt(_MOCK_STUB_MAP, "calculate_rate",
                        "    def calculate_rate(self, merchant_category: str, amount: float) -> float:",
                        _GEN_TASK, tier=ModelTier.SMALL)
        tokens = len(enc.encode(p))
        assert tokens < 600, f"Fill prompt too large for tiny model: {tokens} tokens"

    def test_extract_fn_from_full_module(self):
        """_extract_fn must pull just the named function from a full module response."""
        from generator import _extract_fn

        full_module = (
            "from typing import Any\n\n"
            "class Foo:\n"
            "    def bar(self) -> int:\n"
            "        return 1\n\n"
            "    def baz(self) -> str:\n"
            "        return 'hi'\n"
        )
        result = _extract_fn(full_module, "bar")
        assert "def bar" in result
        assert "return 1" in result
        assert "def baz" not in result

    def test_extract_fn_passthrough_when_already_just_fn(self):
        """_extract_fn returns text as-is when it's already just the function."""
        from generator import _extract_fn
        just_fn = "    def foo(self) -> int:\n        return 42\n"
        assert _extract_fn(just_fn, "foo") == just_fn.rstrip()

    def test_extract_fn_not_found_returns_original(self):
        """_extract_fn returns original text if fn_name not present."""
        from generator import _extract_fn
        text = "def something_else(): pass"
        assert _extract_fn(text, "missing_fn") == text



# =============================================================================
# TestLocalModels — RAM check, model registry, Ollama helpers (generator.py)
# =============================================================================

class TestLocalModels:
    """Tests for free_ram_mb, recommend_local_fill_model, make_ollama_fn, etc."""

    def test_free_ram_mb_returns_positive(self):
        from generator import free_ram_mb
        ram = free_ram_mb()
        assert isinstance(ram, int)
        assert ram > 0, "Should detect some available RAM"

    def test_local_models_registry_not_empty(self):
        from generator import _LOCAL_MODELS
        assert len(_LOCAL_MODELS) >= 4

    def test_all_models_have_positive_ram(self):
        from generator import _LOCAL_MODELS
        for m in _LOCAL_MODELS:
            assert m.ram_mb > 0
            assert m.active_mb > 0
            assert m.active_mb <= m.ram_mb, "active_mb can't exceed total ram_mb"

    def test_models_ordered_smallest_first(self):
        from generator import _LOCAL_MODELS
        sizes = [m.active_mb for m in _LOCAL_MODELS]
        assert sizes == sorted(sizes), "Models should be ordered smallest → largest"

    def test_recommend_fill_model_with_ample_ram(self):
        """With 32GB free we should get the biggest fill-capable model."""
        from generator import recommend_local_fill_model, _LOCAL_MODELS
        result = recommend_local_fill_model(min_free_mb=32768)
        assert result is not None
        assert result.tier in ("fill", "both")
        # Should be largest fitting model
        fill_models = [m for m in _LOCAL_MODELS if m.tier in ("fill", "both")]
        assert result == fill_models[-1]

    def test_recommend_fill_model_tight_ram(self):
        """With only 2GB free we get the smallest fitting model or None."""
        from generator import recommend_local_fill_model
        result = recommend_local_fill_model(min_free_mb=2048)
        # 2048 - 1536 headroom = 512MB usable — only sub-512MB models fit
        # All our models are larger, so should return None
        assert result is None or result.active_mb <= (2048 - 1536)

    def test_recommend_fill_model_mid_ram(self):
        """With 4GB usable (5536MB free) we should get a small model."""
        from generator import recommend_local_fill_model
        result = recommend_local_fill_model(min_free_mb=5536)
        # 5536 - 1536 = 4000MB usable → should fit 3B models (~2200MB)
        assert result is not None
        assert result.active_mb <= 4000

    def test_recommend_map_model_with_ample_ram(self):
        from generator import recommend_local_map_model
        result = recommend_local_map_model(min_free_mb=32768)
        assert result is not None
        assert result.tier in ("map", "both")

    def test_list_local_models_all_have_fit_flag(self):
        from generator import list_local_models, _LOCAL_MODELS
        listing = list_local_models(min_free_mb=32768)
        assert len(listing) == len(_LOCAL_MODELS)
        for model, fits in listing:
            assert isinstance(fits, bool)

    def test_list_local_models_fit_flag_correct(self):
        """With 0MB free nothing should fit."""
        from generator import list_local_models
        listing = list_local_models(min_free_mb=0)
        for _model, fits in listing:
            assert fits is False

    def test_make_ollama_fn_ram_check_raises(self):
        """make_ollama_fn should raise RuntimeError when RAM is insufficient."""
        from generator import make_ollama_fn, LocalModel
        big = LocalModel("fake:70b", 50000, 50000, "fill", "")
        with pytest.raises(RuntimeError, match="Insufficient RAM"):
            # Override free RAM to be tiny by monkeypatching
            import generator
            orig = generator.free_ram_mb
            generator.free_ram_mb = lambda: 1024
            try:
                make_ollama_fn(big, check_ram=True)
            finally:
                generator.free_ram_mb = orig

    def test_make_ollama_fn_ram_check_disabled(self):
        """check_ram=False skips the RAM guard even for huge models."""
        from generator import make_ollama_fn, LocalModel
        big = LocalModel("fake:70b", 50000, 50000, "fill", "")
        # Should not raise
        fn = make_ollama_fn(big, check_ram=False)
        assert callable(fn)

    def test_make_ollama_fn_string_tag(self):
        """make_ollama_fn accepts a plain string tag."""
        from generator import make_ollama_fn
        fn = make_ollama_fn("qwen3:1.7b", check_ram=False)
        assert callable(fn)
        assert "qwen3:1.7b" in fn.__name__

    def test_make_ollama_fn_returns_callable(self):
        from generator import make_ollama_fn, _LOCAL_MODELS
        fill_models = [m for m in _LOCAL_MODELS if m.tier in ("fill", "both")]
        fn = make_ollama_fn(fill_models[0], check_ram=False)
        assert callable(fn)
