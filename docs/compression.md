# Compression

Token compression pipeline for LLM coding agents. Reduces input context 70–95%
before sending to any model. Cuts output noise with scale-aware brevity constraints.

```bash
pip install codeclub-compress
```

## The problem

LLM coding agents send entire files as context. Most is irrelevant to the task.
You pay for every token even though the model only needs a fraction.

## Structural compression (`codeclub.compress.tree`)

Replace function bodies with `...` stubs via tree-sitter. Keep signatures +
docstrings. Supports Python, JS, JSX, TS — language configs are pluggable.

```python
from codeclub.compress import stub_functions

compressed, source_map = stub_functions(code, language="python")
# 500 lines → 40 lines. SourceMap preserves offsets for round-trip.
```

Typical reduction: 70–94%.

## Semantic retrieval (`codeclub.compress.semantic`)

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

## Round-trip expansion (`codeclub.compress.expand`)

LLM edits stubs. Expander splices changed bodies back into the original file.
Kept `...` stubs restore original bodies automatically — no diff required.

```python
from codeclub.compress import expand

patched = expand(original_code, source_map, llm_output)
```

## Brevity constraints (`codeclub.compress.brevity`)

Scale-aware output compression. SMALL tier failure mode is under-completion, not
verbosity — minimal suppression wins.

| Variant | Quality | Output tokens |
|---|---|---|
| No constraint | 100% | verbose |
| `"No explanation. Code only."` | **100%** | ~1800 (winner) |
| `"Output only the changed code"` | 80% | diff fragments |

## Key findings

- **Retrieved + no brevity**: 85% cost reduction, 0% quality loss vs native context
- **Brevity winner**: `"No explanation. Code only."` — 100% quality, ~50% fewer output tokens
- **CJK is NOT a shortcut**: Chinese characters cost more in cl100k_base for equivalent meaning
