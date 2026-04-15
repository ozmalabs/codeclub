# Architecture

## File map

| Package | File | What |
|---|---|---|
| `compress` | `tree.py` | Tree-sitter stubbing, SourceMap |
| `compress` | `semantic.py` | ChromaDB retrieval |
| `compress` | `expand.py` | Splice LLM output back into original |
| `compress` | `brevity.py` | Scale-aware output constraints |
| `compress` | `pipeline.py` | End-to-end compression pipeline |
| `dev` | `loop.py` | Autonomous dev loop, CLI |
| `dev` | `generate.py` | Two-phase map+fill generation |
| `dev` | `spec.py` | Task decomposition (spec-kit style) |
| `dev` | `testgen.py` | pytest generation |
| `dev` | `runner.py` | Test execution, failure compression |
| `dev` | `review.py` | Code review (independent model) |
| `infra` | `models.py` | Model registry, router, presets |
| `infra` | `hardware.py` | Hardware inventory, endpoint registry |
| `accounting` | `tracker.py` | Per-task ledger: tokens, time, energy |
| `accounting` | `power.py` | RAPL / hwmon / TDP energy measurement |
| `accounting` | `baseline.py` | Savings vs reference model calculations |
| `benchmarks` | `tiered.py` | Three-way map backend benchmark |
| `benchmarks` | `llm.py` | Live API benchmark harness |
| — | `tournament.py` | Model tournament, efficiency maps, compound efficiency, cost estimation, parallelism, language proficiency, task profiles, context strategies, sysadmin/cloud archetypes, request classification, TypeScript runner |
| — | `smash_viz.py` | Efficiency map visualisation (PNG, HTML, 3D surfaces, value overlays) |
| — | `smash_server.py` | Interactive HTML efficiency map viewer |
| — | `bench_tiered.py` | Tiered map backend benchmark runner |

## Key concepts

| Concept | What | Where |
|---|---|---|
| `SmashRange` / `SmashCoord` | Two-axis model envelope (difficulty × clarity) | `tournament.py` |
| `fit()` | Quality prediction: 0.0–1.0 at a coordinate | `SmashRange.fit()` |
| Compound efficiency | Value × speed blend with hardware profiles | `compound_efficiency()` |
| Value efficiency | Quality per dollar, speed-independent | `value_efficiency()` |
| Wallclock score | Speed score with hardware + cloud modifiers | `wallclock_score()` |
| `HardwareProfile` | Speed modifier + power draw per hardware tier | `HARDWARE_PROFILES` |
| Cost estimation | Pre-run cost/time/quality prediction per model | `estimate_task()` |
| Routing recommendation | Best-value / best-speed / best-compound pick | `recommend_routing()` |
| Project budget | Aggregate cost across tasks with strategy comparison | `estimate_project_budget()` |
| Decomposition | Map + N×parallel fills, fan-out estimation | `decompose_task()`, `estimate_parallel()` |
| Language proficiency | Per-model per-language quality multiplier | `MEASURED_LANG_PROFICIENCY` |
| `TaskProfile` | Real-world cost model: gather rounds, iterations, dead time | `TASK_PROFILES` (33 profiles) |
| `ContextStrategy` | How context intelligence reduces cost (5 presets) | `CONTEXT_STRATEGIES` |
| `RequestClassification` | Heuristic task type detection (code/sysadmin/cloud/debug) | `classify_request()` |
| Sysadmin/cloud archetypes | SmashCoord + TaskProfile pairs for real ops tasks | `SYSADMIN_ARCHETYPES` (28) |
| `classify_and_estimate()` | Full pipeline: classify → coords → profile | `tournament.py` |
| `estimate_task_profiled()` | Profile-aware cost/time/quality estimation | `tournament.py` |
| `compare_context_strategies()` | Deep-dive context savings for a single archetype | `tournament.py` |
| `TypeScriptRunner` | TS/TSX execution with JSX shim (16 tasks) | `tournament.py` |

## References

- [arXiv:2307.15337](https://arxiv.org/abs/2307.15337) — Skeleton-of-Thought: Prompting LLMs for Efficient Parallel Generation
- [arXiv:2604.00025](https://arxiv.org/abs/2604.00025) — Inverse Scaling Can Be Easily Overcome With Scale-Aware Prompting
- [arXiv:2601.19929](https://arxiv.org/abs/2601.19929) — Stingy Context / TREEFRAG structural compression
- [spec-kit](https://github.com/github/spec-kit) — structured task decomposition
- [LLMLingua](https://github.com/microsoft/LLMLingua)
- [Repomix](https://github.com/yamadashy/repomix)
- [Caveman](https://github.com/juliusbrussee/caveman)
