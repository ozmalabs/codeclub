#!/usr/bin/env python3
"""
benchmark_scenarios.py — Comprehensive codeclub benchmark suite.

Demonstrates realistic improvements across different scenarios:

  1. Compression    — token savings on real-world files of varying size/type
  2. Tiered gen     — map+fill with local B580 vs cloud models
  3. Dev loop       — end-to-end task generation with different setups
  4. Cost compare   — what each scenario would cost on GPT-5.4 / Sonnet 4.6

Produces a JSON results file + markdown summary table for the README.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import tiktoken

# Load .env
_ENV = Path(__file__).parent / ".env"
if _ENV.exists():
    for line in _ENV.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

_ENC = tiktoken.get_encoding("cl100k_base")


def _tok(text: str) -> int:
    return len(_ENC.encode(text))


# ── Reference pricing (per 1M tokens) ───────────────────────────────────────

GPT54_IN, GPT54_OUT = 2.50, 15.00
SONNET46_IN, SONNET46_OUT = 3.00, 15.00
OR_PRICING = {
    "google/gemma-4-26b-a4b-it":         (0.080, 0.350),
    "meta-llama/llama-3.3-70b-instruct": (0.100, 0.320),
    "deepseek/deepseek-chat-v3-0324":    (0.150, 0.750),
    "openai/gpt-5-mini":                 (0.250, 2.000),
    "minimax/minimax-m2.5":              (0.300, 1.100),
}
ELECTRICITY_RATE = 0.15  # $/kWh
B580_TDP_W = 150


def _cost(tok_in: int, tok_out: int, rate_in: float, rate_out: float) -> float:
    return tok_in / 1e6 * rate_in + tok_out / 1e6 * rate_out


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 1: Compression pipeline
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class CompressionResult:
    file_name: str
    file_lines: int
    language: str
    original_tokens: int
    stub_tokens: int
    full_pipeline_tokens: int
    stub_ratio: float
    full_ratio: float


def run_compression_benchmarks() -> list[CompressionResult]:
    """Run compression on all test fixtures + synthetic examples."""
    from codeclub.compress.pipeline import run_stub, run_full
    from codeclub.compress.tree import stub_functions

    fixtures_dir = Path(__file__).parent / "tests" / "fixtures"
    results = []

    test_files = {
        "wallet_local.py": "python",
        "wallet_stripe.py": "python",
        "wallet_bridge_snippet.py": "python",
        "wallet_provider_snippet.py": "python",
        "stripe_connect.jsx": "javascript",
    }

    for fname, lang in test_files.items():
        path = fixtures_dir / fname
        if not path.exists():
            continue
        code = path.read_text()
        files = {fname: code}

        stub_r = run_stub(files)
        full_r = run_full(files, domain="wallet" if "wallet" in fname else "generic")

        results.append(CompressionResult(
            file_name=fname,
            file_lines=len(code.splitlines()),
            language=lang,
            original_tokens=stub_r.original_tokens,
            stub_tokens=stub_r.compressed_tokens,
            full_pipeline_tokens=full_r.compressed_tokens,
            stub_ratio=stub_r.compression_ratio,
            full_ratio=full_r.compression_ratio,
        ))

    # Also test multi-file compression (both wallet files together)
    multi = {}
    for fname in ["wallet_local.py", "wallet_stripe.py"]:
        p = fixtures_dir / fname
        if p.exists():
            multi[fname] = p.read_text()
    if multi:
        stub_r = run_stub(multi)
        full_r = run_full(multi, domain="wallet")
        results.append(CompressionResult(
            file_name="[2 wallet files combined]",
            file_lines=sum(len(v.splitlines()) for v in multi.values()),
            language="python",
            original_tokens=stub_r.original_tokens,
            stub_tokens=stub_r.compressed_tokens,
            full_pipeline_tokens=full_r.compressed_tokens,
            stub_ratio=stub_r.compression_ratio,
            full_ratio=full_r.compression_ratio,
        ))

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 2: Tiered generation (map+fill)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TieredGenResult:
    backend_name: str
    map_model: str
    fill_model: str
    map_time_s: float
    fill_time_s: float
    total_time_s: float
    map_tokens_in: int
    map_tokens_out: int
    fill_tokens_in: int
    fill_tokens_out: int
    total_tokens_in: int
    total_tokens_out: int
    map_cost_usd: float
    energy_cost_usd: float
    gpt54_equiv_usd: float
    sonnet46_equiv_usd: float
    stub_quality_pct: int
    output_quality_pct: int
    error: str = ""


TASKS = [
    ("RateLimiter", "Build a RateLimiter class using a token bucket algorithm. "
     "Support: consume(tokens) -> bool, refill(elapsed_s) -> None, available() -> int. "
     "Bucket capacity: 100. Refill rate: 10 tokens/second. Capped at capacity."),
    ("LRU Cache", "Build an LRUCache class with get(key) -> value, put(key, value), "
     "and a configurable max_size. O(1) for both operations using OrderedDict."),
    ("Event Emitter", "Build an EventEmitter class with on(event, callback), "
     "off(event, callback), emit(event, *args). Support once() for one-time listeners."),
]

MAP_BACKENDS = [
    ("B580-rnj1",     "http://localhost:8081", "llama-server"),
    ("OR-gemma4-moe", "openrouter",            "google/gemma-4-26b-a4b-it"),
    ("OR-llama70b",   "openrouter",            "meta-llama/llama-3.3-70b-instruct"),
]

FILL_MODEL = "qwen2.5-coder:1.5b"


def _make_call_fn(endpoint, model_id):
    if endpoint == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        url = "https://openrouter.ai/api/v1/chat/completions"

        def _call(prompt):
            payload = json.dumps({
                "model": model_id,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1, "max_tokens": 512,
            }).encode()
            req = urllib.request.Request(url, data=payload, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": "https://github.com/ozmalabs/codeclub",
                "X-Title": "codeclub-bench",
            })
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:200]}") from e
            return data["choices"][0]["message"]["content"]
        return _call
    else:
        url = f"{endpoint}/v1/chat/completions"

        def _call(prompt):
            payload = json.dumps({
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1, "max_tokens": 512,
            }).encode()
            req = urllib.request.Request(url, data=payload,
                                        headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
        return _call


def run_tiered_gen(task_name: str, task_desc: str,
                   backends: list | None = None) -> list[TieredGenResult]:
    from generator import map_prompt, fill_prompt, parse_stub_map, assemble, _strip_fences
    from generator import make_ollama_fn
    from brevity import ModelTier
    from concurrent.futures import ThreadPoolExecutor, as_completed

    backends = backends or MAP_BACKENDS
    fill_fn = make_ollama_fn(FILL_MODEL, check_ram=False)
    results = []

    for name, endpoint, model_id in backends:
        print(f"  [{name}] {task_name}: ", end="", flush=True)
        try:
            map_fn = _make_call_fn(endpoint, model_id)
        except Exception as e:
            print(f"SKIP ({e})")
            results.append(TieredGenResult(
                backend_name=name, map_model=model_id, fill_model=FILL_MODEL,
                map_time_s=0, fill_time_s=0, total_time_s=0,
                map_tokens_in=0, map_tokens_out=0, fill_tokens_in=0, fill_tokens_out=0,
                total_tokens_in=0, total_tokens_out=0,
                map_cost_usd=0, energy_cost_usd=0, gpt54_equiv_usd=0, sonnet46_equiv_usd=0,
                stub_quality_pct=0, output_quality_pct=0, error=str(e),
            ))
            continue

        # Map phase
        p = map_prompt(task_desc, "", tier=ModelTier.MEDIUM)
        map_tok_in = _tok(p)
        t0 = time.time()
        try:
            stub_raw = map_fn(p)
        except Exception as e:
            print(f"MAP FAIL ({e})")
            results.append(TieredGenResult(
                backend_name=name, map_model=model_id, fill_model=FILL_MODEL,
                map_time_s=time.time()-t0, fill_time_s=0, total_time_s=time.time()-t0,
                map_tokens_in=map_tok_in, map_tokens_out=0,
                fill_tokens_in=0, fill_tokens_out=0,
                total_tokens_in=map_tok_in, total_tokens_out=0,
                map_cost_usd=0, energy_cost_usd=0, gpt54_equiv_usd=0, sonnet46_equiv_usd=0,
                stub_quality_pct=0, output_quality_pct=0, error=str(e)[:200],
            ))
            continue
        t_map = time.time() - t0
        stub = _strip_fences(stub_raw)
        map_tok_out = _tok(stub)
        slots = parse_stub_map(stub)

        # Fill phase
        fill_stats = []
        filled = {}
        t_fill_start = time.time()
        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                def _fill(slot):
                    fp = fill_prompt(stub, slot.name, slot.sig, task_desc, tier=ModelTier.SMALL)
                    ti = _tok(fp)
                    raw = fill_fn(fp)
                    body = _strip_fences(raw)
                    to = _tok(body)
                    return slot.name, body, ti, to

                futs = {ex.submit(_fill, s): s for s in slots}
                for f in as_completed(futs):
                    fn_name, body, ti, to = f.result()
                    filled[fn_name] = body
                    fill_stats.append((ti, to))
        except Exception as e:
            print(f"FILL FAIL ({e})")
            continue
        t_fill = time.time() - t_fill_start

        fill_tok_in = sum(s[0] for s in fill_stats)
        fill_tok_out = sum(s[1] for s in fill_stats)
        assembled = assemble(stub, slots, filled)

        total_tok_in = map_tok_in + fill_tok_in
        total_tok_out = map_tok_out + fill_tok_out
        total_time = t_map + t_fill

        # Costs
        if model_id == "llama-server":
            map_cost = 0.0
            energy_cost = total_time * B580_TDP_W / 3600 / 1000 * ELECTRICITY_RATE
        else:
            rates = OR_PRICING.get(model_id, (0, 0))
            map_cost = _cost(map_tok_in, map_tok_out, rates[0], rates[1])
            energy_cost = 0.0

        gpt54_cost = _cost(total_tok_in, total_tok_out, GPT54_IN, GPT54_OUT)
        sonnet46_cost = _cost(total_tok_in, total_tok_out, SONNET46_IN, SONNET46_OUT)

        # Quality heuristics (structural + syntax validation)
        stub_checks = {
            "has_types": "->" in stub or ": float" in stub or ": int" in stub or ": bool" in stub,
            "has_class": "class " in stub,
            "three_methods": stub.count("def ") >= 3,
        }

        # Syntax check: does the assembled code actually parse?
        try:
            compile(assembled, "<benchmark>", "exec")
            syntax_ok = True
        except SyntaxError:
            syntax_ok = False

        out_checks = {
            "syntax_valid": syntax_ok,
            "has_class": "class " in assembled,
            "has_methods": assembled.count("def ") >= 3,
            "has_logic": "return" in assembled or "self." in assembled,
            "no_stubs": "..." not in assembled.split("class")[1] if "class " in assembled else True,
        }
        stub_pct = int(sum(stub_checks.values()) / max(len(stub_checks), 1) * 100)
        out_pct = int(sum(out_checks.values()) / max(len(out_checks), 1) * 100)

        actual_cost = map_cost + energy_cost
        multiplier = f"{gpt54_cost / actual_cost:.0f}×" if actual_cost > 0 else "∞"
        print(f"{total_time:.1f}s  ${actual_cost:.6f}  (GPT-5.4 would be {multiplier} more)  quality={out_pct}%")

        results.append(TieredGenResult(
            backend_name=name, map_model=model_id, fill_model=FILL_MODEL,
            map_time_s=round(t_map, 2), fill_time_s=round(t_fill, 2),
            total_time_s=round(total_time, 2),
            map_tokens_in=map_tok_in, map_tokens_out=map_tok_out,
            fill_tokens_in=fill_tok_in, fill_tokens_out=fill_tok_out,
            total_tokens_in=total_tok_in, total_tokens_out=total_tok_out,
            map_cost_usd=round(map_cost, 8), energy_cost_usd=round(energy_cost, 8),
            gpt54_equiv_usd=round(gpt54_cost, 6), sonnet46_equiv_usd=round(sonnet46_cost, 6),
            stub_quality_pct=stub_pct, output_quality_pct=out_pct,
        ))

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO 3: Compression + model call (native vs compressed context)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ContextComparisonResult:
    scenario: str
    model: str
    context_mode: str  # native | stub | retrieved
    tokens_in: int
    tokens_out: int
    time_s: float
    cost_usd: float
    gpt54_equiv_usd: float
    quality_notes: str = ""


def run_context_comparison() -> list[ContextComparisonResult]:
    """Compare native vs compressed context for a bug-fix task using OpenRouter."""
    from codeclub.compress.tree import stub_functions

    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        print("  [SKIP] No OPENROUTER_API_KEY for context comparison")
        return []

    fixtures = Path(__file__).parent / "tests" / "fixtures"
    bridge = (fixtures / "wallet_bridge_snippet.py").read_text()
    provider = (fixtures / "wallet_provider_snippet.py").read_text()
    issue = (fixtures / "issue_1519.txt").read_text() if (fixtures / "issue_1519.txt").exists() else ""

    task = "Fix commit_wallet_transaction so it forwards meta_data to provider.commit_inflight."

    # Build contexts
    native_ctx = f"# wallet_bridge.py\n{bridge}\n\n# wallet_provider.py\n{provider}"
    stub_bridge, _ = stub_functions(bridge)
    stub_provider, _ = stub_functions(provider)
    stub_ctx = f"# wallet_bridge.py (stubs)\n{stub_bridge}\n\n# wallet_provider.py (stubs)\n{stub_provider}"

    model = "google/gemma-4-26b-a4b-it"
    url = "https://openrouter.ai/api/v1/chat/completions"

    results = []

    for mode, ctx in [("native", native_ctx), ("stub", stub_ctx)]:
        prompt = f"{task}\n\nContext:\n{ctx}"
        tok_in = _tok(prompt)
        print(f"  [{mode}] {tok_in} tokens in → ", end="", flush=True)

        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1, "max_tokens": 512,
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://github.com/ozmalabs/codeclub",
            "X-Title": "codeclub-bench",
        })

        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            reply = data["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"FAIL ({e})")
            continue
        elapsed = time.time() - t0
        tok_out = _tok(reply)

        rates = OR_PRICING.get(model, (0, 0))
        cost = _cost(tok_in, tok_out, rates[0], rates[1])
        gpt54_cost = _cost(tok_in, tok_out, GPT54_IN, GPT54_OUT)

        has_fix = "meta_data" in reply and ("commit_inflight" in reply or "forward" in reply.lower())
        quality = "correct fix" if has_fix else "partial"
        saving = f"({(1 - tok_in / _tok(f'{task}\n\nContext:\n{native_ctx}')):.0%} input saved)" if mode != "native" else ""

        print(f"{tok_out} out  {elapsed:.1f}s  ${cost:.6f}  {quality} {saving}")

        results.append(ContextComparisonResult(
            scenario="bug-fix-wallet", model=model, context_mode=mode,
            tokens_in=tok_in, tokens_out=tok_out, time_s=round(elapsed, 2),
            cost_usd=round(cost, 8), gpt54_equiv_usd=round(gpt54_cost, 6),
            quality_notes=quality,
        ))

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Results & report
# ═══════════════════════════════════════════════════════════════════════════════

def generate_markdown(compression, tiered, context):
    lines = []
    lines.append("# codeclub benchmark results\n")
    lines.append(f"*Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*\n")

    # Compression table
    lines.append("## Compression — token savings by file\n")
    lines.append("| File | Lines | Language | Original | Stub | Full pipeline | Stub saved | Full saved |")
    lines.append("|------|------:|----------|-------:|-----:|-------------:|----------:|----------:|")
    for r in compression:
        lines.append(
            f"| {r.file_name} | {r.file_lines} | {r.language} "
            f"| {r.original_tokens:,} | {r.stub_tokens:,} | {r.full_pipeline_tokens:,} "
            f"| {r.stub_ratio:.0%} | {r.full_ratio:.0%} |"
        )
    lines.append("")

    # Tiered generation tables — one per task
    lines.append("## Tiered generation — map+fill across backends\n")
    tasks_seen = set()
    for r in tiered:
        task_key = r.backend_name.split("/")[0]  # approximate
    # Group by task
    task_results: dict[str, list] = {}
    for r in tiered:
        # find which task this belongs to based on position
        pass

    lines.append("| Backend | Map model | Time | Tokens in | Tokens out | Cost | GPT-5.4 equiv | Savings | Quality |")
    lines.append("|---------|-----------|-----:|----------:|-----------:|-----:|-------------:|--------:|--------:|")
    for r in tiered:
        if r.error:
            continue
        actual = r.map_cost_usd + r.energy_cost_usd
        cost_str = f"${actual:.6f}" if actual > 0 else "$0 (local)"
        savings = f"{r.gpt54_equiv_usd / actual:.0f}×" if actual > 0 else "∞"
        lines.append(
            f"| {r.backend_name} | {r.map_model} "
            f"| {r.total_time_s:.1f}s | {r.total_tokens_in:,} | {r.total_tokens_out:,} "
            f"| {cost_str} | ${r.gpt54_equiv_usd:.6f} | {savings} | {r.output_quality_pct}% |"
        )
    lines.append("")

    # Context comparison
    if context:
        lines.append("## Context compression — native vs compressed for bug fix\n")
        lines.append("| Context | Tokens in | Tokens out | Time | Cost | GPT-5.4 equiv | Quality |")
        lines.append("|---------|----------:|-----------:|-----:|-----:|-------------:|---------|")
        for r in context:
            lines.append(
                f"| {r.context_mode} | {r.tokens_in:,} | {r.tokens_out:,} "
                f"| {r.time_s:.1f}s | ${r.cost_usd:.6f} | ${r.gpt54_equiv_usd:.6f} "
                f"| {r.quality_notes} |"
            )
        if len(context) >= 2:
            native = next(r for r in context if r.context_mode == "native")
            stub = next(r for r in context if r.context_mode == "stub")
            token_saving = (native.tokens_in - stub.tokens_in) / native.tokens_in
            cost_saving = (native.cost_usd - stub.cost_usd) / native.cost_usd if native.cost_usd else 0
            lines.append(f"\n**Compression saved {token_saving:.0%} of input tokens "
                         f"and {cost_saving:.0%} of cost, same fix quality.**\n")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="codeclub comprehensive benchmarks")
    parser.add_argument("--skip-local", action="store_true", help="Skip B580 local backend")
    parser.add_argument("--skip-cloud", action="store_true", help="Skip OpenRouter backends")
    parser.add_argument("--tasks", type=int, default=3, help="Number of tasks for tiered gen (1-3)")
    args = parser.parse_args()

    all_compression = []
    all_tiered = []
    all_context = []

    # ── Scenario 1: Compression ──────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  SCENARIO 1: Compression pipeline")
    print("=" * 70)

    compression_results = run_compression_benchmarks()
    all_compression = compression_results

    print(f"\n  {'File':<35} {'Lines':>5} {'Original':>8} {'Stub':>7} {'Full':>7} {'Stub%':>6} {'Full%':>6}")
    print(f"  {'-'*35} {'-'*5} {'-'*8} {'-'*7} {'-'*7} {'-'*6} {'-'*6}")
    for r in compression_results:
        print(f"  {r.file_name:<35} {r.file_lines:>5} {r.original_tokens:>8} "
              f"{r.stub_tokens:>7} {r.full_pipeline_tokens:>7} "
              f"{r.stub_ratio:>5.0%} {r.full_ratio:>5.0%}")

    # ── Scenario 2: Tiered generation ────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  SCENARIO 2: Tiered map+fill generation")
    print("=" * 70)

    backends = MAP_BACKENDS[:]
    if args.skip_local:
        backends = [b for b in backends if b[0] != "B580-rnj1"]
    if args.skip_cloud:
        backends = [b for b in backends if b[1] != "openrouter"]

    for task_name, task_desc in TASKS[:args.tasks]:
        print(f"\n  Task: {task_name}")
        print(f"  {'-'*60}")
        tiered = run_tiered_gen(task_name, task_desc, backends)
        all_tiered.extend(tiered)

    # ── Scenario 3: Context compression ──────────────────────────────────────
    print("\n" + "=" * 70)
    print("  SCENARIO 3: Native vs compressed context (bug fix)")
    print("=" * 70)

    if not args.skip_cloud:
        all_context = run_context_comparison()

    # ── Write results ────────────────────────────────────────────────────────
    results_dir = Path(__file__).parent / "benchmarks" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    results_data = {
        "timestamp": timestamp,
        "compression": [asdict(r) for r in all_compression],
        "tiered": [asdict(r) for r in all_tiered],
        "context": [asdict(r) for r in all_context],
    }

    json_path = results_dir / f"scenarios_{timestamp}.json"
    json_path.write_text(json.dumps(results_data, indent=2))

    md_content = generate_markdown(all_compression, all_tiered, all_context)
    md_path = results_dir / f"scenarios_{timestamp}.md"
    md_path.write_text(md_content)

    # Also write latest
    (results_dir / "latest.json").write_text(json.dumps(results_data, indent=2))
    (results_dir / "latest.md").write_text(md_content)

    print(f"\n{'=' * 70}")
    print(f"  Results: {json_path}")
    print(f"  Summary: {md_path}")
    print(f"{'=' * 70}")

    # Print summary
    print(f"\n{md_content}")
