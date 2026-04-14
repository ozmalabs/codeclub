"""
bench_tiered.py — Three-way map backend benchmark for Skeleton-of-Thought generation.

Map backends under test:
  A. B580 SYCL local    — rnj-1 Q6_K via llama-server (port 8081, Intel Arc XMX)
  B. Cloud cheap        — OpenRouter free-tier model (Gemma 3 12B, zero cost)
  C. Cloud mid          — OpenRouter free-tier 70B (Llama 3.3 70B, zero cost, higher quality)

Fill backend (all conditions):
  Local Ollama CPU — qwen2.5-coder:1.5b (parallel, OLLAMA_NUM_PARALLEL=4)

Task: RateLimiter class (token bucket algorithm)

Measures per condition:
  map_tokens_in, map_tokens_out, fill_tokens_in, fill_tokens_out,
  total_time_s, map_time_s, fill_time_s, throughput (t/s map),
  stub quality (has_types, has_docstrings, has_literal, has_defaults),
  output quality (correct_logic, passes_test, errors)
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path

from codeclub.dev.generate import generate, GenerationResult, make_ollama_fn, _LOCAL_MODELS
from codeclub.compress.brevity import ModelTier

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Load OpenRouter key from .env
_ENV = Path(__file__).parent.parent / ".env"
if _ENV.exists():
    for line in _ENV.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")

TASK = (
    "Build a RateLimiter class using a token bucket algorithm. "
    "It should support: consume(tokens) -> bool, "
    "refill(elapsed_s) -> None, "
    "available() -> int. "
    "Bucket capacity: 100. Refill rate: 10 tokens/second. Capped at capacity."
)

FILL_MODEL = "qwen2.5-coder:1.5b"
FILL_WORKERS = 4  # parallel fill calls

# Map backends
MAP_BACKENDS = [
    ("B580-rnj1",       "http://localhost:8081",  "llama-server"),
    ("OR-gemma4-moe",   "openrouter",             "google/gemma-4-26b-a4b-it"),
    ("OR-llama70b",     "openrouter",             "meta-llama/llama-3.3-70b-instruct"),
]

# OpenRouter cost per million tokens {model_id: (input_$/1M, output_$/1M)}
OR_PRICING: dict[str, tuple[float, float]] = {
    "google/gemma-4-26b-a4b-it":              (0.080, 0.350),
    "google/gemma-4-31b-it":                  (0.130, 0.380),
    "google/gemma-3-12b-it":                  (0.040, 0.130),
    "meta-llama/llama-3.3-70b-instruct":      (0.100, 0.320),
    "openai/gpt-4o-mini":                     (0.150, 0.600),
    "openai/gpt-4.1-mini":                    (0.400, 1.600),
    "mistralai/mistral-small-3.1-24b-instruct": (0.350, 0.560),
}


# ---------------------------------------------------------------------------
# call_fn factories
# ---------------------------------------------------------------------------

def make_llama_server_fn(base_url: str, timeout: int = 120):
    """OpenAI-compat /v1/chat/completions endpoint (llama.cpp server, port 8081)."""
    url = f"{base_url}/v1/chat/completions"

    def _call(prompt: str) -> str:
        payload = json.dumps({
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 512,
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]

    _call.__name__ = f"llama-server:{base_url}"
    return _call


def make_openrouter_fn(model_id: str, timeout: int = 120):
    """OpenRouter cloud call_fn."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    key = OPENROUTER_KEY
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set in .env")

    def _call(prompt: str) -> str:
        payload = json.dumps({
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 512,
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": "https://github.com/codeclub",
                "X-Title": "codeclub-bench",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body[:300]}") from e
        if "error" in data:
            raise RuntimeError(str(data["error"])[:200])
        return data["choices"][0]["message"]["content"]

    _call.__name__ = f"openrouter:{model_id}"
    return _call


# ---------------------------------------------------------------------------
# Stub quality scorer
# ---------------------------------------------------------------------------

def score_stub(stub: str) -> dict[str, bool]:
    """Heuristic quality signals on the map-phase stub output."""
    return {
        "has_types":      "->" in stub or ": float" in stub or ": int" in stub or ": bool" in stub,
        "has_docstrings": '"""' in stub or "'''" in stub,
        "has_capacity":   "capacity" in stub or "= 100" in stub,
        "has_rate":       "rate" in stub or "= 10" in stub,
        "three_methods":  stub.count("def ") >= 3,
        "has_class":      "class " in stub,
    }


def score_output(code: str) -> dict[str, bool]:
    """Quick correctness signals on the assembled output."""
    return {
        "has_consume":      "def consume" in code,
        "has_refill":       "def refill" in code,
        "has_available":    "def available" in code,
        "has_capacity":     "100" in code or "capacity" in code,
        "has_refill_rate":  "10" in code or "rate" in code,
        "uses_min_for_cap": "min(" in code,
        "class_present":    "class RateLimiter" in code,
    }


# ---------------------------------------------------------------------------
# Benchmark result
# ---------------------------------------------------------------------------

@dataclass
class TieredResult:
    name: str
    map_model: str
    map_time_s: float
    fill_time_s: float
    map_tokens_in: int
    map_tokens_out: int
    fill_tokens_in: int
    fill_tokens_out: int
    stub_quality: dict[str, bool] = field(default_factory=dict)
    output_quality: dict[str, bool] = field(default_factory=dict)
    stub: str = ""
    assembled: str = ""
    error: str = ""

    @property
    def total_time_s(self) -> float:
        return self.map_time_s + self.fill_time_s

    @property
    def map_throughput(self) -> float:
        return self.map_tokens_out / self.map_time_s if self.map_time_s > 0 else 0

    @property
    def stub_score(self) -> float:
        if not self.stub_quality:
            return 0.0
        return sum(self.stub_quality.values()) / len(self.stub_quality)

    @property
    def output_score(self) -> float:
        if not self.output_quality:
            return 0.0
        return sum(self.output_quality.values()) / len(self.output_quality)

    @property
    def map_cost_usd(self) -> float:
        """Estimated cost of the map API call (cloud only; $0 for local)."""
        if self.map_model == "llama-server":
            return 0.0
        rates = OR_PRICING.get(self.map_model)
        if not rates:
            return 0.0
        return (self.map_tokens_in / 1_000_000 * rates[0]
                + self.map_tokens_out / 1_000_000 * rates[1])


# ---------------------------------------------------------------------------
# Run one backend
# ---------------------------------------------------------------------------

def run_backend(name: str, endpoint: str, model_id: str) -> TieredResult:
    print(f"\n{'─'*60}")
    print(f"  MAP: {name}  ({model_id})")
    print(f"  FILL: {FILL_MODEL} (Ollama CPU, {FILL_WORKERS} workers)")
    print(f"{'─'*60}")

    # Build map call_fn
    try:
        if endpoint == "openrouter":
            map_fn = make_openrouter_fn(model_id)
        else:
            map_fn = make_llama_server_fn(endpoint)
    except Exception as e:
        print(f"  [SKIP] Cannot build call_fn: {e}")
        return TieredResult(name=name, map_model=model_id, map_time_s=0, fill_time_s=0,
                            map_tokens_in=0, map_tokens_out=0, fill_tokens_in=0, fill_tokens_out=0,
                            error=str(e))

    # Build fill call_fn
    fill_fn = make_ollama_fn(FILL_MODEL, check_ram=False)

    # Run full two-phase generation, timing each phase separately
    from codeclub.dev.generate import map_prompt, _strip_fences, parse_stub_map
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")

    # Wrap map_fn to capture timing
    map_timing: list[float] = []
    original_map_fn = map_fn

    def timed_map_fn(prompt: str) -> str:
        t0 = time.time()
        result = original_map_fn(prompt)
        map_timing.append(time.time() - t0)
        return result

    # Pre-check map (single call, time it, get stub for early quality display)
    map_p = map_prompt(TASK, "", tier=ModelTier.MEDIUM)
    map_tokens_in_est = len(enc.encode(map_p))
    print(f"  Phase 1: sending ~{map_tokens_in_est}t to map model ... ", end="", flush=True)

    t0 = time.time()
    try:
        stub_raw = original_map_fn(map_p)
    except Exception as e:
        print(f"FAILED: {e}")
        return TieredResult(name=name, map_model=model_id, map_time_s=time.time()-t0,
                            fill_time_s=0, map_tokens_in=map_tokens_in_est, map_tokens_out=0,
                            fill_tokens_in=0, fill_tokens_out=0, error=str(e)[:200])
    t_map = time.time() - t0

    stub = _strip_fences(stub_raw)
    map_tokens_out = len(enc.encode(stub))
    slots = parse_stub_map(stub)
    print(f"{map_tokens_out}t out  {t_map:.1f}s  ({map_tokens_out/t_map:.1f} t/s)  {len(slots)} stubs found")

    stub_q = score_stub(stub)
    stub_score_pct = int(sum(stub_q.values()) / len(stub_q) * 100)
    flags = [k for k, v in stub_q.items() if v]
    print(f"  Stub quality: {stub_score_pct}%  [{', '.join(flags)}]")

    if not slots:
        print("  [WARN] No stub slots found — map model may have returned implementations")

    # Phase 2: reuse map output, build fill calls manually to avoid second map call
    from codeclub.dev.generate import fill_prompt, assemble, _strip_fences as sf
    from concurrent.futures import ThreadPoolExecutor, as_completed

    print(f"  Phase 2: filling {len(slots)} functions with {FILL_MODEL} ({FILL_WORKERS} workers) ... ", end="", flush=True)

    filled: dict[str, str] = {}
    fill_stats: list[tuple[int, int]] = []
    fill_errors: dict[str, str] = {}

    def _fill_one(slot):
        p = fill_prompt(stub, slot.name, slot.sig, TASK, tier=ModelTier.SMALL)
        ti = len(enc.encode(p))
        raw = fill_fn(p)
        body = sf(raw)
        to = len(enc.encode(body))
        return slot.name, body, ti, to

    t_fill_start = time.time()
    try:
        with ThreadPoolExecutor(max_workers=FILL_WORKERS) as executor:
            futures = {executor.submit(_fill_one, slot): slot for slot in slots}
            for future in as_completed(futures):
                slot = futures[future]
                try:
                    fn_name, body, ti, to = future.result()
                    filled[fn_name] = body
                    fill_stats.append((ti, to))
                except Exception as exc:
                    fill_errors[slot.name] = str(exc)
    except Exception as e:
        print(f"FAILED: {e}")
        import traceback; traceback.print_exc()
        return TieredResult(name=name, map_model=model_id, map_time_s=t_map,
                            fill_time_s=time.time()-t_fill_start,
                            map_tokens_in=map_tokens_in_est, map_tokens_out=map_tokens_out,
                            fill_tokens_in=0, fill_tokens_out=0,
                            stub=stub, stub_quality=stub_q, error=str(e)[:200])
    t_fill_real = time.time() - t_fill_start

    fill_tokens_in = sum(s[0] for s in fill_stats)
    fill_tokens_out = sum(s[1] for s in fill_stats)

    assembled = assemble(stub, slots, filled)

    # Fake a GenerationResult-like namespace for the rest of the code
    class _R:
        pass
    result = _R()
    result.stub_map = stub
    result.assembled = assembled
    result.map_tokens_in = map_tokens_in_est
    result.map_tokens_out = map_tokens_out
    result.fill_tokens_in = fill_tokens_in
    result.fill_tokens_out = fill_tokens_out
    result.errors = fill_errors

    t_fill_real_display = t_fill_real
    print(f"{fill_tokens_out}t out  {t_fill_real_display:.1f}s")

    out_q = score_output(result.assembled)
    out_score_pct = int(sum(out_q.values()) / len(out_q) * 100)
    out_flags = [k for k, v in out_q.items() if v]
    miss_flags = [k for k, v in out_q.items() if not v]
    print(f"  Output quality: {out_score_pct}%  [OK: {', '.join(out_flags)}]")
    if miss_flags:
        print(f"                              [MISS: {', '.join(miss_flags)}]")
    if fill_errors:
        print(f"  Fill errors: {fill_errors}")

    return TieredResult(
        name=name,
        map_model=model_id,
        map_time_s=t_map,
        fill_time_s=t_fill_real,
        map_tokens_in=result.map_tokens_in,
        map_tokens_out=result.map_tokens_out,
        fill_tokens_in=result.fill_tokens_in,
        fill_tokens_out=result.fill_tokens_out,
        stub_quality=stub_q,
        output_quality=out_q,
        stub=result.stub_map,
        assembled=result.assembled,
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(results: list[TieredResult]) -> None:
    print()
    print("=" * 100)
    print("  TIERED GENERATION BENCHMARK — map model comparison")
    print(f"  Task: {TASK[:70]}...")
    print(f"  Fill: {FILL_MODEL} (local CPU Ollama, {FILL_WORKERS} parallel workers)")
    print("=" * 100)
    print(f"\n  {'Name':18s} {'Map model':40s} {'MapT':>5} {'FillT':>5} {'TotT':>5} "
          f"{'MapIn':>6} {'MapOut':>6} {'FillTok':>7} {'MapCost':>9} {'Stub%':>6} {'Out%':>5}")
    print(f"  {'-'*18} {'-'*40} {'-'*5} {'-'*5} {'-'*5} "
          f"{'-'*6} {'-'*6} {'-'*7} {'-'*9} {'-'*6} {'-'*5}")

    for r in results:
        if r.error and not r.assembled:
            print(f"  {r.name:18s} {r.map_model:40s}  ERROR: {r.error[:60]}")
            continue
        cost_str = f"${r.map_cost_usd:.6f}" if r.map_cost_usd > 0 else "  $0 local"
        fill_tok = r.fill_tokens_in + r.fill_tokens_out
        print(
            f"  {r.name:18s} {r.map_model:40s} "
            f"{r.map_time_s:>5.1f} {r.fill_time_s:>5.1f} {r.total_time_s:>5.1f} "
            f"{r.map_tokens_in:>6} {r.map_tokens_out:>6} {fill_tok:>7} "
            f"{cost_str:>9} {r.stub_score:>5.0%} {r.output_score:>4.0%}"
        )

    print()

    # Stub quality breakdown
    print("  === STUB QUALITY FLAGS ===")
    for r in results:
        if r.error and not r.stub:
            continue
        flags = {k: ("✓" if v else "✗") for k, v in r.stub_quality.items()}
        print(f"  {r.name:18s}  " + "  ".join(f"{k}:{v}" for k, v in flags.items()))

    print()
    print("  === OUTPUT QUALITY FLAGS ===")
    for r in results:
        if r.error and not r.assembled:
            continue
        flags = {k: ("✓" if v else "✗") for k, v in r.output_quality.items()}
        print(f"  {r.name:18s}  " + "  ".join(f"{k}:{v}" for k, v in flags.items()))

    print()
    print("  === STUB MAPS ===")
    for r in results:
        if not r.stub:
            continue
        print(f"\n  [{r.name}]")
        print("  " + r.stub.replace("\n", "\n  ")[:800])

    print()
    print("  === ASSEMBLED OUTPUTS (first 1000 chars) ===")
    for r in results:
        if not r.assembled:
            continue
        print(f"\n  [{r.name}]")
        print("  " + r.assembled.replace("\n", "\n  ")[:1000])

    print()
    print("  === HARDWARE ACCESSIBILITY SUMMARY ===")
    print("  Target: 16GB host RAM, 8GB GPU VRAM")
    print()
    print("  Map options:")
    print("    B580-rnj1:   rnj-1 Q6_K  — 6.5GB weights + 4GB KV@32k = 10.5GB → needs 12GB GPU")
    print("                 rnj-1 Q4_K_M — ~4.5GB weights + 2GB KV@4k  =  6.5GB → fits 8GB GPU ✓")
    print("    OR-gemma3:   cloud (free) — 0 local GPU, works on any hardware ✓")
    print("    OR-llama70b: cloud (free) — 0 local GPU, best quality, 70B reasoning ✓")
    print()
    print("  Fill (all conditions): qwen2.5-coder:1.5b — 1.3GB RAM, CPU only ✓")
    print()
    print("  Minimum viable config (8GB GPU):")
    print("    Map:  rnj-1 Q4_K_M on B580 (needs download) OR free OpenRouter cloud")
    print("    Fill: qwen2.5-coder:1.5b on CPU")
    print()
    print("  No GPU config (16GB host only):")
    print("    Map:  free OpenRouter (llama-3.3-70b is free and excellent)")
    print("    Fill: qwen2.5-coder:1.5b on CPU (or 3b if 3GB+ free)")
    print("=" * 100)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--backends", nargs="+",
                        choices=["B580-rnj1", "OR-gemma3-12b", "OR-llama70b"],
                        default=None, help="Which backends to test (default: all)")
    parser.add_argument("--skip-local", action="store_true",
                        help="Skip B580 local backend (if llama-server not running)")
    args = parser.parse_args()

    backends = MAP_BACKENDS
    if args.skip_local:
        backends = [b for b in backends if b[0] != "B580-rnj1"]
    if args.backends:
        backends = [b for b in MAP_BACKENDS if b[0] in args.backends]

    if not OPENROUTER_KEY and any(b[1] == "openrouter" for b in backends):
        print("[WARN] OPENROUTER_API_KEY not found in .env — OpenRouter backends will fail")

    print(f"\ncodeclub tiered benchmark — {len(backends)} map backends, fill={FILL_MODEL}")
    print(f"Task: {TASK[:80]}...")

    results = []
    for name, endpoint, model_id in backends:
        r = run_backend(name, endpoint, model_id)
        results.append(r)

    print_report(results)
