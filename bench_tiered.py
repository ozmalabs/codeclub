#!/usr/bin/env python3
"""
🏏 Targeted benchmark runner — populate the efficiency grid fast.

Instead of the full tournament (N² combinations), this runs specific
model × task combinations chosen to maximise coverage of the
difficulty × clarity × throughput space.

Features:
    - SQLite-backed result store — only runs new/changed combos
    - Early cutoffs — skips a model once it's clearly failing at a level
    - Scaled max_tokens — harder tasks get bigger output budgets

Usage:
    python bench_tiered.py                    # default grid (skips cached)
    python bench_tiered.py --force            # re-run everything
    python bench_tiered.py --models gemini-2.5-pro --force
    python bench_tiered.py --import-json benchmarks/results_all.json
    python bench_tiered.py --show             # print stored results
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
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
    Contender,
    TournamentTask,
)
from benchmarks.store import BenchStore

# ── Target grid: spread of tasks across difficulty spectrum ──
EASY_TASKS   = ["counter", "rust-hello-struct"]          # diff 8, 12
MEDIUM_TASKS = ["rate-limiter", "min-stack", "csv-parser"]  # diff 20, 30, 35
HARD_TASKS   = ["event-emitter", "expr-parser", "rust-stack-calc"]  # diff 40, 55, 45
HARDER_TASKS = ["json-path", "rust-json-parser", "mini-regex"]  # diff 70, 75, 78
EXTREME_TASKS = ["rust-arena-alloc"]                     # diff 82
CLARITY_TASKS = [                                        # clarity axis
    "counter-vague", "counter-mid",
    "rate-limiter-vague",
    "expr-parser-vague", "expr-parser-mid",
    "lru-cache-vague",
    "rust-hello-vague",
    "event-emitter-mid",
    "csv-parser-vague",
    "min-stack-mid",
]
FRONTIER_TASKS = [                                       # vague + hard
    "url-shortener-vague", "kv-store-vague",
    "markdown-vague", "task-queue-vague",
    "saas-vague", "rust-minidb-vague",
]

# Models to benchmark (covering different sizes/speeds)
QUICK_MODELS = ["gpt-5.4-nano", "gpt-5.4-mini", "claude-sonnet-4.6"]
DEFAULT_MODELS = [
    # Tiny (should ace easy, struggle on hard)
    "qwen2.5-coder:1.5b", "gpt-5.4-nano", "phi-4",
    # Medium (should handle most things)
    "rnj-1:8b", "gpt-5.4-mini", "gemini-2.5-flash",
    "codestral-2508", "devstral-small", "claude-haiku-4.5",
    # Large (should handle everything, but overkill for easy)
    "claude-sonnet-4.6", "gpt-5.4", "gemini-2.5-pro",
    "deepseek-v3.1", "deepseek-r1",
]

# ── Cutoff config ──
# If a model gets this many consecutive 0% results in difficulty order,
# skip all remaining harder tasks for that model.
CUTOFF_CONSECUTIVE_ZEROS = 3

# Per-fight cost cap — abort if estimated cost exceeds this
MAX_COST_PER_FIGHT = 0.15  # $0.15


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        return ""


def _max_tokens_for(task: TournamentTask) -> int:
    """Scale output budget with task difficulty.  Easy tasks need ~2k,
    hard tasks need 8k+.  Prevents verbose models from truncating."""
    d = task.base_difficulty
    if d <= 15:
        return 4000
    if d <= 40:
        return 6000
    if d <= 70:
        return 8000
    return 12000


def _should_cutoff(model_zeros: dict[str, int], model: str) -> bool:
    """Check if model has hit the consecutive-zero cutoff."""
    return model_zeros.get(model, 0) >= CUTOFF_CONSECUTIVE_ZEROS


def main():
    parser = argparse.ArgumentParser(description="🏏 Targeted efficiency grid benchmarks")
    parser.add_argument("--quick", action="store_true", help="Quick run (3 models × 3 tasks)")
    parser.add_argument("--json", metavar="FILE", help="Also export results to JSON")
    parser.add_argument("--models", help="Comma-separated model names")
    parser.add_argument("--tasks", help="Comma-separated task IDs")
    parser.add_argument("--difficulty",
                        choices=["easy", "medium", "hard", "harder", "extreme",
                                 "clarity", "frontier", "all"],
                        default="all", help="Difficulty band to run")
    parser.add_argument("--force", action="store_true", help="Re-run even if cached results exist")
    parser.add_argument("--no-cutoff", action="store_true", help="Disable early cutoffs")
    parser.add_argument("--show", action="store_true", help="Show stored results and exit")
    parser.add_argument("--import-json", metavar="FILE", help="Import old JSON results into store")
    parser.add_argument("--tag", default="", help="Label for this benchmark run")
    args = parser.parse_args()

    store = BenchStore()

    # ── Import mode ──
    if args.import_json:
        p = Path(args.import_json)
        if not p.exists():
            print(f"❌  File not found: {p}")
            sys.exit(1)
        run_id = store.start_run(tag=args.tag or f"import:{p.name}", git_sha=_git_sha())
        count = store.import_json(p, run_id)
        store.finish_run(run_id)
        print(f"✅  Imported {count} results from {p}")
        return

    # ── Show mode ──
    if args.show:
        summaries = store.model_summary()
        if not summaries:
            print("📭  No stored results")
            return
        print(f"\n  {'Model':25s} {'Fights':>6} {'Perfect':>7} {'Avg Q':>6} {'Cost':>8} {'Avg ⏱':>7}")
        print(f"  {'─' * 25} {'─' * 6} {'─' * 7} {'─' * 6} {'─' * 8} {'─' * 7}")
        for s in summaries:
            print(f"  {s['model']:25s} {s['fights']:6d} {s['perfect']:7d}"
                  f" {s['avg_quality']:5.0%} ${s['total_cost'] or 0:7.4f}"
                  f" {s['avg_time_s'] or 0:6.1f}s")
        total = sum(s['fights'] for s in summaries)
        print(f"\n  {total} results across {len(summaries)} models")
        return

    # ── Build task list ──
    if args.tasks:
        task_ids = [t.strip() for t in args.tasks.split(",")]
    elif args.difficulty == "all":
        task_ids = EASY_TASKS + MEDIUM_TASKS + HARD_TASKS + HARDER_TASKS + EXTREME_TASKS
    elif args.difficulty == "clarity":
        task_ids = CLARITY_TASKS
    elif args.difficulty == "frontier":
        task_ids = FRONTIER_TASKS
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

    # Sort tasks by difficulty so cutoffs work correctly
    tasks.sort(key=lambda t: t.base_difficulty)

    # ── Build model list ──
    if args.models:
        model_names = [m.strip() for m in args.models.split(",")]
    elif args.quick:
        model_names = QUICK_MODELS
    else:
        model_names = DEFAULT_MODELS

    # ── Check endpoints ──
    print("📡  Checking endpoints...")
    contenders = check_endpoints(build_contenders())
    alive_names = {c.name for c in contenders}
    selected = [c for c in contenders if c.name in model_names]
    skipped = [n for n in model_names if n not in alive_names]
    if skipped:
        print(f"⚠️  Skipped (offline): {', '.join(skipped)}")

    if not selected or not tasks:
        print("❌  Nothing to run")
        sys.exit(1)

    # ── Count what needs running ──
    combos_to_run = []
    cached_count = 0
    for task in tasks:
        for c in selected:
            if not args.force and store.has_result(c.name, task.id, task.content_hash):
                cached_count += 1
            else:
                combos_to_run.append((c, task))

    total_possible = len(selected) * len(tasks)
    print(f"   {total_possible} combos: {cached_count} cached, {len(combos_to_run)} to run\n")

    if not combos_to_run:
        print("✅  All results cached — nothing to run (use --force to re-run)")
        if args.json:
            count = store.export_json(Path(args.json))
            print(f"  📁  Exported {count} results to {args.json}")
        return

    # ── Start run ──
    tag = args.tag or f"bench-{time.strftime('%Y%m%d-%H%M%S')}"
    run_id = store.start_run(tag=tag, git_sha=_git_sha())

    # ── Run fights ──
    done = 0
    passed_count = 0
    failed_count = 0
    skipped_cutoff = 0
    total_cost = 0.0

    # Track consecutive zeros per model for cutoff logic
    model_zeros: dict[str, int] = {}

    for task in tasks:
        coord = task.coord_for("oneshot")
        print(f"{'━' * 70}")
        print(f"  🏔️  {task.name} (diff={task.base_difficulty}, clarity={task.spec_clarity or 65},"
              f" {task.lang}, {len(task.tests)} tests)")
        print(f"  🏏  oneshot coord: {coord}")
        print(f"{'━' * 70}")

        for c in selected:
            # Skip if cached
            if not args.force and store.has_result(c.name, task.id, task.content_hash):
                continue

            done += 1

            # Early cutoff check
            if not args.no_cutoff and _should_cutoff(model_zeros, c.name):
                print(f"\n  [{done}/{len(combos_to_run)}] ⏭️  {c.name}"
                      f"  — cutoff ({CUTOFF_CONSECUTIVE_ZEROS} consecutive 0%)")
                skipped_cutoff += 1

                # Store the cutoff as a result so we don't re-run
                store.store_result(
                    run_id, model=c.name, task_id=task.id,
                    task_hash=task.content_hash,
                    task_name=task.name, task_lang=task.lang,
                    task_difficulty=task.base_difficulty,
                    coord_difficulty=coord.difficulty,
                    coord_clarity=coord.clarity,
                    quality=0, tests_passed=0, tests_total=len(task.tests),
                    error=f"cutoff: {CUTOFF_CONSECUTIVE_ZEROS} consecutive 0% results",
                )
                failed_count += 1
                continue

            fit = c.smash.fit(coord)
            est_time = c.time_for(coord)
            max_tok = _max_tokens_for(task)
            print(f"\n  [{done}/{len(combos_to_run)}] {c.club} {c.name}"
                  f"  (fit={fit:.2f}, est={est_time:.1f}s, max_tok={max_tok})",
                  end="", flush=True)

            t0 = time.time()
            r = fight_oneshot(c, task, max_tokens=max_tok)
            wall = time.time() - t0

            icon = "🏆" if r.quality >= 1.0 else "⚠️" if r.quality > 0 else "💀"
            print(f"  → {icon} {r.tests_passed}/{r.tests_total}"
                  f"  ⏱{r.elapsed_s:.1f}s  💰${r.cost:.6f}")

            if r.error:
                print(f"       🔥 {r.error[:100]}")
            elif r.quality < 1.0 and r.test_details:
                failed = [(n, e) for n, p, e in r.test_details if not p]
                if failed:
                    print(f"       ❌ {failed[0][1][:80]}")

            total_cost += r.cost
            if r.quality >= 1.0:
                passed_count += 1
                model_zeros[c.name] = 0  # reset streak
            else:
                failed_count += 1
                if r.quality == 0:
                    model_zeros[c.name] = model_zeros.get(c.name, 0) + 1
                else:
                    model_zeros[c.name] = 0  # partial pass resets streak

            # Store in DB
            store.store_result(
                run_id, model=c.name, task_id=task.id,
                task_hash=task.content_hash,
                task_name=task.name, task_lang=task.lang,
                task_difficulty=task.base_difficulty,
                coord_difficulty=coord.difficulty,
                coord_clarity=coord.clarity,
                model_params_b=c.params_b,
                model_quant=c.quant or "",
                model_tok_s=c.tok_s,
                model_is_local=1 if c.is_local else 0,
                smash_fit=fit,
                est_time_s=est_time,
                smash_measured=r.smash_measured,
                quality=r.quality,
                tests_passed=r.tests_passed,
                tests_total=r.tests_total,
                actual_time_s=r.elapsed_s,
                wall_time_s=wall,
                cost_usd=r.cost,
                tokens_in=r.tokens_in,
                tokens_out=r.tokens_out,
                energy_j=r.energy_j,
                error=r.error,
                generated_code=r.final_code or "",
            )

    store.finish_run(run_id)

    # ── Summary ──
    print(f"\n{'═' * 70}")
    print(f"  📊  BENCHMARK COMPLETE (run: {tag})")
    print(f"  ✅ {passed_count} passed  ❌ {failed_count} failed"
          f"  ⏭️ {skipped_cutoff} cutoff  💰 ${total_cost:.4f} total")
    if cached_count:
        print(f"  📦 {cached_count} cached (use --force to re-run)")
    print(f"{'═' * 70}")

    # ── Results table (from store — includes cached) ──
    all_results = store.all_results()
    # Filter to just the models and tasks we care about
    model_set = {c.name for c in selected}
    task_set = {t.id for t in tasks}
    relevant = [r for r in all_results if r["model"] in model_set and r["task_id"] in task_set]

    if relevant:
        print(f"\n  {'Model':25s} {'Task':20s} {'Diff':>4} {'Clar':>4} {'Fit':>5}"
              f" {'Q':>5} {'Time':>6} {'Cost':>8} {'Smash':>5}")
        print(f"  {'─' * 25} {'─' * 20} {'─' * 4} {'─' * 4} {'─' * 5}"
              f" {'─' * 5} {'─' * 6} {'─' * 8} {'─' * 5}")
        for r in sorted(relevant, key=lambda x: (x["task_difficulty"] or 0, x["model"])):
            q_icon = "✅" if r["quality"] >= 1.0 else "⚠️" if r["quality"] > 0 else "❌"
            print(f"  {r['model']:25s} {r['task_name'] or '':20s} {r['task_difficulty'] or 0:4d}"
                  f" {r['coord_clarity'] or 0:4d} {r['smash_fit'] or 0:5.2f}"
                  f" {q_icon}{r['quality']:4.0%}"
                  f" {r['actual_time_s'] or 0:5.1f}s ${r['cost_usd'] or 0:.5f}"
                  f" {r['smash_measured'] or 0:5d}")

    # ── Export ──
    if args.json:
        count = store.export_json(Path(args.json))
        print(f"\n  📁  Exported {count} results to {args.json}")


if __name__ == "__main__":
    main()


