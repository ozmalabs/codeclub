# codeclub

> Caveman not have H100. Caveman only have club.

## The story

It started with a question: **why does my local 8B model get the same tokens as GPT-5?**

I was running [Caveman](https://github.com/juliusbrussee/caveman) — the insight that
you don't need frontier models if you compress intelligently. Stub out functions,
send structure not source, reconstruct on the other side. Beautiful. 70–95% token
savings. But then I watched my little rnj-1:8b on an Arc B580 nail a rate limiter,
choke on a parser, and completely die on a vague spec.

Same model. Same hardware. Wildly different results. The model wasn't the problem
— the context was. Most of those tokens were function bodies the model never needed
to see.

That insight became codeclub: **strip code to its structure, send less, pay less,
get the same result.** Tree-sitter parses the AST, replaces bodies with `...`,
keeps signatures and docstrings. The model works with the shape of your code, not
the noise. Round-trip expansion puts it all back.

---

## What it does

codeclub is a code compression library. It uses tree-sitter AST parsing to replace
function bodies with `...` stubs, keeping signatures and docstrings. The model sees
the structure of your code without the implementation noise. Round-trip expansion
splices edits back into the original — no diff required.

| | What | Result |
|---|---|---|
| **Tree-sitter stubbing** | Replace function bodies with `...` | 70–95% fewer tokens |
| **Semantic retrieval** | Index stubs, return only relevant ones | 95%+ reduction on large codebases |
| **Round-trip expansion** | LLM edits stubs → splice back | Lossless reconstruction |

Works with Python, JavaScript, JSX, TypeScript, TSX, and C#.

### Compression savings (real files)

| File | Tokens | After pipeline | Saved |
|---|---:|---:|---:|
| wallet_stripe.py (934 lines) | 7,202 | 300 | **96%** |
| 2 wallet files combined (1,343 lines) | 9,680 | 789 | **92%** |
| wallet_local.py (409 lines) | 2,478 | 489 | **80%** |
| wallet_bridge_snippet.py (78 lines) | 504 | 160 | **68%** |

## Quick start

### Python library

```bash
pip install codeclub
```

```python
from codeclub.compress import stub_functions

compressed, source_map = stub_functions(code, language="python")
# 500 lines → 40 lines, round-trippable
```

### MCP server (TypeScript)

Works with Claude Code, Copilot CLI, Cursor, Windsurf, Cline — any MCP client.

```bash
# Claude Code
claude mcp add codeclub -- npx @codeclub/mcp-server

# Copilot CLI
/mcp add codeclub -- npx @codeclub/mcp-server
```

No API key needed. Compression runs locally using tree-sitter WASM grammars.

### MCP server (Python)

```bash
claude mcp add codeclub -- uv run python -m codeclub.claude_code_mcp
```

---

## Compress — send less, pay less

LLM agents send whole files as context. Most is irrelevant. You pay for every token
the model ignores.

### Tree-sitter stubbing

Replace function bodies with `...` stubs. Keep signatures + docstrings.

```python
from codeclub.compress import stub_functions

compressed, source_map = stub_functions(code, language="python")
# 500 lines → 40 lines. SourceMap preserves offsets for round-trip.
```

Supports Python, JS, JSX, TS, TSX, C#. Language configs pluggable.

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

---

## MCP server

The MCP server exposes `compress_context` — no API key, no network, runs locally.

If you also use [clubrouter.com](https://clubrouter.com), the MCP server can
proxy routing tools (`pick_model`, `classify_task`, `estimate_cost`) to it.
Set `CLUBROUTER_API_KEY` or run `codeclub login` to enable. Without it, only
compression is available.

### Agent plugin

| Agent | Install |
|-------|---------|
| **Claude Code** | `claude mcp add codeclub -- npx @codeclub/mcp-server` |
| **Copilot CLI** | `/mcp add codeclub -- npx @codeclub/mcp-server` |
| **Cursor** | `npx skills add ozmalabs/codeclub -a cursor` |
| **Windsurf** | `npx skills add ozmalabs/codeclub -a windsurf` |
| **Cline** | `npx skills add ozmalabs/codeclub -a cline` |
| **Any MCP client** | Run `npx @codeclub/mcp-server` via stdio |

---

## clubrouter — smart routing as a service

codeclub handles compression. For model routing, dev loops, and efficiency maps,
see [clubrouter.com](https://clubrouter.com).

clubrouter classifies every request by difficulty and clarity, then routes to the
cheapest model that can handle it. 40–60% savings with no quality loss. Built on
232 tournament fights across 19 models and 47 tasks.

Features that live in clubrouter:

- **Model routing** — difficulty/clarity classification → cheapest capable model
- **Dev loop** — spec → generate → test → fix → review → report
- **Efficiency maps** — per-model sweet spots, language blindspots, clarity cliff
- **Cost estimation** — token/cost/wallclock predictions across all models
- **Task profiles** — 33 profiles for coding, sysadmin, cloud, debug tasks

```bash
# Use clubrouter with any OpenAI-compatible client
export OPENAI_BASE_URL=https://clubrouter.com/v1
export OPENAI_API_KEY=cr-your-key
```

---

## Install

```bash
pip install codeclub          # Python library (compression + retrieval)
npm install @codeclub/mcp-server  # TypeScript MCP server
```

## Docs

- [Compression](docs/compression.md) — tree-sitter stubbing, semantic retrieval, brevity constraints
- [Architecture](docs/architecture.md) — file map, references

For model routing, dev loops, and efficiency maps: [clubrouter.com](https://clubrouter.com)

## References

- [Caveman](https://github.com/juliusbrussee/caveman) — the original insight: you don't need frontier models
- [arXiv:2601.19929](https://arxiv.org/abs/2601.19929) — Stingy Context / TREEFRAG structural compression
- [arXiv:2307.15337](https://arxiv.org/abs/2307.15337) — Skeleton-of-Thought: Prompting LLMs for Efficient Parallel Generation

---

Brought to you by Ozma from [ozmalabs.com](https://ozmalabs.com).
