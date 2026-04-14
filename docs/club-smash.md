# Club Smash — Right-Sizing Models to Tasks

> You don't use a sledgehammer to crack a nut.
> You don't use a nut to crack a boulder.
> Club Smash finds the right club.

Club Smash is a two-axis efficiency system that maps every model and every task
onto the same plane, then routes by geometry — no role-specific logic, no
if/else chains.

## The two axes

**Difficulty** (0–100): How hard is the problem?

```
0 ────────────────────────────────────────── 100
trivial        moderate       hard        PhD-level
"rename var"   "rate limiter" "parser"    "compiler"
```

**Clarity** (0–100): How well-specified is the input?

```
0 ────────────────────────────────────────── 100
vague chat     spec doc       skeleton    skeleton + tests
"make it work" "build X with" "fill this" "fill this, here are tests"
```

## The compressor map

Every model has an efficiency region on this plane — exactly like a turbo
compressor map. There's a sweet spot where the model is right-sized, a
boundary where it starts struggling, and an outer region where it's either
overkill or overwhelmed.

```
    100c │▒▒▒▒▓▓▓█▓▓▓▓▓▓█████▓▒▒░░░···          │
     85c │▒▒▒▒▓▓▓█▓▓▓▓▓▓█████▓▒▒░░░···          │  ← min clarity
     70c │▒▒▒▒▓▓▓█▓▓▓▓▓▓█████▓▒▒░░░···          │
     55c │░░░░░░▒▒░░░░▒▒▒▒▒▒▒░░░░····           │
     40c │   ··················                   │
         └────────────────────────────────────────┘
          0    10   20   30   40   50   60   70
                      difficulty →

    █ peak  ▓ high  ▒ ok  ░ weak  · waste
```

Small models have a tight, high-up island — they need clear instructions but
nail simple tasks fast. Large models have a wide, low plateau — they handle
ambiguity but are wasteful on simple work.

### How to read it

- **Green/peak zone**: model is right-sized. Fast, cheap, accurate.
- **Orange/weak zone**: model can do it but it's struggling or overkill.
- **Red/waste zone**: wrong tool for the job.
- **★ star**: the model's sweet spot.
- **◆ diamonds**: where benchmark tasks land on this model's map.

## Roles are coordinates

The key insight: roles like `fill`, `map`, `review`, `oneshot` aren't special
code paths — they're just reference points on the plane.

```python
ROLE_DEFAULTS = {
    "fill":      {"diff_offset": -10, "clarity": 90},  # skeleton → code
    "expand":    {"diff_offset":  -5, "clarity": 80},
    "compress":  {"diff_offset": -15, "clarity": 85},
    "review":    {"diff_offset":  -5, "clarity": 75},
    "map":       {"diff_offset":   0, "clarity": 70},  # NL → architecture
    "testgen":   {"diff_offset":  +5, "clarity": 65},
    "generate":  {"diff_offset":  +5, "clarity": 60},
    "oneshot":   {"diff_offset": +10, "clarity": 65},  # NL → complete code
}
```

A `fill` task at base difficulty 50 becomes `(40d, 90c)` — easier (skeleton
helps) and very clear (you can see what to write). An `oneshot` at the same
base is `(60d, 65c)` — harder and less clear.

This means:
- **No role-specific routing code.** The router just checks `model.smash.covers(task_coord)`.
- **New roles are free.** Add a row to the table, routing works automatically.
- **Tasks can override.** If a specific task's `fill` is unusually hard, it
  provides a custom coordinate.

## SmashRange — the model envelope

Every model gets a `SmashRange` describing its operating envelope:

```python
SmashRange(
    low=15,         # minimum difficulty it handles
    sweet=35,       # peak efficiency point
    high=55,        # maximum difficulty it can attempt
    min_clarity=65, # below this, model can't handle the ambiguity
)
```

Estimated from model specs:

| Effective params | Range | Min clarity |
|---|---|---|
| <2B | 5–15–25 | 85+ |
| <5B | 10–25–40 | 75+ |
| <10B | 15–35–55 | 65+ |
| <20B | 20–45–65 | 55+ |
| <40B | 25–50–75 | 45+ |
| <80B | 30–55–85 | 35+ |
| 80B+ | 35–60–95 | 25+ |

Quantization degrades capability: Q8→0.95×, Q6_K→0.90×, Q4_K_M→0.80×, Q2_K→0.55×.

These estimates are the cold start — used before real benchmarks exist. Once a model
has an empirical efficiency map, the map is the truth regardless of architecture.

## Efficiency visualisation

Generate turbo compressor–style efficiency maps:

```bash
# All models + overlay + comparisons → benchmarks/maps/
python smash_viz.py

# Single model
python smash_viz.py --model rnj-1:8b

# Compare quantizations
python smash_viz.py --quant-compare gemma4-26b-a4b

# Interactive HTML viewer
python smash_server.py
```

Three output formats:
- **PNG** (matplotlib) — publication-quality static images
- **HTML** (plotly) — interactive hover, zoom, toggle models on/off
- **ASCII** (terminal) — `python tournament.py --map`

### Example: rnj-1:8b (8B, Q6_K, Intel Arc B580)

![rnj-1:8b efficiency map](../benchmarks/maps/rnj-1-8b.png)

Tight island around 35d difficulty, needs 65+ clarity. Tasks like RateLimiter
(30d) and LRU Cache (35d) land right in the sweet spot. ExprParser (65d) is at
the edge — it works because the task has very high clarity (well-specified tests).

### Example: Model overlay

![Model overlay](../benchmarks/maps/overlay.png)

All models on one chart. You can see:
- **qwen2.5-coder:1.5b** — tiny island, high clarity only
- **rnj-1:8b / gpt-4.1-nano** — mid-range, overlapping
- **qwen3-coder:30b / deepseek-v3** — wide coverage, low clarity tolerance
- Tasks cluster in the 30–70 difficulty, 60–90 clarity range

### Example: Quantization comparison

![Gemma4 quant comparison](../benchmarks/maps/quant_gemma4-26b-a4b.png)

Same model (gemma4-26b-a4b), different quantizations. bf16 has the widest
envelope; Q2_K shrinks it dramatically. This helps decide: is Q4_K_M good
enough for your tasks, or do you need Q6_K?

## The fit function

`SmashRange.fit(coord)` returns 0.0–1.0:

```
1.0  ─── peak efficiency (sweet spot)
0.85 ─── high efficiency
0.70 ─── usable
0.50 ─── marginal (covers threshold)
0.30 ─── struggling
0.0  ─── out of range
```

Two gates multiplied:
- **Difficulty gate**: undersized (above high) → 0, oversized (below low) →
  slight penalty, sweet spot → ~1.0
- **Clarity gate**: above min_clarity → 1.0, below → steep penalty

## Heuristic routing

For arbitrary queries (no benchmark data), `estimate_query_coords()` estimates
a task's position from text signals:

```python
coord = estimate_query_coords(
    "Build a concurrent rate limiter with token bucket",
    role="oneshot",
    has_tests=True,
)
# → SmashCoord(difficulty=55, clarity=80)
```

Signals: word count, complexity keywords ("async", "parser", "distributed"),
presence of tests/examples/signatures. The role applies its offset.

## Tournament validation

The tournament (`tournament.py`) validates these heuristics with real code
execution. Models fight on real tasks with real test suites. Results feed back
to refine the smash estimates.

```bash
python tournament.py --task rate-limiter    # single task
python tournament.py --quick               # stop at first champion
python tournament.py --map                 # show ASCII maps
python tournament.py --json results.json   # export data
```

→ [Tournament results](benchmarks.md)
