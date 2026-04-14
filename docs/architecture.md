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

## References

- [arXiv:2307.15337](https://arxiv.org/abs/2307.15337) — Skeleton-of-Thought: Prompting LLMs for Efficient Parallel Generation
- [arXiv:2604.00025](https://arxiv.org/abs/2604.00025) — Inverse Scaling Can Be Easily Overcome With Scale-Aware Prompting
- [arXiv:2601.19929](https://arxiv.org/abs/2601.19929) — Stingy Context / TREEFRAG structural compression
- [spec-kit](https://github.com/github/spec-kit) — structured task decomposition
- [LLMLingua](https://github.com/microsoft/LLMLingua)
- [Repomix](https://github.com/yamadashy/repomix)
- [Caveman](https://github.com/juliusbrussee/caveman)
