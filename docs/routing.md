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

## Efficiency-based routing

For pre-run planning, `recommend_routing()` picks the best model for a task
based on compound efficiency — not just "can it do it?" but "what's the best
value for my priorities?"

```python
from tournament import recommend_routing, SmashCoord, build_contenders

rec = recommend_routing(
    SmashCoord(difficulty=45, clarity=70),
    build_contenders(),
    lang="rust",
    hw_profile="gpu_consumer",
    speed_weight=0.5,     # 0=pure value, 0.5=balanced, 1=speed-critical
    min_quality=0.5,
)
rec.best_value     # cheapest model that passes quality threshold
rec.best_speed     # fastest model
rec.best_compound  # best blend of value and speed
```

Three optimisation modes via `speed_weight`:
- **0.0 — value**: batch jobs, overnight runs. Don't care about speed.
- **0.5 — balanced**: default. Good tradeoff.
- **1.0 — speed-critical**: interactive coding, live demos. Pay more for speed.

## Hardware profiles

Hardware affects wallclock time, not value efficiency. Same model quality/cost,
different wait times.

| Profile | Speed | Power | Description |
|---|---:|---:|---|
| `cpu_budget` | 0.15× | 65W | i5 / Ryzen 5 |
| `cpu_workstation` | 0.35× | 100W | Xeon / Threadripper |
| `gpu_consumer` | **1.0×** | 150W | RTX 3060 / Arc B580 — **reference** |
| `gpu_midrange` | 1.5× | 200W | RTX 4070 / Arc B770 |
| `gpu_enthusiast` | 2.2× | 350W | RTX 4090 / RTX 5080 |
| `gpu_workstation` | 3.0× | 300W | A6000 / L40S |
| `a100` | 3.5× | 400W | A100 80GB SXM |
| `h100` | 5.0× | 700W | H100 80GB SXM |
| `cloud_api` | 1.0× | 0W | Provider-managed |

`cloud_speed_modifier` is separate — for provider congestion, rate limits,
or shared infrastructure. Both modifiers multiply into wallclock score:

```python
from tournament import wallclock_score, SmashCoord

# Same model, different hardware
wallclock_score(tok_s=40, coord=SmashCoord(35, 70), hw_speed_modifier=0.15)  # CPU: 3.2
wallclock_score(tok_s=40, coord=SmashCoord(35, 70), hw_speed_modifier=5.0)   # H100: 93.1
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

## Request classification

Before routing, the system detects *what kind* of task this is. This matters
because task type fundamentally changes the cost model — a d=45 sysadmin task
costs 10-50× more than a d=45 coding task due to context gathering.

```python
from tournament import classify_request, classify_and_estimate

# Quick classification
r = classify_request("build me a docker container with frigate using GPU offload")
# → category="sysadmin", subcategory="docker", confidence=0.65
# → suggested_profile="sysadmin-docker-moderate"
# → signals=["container", "docker"]

# Full pipeline: classify → estimate coords → select profile
classification, coord, profile = classify_and_estimate(
    "set up terraform for ECS fargate with blue-green deployments",
    role="oneshot",
)
# classification.category = "cloud"
# coord = SmashCoord(difficulty=55, clarity=50)  # cloud tasks get difficulty bump
# profile = TASK_PROFILES["cloud-iac-moderate"]  # 5 gather rounds, 3 iterations
```

Categories and what they detect:

| Category | Subcategories | Example signals |
|---|---|---|
| `code` | build, bugfix | "implement", "write a function", "fix the bug", "broken" |
| `sysadmin` | docker, networking, service, database, security, storage | "dockerfile", "nginx", "systemctl", "pg_dump", "fail2ban" |
| `cloud` | terraform, aws, gcp, azure, networking, cicd | "terraform", "ecs", "lambda", "vpc", "github actions" |
| `debug` | general, profiling | "traceback", "memory leak", "503", "bottleneck" |
| `cross-codebase` | general, migration | "across repos", "microservice", "migrate from" |

Keyword-based, no LLM call, runs in microseconds. The classification nudges
the routing coordinates — cloud tasks get +10 difficulty and −15 clarity
(hidden complexity), sysadmin gets +5/−10, debug +5/−5.

## Profile-aware routing

For tasks beyond pure code generation, `estimate_task_profiled()` accounts
for the full cost structure: context gathering, iteration loops, and dead
wallclock time.

```python
from tournament import estimate_task_profiled, SmashCoord, TASK_PROFILES, build_contenders

coord = SmashCoord(difficulty=45, clarity=60)
profile = TASK_PROFILES["sysadmin-docker-moderate"]

estimates = estimate_task_profiled(
    coord, profile, build_contenders(),
    lang="python", hw_profile="gpu_consumer",
)
# Each estimate includes: total_tokens (with gathering), total_time (with dead time),
# total_cost (API + energy), quality, compound_efficiency
```

Same task, different profiles — wildly different costs:

| Profile | Gather rounds | Iterations | Dead time | Total tokens |
|---|---:|---:|---:|---:|
| code-moderate | 0 | 1 | 0s | 1,200 |
| sysadmin-docker-moderate | 5 | 3 | 180s | 22,000 |
| cloud-iac-moderate | 5 | 3 | 480s | 26,000 |

## Coming soon

**Web interface** — Visual task progression, queue management, accounting
dashboard, model/endpoint status, git integration with one-click commit.
