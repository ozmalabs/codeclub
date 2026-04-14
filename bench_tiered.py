#!/usr/bin/env python3
"""
🏏 Targeted benchmark runner — populate the efficiency grid fast.

Instead of the full tournament (N² combinations), this runs specific
model × task combinations chosen to maximise coverage of the
difficulty × clarity × throughput space.

Usage:
    python bench_tiered.py                    # default grid
    python bench_tiered.py --quick            # just 3 models × 3 tasks
    python bench_tiered.py --json results.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from tournament import (
    TASKS,
    FightResult,
    build_contenders,
    check_endpoints,
    fight_oneshot,
    run_tests,
    SmashCoord,
)

# ── Target grid: spread of tasks across difficulty spectrum ──
EASY_TASKS   = ["counter", "rust-hello-struct"]          # diff 8, 12
MEDIUM_TASKS = ["rate-limiter", "min-stack", "csv-parser"]  # diff 20, 30, 35
HARD_TASKS   = ["event-emitter", "expr-parser", "rust-stack-calc"]  # diff 40, 55, 45
HARDER_TASKS = ["json-path", "rust-json-parser", "mini-regex"]  # diff 70, 75, 78
EXTREME_TASKS = ["rust-arena-alloc"]                     # diff 82

# Models to benchmark (covering different sizes/speeds)
QUICK_MODELS = ["gpt-4.1-nano", "gpt-4.1-mini", "claude-sonnet-4"]
DEFAULT_MODELS = [
    # Tiny (should ace easy, struggle on hard)
    "qwen2.5-coder:1.5b", "gpt-4.1-nano", "phi-4",
    # Medium (should handle most things)
    "rnj-1:8b", "gpt-4.1-mini", "gemini-2.5-flash",
    "codestral-2508", "devstral-small", "claude-haiku-4.5",
    # Large (should handle everything, but overkill for easy)
    "claude-sonnet-4", "gpt-4.1", "gemini-2.5-pro",
    "deepseek-v3.1", "deepseek-r1",
]

def main():
    parser = argparse.ArgumentParser(description="🏏 Targeted efficiency grid benchmarks")
    parser.add_argument("--quick", action="store_true", help="Quick run (3 models × 3 tasks)")
    parser.add_argument("--json", metavar="FILE", help="Export results to JSON")
    parser.add_argument("--models", help="Comma-separated model names")
    parser.add_argument("--tasks", help="Comma-separated task IDs")
    parser.add_argument("--difficulty", choices=["easy", "medium", "hard", "harder", "extreme", "all"],
                        default="all", help="Difficulty band to run")
    args = parser.parse_args()

    # Build task list
    if args.tasks:
        task_ids = [t.strip() for t in args.tasks.split(",")]
    elif args.difficulty == "all":
        task_ids = EASY_TASKS + MEDIUM_TASKS + HARD_TASKS + HARDER_TASKS + EXTREME_TASKS
    else:
        band = {
            "easy": EASY_TASKS, "medium": MEDIUM_TASKS,
            "hard": HARD_TASKS, "harder": HARDER_TASKS,
            "extreme": EXTREME_TASKS,
        }[args.difficulty]
        task_ids = band

    tasks = []
    for tid in task_ids:
        if tid not in TASKS:
            print(f"⚠️  Unknown task: {tid}")
            continue
        tasks.append(TASKS[tid])

    # Build model list
    if args.models:
        model_names = [m.strip() for m in args.models.split(",")]
    elif args.quick:
        model_names = QUICK_MODELS
    else:
        model_names = DEFAULT_MODELS

    # Check endpoints
    print("📡  Checking endpoints...")
    contenders = check_endpoints(build_contenders())
    alive_names = {c.name for c in contenders}
    selected = [c for c in contenders if c.name in model_names]
    skipped = [n for n in model_names if n not in alive_names]
    if skipped:
        print(f"⚠️  Skipped (offline): {', '.join(skipped)}")
    print(f"   {len(selected)} models × {len(tasks)} tasks = {len(selected) * len(tasks)} fights\n")

    if not selected or not tasks:
        print("❌  Nothing to run")
        sys.exit(1)

    # Run fights
    results: list[dict] = []
    total = len(selected) * len(tasks)
    done = 0
    passed_count = 0
    failed_count = 0
    total_cost = 0.0

    for task in tasks:
        coord = task.coord_for("oneshot")
        print(f"{'━' * 70}")
        print(f"  🏔️  {task.name} (diff={task.base_difficulty}, {task.lang}, {len(task.tests)} tests)")
        print(f"  🏏  oneshot coord: {coord}")
        print(f"{'━' * 70}")

        for c in selected:
            done += 1
            fit = c.smash.fit(coord)
            est_time = c.time_for(coord)
            print(f"\n  [{done}/{total}] {c.club} {c.name}"
                  f"  (fit={fit:.2f}, est={est_time:.1f}s)", end="", flush=True)

            t0 = time.time()
            r = fight_oneshot(c, task)
            wall = time.time() - t0

            icon = "🏆" if r.quality >= 1.0 else "⚠️" if r.quality > 0 else "💀"
            print(f"  → {icon} {r.tests_passed}/{r.tests_total}"
                  f"  ⏱{r.elapsed_s:.1f}s  💰${r.cost:.6f}")

            if r.quality < 1.0 and r.test_details:
                failed = [(n, e) for n, p, e in r.test_details if not p]
                if failed:
                    print(f"       ❌ {failed[0][1][:80]}")

            total_cost += r.cost
            if r.quality >= 1.0:
                passed_count += 1
            else:
                failed_count += 1

            # Record result
            results.append({
                "task_id": task.id,
                "task_name": task.name,
                "task_lang": task.lang,
                "task_difficulty": task.base_difficulty,
                "coord_difficulty": coord.difficulty,
                "coord_clarity": coord.clarity,
                "model": c.name,
                "model_params_b": c.params_b,
                "model_quant": c.quant,
                "model_tok_s": c.tok_s,
                "model_is_local": c.is_local,
                "smash_fit": fit,
                "est_time_s": est_time,
                "actual_time_s": r.elapsed_s,
                "wall_time_s": wall,
                "quality": r.quality,
                "tests_passed": r.tests_passed,
                "tests_total": r.tests_total,
                "cost_usd": r.cost,
                "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out,
                "smash_measured": r.smash_measured,
                "energy_j": r.energy_j,
            })

    # Summary
    print(f"\n{'═' * 70}")
    print(f"  📊  BENCHMARK COMPLETE")
    print(f"  ✅ {passed_count} passed  ❌ {failed_count} failed  💰 ${total_cost:.4f} total")
    print(f"{'═' * 70}")

    # Results table
    print(f"\n  {'Model':25s} {'Task':20s} {'Diff':>4} {'Fit':>5}"
          f" {'Q':>5} {'Time':>6} {'Cost':>8} {'Smash':>5}")
    print(f"  {'─' * 25} {'─' * 20} {'─' * 4} {'─' * 5}"
          f" {'─' * 5} {'─' * 6} {'─' * 8} {'─' * 5}")
    for r in sorted(results, key=lambda x: (x["task_difficulty"], x["model"])):
        q_icon = "✅" if r["quality"] >= 1.0 else "⚠️" if r["quality"] > 0 else "❌"
        print(f"  {r['model']:25s} {r['task_name']:20s} {r['task_difficulty']:4d}"
              f" {r['smash_fit']:5.2f} {q_icon}{r['quality']:4.0%}"
              f" {r['actual_time_s']:5.1f}s ${r['cost_usd']:.5f}"
              f" {r['smash_measured']:5d}")

    # Export
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=2))
        print(f"\n  📁  Exported to {out}")


if __name__ == "__main__":
    main()

