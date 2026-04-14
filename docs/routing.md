# Hardware & Model Routing

Model routing as a lookup problem, not an LLM problem. A registry of benchmark
scores, hardware fit checks, dynamic escalation, and adapters for every provider.

```bash
pip install codeclub          # everything
pip install codeclub-infra    # routing only
```

## Hardware declaration

```python
from codeclub.infra.hardware import HardwareSetup
from codeclub.infra.models import router_for_setup

hw = HardwareSetup.from_dict({
    "devices": [
        {"name": "Intel Arc B580", "vram_mb": 12288, "backend": "sycl",
         "endpoint": "http://localhost:8081"},
        {"name": "NVIDIA RTX 3080", "vram_mb": 10240, "backend": "cuda",
         "endpoint": "http://192.168.1.10:8081"},
    ],
    "ram_mb": 32768,
    "ollama_url": "http://localhost:11434",
})
hw.probe()  # HTTP health-check each endpoint

router = router_for_setup("local_b580", hardware=hw)
```

Or detect automatically:

```bash
python -m codeclub.infra.hardware   # detect and print
python dev_loop.py "..." --detect-hardware
python dev_loop.py "..." --probe
```

## "Club until it fits"

The router tries the best quality quant first. If it doesn't fit in available
VRAM, it steps down through Q6_K → Q4_K_M → Q3_K_M until something runs. If
nothing fits on GPU, it falls back to CPU. The routing table shows `[gpu]` vs
`[cpu-fallback]`.

```
python dev_loop.py "..." --setup local_b580 --routing-table
```

## Setup presets

| Setup | What |
|---|---|
| `local_only` | Ollama CPU only, no internet |
| `local_b580` | B580 SYCL for map/review, Ollama CPU for fill |
| `openrouter_free` | Free-tier OpenRouter (rate-limited) |
| `openrouter_cheap` | Paid OpenRouter < $0.002/call |
| `anthropic` | Direct Anthropic API |
| `copilot` | GitHub Copilot SDK via local Copilot CLI |
| `github` | GitHub Models HTTP endpoint (free via `GITHUB_TOKEN`) |
| `best_local_first` | Local preferred, cloud fallback |

## Dynamic levers

The router is live. Change it between runs without restarting.

```python
router = router_for_setup("best_local_first", budget="cheap")
tracker = PerformanceTracker()

result1 = run(task1, router=router, tracker=tracker)

# Suddenly in a hurry
router.prefer_local = False
router.budget = "medium"

result2 = run(task2, router=router, tracker=tracker)
# tracker carries escalation history across both runs
```

## Supported providers

| Provider | How | Auth |
|---|---|---|
| Anthropic | Direct SDK | `ANTHROPIC_API_KEY` |
| OpenRouter | HTTP | `OPENROUTER_API_KEY` |
| GitHub Copilot SDK | Copilot CLI JSON-RPC | `gh auth login` or `GITHUB_TOKEN` |
| GitHub Models | HTTP | `GITHUB_TOKEN` |
| Ollama | HTTP localhost | none |
| llama.cpp server | HTTP any URL | none |

## Coming soon

**Web interface** — Visual task progression, queue management, accounting
dashboard, model/endpoint status, git integration with one-click commit.

**Language support** — Test runner abstraction: detect language → run `pytest` /
`jest` / `go test` / `cargo test`. Structural compression already
language-agnostic via tree-sitter; per-language configs for node type queries
are pluggable.
