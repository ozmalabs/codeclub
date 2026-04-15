#!/usr/bin/env python3
"""
🏏  ADAPTIVE PROBE RUNNER  🏏

Minimum fights to calibrate a model's Efficiency Map.

Three dimensions:
  1. Boundary — pass/fail difficulty ceiling (binary search)
  2. Language proficiency — ratio of lang boundary to Python boundary
  3. Token efficiency shape — how tokens_out scales with difficulty

Usage:
    python probe.py --model gpt-5.4-nano                    # full probe
    python probe.py --model gpt-5.4-nano --lang typescript   # lang delta only
    python probe.py --model qwen3-coder:30b --quant q4_k_m  # quant delta
    python probe.py --status                                 # show all models
    python probe.py --model phi-4 --dry-run                  # plan without fighting
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field

from tournament import (
    TASKS, TournamentTask, SmashRange, SmashCoord,
    Contender, FightResult,
    build_contenders, check_endpoints,
    fight_oneshot, estimate_smash_range, estimate_lang_proficiency,
    MEASURED_LANG_PROFICIENCY,
)


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

DB_PATH = os.path.join(os.path.dirname(__file__), "benchmarks", "results.db")


def _db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def get_existing_results(model: str) -> list[dict]:
    """Pull all existing results for a model from the DB."""
    conn = _db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM results WHERE model = ? ORDER BY coord_difficulty",
        (model,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_probe_result(
    contender: Contender, task: TournamentTask, result: FightResult,
    run_tag: str = "probe",
) -> None:
    """Save a probe fight result to the DB (same schema as tournament)."""
    conn = _db()

    # Get or create run
    run = conn.execute(
        "SELECT id FROM runs WHERE run_tag = ? ORDER BY id DESC LIMIT 1",
        (run_tag,),
    ).fetchone()
    if run:
        run_id = run[0]
    else:
        cur = conn.execute(
            "INSERT INTO runs (run_tag, started_at, notes) VALUES (?, ?, ?)",
            (run_tag, time.time(), "adaptive probe"),
        )
        run_id = cur.lastrowid

    coord = task.coord_for("oneshot")

    conn.execute("""
        INSERT OR REPLACE INTO results (
            run_id, model, task_id, task_hash,
            task_name, task_lang, task_difficulty,
            coord_difficulty, coord_clarity,
            model_params_b, model_quant, model_tok_s, model_is_local,
            smash_fit, est_time_s, smash_measured,
            quality, tests_passed, tests_total,
            actual_time_s, wall_time_s, cost_usd,
            tokens_in, tokens_out, energy_j,
            error, generated_code, created_at
        ) VALUES (
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?
        )
    """, (
        run_id, contender.name, task.id, task.content_hash,
        task.name, task.lang, task.base_difficulty,
        coord.difficulty, coord.clarity,
        contender.params_b, contender.quant, contender.tok_s,
        1 if contender.is_local else 0,
        round(result.smash_fit, 4), contender.time_for(coord),
        result.smash_measured,
        result.quality, result.tests_passed, result.tests_total,
        result.elapsed_s, result.elapsed_s, result.cost,
        result.tokens_in, result.tokens_out, result.energy_j,
        result.error or "", result.final_code or "", time.time(),
    ))
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# TASK SELECTION
# ═══════════════════════════════════════════════════════════════════════════════

def tasks_by_lang() -> dict[str, list[tuple[int, TournamentTask]]]:
    """Group tasks by language, sorted by oneshot difficulty. High-clarity only."""
    by_lang: dict[str, list[tuple[int, TournamentTask]]] = {}
    for task in TASKS.values():
        coord = task.coord_for("oneshot")
        # Only use high-clarity tasks for boundary probing (clarity ≥ 60)
        # Low clarity adds noise — we're measuring capability, not ambiguity tolerance
        if coord.clarity < 60:
            continue
        lang = task.lang
        by_lang.setdefault(lang, []).append((coord.difficulty, task))
    for lang in by_lang:
        by_lang[lang].sort(key=lambda x: x[0])
    return by_lang


def pick_task_near(lang: str, target_difficulty: int,
                   exclude: set[str] | None = None) -> TournamentTask | None:
    """Find a high-clarity task in `lang` closest to `target_difficulty`."""
    pool = tasks_by_lang().get(lang, [])
    if exclude:
        pool = [(d, t) for d, t in pool if t.id not in exclude]
    if not pool:
        return None
    return min(pool, key=lambda x: abs(x[0] - target_difficulty))[1]


# ═══════════════════════════════════════════════════════════════════════════════
# MODEL KNOWLEDGE — what do we already know?
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ModelKnowledge:
    """What we know about a model from existing data + estimation."""
    name: str
    contender: Contender

    # Per-lang: (max_pass_difficulty, min_fail_difficulty, fight_count)
    boundaries: dict[str, tuple[int, int, int]] = field(default_factory=dict)

    # Per-lang: list of (difficulty, tokens_out) for efficiency shape
    token_points: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    @property
    def has_python_boundary(self) -> bool:
        b = self.boundaries.get("python")
        return b is not None and b[2] >= 2  # at least 2 fights

    @property
    def python_boundary(self) -> int | None:
        """Best estimate of Python pass/fail boundary."""
        b = self.boundaries.get("python")
        if not b:
            return None
        max_pass, min_fail, _ = b
        if max_pass > 0 and min_fail < 999:
            return (max_pass + min_fail) // 2
        if max_pass > 0:
            return max_pass + 5  # passes everything we've tested
        return None

    def lang_proficiency(self, lang: str) -> float | None:
        """Measured proficiency ratio (lang boundary / python boundary)."""
        py_b = self.python_boundary
        if not py_b:
            return None
        b = self.boundaries.get(lang)
        if not b or b[2] < 1:
            return None
        max_pass, min_fail, _ = b
        if max_pass == 0:
            return 0.0
        lang_b = (max_pass + min(min_fail, max_pass + 10)) // 2
        return min(1.5, lang_b / py_b)

    def token_efficiency(self, lang: str = "python") -> tuple[float, float] | None:
        """Fit tokens = base × (d/d_min)^exp. Returns (base_tokens, scaling_exp)."""
        points = self.token_points.get(lang, [])
        if len(points) < 3:
            return None
        # Log-log least squares: log(tok) = log(base) + exp * log(d/d_min)
        d_min = min(d for d, _ in points)
        if d_min <= 0:
            return None
        xs = [math.log(d / d_min) for d, _ in points if d > 0]
        ys = [math.log(t) for _, t in points if t > 0]
        if len(xs) < 3 or not any(x > 0 for x in xs):
            return None
        # Filter to points where x > 0 (can't fit on the anchor point alone)
        pairs = [(x, y) for x, y in zip(xs, ys) if x > 0]
        if len(pairs) < 2:
            # Not enough spread — report flat
            avg_tok = sum(t for _, t in points) / len(points)
            return (avg_tok, 0.3)
        n = len(pairs)
        sx = sum(x for x, _ in pairs)
        sy = sum(y for _, y in pairs)
        sxx = sum(x * x for x, _ in pairs)
        sxy = sum(x * y for x, y in pairs)
        denom = n * sxx - sx * sx
        if abs(denom) < 1e-9:
            avg_tok = sum(t for _, t in points) / len(points)
            return (avg_tok, 0.3)
        exp = (n * sxy - sx * sy) / denom
        log_base = (sy - exp * sx) / n
        # base_tokens is at d_min
        base = math.exp(log_base - exp * math.log(1))  # at x=0, d=d_min
        # Clamp to reasonable range
        exp = max(0.0, min(4.0, exp))
        return (d_min, base, exp)  # type: ignore[return-value]


def build_knowledge(contender: Contender) -> ModelKnowledge:
    """Build knowledge from existing DB results."""
    mk = ModelKnowledge(name=contender.name, contender=contender)
    results = get_existing_results(contender.name)

    for r in results:
        lang = r["task_lang"]
        d = r["coord_difficulty"]
        q = r["quality"]
        tok = r["tokens_out"]

        # Boundary tracking
        if lang not in mk.boundaries:
            mk.boundaries[lang] = (0, 999, 0)
        max_pass, min_fail, count = mk.boundaries[lang]
        if q >= 1.0:
            max_pass = max(max_pass, d)
        else:
            min_fail = min(min_fail, d)
        mk.boundaries[lang] = (max_pass, min_fail, count + 1)

        # Token efficiency points (only from passing fights)
        if q >= 1.0 and tok and tok > 0:
            mk.token_points.setdefault(lang, []).append((d, tok))

    return mk


# ═══════════════════════════════════════════════════════════════════════════════
# PROBE STRATEGIES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ProbeStep:
    """A planned probe fight."""
    task: TournamentTask
    reason: str
    target_difficulty: int

    def __str__(self) -> str:
        coord = self.task.coord_for("oneshot")
        return (f"  {self.task.id:<25} d={coord.difficulty:<4} "
                f"c={coord.clarity:<4} [{self.reason}]")


def plan_boundary_probe(
    mk: ModelKnowledge, lang: str = "python",
) -> list[ProbeStep]:
    """Plan fights to find the pass/fail boundary for a language."""
    steps: list[ProbeStep] = []
    used: set[str] = set()

    # What do we already know?
    b = mk.boundaries.get(lang)
    if b and b[2] >= 2:
        max_pass, min_fail, count = b
        gap = min_fail - max_pass if min_fail < 999 else 20
        if gap <= 10:
            # Already well-bracketed
            return []

    # Estimate where the boundary should be
    if mk.has_python_boundary and lang != "python":
        # Delta probe: use Python boundary + estimated proficiency
        py_b = mk.python_boundary
        est_prof = mk.contender.lang_proficiency.get(
            lang, mk.contender.lang_proficiency.get("default", 0.7)
        )
        est_boundary = int(py_b * est_prof)
        # Probe near the estimated boundary
        t1 = pick_task_near(lang, est_boundary, used)
        if t1:
            steps.append(ProbeStep(t1, f"lang delta near py×{est_prof:.2f}={est_boundary}d", est_boundary))
            used.add(t1.id)

        # Second probe: bracket in the direction of uncertainty
        if b and b[0] > 0:  # have some data
            max_pass = b[0]
            t2 = pick_task_near(lang, max_pass + 15, used)
        else:
            t2 = pick_task_near(lang, int(est_boundary * 1.2), used)
        if t2 and t2.id not in used:
            steps.append(ProbeStep(t2, "bracket upper", int(est_boundary * 1.2)))
            used.add(t2.id)
    else:
        # Full boundary search from scratch
        est = mk.contender.smash
        # Start at sweet spot
        t1 = pick_task_near(lang, est.sweet, used)
        if t1:
            steps.append(ProbeStep(t1, f"initial probe at sweet={est.sweet}d", est.sweet))
            used.add(t1.id)

        # Easy sanity check — should pass
        t_easy = pick_task_near(lang, est.low + 10, used)
        if t_easy and t_easy.id not in used:
            steps.append(ProbeStep(t_easy, "easy sanity check", est.low + 10))
            used.add(t_easy.id)

        # Hard probe — near estimated high
        t_hard = pick_task_near(lang, est.high - 5, used)
        if t_hard and t_hard.id not in used:
            steps.append(ProbeStep(t_hard, f"hard probe near high={est.high}d", est.high - 5))
            used.add(t_hard.id)

    return steps


def plan_adaptive_followup(
    mk: ModelKnowledge, lang: str, last_result: FightResult,
    used: set[str],
) -> ProbeStep | None:
    """Given a probe result, decide if we need one more fight to bracket."""
    b = mk.boundaries.get(lang)
    if not b:
        return None
    max_pass, min_fail, count = b
    gap = min_fail - max_pass if min_fail < 999 else 30

    if gap <= 10:
        return None  # well-bracketed

    if last_result.quality >= 1.0:
        # Passed — try harder to find the ceiling
        target = last_result.task_id
        coord = TASKS[target].coord_for("oneshot")
        next_d = coord.difficulty + int(gap * 0.6)
        t = pick_task_near(lang, next_d, used)
        if t:
            return ProbeStep(t, f"passed d={coord.difficulty}, pushing to {next_d}", next_d)
    else:
        # Failed — try easier to find the floor
        target = last_result.task_id
        coord = TASKS[target].coord_for("oneshot")
        next_d = coord.difficulty - int(gap * 0.4)
        t = pick_task_near(lang, next_d, used)
        if t:
            return ProbeStep(t, f"failed d={coord.difficulty}, dropping to {next_d}", next_d)

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN EFFICIENCY SHAPE ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def describe_token_shape(mk: ModelKnowledge, lang: str = "python") -> str:
    """Human-readable description of the token efficiency shape."""
    points = mk.token_points.get(lang, [])
    if len(points) < 2:
        return "insufficient data"

    toks = [t for _, t in points]
    ratio = max(toks) / max(min(toks), 1)

    if ratio < 3.0:
        shape = "flat"
        desc = "consistent token usage — cost scales predictably"
    elif ratio < 8.0:
        shape = "linear"
        desc = "moderate scaling — tokens grow with difficulty"
    else:
        shape = "super-linear"
        desc = "reasoning overhead dominates at high difficulty"

    eff = mk.token_efficiency(lang)
    if eff and len(eff) == 3:
        _, base, exp = eff
        return f"{shape} (base≈{base:.0f}tok, exp={exp:.2f}) — {desc}"

    return f"{shape} ({ratio:.1f}x range) — {desc}"


# ═══════════════════════════════════════════════════════════════════════════════
# PROBE RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

def run_probe(
    contender: Contender,
    langs: list[str] | None = None,
    dry_run: bool = False,
    max_fights: int = 12,
) -> ModelKnowledge:
    """
    Adaptive probe: minimum fights to map a model's efficiency surface.

    Returns updated ModelKnowledge after probing.
    """
    mk = build_knowledge(contender)

    if langs is None:
        # Default: Python first (baseline), then other available langs
        available_langs = sorted({t.lang for t in TASKS.values()})
        langs = ["python"] + [l for l in available_langs if l != "python"]

    fights_run = 0
    used_tasks: set[str] = set()

    for lang in langs:
        if fights_run >= max_fights:
            print(f"\n  ⚠️  Hit fight budget ({max_fights}), stopping")
            break

        pool = tasks_by_lang().get(lang, [])
        if not pool:
            print(f"\n  ⚠️  No high-clarity tasks for {lang}, skipping")
            continue

        print(f"\n{'─' * 60}")
        print(f"  🏏  Probing {contender.name} × {lang}")

        # What do we know?
        b = mk.boundaries.get(lang)
        if b:
            max_pass, min_fail, count = b
            gap = min_fail - max_pass if min_fail < 999 else "∞"
            print(f"  📊  Existing: {count} fights, pass≤{max_pass}d, fail≥{min_fail if min_fail < 999 else '?'}d (gap={gap})")
        else:
            print(f"  📊  No existing data")

        # Plan probes
        steps = plan_boundary_probe(mk, lang)
        if not steps:
            tok_desc = describe_token_shape(mk, lang)
            print(f"  ✅  Boundary well-bracketed, no probes needed")
            print(f"  📈  Token shape: {tok_desc}")
            continue

        print(f"  📋  Plan: {len(steps)} probe(s)")
        for s in steps:
            print(s)

        if dry_run:
            continue

        # Execute probes
        for step in steps:
            if fights_run >= max_fights:
                break

            coord = step.task.coord_for("oneshot")
            print(f"\n  ⚔️  {step.task.id} (d={coord.difficulty}, c={coord.clarity})")
            result = fight_oneshot(contender, step.task)
            fights_run += 1
            used_tasks.add(step.task.id)

            # Update knowledge
            lang_key = step.task.lang
            if lang_key not in mk.boundaries:
                mk.boundaries[lang_key] = (0, 999, 0)
            max_pass, min_fail, count = mk.boundaries[lang_key]
            if result.quality >= 1.0:
                max_pass = max(max_pass, coord.difficulty)
            else:
                min_fail = min(min_fail, coord.difficulty)
            mk.boundaries[lang_key] = (max_pass, min_fail, count + 1)

            if result.quality >= 1.0 and result.tokens_out and result.tokens_out > 0:
                mk.token_points.setdefault(lang_key, []).append(
                    (coord.difficulty, result.tokens_out)
                )

            # Print result
            icon = "🏆" if result.quality >= 1.0 else "💀"
            tok_str = f"  📝{result.tokens_out}tok" if result.tokens_out else ""
            print(f"     {icon} {int(result.quality * 100)}% ({result.tests_passed}/{result.tests_total})"
                  f"  ⏱{result.elapsed_s:.1f}s  💰${result.cost:.6f}{tok_str}")

            if result.quality < 1.0 and result.test_details:
                failed = [n for n, p, _ in result.test_details if not p]
                if failed:
                    print(f"     ❌ Failed: {', '.join(failed[:3])}")

            # Save to DB
            save_probe_result(contender, step.task, result)

            # Adaptive: do we need another fight?
            followup = plan_adaptive_followup(mk, lang_key, result, used_tasks)
            if followup and fights_run < max_fights:
                print(f"\n  🔄  Adaptive: {followup.reason}")
                fcoord = followup.task.coord_for("oneshot")
                print(f"  ⚔️  {followup.task.id} (d={fcoord.difficulty}, c={fcoord.clarity})")
                f_result = fight_oneshot(contender, followup.task)
                fights_run += 1
                used_tasks.add(followup.task.id)

                # Update knowledge again
                if lang_key not in mk.boundaries:
                    mk.boundaries[lang_key] = (0, 999, 0)
                mp, mf, ct = mk.boundaries[lang_key]
                if f_result.quality >= 1.0:
                    mp = max(mp, fcoord.difficulty)
                else:
                    mf = min(mf, fcoord.difficulty)
                mk.boundaries[lang_key] = (mp, mf, ct + 1)

                if f_result.quality >= 1.0 and f_result.tokens_out and f_result.tokens_out > 0:
                    mk.token_points.setdefault(lang_key, []).append(
                        (fcoord.difficulty, f_result.tokens_out)
                    )

                icon = "🏆" if f_result.quality >= 1.0 else "💀"
                tok_str = f"  📝{f_result.tokens_out}tok" if f_result.tokens_out else ""
                print(f"     {icon} {int(f_result.quality * 100)}% ({f_result.tests_passed}/{f_result.tests_total})"
                      f"  ⏱{f_result.elapsed_s:.1f}s  💰${f_result.cost:.6f}{tok_str}")

                save_probe_result(contender, followup.task, f_result)

    # Final summary
    print(f"\n{'═' * 60}")
    print(f"  🏏  PROBE COMPLETE: {contender.name}")
    print(f"  {fights_run} fights run")
    print(f"{'═' * 60}")

    for lang in langs:
        b = mk.boundaries.get(lang)
        if not b:
            continue
        max_pass, min_fail, count = b
        gap = min_fail - max_pass if min_fail < 999 else "∞"
        prof = mk.lang_proficiency(lang)
        prof_str = f"  prof={prof:.2f}" if prof is not None else ""
        tok_desc = describe_token_shape(mk, lang)
        print(f"  {lang:<12} boundary=[{max_pass}–{min_fail if min_fail < 999 else '?'}] "
              f"gap={gap}  n={count}{prof_str}")
        print(f"               tokens: {tok_desc}")

    return mk


# ═══════════════════════════════════════════════════════════════════════════════
# STATUS — show what we know about all models
# ═══════════════════════════════════════════════════════════════════════════════

def show_status() -> None:
    """Show current knowledge state for all models."""
    contenders = build_contenders()

    print("\n🏏  MODEL KNOWLEDGE STATUS\n")
    print(f"  {'Model':<25} {'Python':<18} {'Rust':<18} {'TS':<18} {'Token Shape':<30}")
    print(f"  {'─' * 25} {'─' * 18} {'─' * 18} {'─' * 18} {'─' * 30}")

    for c in sorted(contenders, key=lambda c: c.name):
        mk = build_knowledge(c)

        cols = []
        for lang in ("python", "rust", "typescript"):
            b = mk.boundaries.get(lang)
            if b and b[2] > 0:
                max_pass, min_fail, count = b
                fail_str = str(min_fail) if min_fail < 999 else "?"
                prof = mk.lang_proficiency(lang)
                prof_str = f" ×{prof:.2f}" if prof is not None else ""
                cols.append(f"[{max_pass}–{fail_str}] n={count}{prof_str}")
            else:
                # Estimated
                est = c.lang_proficiency.get(lang, c.lang_proficiency.get("default", 0.7))
                cols.append(f"est ×{est:.2f}")

        tok_desc = describe_token_shape(mk, "python")
        print(f"  {c.name:<25} {cols[0]:<18} {cols[1]:<18} {cols[2]:<18} {tok_desc}")

    # Summary
    conn = _db()
    total = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    models = conn.execute("SELECT COUNT(DISTINCT model) FROM results").fetchone()[0]
    conn.close()
    print(f"\n  Total: {total} fights across {models} models")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    # Load .env
    _env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.isfile(_env_path):
        with open(_env_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if "=" in _line and not _line.startswith("#"):
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())

    parser = argparse.ArgumentParser(
        description="🏏 Adaptive Probe — minimum fights to calibrate a model",
    )
    parser.add_argument("--model", help="Model name to probe (e.g. gpt-5.4-nano)")
    parser.add_argument(
        "--lang", action="append",
        help="Language(s) to probe (repeatable; default: all)",
    )
    parser.add_argument("--cloud", action="store_true", help="Skip local models")
    parser.add_argument("--dry-run", action="store_true", help="Plan probes without fighting")
    parser.add_argument("--max-fights", type=int, default=12, help="Max fights per probe run")
    parser.add_argument("--status", action="store_true", help="Show knowledge status for all models")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if not args.model:
        parser.error("--model is required (or use --status)")

    # Find the contender
    all_contenders = build_contenders()
    if args.cloud:
        all_contenders = [c for c in all_contenders if not c.is_local]

    match = [c for c in all_contenders if c.name == args.model]
    if not match:
        # Fuzzy match
        match = [c for c in all_contenders if args.model.lower() in c.name.lower()]
    if not match:
        print(f"  ❌  Unknown model: {args.model}")
        print(f"  Available: {', '.join(c.name for c in all_contenders)}")
        sys.exit(1)

    contender = match[0]

    # Health check
    print(f"\n📡  Checking {contender.name}...")
    available = check_endpoints([contender])
    if not available:
        print(f"  ❌  {contender.name} is unreachable")
        sys.exit(1)
    contender = available[0]
    print(f"  ✅  {contender.club} {contender.name} — {contender.tag()}")

    run_probe(contender, langs=args.lang, dry_run=args.dry_run,
              max_fights=args.max_fights)


if __name__ == "__main__":
    main()
