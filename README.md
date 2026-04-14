# codeclub

> Caveman not have H100. Caveman only have club.

Three tools that compose. Use one, two, or all three.

---

## Part 1 — Compression

Token compression pipeline for LLM coding agents. Reduces input context 70–95% before
sending to any model. Cuts output noise with scale-aware brevity constraints.

```bash
pip install codeclub-compress
```

### The problem

LLM coding agents send entire files as context. Most is irrelevant to the task. You pay
for every token even though the model only needs a fraction.

### Structural compression (`codeclub.compress.tree`)

Replace function bodies with `...` stubs via tree-sitter. Keep signatures + docstrings.
Supports Python, JS, JSX, TS — language configs are pluggable.

```python
from codeclub.compress import stub_functions

compressed, source_map = stub_functions(code, language="python")
# 500 lines → 40 lines. SourceMap preserves offsets for round-trip.
```

Typical reduction: 70–94%.

### Semantic retrieval (`codeclub.compress.semantic`)

Index stubs into ChromaDB. Query by task description. Return only the top-K most
relevant functions within a token budget.

```python
from codeclub.compress.semantic import ChromaRetriever, build_stub_index

index = build_stub_index(files)
retriever = ChromaRetriever()
retriever.index(index)
results = retriever.query("Fix commit_inflight metadata merge", budget_tokens=600)
# 9,680 token repo → 444 token prompt (95.4% reduction)
```

### Round-trip expansion (`codeclub.compress.expand`)

LLM edits stubs. Expander splices changed bodies back into the original file.
Kept `...` stubs restore original bodies automatically — no diff required.

```python
from codeclub.compress import expand

patched = expand(original_code, source_map, llm_output)
```

### Brevity constraints (`codeclub.compress.brevity`)

Scale-aware output compression. SMALL tier failure mode is under-completion, not
verbosity — minimal suppression wins.

| Variant | Quality | Output tokens |
|---|---|---|
| No constraint | 100% | verbose |
| `"No explanation. Code only."` | **100%** | ~1800 (winner) |
| `"Output only the changed code"` | 80% | diff fragments |

### Key findings

- **Retrieved + no brevity**: 85% cost reduction, 0% quality loss vs native context
- **Brevity winner**: `"No explanation. Code only."` — 100% quality, ~50% fewer output tokens
- **CJK is NOT a shortcut**: Chinese characters cost more in cl100k_base for equivalent meaning

---

## Part 2 — Development loops

> Agents? Me only know caveman.

Autonomous write → test → fix → review → report. No agent framework. No orchestration
layer. A loop, a test runner, and whatever models you have.

```bash
pip install codeclub-dev
```

### How it works

```
1. Spec      decompose task → user story, requirements, acceptance criteria
2. Map        mid-tier model produces module skeleton (signatures + ...)
3. Fill       each function filled in isolation, in parallel (small model)
4. TestGen    generate pytest tests from spec + assembled code
5. TestRun    execute tests, capture failures
6. Fix loop   compress failures → re-fill implicated functions → repeat
7. Review     independent model reviews code against spec + test results
8. Report     light model summarises what was built, issues, cost
```

Map+fill is Skeleton-of-Thought (arXiv:2307.15337). The key: once the stub map sets
the interface contract, filling a single isolated function body is well within a 3B
model's capability. You don't need a frontier model for the whole thing.

### Usage

```python
from codeclub.dev import run
from codeclub.infra.models import router_for_setup

router = router_for_setup("local_b580")

result = run(
    "Build a RateLimiter class with token bucket algorithm",
    router=router,
)
print(result.report)
print(result.ledger.summary())
```

### The fix loop

Tests fail. The loop compresses the failure — stubs passing functions, keeps full
bodies of failing ones — then re-fills only the implicated functions with the error
context prepended. Converges in 1–2 iterations for moderate complexity tasks.

The reviewer uses a different model from the generator intentionally. Same model
normalises over its own bugs.

### Two-phase benchmark (RateLimiter task)

| Setup | Time | Cost | Output quality |
|---|---|---|---|
| rnj-1 Q6_K (B580) | 12.6s | $0 | 86% |
| Gemma 4 26B MoE (OpenRouter) | 31.0s | $0.000067 | 86% |
| Llama 3.3 70B (OpenRouter) | 19.9s | $0.000063 | 57% |

B580 is 2.5× faster than cloud for this task, at zero cost.

### Accounting

Every run produces a ledger: wallclock, tokens, energy (measured from hardware
counters where available, TDP estimate otherwise), API cost, and a comparison against
what the same task would have cost on GPT-4o.

```
  ┌─ Accounting ──────────────────────────────────────────────
  │  Wallclock   45.2s
  │  Tokens      3,847 (2,891 in + 956 out)
  │  Energy      0.2341 Wh
  │  Energy $    $0.000035  (@$0.15/kWh)
  │  Total $     $0.000035
  ├─ Cost comparison ─────────────────────────────────────────
  │  Actual cost      $0.000035
  │  vs gpt-4o                 $0.031900  (911× more)
  │  vs claude-opus-4-6        $0.038250
  │  vs gpt-4o-mini            $0.000808  (23× more)
  └───────────────────────────────────────────────────────────
```

Pass `--electricity-rate 0.28` if you're in the UK.

---

## Part 3 — Hardware and interfaces

> You tell codeclub what you have. It hits the task with a club until it fits.

Model routing as a lookup problem, not an LLM problem. A registry of benchmark scores,
hardware fit checks, dynamic escalation, and adapters for every provider.

The web interface (in progress) shows task progression, queue, and the accounting
dashboard. The CLI and LSP server make it usable from anywhere.

