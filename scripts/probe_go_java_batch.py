#!/usr/bin/env python3
"""Batch Go/Java probe for all cloud models — adaptive task selection."""
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

# Go tasks by difficulty tier
GO_EASY = ["go-stack"]                # d=15
GO_MED  = ["go-rate-limiter", "go-lru-cache"]  # d=30, 40
GO_HARD = ["go-json-tokenizer", "go-chan-pipeline"]  # d=55, 65
GO_EXT  = ["go-json-parser", "go-worker-pool"]  # d=70, 80

# Java tasks by difficulty tier
JAVA_EASY = ["java-stack"]            # d=15
JAVA_MED  = ["java-rate-limiter", "java-lru-cache", "java-event-emitter"]  # d=30, 40, 45
JAVA_HARD = ["java-expr-parser", "java-json-parser"]  # d=60, 70
JAVA_EXT  = ["java-thread-pool"]      # d=80


def pick_go_tasks(py_boundary: int) -> list[str]:
    """Pick 3-4 Go tasks based on known Python boundary."""
    tasks = ["go-stack"]  # always easy

    if py_boundary <= 50:
        tasks.append("go-rate-limiter")      # d=30
        tasks.append("go-lru-cache")         # d=40
    elif py_boundary <= 65:
        tasks.append("go-lru-cache")         # d=40
        tasks.append("go-json-tokenizer")    # d=55
    else:
        tasks.append("go-json-tokenizer")    # d=55
        tasks.append("go-chan-pipeline")      # d=65
        tasks.append("go-worker-pool")       # d=80

    return tasks


def pick_java_tasks(py_boundary: int) -> list[str]:
    """Pick 3-4 Java tasks based on known Python boundary."""
    tasks = ["java-stack"]  # always easy

    if py_boundary <= 50:
        tasks.append("java-rate-limiter")    # d=30
        tasks.append("java-lru-cache")       # d=40
    elif py_boundary <= 65:
        tasks.append("java-lru-cache")       # d=40
        tasks.append("java-expr-parser")     # d=60
    else:
        tasks.append("java-expr-parser")     # d=60
        tasks.append("java-json-parser")     # d=70
        tasks.append("java-thread-pool")     # d=80

    return tasks


def run_batch(models_filter: list[str] | None = None, dry_run: bool = False,
              skip_local: bool = True, langs: list[str] | None = None):
    if langs is None:
        langs = ["go", "java"]

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
        py_data = mk.boundaries.get("python")
        if py_data and py_data[0] > 0:
            py_b = py_data[0]
        else:
            py_b = mk.python_boundary or int(c.smash.high * 0.8)

        probes: list[tuple[str, str]] = []

        if "go" in langs:
            go_tasks = pick_go_tasks(py_b)
            for tid in go_tasks:
                coord = TASKS[tid].coord_for("oneshot")
                probes.append((tid, f"Go d={coord.difficulty}"))

        if "java" in langs:
            java_tasks = pick_java_tasks(py_b)
            for tid in java_tasks:
                coord = TASKS[tid].coord_for("oneshot")
                probes.append((tid, f"Java d={coord.difficulty}"))

        if not probes:
            continue

        print(f"\n{'═' * 65}")
        print(f"  {c.club} {c.name}  (py boundary ≈{py_b}d)")
        print(f"{'═' * 65}")
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
    parser.add_argument("--lang", action="append", choices=["go", "java"],
                       help="Languages to probe (default: both)")
    args = parser.parse_args()

    run_batch(
        models_filter=args.model,
        dry_run=args.dry_run,
        skip_local=not args.include_local,
        langs=args.lang,
    )
