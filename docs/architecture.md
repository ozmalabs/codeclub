# Architecture

codeclub is a code compression library. Routing, dev loops, and efficiency maps
live in [clubrouter](https://clubrouter.com).

## File map

### Compression (core — lives here)

| Package | File | What |
|---|---|---|
| `compress` | `tree.py` | Tree-sitter stubbing, SourceMap |
| `compress` | `semantic.py` | ChromaDB retrieval |
| `compress` | `expand.py` | Splice LLM output back into original |
| `compress` | `brevity.py` | Scale-aware output constraints |
| `compress` | `pipeline.py` | End-to-end compression pipeline |

### MCP server (TypeScript)

| File | What |
|---|---|
| `mcp-server/src/compress.ts` | Tree-sitter WASM stubbing (Python, JS, TS, C#) |
| `mcp-server/src/clubrouter.ts` | HTTP client for clubrouter.com API (optional) |
| `mcp-server/src/index.ts` | MCP server — compress_context + routing proxy |

### MCP server (Python)

| File | What |
|---|---|
| `codeclub/claude_code_mcp.py` | MCP server for Claude Code — compression + routing proxy |

### Supporting (used by clubrouter)

These files provide data and utilities that clubrouter imports. They remain
in the codeclub repo as library code.

| File | What |
|---|---|
| `infra/models.py` | Model registry, ModelSpec, ModelRouter |
| `infra/hardware.py` | Hardware inventory, endpoint registry |
| `tournament.py` | Tournament data, efficiency maps, SmashCoord/SmashRange |
| `accounting/tracker.py` | Per-task ledger: tokens, time, energy |
| `accounting/power.py` | RAPL / hwmon / TDP energy measurement |

### Moved to clubrouter

The following features now live in the [clubrouter](https://clubrouter.com) repo:

- Dev loop (spec → generate → test → fix → review)
- Request classification and routing
- Context proxy with session indexing
- Efficiency map visualisation
- Task profiles and context strategies

## Key concepts (compression)

| Concept | What | Where |
|---|---|---|
| `stub_functions()` | Replace function bodies with `...` | `compress/tree.py` |
| `SourceMap` | Original→compressed line mapping | `compress/tree.py` |
| `expand()` | Splice LLM edits back into original | `compress/expand.py` |
| `ChromaRetriever` | Semantic search over stubs | `compress/semantic.py` |
| `build_stub_index()` | Index all stubs for retrieval | `compress/semantic.py` |

## References

- [arXiv:2601.19929](https://arxiv.org/abs/2601.19929) — Stingy Context / TREEFRAG structural compression
- [arXiv:2307.15337](https://arxiv.org/abs/2307.15337) — Skeleton-of-Thought
- [Caveman](https://github.com/juliusbrussee/caveman)
- [LLMLingua](https://github.com/microsoft/LLMLingua)
- [Repomix](https://github.com/yamadashy/repomix)
