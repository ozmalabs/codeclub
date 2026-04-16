---
name: codeclub
description: >
  Tree-sitter code compression for LLM agents. Replaces function bodies with
  `...` stubs, keeping signatures and docstrings. 70–95% fewer tokens, zero
  quality loss. Supports Python, JS, TS, JSX, TSX, C#.
  Use when: sending large files to models, compressing context before sub-agent
  calls, or reducing token costs on any LLM interaction.
---

# codeclub

> Caveman not have H100. Caveman only have club.

Code compression that saves 70–95% of tokens with zero quality loss.

| Tool | Local | What |
|---|---|---|
| **compress_context** | Yes | Tree-sitter stubbing — strip function bodies, keep signatures |
| **pick_model** | clubrouter | Pick cheapest capable model for a task |
| **classify_task** | clubrouter | Classify difficulty, clarity, category |
| **estimate_cost** | clubrouter | Token/cost estimates across models |

## Compress — send less, pay less

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

## Routing — via clubrouter.com (optional)

Routing, dev loops, and efficiency maps are provided by
[clubrouter.com](https://clubrouter.com). Set `CLUBROUTER_API_KEY` or run
`codeclub login` to enable routing tools in the MCP server.

Without clubrouter, only `compress_context` is available. Routing tools
return a setup hint if not configured.

## When to use what

| Situation | Tool |
|---|---|
| Sending large files to model | `compress_context` |
| Picking the right model for a task | `pick_model` (requires clubrouter) |
| Comparing costs across models | `estimate_cost` (requires clubrouter) |
| Building code from a spec | [clubrouter dev loop](https://clubrouter.com) |

## Install

```bash
pip install codeclub                    # Python library
npm install @codeclub/mcp-server        # TypeScript MCP server
```
