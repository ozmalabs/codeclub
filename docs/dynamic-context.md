# Dynamic Context — Index Everything, Send Nothing

> The major problem with LLM conversations is that they're treated as
> append-only logs. Everything accumulates. When the window fills, you
> compact. This is horribly inefficient.

Dynamic context replaces the log with a queryable index. Each request
triggers a focused retrieval — only the relevant code, turns, and decisions
are sent. Old context is always searchable, never blocking.

## How it works

```
Client request
    ↓
┌─────────────────────┐
│  1. Classify intent  │  heuristic pattern match (no model call)
│  2. Estimate clarity │  keyword scoring → clarity score
│  3. Uplift?          │  if below cliff (~40c), restructure spec
│  4. Assemble context │  retrieve relevant code + turns + decisions
│  5. Route            │  pick best-fit model for actual context size
│  6. Forward + stream │  OpenAI-compatible pass-through
│  7. Index response   │  store for future retrieval
│  8. Background tasks │  compact old episodes, extract decisions
└─────────────────────┘
```

## Intent classification

Ten categories, pattern-matched from keywords and structure:

| Intent | Trigger patterns | Context pulled |
|---|---|---|
| `new_task` | build, create, implement | repo structure + stack hints |
| `follow_up` | refs to recent code/fns | specific files + recent episode |
| `debug` | error traces, failing | failing code + tests + error |
| `question` | how, what, why | relevant stubs + decisions |
| `refactor` | rename, move, extract | target files (full) + dependents (stubs) |
| `review` | review, look at | diff + test results |
| `explore` | find, where, search | symbol index, maybe no bodies |
| `continue` | keep going, next | current episode context |
| `pivot` | topic change | starts new episode (clean slate) |
| `meta` | what have we done | episode summaries + decision log |

Safe default: `follow_up` when uncertain (includes current episode).

## Fit precision

Users control how aggressively the system trims context:

| Level | Padding | Use case |
|---|---|---|
| `minimal` | 0% | Cost-sensitive, well-understood task |
| `tight` | 10% | Routine work, confident |
| `balanced` | 25% | **Default.** Most work. |
| `generous` | 50% | Exploring, complex, uncertain |
| `full` | 100% | Escape hatch — sends everything |

Padding is extra budget above what the classifier estimates is needed.
At `balanced`, if the classifier says 3K tokens, the assembler pulls ~3.75K.

Set via header:

```
X-Context-Fit: tight
```

### Adaptive fit

The system tracks outcomes per intent and fit level. If tight fits
consistently cause errors (model asks for more context, produces wrong
results), the budget automatically increases. If everything succeeds,
it tightens.

Rolling window of 20 outcomes. Adjusts ±5% per step, bounded at ±25%.
Persists to `adaptive_fit.json`.

## Clarity uplift

Empirical finding from 232 benchmark fights: there's a **clarity cliff**
at ~40. Below it, all models crater to ~0% quality — including frontier
models. Above 50, even small models hit 80–100%.

When a request falls below the cliff, the system can restructure the spec
before routing:

```
"make a rate limiter" (clarity ~25)
    ↓ clarifier (cheap model, ~500 tokens)
"implement a RateLimiter class using token bucket algorithm with
 configurable rate and burst, thread-safe, with acquire() and
 try_acquire() methods" (clarity ~60)
    ↓ route to smaller model (saves 10-100x)
```

This isn't asking the user questions — it's spec decomposition. The
clarifier restructures the vague request into a structured spec by
extracting implicit requirements and adding standard assumptions.

Decision heuristic considers user preference:
- **Speed-optimised**: skip uplift if frontier model is fast enough
- **Efficiency-optimised**: always uplift when ROI is positive
- **Balanced**: uplift when savings exceed 2× the clarification cost

## Episode system

Episodes group related turns into semantic units. Boundaries detected by:
- Topic change (new files, different domain keywords)
- Explicit pivot ("actually, let's work on X instead")
- Idle gap > N minutes
- Intent change (e.g., debug → new_task)

Within an episode, all turns are stored. Only the most recent 2–3 turns
plus the episode summary are included in assembled context. Old turns
are searchable but not sent by default.

## Routing integration

Context size becomes a routing axis alongside difficulty and clarity:

```
task(difficulty, clarity, context_tokens) → model selection
```

This creates a virtuous loop:
1. Better classification → more focused context
2. Smaller context → can use faster/cheaper model
3. Faster model → lower latency, lower cost
4. Lower cost → can afford more iterations

Tight fit preference pushes toward smaller models (max efficiency).
Generous pushes toward larger models (max safety).

## Background compaction

Compaction is background maintenance, not blocking:

1. Episode closes (pivot, idle gap) → background job summarises it
2. Summary stored alongside full turns (both searchable)
3. Old episodes with summaries: full turns pruned after N days
4. Active episode never compacted

The user never waits for compaction. The session never fills up.

## Proxy API

OpenAI-compatible — any client works transparently:

```bash
# Start proxy
python -m codeclub.context --upstream http://localhost:11434/v1

# Standard completions endpoint
POST /v1/chat/completions

# Session management
GET  /v1/session/episodes     # list episodes
GET  /v1/session/stats        # tokens saved, routing decisions
GET  /v1/session/fit-stats    # per-intent fit precision analytics
POST /v1/session/reset        # archive and start fresh
```

Headers:
- `X-Context-Fit`: override fit precision (tight|balanced|generous)
- `X-Context-Model`: force a specific model (bypass routing)

## What it reuses

The dynamic context system integrates existing codeclub components:

- `treefrag.py` / `tree.py`: stub-based code context
- `retriever.py` / `semantic.py`: code search within budget
- `compressor.py`, `symbol_table.py`: token compression
- `compact.py`: noise stripping
- `token_counter.py`: budget management
- `brevity.py`: output compression
- `models.py`: context windows + routing
- `spec.py decompose()`: clarity uplift (spec decomposition)
- `runner.py compress_failure()`: debug intent context assembly

## Implementation status

- ✅ Session store (SQLite, episodes, FTS5 search)
- ✅ Request classifier (10 intents, heuristic)
- ✅ Episode boundary detection
- ✅ Context assembler (per-intent rules)
- ✅ Fit precision levels (5 levels + header)
- ✅ Proxy server (FastAPI, streaming SSE)
- ✅ Provider backends (OpenRouter, Ollama, llama-server, Anthropic, Copilot SDK)
- ✅ Clarity uplift (automatic spec restructuring)
- ✅ Adaptive fit tracking (rolling-window, auto-adjust)
- 🔲 Background compaction (episode summarisation)
- 🔲 Production tuning (real-world usage data)
