# codeclub

> Caveman not have H100. Caveman only have club.

Three tools that compose. Use one, two, or all three.

| | What | Result |
|---|---|---|
| 🗜️ **Compress** | Strip context to what the model actually needs | 70–95% fewer tokens, zero quality loss |
| 🔄 **Dev loop** | Spec → generate → test → fix → review → report | Working code from a sentence |
| 🧭 **Route** | Club until it fits | $0 local runs that match cloud quality |

## Caveman vs cloud

Averaged across 3 tasks (RateLimiter, LRU Cache, Event Emitter). Real runs, real numbers.

| Backend | Avg time | Avg cost | vs GPT-5.4 | Quality |
|---|---|---|---|---|
| **B580 local** (rnj-1 Q6_K) | 28.5s | **$0.000178** | 141× cheaper | 92% |
| **Llama 70B** (OpenRouter free) | 15.9s | **$0.000057** | 439× cheaper | 100% |
| **Gemma 4 26B** (OpenRouter free) | 42.2s | **$0.000141** | 178× cheaper | 83% |
| GPT-5.4 (reference) | — | ~$0.025 | baseline | baseline |

Caveman pay electricity. Cloud pay rent. Electricity cheaper.

### Compression savings (real files)

| File | Tokens | After full pipeline | Saved |
|---|---:|---:|---:|
| wallet_stripe.py (934 lines) | 7,202 | 300 | **96%** |
| 2 wallet files combined (1,343 lines) | 9,680 | 789 | **92%** |
| wallet_local.py (409 lines) | 2,478 | 489 | **80%** |
| wallet_bridge_snippet.py (78 lines) | 504 | 160 | **68%** |

→ [Full benchmark results](benchmarks/results/latest.md)

## Quick start

```bash
git clone https://github.com/ozmalabs/codeclub && cd codeclub
uv sync
uv run pytest tests/

# Hit task with club
uv run python dev_loop.py "Build a rate limiter with token bucket algorithm" \
    --setup local_b580 \
    --max-iterations 3 \
    --output rate_limiter.py
```

---

## Compress — send less, pay less

LLM agents send whole files as context. Most is irrelevant. You pay for every token
the model ignores.

```python
from codeclub.compress import stub_functions

compressed, source_map = stub_functions(code, language="python")
# 500 lines → 40 lines, round-trippable
```

9,680 tokens → 444 tokens with semantic retrieval. Model sees only what matters.
Round-trip expansion splices edits back into the original. No diff required.

→ [How compression works](docs/compression.md)

## Dev loop — agents? me only know caveman

No agent framework. No orchestration layer. A loop, a test runner, and whatever
models you have.

```python
from codeclub.dev import run
from codeclub.infra.models import router_for_setup

result = run("Build a RateLimiter class with token bucket algorithm",
             router=router_for_setup("local_b580"))
```

Big model designs the skeleton. Small model fills each function in parallel
(Skeleton-of-Thought). Once the stub map sets the interface contract, filling a
single isolated function body is well within a 3B model's capability. You don't
need a frontier model for the whole thing.

Stack hints auto-detect your project type and inject library constraints into every
prompt. Data-driven, not LLM-based. Models use the right libraries, right versions,
right patterns — no hallucinated imports, no outdated APIs.

```bash
# Auto-detects "cli" stack from task keywords
uv run python dev_loop.py "Build a CLI tool that manages nvmeof devices" \
    --setup local_b580

# Or specify explicitly
uv run python dev_loop.py "Build a REST API for user management" \
    --setup copilot --stack web-api
```

5 stacks: `web-api` · `cli` · `data` · `library` · `async-service`

Tests fail → compress failure → re-fill → repeat. Converges in 1–2 iterations.
Reviewer is a different model from the generator — same model normalises over its
own bugs.

Every run produces a ledger: wallclock, tokens, energy, cost, and what the same
task would have cost on GPT-4o. Pass `--electricity-rate 0.28` if you're in the UK.

→ [How the dev loop works](docs/dev-loop.md)

## Route — club until it fits

> You tell codeclub what you have. It hits the task with a club until it fits.

The router tries the best quality quant first. Doesn't fit in VRAM? Steps down
through Q6_K → Q4_K_M → Q3_K_M. Nothing fits on GPU? Falls back to CPU. No
internet? No problem.

| Setup | What |
|---|---|
| `local_only` | Ollama CPU, no internet |
| `local_b580` | B580 SYCL for map/review, Ollama CPU for fill |
| `copilot` | GitHub Copilot SDK (free) |
| `anthropic` | Direct Anthropic API |
| `openrouter_cheap` | Paid OpenRouter < $0.002/call |
| `best_local_first` | Local preferred, cloud fallback |

