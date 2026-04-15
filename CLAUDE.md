## Model routing (MCP: codeclub)

This project has a codeclub MCP server connected. It routes tasks to the
cheapest Claude model that can handle them. **Haiku is very capable** — it
handles most single-file coding, tests, debugging, and standard features.

### Before spawning a sub-agent

Call `pick_model` with the task description. Use the returned `model_id` as the
sub-agent's model. Pass `context_chars` if you know how much context you'll send.

```
pick_model(task="add docstrings to all public functions", context_chars=8000)
→ { "tier": "haiku", "model_id": "claude-haiku-4-5-20251001", "context_strategy": { "action": "full" } }
```

If `context_strategy.action` is `"compress"`, call `compress_context` on the
code before sending it to the sub-agent.

### Tier thresholds (from classify_and_estimate)

| Difficulty | Clarity ≥ 35 | Clarity < 35 |
|---|---|---|
| d ≤ 35 | haiku | sonnet |
| 36–65 | sonnet | opus |
| d > 65 | opus | opus |

### Budget hints

Pass `budget="haiku"` to force the cheapest tier, or `budget="opus"` to force
the strongest. The tool will note when an override changes the auto-route.

### Tools

| Tool | When |
|---|---|
| `pick_model` | Before spawning any sub-agent |
| `classify_task` | When you need raw difficulty/clarity/category data |
| `compress_context` | When pick_model says to compress, or when sending large files |
| `estimate_cost` | When you want to show the user the haiku/sonnet/opus cost tradeoff |
| `run_dev_loop` | When the user asks to build something from scratch — runs spec → generate → test → fix → review |

---

@./skills/codeclub/SKILL.md
