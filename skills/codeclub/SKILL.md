---
name: codeclub
description: >
  Token compression, autonomous dev loops, and hardware-aware model routing
  for LLM coding agents. Three tools that compose. Use one, two, or all three.
  911× cheaper than GPT-4o on real tasks.
  Use when: compressing context before model calls, generating code from specs,
  routing between local and cloud models, or any combination.
---

# codeclub

> Caveman not have H100. Caveman only have club.

Three composable tools. Use any combination.

| Tool | What | Result |
|---|---|---|
| **Compress** | Strip context to what model needs | 70–95% fewer tokens, zero quality loss |
| **Dev loop** | Spec → generate → test → fix → review | Working code from a sentence |
| **Route** | Pick right model for hardware | $0 local runs, cloud fallback |

## Compress — send less, pay less

### Tree-sitter stubbing

Replace function bodies with `...` stubs. Keep signatures + docstrings.

```python
from codeclub.compress import stub_functions

compressed, source_map = stub_functions(code, language="python")
# 500 lines → 40 lines. SourceMap preserves offsets for round-trip.
```

Supports Python, JS, JSX, TS. Language configs pluggable.

### Semantic retrieval

Index stubs. Query by task. Return only top-K relevant functions within token budget.

```python
from codeclub.compress.semantic import ChromaRetriever, build_stub_index

index = build_stub_index(files)
retriever = ChromaRetriever()
retriever.index(index)
results = retriever.query("Fix commit_inflight metadata merge", budget_tokens=600)
# 9,680 tokens → 444 tokens (95.4% reduction)
```

### Round-trip expansion

LLM edits stubs. Expander splices changed bodies back into original file.
Kept `...` stubs restore original bodies automatically.

```python
from codeclub.compress import expand

patched = expand(original_code, source_map, llm_output)
```

### Brevity constraints

Best output constraint: `"No explanation. Code only."` — 100% quality, ~50% fewer output tokens.
CJK is NOT a shortcut — costs more in cl100k_base.

## Dev loop — spec to working code

Autonomous write → test → fix → review → report. No agent framework. No orchestration layer.

```python
from codeclub.dev import run
from codeclub.infra.models import router_for_setup

result = run("Build a RateLimiter class with token bucket algorithm",
             router=router_for_setup("local_b580"))
print(result.report)
print(result.ledger.summary())
```

### Pipeline

```
Spec → Map → Fill → TestGen → TestRun → Fix loop → Review → Report
```

- **Map**: Mid-tier model produces skeleton (signatures + `...`)
- **Fill**: Each function filled in isolation, in parallel (small model)
- **Fix loop**: Compress failures → re-fill implicated functions → repeat (1–2 iterations)
- **Review**: Independent model reviews against spec (different model from generator)

### CLI

```bash
uv run python dev_loop.py "Build a rate limiter" \
    --setup local_b580 \
    --max-iterations 3 \
    --output rate_limiter.py
```

### Stack hints

Auto-detect or specify a library stack. Data-driven, no LLM calls.
Models get exact import names, versions, anti-patterns, and architecture constraints.

```python
# Auto-detected from task keywords
result = run("Build a REST API for user management",
             router=router_for_setup("copilot"))

# Explicit stack
result = run("Build a CLI tool for NVMeoF devices",
             router=router_for_setup("local_b580"),
             stack="cli")
```

```bash
# CLI usage
uv run python dev_loop.py "Build an ETL pipeline" --stack data
```

Available stacks: `web-api`, `cli`, `data`, `library`, `async-service`.

Each stack includes:
- Curated libraries with pip names, import names, version constraints
- Anti-patterns: "don't use flask→use fastapi", "don't use requests→use httpx"
- Architecture patterns and recommended file structure
- Test framework hints (pytest, hypothesis, respx)

See `codeclub/stacks.py` for the full registry.

### Accounting

Every run produces a ledger: wallclock, tokens, energy, cost, comparison vs GPT-4o.

```bash
# UK electricity rate
uv run python dev_loop.py "..." --electricity-rate 0.28
```

## Route — club until it fits

Tell codeclub your hardware. It picks models, tries best quant first, steps down
until something fits. Q6_K → Q4_K_M → Q3_K_M → CPU fallback.

### Setup presets

```python
from codeclub.infra.models import router_for_setup

router = router_for_setup("local_b580")       # B580 SYCL + Ollama CPU
router = router_for_setup("copilot")           # GitHub Copilot SDK (free)
router = router_for_setup("anthropic")         # Direct Anthropic API
router = router_for_setup("best_local_first")  # Local preferred, cloud fallback
router = router_for_setup("openrouter_cheap")  # Paid OpenRouter < $0.002/call
```

### Custom hardware

```python
from codeclub.infra.hardware import HardwareSetup

hw = HardwareSetup.from_dict({
    "devices": [
        {"name": "Intel Arc B580", "vram_mb": 12288, "backend": "sycl",
         "endpoint": "http://localhost:8081"},
    ],
    "ram_mb": 32768,
    "ollama_url": "http://localhost:11434",
})
hw.probe()
router = router_for_setup("local_b580", hardware=hw)
```

### Providers

| Provider | Auth |
|---|---|
| Anthropic | `ANTHROPIC_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` |
| GitHub Copilot SDK | `gh auth login` or `GITHUB_TOKEN` |
| GitHub Models | `GITHUB_TOKEN` |
| Ollama / llama.cpp | none |

### Dynamic levers

```python
router.prefer_local = False
router.budget = "medium"
# Changes take effect next run. No restart needed.
```

## When to use what

| Situation | Tool |
|---|---|
| Sending large files to model | Compress first with `stub_functions` |
| "Build me X" from a sentence | Dev loop with `run()` |
| Have local GPU, want to use it | Route with local preset |
| Want cheapest possible runs | Route with `budget="free"` |
| All of the above | They compose — compress feeds into dev loop which uses routing |

## Install

```bash
pip install codeclub          # everything
pip install codeclub-compress # compression only
pip install codeclub-dev      # dev loop only
pip install codeclub-infra    # routing only
```
