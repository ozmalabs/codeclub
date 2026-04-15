#!/usr/bin/env python3
"""Batch TS/TSX probe for all cloud models — adaptive task selection."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Load env
_env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
if os.path.isfile(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from tournament import (
    TASKS, Contender, build_contenders, check_endpoints,
    fight_oneshot,
)
import probe as P

# TS tasks by difficulty tier
TS_EASY = ["ts-stack", "tsx-greeting"]          # d=22, 25
TS_MED  = ["tsx-toggle-button", "ts-event-emitter", "tsx-todo-list", "ts-result-type"]  # d=30-50
TS_HARD = ["tsx-data-table", "ts-observable", "ts-state-machine"]  # d=55-65
TS_EXTREME = ["ts-promise-pool", "tsx-virtual-list", "ts-async-queue", "ts-schema-validator"]  # d=70-80

# Rust tasks for models missing rust data
RUST_EASY = ["rust-hello-struct"]  # d=22
RUST_MED  = ["csv-parser"]        # d=45
RUST_HARD = ["rust-stack-calc"]    # d=55
RUST_EXT  = ["rust-json-parser"]   # d=85


def pick_ts_tasks(py_boundary: int) -> list[str]:
    """Pick 3-4 TS tasks based on known Python boundary."""
    tasks = []
    # Always include an easy one
    tasks.append("ts-stack")  # d=22

    if py_boundary <= 50:
        # Weaker model: easy + medium
        tasks.append("tsx-toggle-button")  # d=30
        tasks.append("ts-result-type")     # d=50
    elif py_boundary <= 65:
        # Mid-tier: medium + hard
        tasks.append("ts-result-type")     # d=50
        tasks.append("ts-observable")      # d=65
    else:
        # Strong: medium + hard + extreme
        tasks.append("ts-result-type")     # d=50
        tasks.append("ts-observable")      # d=65
        tasks.append("ts-schema-validator") # d=80

    # Add one TSX task if not already included
    tsx_in = any(t.startswith("tsx-") for t in tasks)
    if not tsx_in:
        if py_boundary <= 50:
            tasks.append("tsx-greeting")       # d=25
        elif py_boundary <= 65:
            tasks.append("tsx-todo-list")      # d=45
        else:
            tasks.append("tsx-data-table")     # d=55

    return tasks


def pick_rust_tasks(py_boundary: int) -> list[str]:
    """Pick 2-3 Rust tasks for models missing rust data."""
    tasks = ["rust-hello-struct"]  # always easy
    if py_boundary <= 50:
        tasks.append("csv-parser")         # d=45
    elif py_boundary <= 65:
        tasks.append("csv-parser")         # d=45
        tasks.append("rust-stack-calc")    # d=55
    else:
        tasks.append("rust-stack-calc")    # d=55
        tasks.append("rust-json-parser")   # d=85
    return tasks


def run_batch(models_filter: list[str] | None = None, dry_run: bool = False,
              skip_local: bool = True, include_rust: bool = True):
    all_contenders = build_contenders()
    if skip_local:
        all_contenders = [c for c in all_contenders if not c.is_local]

    if models_filter:
        all_contenders = [c for c in all_contenders
                         if any(f.lower() in c.name.lower() for f in models_filter)]

    print(f"\n📡  Checking {len(all_contenders)} endpoints...")
    contenders = check_endpoints(all_contenders)
    print(f"   {len(contenders)} reachable\n")

    total_fights = 0

    for c in sorted(contenders, key=lambda x: x.name):
        mk = P.build_knowledge(c)
        # Use max_pass directly — midpoint gets dragged down by low-clarity failures
        py_data = mk.boundaries.get("python")
        if py_data and py_data[0] > 0:
            py_b = py_data[0]  # max passing difficulty
        else:
            py_b = mk.python_boundary or int(c.smash.high * 0.8)

        # Determine what's needed
        needs_ts = "typescript" not in mk.boundaries or mk.boundaries["typescript"][2] < 2
        needs_rust = include_rust and ("rust" not in mk.boundaries or mk.boundaries["rust"][2] < 1)

        if not needs_ts and not needs_rust:
            continue

        print(f"\n{'═' * 65}")
        print(f"  {c.club} {c.name}  (py boundary ≈{py_b}d)")
        print(f"{'═' * 65}")

        probes: list[tuple[str, str]] = []  # (task_id, reason)

        if needs_ts:
            ts_tasks = pick_ts_tasks(py_b)
            for tid in ts_tasks:
                coord = TASKS[tid].coord_for("oneshot")
                probes.append((tid, f"TS d={coord.difficulty}"))

        if needs_rust:
            rust_tasks = pick_rust_tasks(py_b)
            for tid in rust_tasks:
                coord = TASKS[tid].coord_for("oneshot")
                probes.append((tid, f"Rust d={coord.difficulty}"))

        print(f"  📋  {len(probes)} probes planned:")
        for tid, reason in probes:
            coord = TASKS[tid].coord_for("oneshot")
            print(f"      {tid:<25} d={coord.difficulty:<4} c={coord.clarity:<4} [{reason}]")

        if dry_run:
            total_fights += len(probes)
            continue

        for tid, reason in probes:
            task = TASKS[tid]
            coord = task.coord_for("oneshot")
            print(f"\n  ⚔️  {tid} (d={coord.difficulty}, c={coord.clarity}) [{reason}]")

            r = fight_oneshot(c, task)
            total_fights += 1

            icon = "🏆" if r.quality >= 1.0 else "💀"
            tok = f"  📝{r.tokens_out}tok" if r.tokens_out else ""
            print(f"     {icon} {int(r.quality*100)}% ({r.tests_passed}/{r.tests_total})"
                  f"  ⏱{r.elapsed_s:.1f}s  💰${r.cost:.6f}{tok}")

            if r.quality < 1.0 and r.test_details:
                failed = [n for n, p, _ in r.test_details if not p]
                if failed:
                    print(f"     ❌ Failed: {', '.join(failed[:3])}")

            P.save_probe_result(c, task, r)

    action = "planned" if dry_run else "run"
    print(f"\n{'═' * 65}")
    print(f"  ✅  BATCH COMPLETE — {total_fights} fights {action}")
    print(f"{'═' * 65}\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="append", help="Filter to specific model(s)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-local", action="store_true")
    parser.add_argument("--no-rust", action="store_true")
    args = parser.parse_args()

    run_batch(
        models_filter=args.model,
        dry_run=args.dry_run,
        skip_local=not args.include_local,
        include_rust=not args.no_rust,
    )