Six providers. Zero config for local, one env var for cloud.

→ [How routing works](docs/routing.md)

## Club Smash — right-sizing models to tasks

> You don't use a sledgehammer to crack a nut. Club Smash finds the right club.

Every model has an efficiency map — like a turbo compressor map. Two axes:
**difficulty** (how hard) and **clarity** (how well-specified). The sweet spot
is where the model is right-sized. Outside it, the model is either overkill or
overwhelmed.

Roles aren't special code paths — they're just coordinates on this plane:

| Role | Difficulty offset | Clarity | What it means |
|---|---|---|---|
| `fill` | −10 | 90 | Skeleton → code. Very clear, easier. |
| `map` | 0 | 70 | NL → architecture. Baseline difficulty. |
| `oneshot` | +10 | 65 | NL → complete code. Harder, less clear. |
| `review` | −5 | 75 | Check existing code. Moderate. |

### Efficiency maps

<table>
<tr>
<td width="50%">
<strong>rnj-1:8b</strong> (8B, Q6_K, B580 GPU)<br>
Tight island around 35d. Nails easy-moderate tasks with clear specs.<br>
<img src="benchmarks/maps/rnj-1-8b.png" alt="rnj-1:8b efficiency map" width="100%">
</td>
<td width="50%">
<strong>qwen3-coder:30b</strong> (30B, Q4_K_M, CPU)<br>
Wide plateau. Handles ambiguity, covers most of the task space.<br>
<img src="benchmarks/maps/qwen3-coder-30b.png" alt="qwen3-coder:30b efficiency map" width="100%">
</td>
</tr>
</table>

**Model overlay** — all models on one chart. Find the gaps, find the overlaps.

<img src="benchmarks/maps/overlay.png" alt="Model efficiency overlay" width="700">

**Quantization comparison** — same model, different quants. See how much
capability you lose stepping down from bf16 → Q4_K_M → Q2_K.

<img src="benchmarks/maps/quant_gemma4-26b-a4b.png" alt="Gemma4 quantization comparison" width="700">

```bash
python smash_viz.py                           # generate all maps
python smash_viz.py --quant-compare rnj-1:8b  # compare quants
python smash_server.py                        # interactive browser
python tournament.py --map                    # ASCII in terminal
```

→ [How Club Smash works](docs/club-smash.md)

## Agent plugin

Teach your AI agent how to use codeclub. One command. Works everywhere.

| Agent | Install |
|-------|---------|
| **Claude Code** | `claude plugin install codeclub@codeclub` |
| **Codex** | Clone repo → `/plugins` → Install |
| **Gemini CLI** | `gemini extensions install https://github.com/ozmalabs/codeclub` |
| **Cursor** | `npx skills add ozmalabs/codeclub -a cursor` |
| **Windsurf** | `npx skills add ozmalabs/codeclub -a windsurf` |
| **Copilot** | `npx skills add ozmalabs/codeclub -a github-copilot` |
| **Cline** | `npx skills add ozmalabs/codeclub -a cline` |
| **Any other** | `npx skills add ozmalabs/codeclub` |

Install once. Agent knows compress, dev loop, and routing APIs from first message.

## Install

```bash
pip install codeclub          # everything
pip install codeclub-compress # compression only
pip install codeclub-dev      # dev loop only
pip install codeclub-infra    # routing only
```

## Docs

- [Compression](docs/compression.md) — tree-sitter stubbing, semantic retrieval, brevity constraints
- [Dev loop](docs/dev-loop.md) — pipeline, fix loop, benchmarks, accounting
- [Routing](docs/routing.md) — hardware declaration, setup presets, providers, dynamic levers
- [Club Smash](docs/club-smash.md) — two-axis model routing, efficiency maps, right-sizing
- [Benchmarks](docs/benchmarks.md) — full results, reproduction steps, methodology
- [Architecture](docs/architecture.md) — file map, references

## References

- arXiv:2307.15337 — Skeleton-of-Thought: Prompting LLMs for Efficient Parallel Generation
- arXiv:2604.00025 — Inverse Scaling Can Be Easily Overcome With Scale-Aware Prompting
- arXiv:2601.19929 — Stingy Context / TREEFRAG structural compression
- [Caveman](https://github.com/juliusbrussee/caveman)

## Star This Repo

If caveman save you mass token, mass money — leave mass star. ⭐

[![Star History Chart](https://api.star-history.com/svg?repos=ozmalabs/codeclub&type=Date)](https://star-history.com/#ozmalabs/codeclub&Date)

---

Brought to you by [Ozma](https://ozmalabs.com) from [ozmalabs.com](https://ozmalabs.com).