```bash
pip install codeclub          # everything
pip install codeclub-infra    # routing only
```

### Hardware declaration

```python
from codeclub.infra.hardware import HardwareSetup
from codeclub.infra.models import router_for_setup

hw = HardwareSetup.from_dict({
    "devices": [
        {"name": "Intel Arc B580", "vram_mb": 12288, "backend": "sycl",
         "endpoint": "http://localhost:8081"},
        {"name": "NVIDIA RTX 3080", "vram_mb": 10240, "backend": "cuda",
         "endpoint": "http://192.168.1.10:8081"},
    ],
    "ram_mb": 32768,
    "ollama_url": "http://localhost:11434",
})
hw.probe()  # HTTP health-check each endpoint

router = router_for_setup("local_b580", hardware=hw)
```

Or detect automatically:

```bash
python -m codeclub.infra.hardware   # detect and print
python dev_loop.py "..." --detect-hardware
python dev_loop.py "..." --probe
```

### "Club until it fits"

The router tries the best quality quant first. If it doesn't fit in available VRAM,
it steps down through Q6_K → Q4_K_M → Q3_K_M until something runs. If nothing fits
on GPU, it falls back to CPU. The routing table shows `[gpu]` vs `[cpu-fallback]`.

```
python dev_loop.py "..." --setup local_b580 --routing-table
```

### Model routing

Selection is a lookup — benchmark scores, cost, observed speed, hardware fit — not
another LLM call.

| Setup | What |
|---|---|
| `local_only` | Ollama CPU only, no internet |
| `local_b580` | B580 SYCL for map/review, Ollama CPU for fill |
| `openrouter_free` | Free-tier OpenRouter (rate-limited) |
| `openrouter_cheap` | Paid OpenRouter < $0.002/call |
| `anthropic` | Direct Anthropic API |
| `github` | GitHub Models / Copilot (free via `GITHUB_TOKEN`) |
| `best_local_first` | Local preferred, cloud fallback |

### Dynamic levers

The router is live. Change it between runs without restarting.

```python
router = router_for_setup("best_local_first", budget="cheap")
tracker = PerformanceTracker()

result1 = run(task1, router=router, tracker=tracker)

# Suddenly in a hurry
router.prefer_local = False
router.budget = "medium"

result2 = run(task2, router=router, tracker=tracker)
# tracker carries escalation history across both runs
```

### Supported providers

| Provider | How | Auth |
|---|---|---|
| Anthropic | Direct SDK | `ANTHROPIC_API_KEY` |
| OpenRouter | HTTP | `OPENROUTER_API_KEY` |
| GitHub Models / Copilot | HTTP | `GITHUB_TOKEN` |
| Ollama | HTTP localhost | none |
| llama.cpp server | HTTP any URL | none |

### Web interface (coming)

- Visual task progression (spec → generate → test → fix → review → report)
- Queue management — submit tasks, prioritise, cancel
- Accounting dashboard — cost per task, energy over time, savings vs baseline
- Model/endpoint status — which endpoints are alive, current load, t/s
- Git integration — optional diff view, one-click commit of generated code

### Language support (coming)

Test runner abstraction: detect language → run `pytest` / `jest` / `go test` /
`cargo test`. Structural compression already language-agnostic via tree-sitter;
per-language configs for node type queries are pluggable.

---

## Quick start

```bash
git clone https://github.com/your/codeclub
cd codeclub
uv sync
uv run pytest tests/           # 110 tests

# See what models would run for your task
uv run python -m codeclub.infra.models --task "Build a rate limiter"

# Run the full loop
uv run python dev_loop.py "Build a rate limiter with token bucket algorithm" \
    --setup local_b580 \
    --max-iterations 3 \
    --output rate_limiter.py
```

## Files

| Package | File | What |
|---|---|---|
| `compress` | `tree.py` | Tree-sitter stubbing, SourceMap |
| `compress` | `semantic.py` | ChromaDB retrieval |
| `compress` | `expand.py` | Splice LLM output back into original |
| `compress` | `brevity.py` | Scale-aware output constraints |
| `compress` | `pipeline.py` | End-to-end compression pipeline |
| `dev` | `loop.py` | Autonomous dev loop, CLI |
| `dev` | `generate.py` | Two-phase map+fill generation |
| `dev` | `spec.py` | Task decomposition (spec-kit style) |
| `dev` | `testgen.py` | pytest generation |
| `dev` | `runner.py` | Test execution, failure compression |
| `dev` | `review.py` | Code review (independent model) |
| `infra` | `models.py` | Model registry, router, presets |
| `infra` | `hardware.py` | Hardware inventory, endpoint registry |
| `accounting` | `tracker.py` | Per-task ledger: tokens, time, energy |
| `accounting` | `power.py` | RAPL / hwmon / TDP energy measurement |
| `accounting` | `baseline.py` | Savings vs reference model calculations |
| `benchmarks` | `tiered.py` | Three-way map backend benchmark |
| `benchmarks` | `llm.py` | Live API benchmark harness |

## References

- arXiv:2307.15337 — Skeleton-of-Thought: Prompting LLMs for Efficient Parallel Generation
- arXiv:2604.00025 — Inverse Scaling Can Be Easily Overcome With Scale-Aware Prompting
- arXiv:2601.19929 — Stingy Context / TREEFRAG structural compression
- [spec-kit](https://github.com/github/spec-kit) — structured task decomposition
- [LLMLingua](https://github.com/microsoft/LLMLingua)
- [Repomix](https://github.com/yamadashy/repomix)
- [Caveman](https://github.com/juliusbrussee/caveman)
