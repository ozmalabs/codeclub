# Development Loop

Autonomous write → test → fix → review → report. No agent framework. No
orchestration layer. A loop, a test runner, and whatever models you have.

```bash
pip install codeclub-dev
```

## Pipeline

```
1. Spec      decompose task → user story, requirements, acceptance criteria
2. Map       mid-tier model produces module skeleton (signatures + ...)
3. Fill      each function filled in isolation, in parallel (small model)
4. TestGen   generate pytest tests from spec + assembled code
5. TestRun   execute tests, capture failures
6. Fix loop  compress failures → re-fill implicated functions → repeat
7. Review    independent model reviews code against spec + test results
8. Report    light model summarises what was built, issues, cost
```

Map+fill is Skeleton-of-Thought ([arXiv:2307.15337](https://arxiv.org/abs/2307.15337)).
Once the stub map sets the interface contract, filling a single isolated function
body is well within a 3B model's capability. You don't need a frontier model for
the whole thing.

## Usage

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

## Stack hints

The loop auto-detects the best library stack for your task and injects
constraints into every prompt (spec, map, fill, testgen). No LLM calls —
keyword matching picks the stack, data drives the hints.

```python
result = run(
    "Build a REST API that manages NVMeoF devices",
    router=router,
    stack="web-api",  # or auto-detect from task keywords
)
```

Available stacks: `web-api`, `cli`, `data`, `library`, `async-service`.

Each stack includes:
- **Curated libraries** with exact import names, versions, and usage notes
- **Anti-patterns** — "don't use flask, use fastapi" with reasons
- **Architecture patterns** — repo pattern, dependency injection, etc.
- **File structure** — recommended project layout

The hints appear as `<stack>`, `<imports>`, and `<test_stack>` XML blocks
in prompts. Models read them as hard constraints, not suggestions.

See `codeclub/stacks.py` for the full registry.

## The fix loop

Tests fail. The loop compresses the failure — stubs passing functions, keeps full
bodies of failing ones — then re-fills only the implicated functions with the
error context prepended. Converges in 1–2 iterations for moderate complexity tasks.

The reviewer uses a different model from the generator intentionally. Same model
normalises over its own bugs.

## Benchmarks (RateLimiter task)

| Setup | Time | Cost | Quality |
|---|---|---|---|
| rnj-1 Q6_K (B580) | 12.6s | $0 | 86% |
| Gemma 4 26B MoE (OpenRouter) | 31.0s | $0.000067 | 86% |
| Llama 3.3 70B (OpenRouter) | 19.9s | $0.000063 | 57% |

B580 is 2.5× faster than cloud for this task, at zero cost.

## Accounting

Every run produces a ledger: wallclock, tokens, energy (measured from hardware
counters where available, TDP estimate otherwise), API cost, and a comparison
against what the same task would have cost on GPT-4o.

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
