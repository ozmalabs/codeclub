# Copilot Premium Request Multipliers

> Source: https://docs.github.com/en/copilot/concepts/billing/copilot-requests
> Last updated: April 2026

## How premium requests work

A premium request is **one user prompt** × the model's multiplier. The billing
unit is the *prompt*, not tokens or context size.

### What counts as a request

| Action | Requests |
|--------|----------|
| One user prompt in chat/CLI | 1 × model multiplier |
| Multi-turn conversation (5 messages) | 5 × model multiplier |
| Agentic tool calls (Copilot does autonomously) | **0** (free) |
| Coding agent session | 1 × model multiplier per session |
| Steering comment during active agent session | 1 × model multiplier |

**Key insight:** Token count and context window size do NOT affect billing.
A 200-token prompt costs the same as a 200,000-token prompt. Only the number
of user-initiated prompts matters.

### What does NOT count

- Autonomous agent actions (file edits, tool calls, searches)
- Inline code completions (separate quota, unlimited on paid plans)
- Context gathering / retrieval steps

## Included models (0× on paid plans)

These are **free** on paid Copilot plans — unlimited use, no premium request cost:

| Model | Multiplier (paid) | Multiplier (free plan) |
|-------|-------------------|----------------------|
| GPT-5 mini | 0× | 1× |
| GPT-4.1 | 0× | 1× |
| GPT-4o | 0× | 1× |
| Raptor mini | 0× | — |

## Premium models

| Model | Multiplier | Cost at $0.04/request |
|-------|-----------|----------------------|
| Gemini 2.0 Flash | 0.25× | $0.01 |
| Claude Sonnet 4 / 4.5 / 4.6 | 1× | $0.04 |
| Claude Sonnet 3.7 Reasoning | 1.25× | $0.05 |
| Claude Opus 4.5 | 3× | $0.12 |
| Claude Opus 4 / 4.6 | 10× | $0.40 |
| GPT-4.5 | 50× | $2.00 |

## Plan allowances

| Plan | Monthly premium requests | Overage |
|------|------------------------|---------|
| Free | 50 | blocked |
| Pro | 1,500 | $0.04/req × multiplier |
| Pro+ | 1,500+ | $0.04/req × multiplier |
| Business | 300/user | $0.04/req × multiplier |
| Enterprise | 1,000/user | $0.04/req × multiplier |

When you exceed your allowance, Copilot falls back to included models
(GPT-4.1, GPT-5 mini) automatically.

## Rate limits

Rate limits are separate from premium request billing:
- **Global/weekly limits** for overall service reliability
- **Model-specific limits** for high-demand models (Opus, GPT-4.5)
- Included models (GPT-4.1, GPT-5 mini) have the most generous rate limits
- Premium models may have stricter limits due to capacity

When rate limited, Copilot suggests switching to a different model.

## Implications for codeclub routing

### The billing model is prompt-count, not token-count

This fundamentally changes the optimisation strategy compared to API billing:

| API billing | Copilot billing |
|-------------|----------------|
| Cost = tokens × $/token | Cost = prompts × multiplier |
| Compression saves money | Compression saves nothing on cost |
| Large context = expensive | Large context = free |
| Many small calls = cheap | Many small calls = expensive |

**For Copilot users, compression helps with speed and rate limits but not cost.**
The cost lever is **which model** and **how many prompts**.

### Optimal routing strategy

1. **Use included models for everything possible** — GPT-4.1 and GPT-5 mini
   are genuinely free (0×) and capable enough for fill, testgen, report phases
2. **Reserve premium models for high-value prompts** — spec/map on complex tasks
   where quality directly impacts iteration count
3. **Minimise prompt count** — batch work into fewer, larger prompts rather than
   many small ones (opposite of API optimisation!)
4. **Avoid GPT-4.5** (50×) — almost never worth it when Sonnet 4.6 (1×) or
   Opus 4.6 (10×) exist
5. **Leverage free agentic actions** — tool calls are free, so agent mode
   is actually more efficient than multi-turn chat for complex tasks

### Cost comparison: 6-phase dev loop task

| Strategy | Phases × model | Premium requests | Cost |
|----------|---------------|-----------------|------|
| All Opus 4.6 | 6 × 10× | **60** | $2.40 |
| All Sonnet 4.6 | 6 × 1× | **6** | $0.24 |
| Smart routing | 2× Sonnet + 4× GPT-4.1 | **2** | $0.08 |
| All GPT-4.1 | 6 × 0× | **0** | $0.00 |

### What codeclub's `set_available_models` enables

When a Copilot CLI user seeds their models, the router knows:
- GPT-4.1 is 0× → use for fill, testgen, report (free)
- GPT-5 mini is 0× → use for map on moderate tasks (free)
- Sonnet 4.6 is 1× → use for spec/review on complex tasks (cheap)
- Opus 4.6 is 10× → only for expert-level spec where quality matters most

This means a typical moderate coding task uses **0 premium requests** by
routing everything to included models.
