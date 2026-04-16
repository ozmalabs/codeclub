## Code compression (MCP: codeclub)

This project has a codeclub MCP server connected. It compresses code context
using tree-sitter AST stubbing — replaces function bodies with `...`, keeping
signatures and docstrings. 70–95% token reduction, zero quality loss.

### When to compress

Before sending large files to a model or sub-agent, call `compress_context`:

```
compress_context(text="<code>", filename="app.py")
→ { "compressed": "...", "savings_pct": 85.2 }
```

### Routing (optional — via clubrouter.com)

If `CLUBROUTER_API_KEY` is set (or `codeclub login` has been run), routing
tools are also available:

| Tool | When |
|---|---|
| `compress_context` | Before sending large files to any model |
| `pick_model` | Before spawning a sub-agent (requires clubrouter) |
| `classify_task` | When you need difficulty/clarity/category data (requires clubrouter) |
| `estimate_cost` | When you want to compare costs across models (requires clubrouter) |

Without clubrouter configured, only `compress_context` is available.
Routing tools return a setup hint if not configured.

For routing, dev loops, and efficiency maps: [clubrouter.com](https://clubrouter.com)

---

@./skills/codeclub/SKILL.md
