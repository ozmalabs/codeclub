#!/usr/bin/env python3
"""Quick C# probe to validate mapping to Java boundary."""
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
    fight_oneshot, pick_probe_tasks,
)
import probe as P


def pick_csharp_tasks(py_boundary: int) -> list[str]:
    """Pick 3-4 C# tasks based on known Python boundary."""
    tasks = ["csharp-stack"]  # always easy

    if py_boundary <= 50:
        tasks.append("csharp-lru-cache")       # d=40
    elif py_boundary <= 65:
        tasks.append("csharp-lru-cache")       # d=40
        tasks.append("csharp-expr-parser")     # d=60
    else:
        tasks.append("csharp-expr-parser")     # d=60
        tasks.append("csharp-json-parser")     # d=70
        tasks.append("csharp-thread-pool")     # d=80

    return tasks


def run_batch(models_filter: list[str] | None = None, dry_run: bool = False,
              skip_local: bool = True):
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

        cs_tasks = pick_csharp_tasks(py_b)
        probes = [(tid, f"C# d={TASKS[tid].coord_for('oneshot').difficulty}") for tid in cs_tasks]

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
            result = fight_oneshot(c, task)
            total_fights += 1
            emoji = "🏆" if result.quality == 1.0 else "💀"
            print(f"     {emoji} {result.quality:.0%} ({result.tests_passed}/{result.tests_total})"
                  f"  ⏱{result.elapsed_s:.1f}s  💰${result.cost:.6f}  📝{result.tokens_out}tok")
            if result.error:
                print(f"     ⚠️  {result.error[:120]}")
            if result.quality < 1.0:
                failed = [name for name, ok, _ in result.test_details if not ok]
                if failed:
                    print(f"     ❌ Failed: {', '.join(failed[:3])}")

            P.save_probe_result(c, task, result)

    print(f"\n{'═' * 65}")
    print(f"  ✅  BATCH COMPLETE — {total_fights} fights run")
    print(f"{'═' * 65}\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="C# boundary probe")
    parser.add_argument("--model", nargs="*", help="Filter models by name substring")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-local", action="store_true")
    args = parser.parse_args()

    run_batch(
        models_filter=args.model,
        dry_run=args.dry_run,
        skip_local=not args.include_local,
    )
