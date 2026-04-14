"""
benchmark_llm.py — Compare native vs compressed context across Copilot models.

Tests:
  - gpt-5-mini       (small/cheap)
  - gpt-4.1          (mid-tier)
  - claude-sonnet-4.6 (large)

Conditions per model:
  A. native     — full source files, no compression
  B. stub       — structural stubs only (treefrag)
  C. retrieved  — ChromaDB top-K stubs for task
  D. retrieved+brevity — retrieved + scale-aware brevity constraint

Task: Fix commit_inflight to include referral metadata (issue #1519)

Measures: input_tokens, output_tokens, latency_s, quality_score (heuristic)
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import tiktoken
from codeclub.compress.tree import stub_functions
from codeclub.compress.semantic import build_stub_index, ChromaRetriever, render_retrieved_context
from codeclub.compress.brevity import BrevityPrompt, recommend_tier

ROOT = Path(__file__).parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
ENC = tiktoken.get_encoding("cl100k_base")


# ---------------------------------------------------------------------------
# Models to test (cheapest → most expensive)
# ---------------------------------------------------------------------------

MODELS = [
    "gpt-5-mini",
    "gpt-4.1",
    "claude-sonnet-4.6",
]

# Approximate cost per million tokens (USD), input/output — public Copilot pricing estimates
# These are relative indicators only; actual billing via Copilot subscription
COST_PER_1M = {
    "gpt-5-mini":        {"in": 0.15,  "out": 0.60},
    "gpt-4.1":           {"in": 2.00,  "out": 8.00},
    "claude-sonnet-4.6": {"in": 3.00,  "out": 15.00},
}

# The task — based on issue #1519
TASK = """Fix the `commit_inflight` method so that when committing a wallet
transaction, the existing metadata dict is preserved and merged with any new
metadata passed at commit time. Currently metadata passed at commit is ignored."""


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------

def _load_files() -> dict[str, str]:
    return {
        "wallet_local.py": (FIXTURES / "wallet_local.py").read_text(),
        "wallet_stripe.py": (FIXTURES / "wallet_stripe.py").read_text(),
    }


def build_native_context(files: dict[str, str]) -> str:
    parts = []
    for name, code in files.items():
        parts.append(f"# {name}\n```python\n{code}\n```")
    return "\n\n".join(parts)


def build_stub_context(files: dict[str, str]) -> str:
    parts = []
    for name, code in files.items():
        stubbed, _ = stub_functions(code, language="python")
        parts.append(f"# {name}\n```python\n{stubbed}\n```")
    return "\n\n".join(parts)


def build_retrieved_context(files: dict[str, str], task: str, budget: int = 800) -> str:
    index = build_stub_index(files)
    retriever = ChromaRetriever(collection_name=f"bench_{int(time.time())}")
    retriever.index(index)
    results = retriever.query(task, budget_tokens=budget)
    return render_retrieved_context(results)


def build_hybrid_context(files: dict[str, str], task: str, target_fn: str = "commit_inflight", budget: int = 600) -> str:
    """
    Hybrid: full body of the target function + compressed stubs for everything else.

    This is the correct design for code-editing tasks:
      - The function being changed needs its full body (LLM can't fix what it can't see)
      - All other functions only need signatures (structural context)
    """
    import re
    parts = []
    for name, code in files.items():
        stubbed, smap = stub_functions(code, language="python")
        lines = code.splitlines(keepends=True)
        stub_lines = stubbed.splitlines(keepends=True)

        # Find the target function in the original and splice its body back in
        for stub in smap.stubs:
            if stub.name == target_fn:
                orig_body = "".join(lines[stub.orig_start: stub.orig_end + 1])
                stub_lines_list = list(stub_lines)
                replacement = orig_body.splitlines(keepends=True)
                stub_lines_list[stub.comp_start: stub.comp_end + 1] = replacement
                stubbed = "".join(stub_lines_list)
                break

        parts.append(f"# {name}\n```python\n{stubbed}\n```")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Prompt builders per condition
# ---------------------------------------------------------------------------

def make_prompt(context: str, task: str, condition: str, model: str) -> str:
    tier = recommend_tier(num_files_changed=1, num_functions_changed=1)

    if condition == "native":
        return (
            f"You are a Python coding assistant.\n\n"
            f"<context>\n{context}\n</context>\n\n"
            f"<task>\n{task}\n</task>\n\n"
            f"Provide the corrected code."
        )
    elif condition == "stub":
        return (
            f"You are a Python coding assistant. "
            f"Function bodies are replaced with '...' — keep '...' for functions you don't change.\n\n"
            f"<context>\n{context}\n</context>\n\n"
            f"<task>\n{task}\n</task>\n\n"
            f"Provide only the changed function(s)."
        )
    elif condition == "retrieved":
        return BrevityPrompt.with_source_map_instruction(context, task, tier=tier)
    elif condition == "retrieved+brevity":
        return BrevityPrompt.code_edit(context, task, tier=tier, caveman=True)
    elif condition == "hybrid":
        tier = recommend_tier(num_files_changed=1, num_functions_changed=1)
        return BrevityPrompt.with_source_map_instruction(context, task, tier=tier)
    return context


# ---------------------------------------------------------------------------
# API caller — uses GitHub Copilot API via gh token
# ---------------------------------------------------------------------------

def call_copilot(model: str, prompt: str, max_tokens: int = 1024, retries: int = 4) -> dict:
    """Call the Copilot completions API, return full response dict. Retries on empty response."""
    token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }

    for attempt in range(retries + 1):
        if attempt > 0:
            wait = 5 * attempt
            print(f"(retry {attempt}, wait {wait}s) ", end="", flush=True)
            time.sleep(wait)
        result = subprocess.run(
            [
                "curl", "-s",
                "-X", "POST",
                "-H", f"Authorization: Bearer {token}",
                "-H", "Content-Type: application/json",
                "-d", json.dumps(payload),
                "https://api.githubcopilot.com/chat/completions",
            ],
            capture_output=True, text=True, timeout=120,
        )
        stdout = result.stdout.strip()
        if not stdout:
            if attempt < retries:
                continue
            raise RuntimeError("Empty response from API after retries")
        data = json.loads(stdout)
        # Treat explicit rate-limit errors as retryable
        if "error" in data and "rate" in str(data["error"]).lower():
            if attempt < retries:
                continue
        return data
    raise RuntimeError("No response")


# ---------------------------------------------------------------------------
# Quality scorer — heuristic signal: does output address the task?
# ---------------------------------------------------------------------------

def score_quality(output: str) -> tuple[float, str]:
    """
    Heuristic quality check. Returns (score 0–1, reason).
    Checks for task-relevant signals in the output.
    """
    # Deflection penalty — model asked for more context instead of answering
    deflection_phrases = [
        "need to see", "need the full", "need more context", "show me the",
        "please provide", "can you share", "cannot see the body",
    ]
    deflected = any(p in output.lower() for p in deflection_phrases)

    signals = {
        "mentions commit_inflight": "commit_inflight" in output,
        "mentions metadata": "metadata" in output,
        "shows dict merge": any(x in output for x in [".update(", "| ", "{**", "merge", "existing"]),
        "has code block": "```" in output or "def " in output,
        "not empty/deflecting": len(output.strip()) > 50 and not deflected,
    }
    score = sum(signals.values()) / len(signals)
    if deflected:
        score *= 0.3  # heavy penalty for deflection responses
    reasons = [k for k, v in signals.items() if v]
    if deflected:
        reasons.append("DEFLECTED")
    return score, ", ".join(reasons) if reasons else "no signal"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class BenchResult:
    model: str
    condition: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    quality_score: float
    quality_reasons: str
    output_snippet: str
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def est_cost_usd(self) -> float:
        rates = COST_PER_1M.get(self.model, {"in": 1.0, "out": 4.0})
        return (
            self.prompt_tokens / 1_000_000 * rates["in"]
            + self.completion_tokens / 1_000_000 * rates["out"]
        )


# ---------------------------------------------------------------------------
# Run benchmark
# ---------------------------------------------------------------------------

def run_benchmark(models: list[str] | None = None, conditions: list[str] | None = None) -> list[BenchResult]:
    if models is None:
        models = MODELS
    if conditions is None:
        conditions = ["native", "stub", "hybrid", "retrieved", "retrieved+brevity"]

    files = _load_files()

    # Pre-build contexts once
    contexts = {
        "native":             build_native_context(files),
        "stub":               build_stub_context(files),
        "hybrid":             build_hybrid_context(files, TASK),
        "retrieved":          build_retrieved_context(files, TASK),
        "retrieved+brevity":  build_retrieved_context(files, TASK),
    }

    results: list[BenchResult] = []

    for model in models:
        for condition in conditions:
            context = contexts[condition]
            prompt = make_prompt(context, TASK, condition, model)
            prompt_tokens = len(ENC.encode(prompt))

            print(f"  {model:25s} {condition:20s} {prompt_tokens:5d}t in ... ", end="", flush=True)

            t0 = time.time()
            try:
                resp = call_copilot(model, prompt, max_tokens=600)
                latency = time.time() - t0

                if "error" in resp:
                    raise RuntimeError(resp["error"].get("message", str(resp["error"])))

                choice = resp["choices"][0]
                output = choice["message"]["content"] or ""
                usage = resp.get("usage", {})
                comp_tokens = usage.get("completion_tokens", len(ENC.encode(output)))
                in_tokens = usage.get("prompt_tokens", prompt_tokens)

                quality, reasons = score_quality(output)

                results.append(BenchResult(
                    model=model,
                    condition=condition,
                    prompt_tokens=in_tokens,
                    completion_tokens=comp_tokens,
                    latency_s=round(latency, 2),
                    quality_score=quality,
                    quality_reasons=reasons,
                    output_snippet=output[:220].replace("\n", " "),
                ))
                print(f"{comp_tokens:4d}t out  {latency:.1f}s  quality={quality:.0%}")

            except Exception as e:
                latency = time.time() - t0
                results.append(BenchResult(
                    model=model,
                    condition=condition,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=0,
                    latency_s=round(latency, 2),
                    quality_score=0.0,
                    quality_reasons="",
                    output_snippet="",
                    error=str(e)[:100],
                ))
                print(f"ERROR: {e}")

            # Brief pause between calls to avoid rate limits
            time.sleep(3)

    return results


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_report(results: list[BenchResult]) -> None:
    print()
    print("=" * 110)
    print(f"  {'Model':25s} {'Condition':20s} {'In tok':>7} {'Out tok':>7} {'Total':>7} {'Cost $':>8} {'Lat s':>6} {'Quality':>8}")
    print(f"  {'-'*25} {'-'*20} {'-'*7} {'-'*7} {'-'*7} {'-'*8} {'-'*6} {'-'*8}")

    # Group by model for compression ratio display
    native_tokens: dict[str, int] = {}
    for r in results:
        if r.condition == "native" and not r.error:
            native_tokens[r.model] = r.prompt_tokens

    for r in results:
        if r.error:
            print(f"  {r.model:25s} {r.condition:20s}  ERROR: {r.error[:50]}")
            continue
        native = native_tokens.get(r.model, r.prompt_tokens)
        reduction = f"({1 - r.prompt_tokens/native:.0%})" if native and r.condition != "native" else ""
        cost_str = f"${r.est_cost_usd:.5f}"
        print(
            f"  {r.model:25s} {r.condition:20s} "
            f"{r.prompt_tokens:>7} {r.completion_tokens:>7} {r.total_tokens:>7} "
            f"{cost_str:>8} {r.latency_s:>6.1f} {r.quality_score:>7.0%}  {reduction}"
        )

    print("=" * 110)

    # Summary: best cost-quality per model
    print("\n  === COMPRESSION SAVINGS vs NATIVE ===")
    for model in {r.model for r in results}:
        model_results = [r for r in results if r.model == model and not r.error]
        native_r = next((r for r in model_results if r.condition == "native"), None)
        if not native_r:
            continue
        for r in model_results:
            if r.condition == "native":
                continue
            tok_save = 1 - r.prompt_tokens / native_r.prompt_tokens if native_r.prompt_tokens else 0
            cost_save = 1 - r.est_cost_usd / native_r.est_cost_usd if native_r.est_cost_usd else 0
            q_delta = r.quality_score - native_r.quality_score
            print(
                f"  {model:25s} {r.condition:20s}  "
                f"input -{tok_save:.0%}  cost -{cost_save:.0%}  "
                f"quality {'+' if q_delta >= 0 else ''}{q_delta:.0%}"
            )
    print()

    print("  === RAW OUTPUT SNIPPETS ===")
    for r in results:
        if not r.error:
            print(f"\n  [{r.model} / {r.condition}]")
            print(f"  {r.output_snippet[:180]}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_variant_benchmark(model: str = "gpt-5-mini") -> None:
    """
    A/B test SMALL tier brevity constraint variants against the Copilot API.

    Tests all 5 named variants in _CODE_BREVITY_VARIANTS (v0–v4) on the
    retrieved context, prints quality scores side by side.

    v0_original is the old constraint that scored 80% quality (diff fragments).
    v1_complete_function is the new default. This test shows whether it's better.
    """
    from codeclub.compress.brevity import _CODE_BREVITY_VARIANTS, BrevityPrompt, ModelTier

    files = _load_files()
    context = build_retrieved_context(files, TASK)
    context_tokens = len(ENC.encode(context))

    print(f"\n=== VARIANT A/B BENCHMARK: {model} ===")
    print(f"Context: {context_tokens} tokens (retrieved)")
    print(f"Task: {TASK[:70]}...\n")
    print(f"  {'Variant':30s} {'In':>6} {'Out':>6} {'Lat':>6} {'Quality':>8}  Reasons")
    print(f"  {'-'*30} {'-'*6} {'-'*6} {'-'*6} {'-'*8}  -------")

    results = []
    for variant_key in _CODE_BREVITY_VARIANTS:
        prompt = BrevityPrompt.code_edit(context, TASK, tier=ModelTier.SMALL, variant=variant_key)
        prompt_tokens = len(ENC.encode(prompt))
        print(f"  {variant_key:30s} {prompt_tokens:>6} ... ", end="", flush=True)

        t0 = time.time()
        try:
            resp = call_copilot(model, prompt, max_tokens=600)
            latency = time.time() - t0

            if "error" in resp:
                raise RuntimeError(resp["error"].get("message", str(resp["error"])))

            output = resp["choices"][0]["message"]["content"] or ""
            usage = resp.get("usage", {})
            comp_tokens = usage.get("completion_tokens", len(ENC.encode(output)))
            in_tokens = usage.get("prompt_tokens", prompt_tokens)

            quality, reasons = score_quality(output)
            results.append((variant_key, in_tokens, comp_tokens, round(latency, 2), quality, output))
            print(f"{comp_tokens:>6} {latency:>6.1f} {quality:>7.0%}  {reasons[:60]}")

        except Exception as e:
            latency = time.time() - t0
            results.append((variant_key, prompt_tokens, 0, round(latency, 2), 0.0, ""))
            print(f"  ERROR: {e}")

        time.sleep(3)

    print(f"\n=== FULL OUTPUTS ===")
    for variant_key, _, _, _, quality, output in results:
        print(f"\n[{variant_key}]  quality={quality:.0%}")
        print("-" * 60)
        print(output[:800] if output else "(no output)")

    # Highlight winner
    successful = [r for r in results if r[4] > 0]
    if successful:
        best = max(successful, key=lambda x: (x[4], -x[2]))  # quality desc, output tokens asc
        print(f"\n>>> WINNER: {best[0]} — quality {best[4]:.0%}, {best[2]} output tokens")
    else:
        print("\n>>> No successful results")

def run_generator_benchmark(
    map_model: str = "gpt-4.1",
    fill_model: str = "gpt-5-mini",
) -> None:
    """
    Compare two-phase generation vs one-shot for a new-code task.

    map_model:   mid-tier model for Phase 1 (architecture decisions)
    fill_model:  small/cheap model for Phase 2 (isolated function bodies)
    """
    from codeclub.dev.generate import generate, parse_stub_map, map_prompt, fill_prompt
    from codeclub.compress.brevity import ModelTier

    task = (
        "Build a RateLimiter class using a token bucket algorithm. "
        "It should support: consume(tokens) -> bool, "
        "refill(elapsed_s) -> None, "
        "available() -> int. "
        "Bucket capacity: 100. Refill rate: 10 tokens/second. Capped at capacity."
    )
    context = ""  # no existing code — pure generation

    print(f"\n=== GENERATOR BENCHMARK ===")
    print(f"Task: {task[:80]}...")
    print(f"Map model: {map_model}   Fill model: {fill_model}\n")

    # --- Condition A: One-shot on fill_model ---
    one_shot_prompt = (
        f"<task>\n{task}\n</task>\n\n"
        f"Write complete, runnable Python code. No explanation."
    )
    one_shot_tokens_in = len(ENC.encode(one_shot_prompt))
    print(f"  one-shot ({fill_model}): {one_shot_tokens_in}t in ... ", end="", flush=True)
    t0 = time.time()
    try:
        resp = call_copilot(fill_model, one_shot_prompt, max_tokens=800)
        one_shot_lat = time.time() - t0
        one_shot_out = resp["choices"][0]["message"]["content"] or ""
        one_shot_tokens_out = resp.get("usage", {}).get("completion_tokens", len(ENC.encode(one_shot_out)))
        print(f"{one_shot_tokens_out}t out  {one_shot_lat:.1f}s")
    except Exception as e:
        print(f"ERROR: {e}")
        one_shot_out = ""
        one_shot_tokens_out = 0
        one_shot_lat = 0

    time.sleep(3)

    # --- Condition B: Two-phase on map_model + fill_model ---
    print(f"  two-phase (map={map_model}, fill={fill_model}):")

    def call_map(prompt: str) -> str:
        resp = call_copilot(map_model, prompt, max_tokens=400)
        return resp["choices"][0]["message"]["content"] or ""

    def call_fill(prompt: str) -> str:
        time.sleep(2)
        resp = call_copilot(fill_model, prompt, max_tokens=400)
        return resp["choices"][0]["message"]["content"] or ""

    t0 = time.time()
    try:
        result = generate(
            task, context,
            call_fn=call_map,   # Phase 1: map model
            map_tier=ModelTier.MEDIUM,
            fill_tier=ModelTier.SMALL,
            max_workers=1,      # sequential to avoid rate limits in bench
            language="python",
        )
        # Re-run fills with fill_model (generate() uses call_fn for both phases)
        # For split-model demo, manually run Phase 2 with fill_model
        two_phase_lat = time.time() - t0
        map_tokens_in = result.map_tokens_in
        map_tokens_out = result.map_tokens_out

        # Phase 2 separately with fill_model
        slots = parse_stub_map(result.stub_map)
        print(f"    Phase 1 ({map_model}): {map_tokens_in}t in, {map_tokens_out}t out  (stub map, {len(slots)} functions)")

        fill_in_total = 0
        fill_out_total = 0
        filled_bodies: dict[str, str] = {}
        for slot in slots:
            p = fill_prompt(result.stub_map, slot.name, slot.sig, task, tier=ModelTier.SMALL)
            fill_in_total += len(ENC.encode(p))
            print(f"    Phase 2 fill {slot.name}: {len(ENC.encode(p))}t in ... ", end="", flush=True)
            t1 = time.time()
            try:
                resp2 = call_copilot(fill_model, p, max_tokens=400)
                body = resp2["choices"][0]["message"]["content"] or ""
                tokens_out = resp2.get("usage", {}).get("completion_tokens", len(ENC.encode(body)))
                fill_out_total += tokens_out
                filled_bodies[slot.name] = body
                print(f"{tokens_out}t out  {time.time()-t1:.1f}s")
            except Exception as e:
                print(f"ERROR: {e}")
            time.sleep(3)

        from codeclub.dev.generate import assemble
        final_assembled = assemble(result.stub_map, slots, filled_bodies)

        print(f"\n  === RESULTS ===")
        print(f"  One-shot  ({fill_model}):  {one_shot_tokens_in}t in + {one_shot_tokens_out}t out = {one_shot_tokens_in+one_shot_tokens_out}t total  {one_shot_lat:.1f}s")
        print(f"  Two-phase (map+fill):    {map_tokens_in+fill_in_total}t in + {map_tokens_out+fill_out_total}t out = {map_tokens_in+fill_in_total+map_tokens_out+fill_out_total}t total")
        print(f"    Phase 1 ({map_model}): {map_tokens_in}t in, {map_tokens_out}t out")
        print(f"    Phase 2 ({fill_model}): {fill_in_total}t in, {fill_out_total}t out  ({len(slots)} functions)")
        fill_frac = fill_in_total / (map_tokens_in + fill_in_total) * 100 if map_tokens_in + fill_in_total else 0
        print(f"    {fill_frac:.0f}% of input tokens on cheap {fill_model}")

        print(f"\n  === STUB MAP (Phase 1 output) ===")
        print(result.stub_map[:600])
        print(f"\n  === ASSEMBLED OUTPUT (first 600 chars) ===")
        print(final_assembled[:600])
        print(f"\n  === ONE-SHOT OUTPUT (first 600 chars) ===")
        print(one_shot_out[:600])

    except Exception as e:
        print(f"  ERROR: {e}")
        import traceback; traceback.print_exc()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=None, help="Models to test")
    parser.add_argument("--conditions", nargs="+", default=None, help="Conditions to test")
    parser.add_argument("--quick", action="store_true", help="Quick run: 1 model, 2 conditions")
    parser.add_argument("--variants", action="store_true", help="A/B test brevity constraint variants on gpt-5-mini")
    parser.add_argument("--variant-model", default="gpt-5-mini", help="Model to use for variant A/B test")
    parser.add_argument("--generate", action="store_true", help="Two-phase map+fill generation benchmark")
    parser.add_argument("--map-model", default="gpt-4.1", help="Model for Phase 1 map (default: gpt-4.1)")
    parser.add_argument("--fill-model", default="gpt-5-mini", help="Model for Phase 2 fill (default: gpt-5-mini)")
    args = parser.parse_args()

    if args.variants:
        run_variant_benchmark(model=args.variant_model)
        sys.exit(0)

    if args.generate:
        run_generator_benchmark(map_model=args.map_model, fill_model=args.fill_model)
        sys.exit(0)

    models = args.models
    conditions = args.conditions

    if args.quick:
        models = models or ["gpt-5-mini", "gpt-4.1"]
        conditions = conditions or ["native", "retrieved+brevity"]

    print(f"\nBenchmark: native vs compressed context across Copilot models")
    print(f"Task: {TASK[:80]}...\n")

    results = run_benchmark(models=models, conditions=conditions)
    print_report(results)


# ---------------------------------------------------------------------------
# Generator benchmark — two-phase map+fill vs one-shot
# ---------------------------------------------------------------------------

