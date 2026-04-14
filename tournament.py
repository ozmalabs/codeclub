#!/usr/bin/env python3
"""
🏔️  CAVEMAN MODEL TOURNAMENT  🏔️

Models fight with clubs. Best caveman wins.

Quality is a HARD GATE — 100% test pass rate required.
Among champions, rank by configurable fitness:
  cost · speed · power · locality

When a metric is "solved" (all candidates tie),
its weight redistributes to the remaining metrics.

Usage:
    python tournament.py                      # balanced fitness
    python tournament.py --optimize fastest   # speed priority
    python tournament.py --optimize greenest  # power priority
    python tournament.py --optimize cheapest  # cost priority
    python tournament.py --task rate-limiter  # single task
    python tournament.py --quick              # stop at first champion
    python tournament.py --json results.json  # export JSON
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
import textwrap
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import requests


# ═══════════════════════════════════════════════════════════════════════════════
# LANGUAGE RUNNERS — pluggable execution engines
# ═══════════════════════════════════════════════════════════════════════════════

@runtime_checkable
class LanguageRunner(Protocol):
    """Pluggable language execution for tournament tests."""

    @property
    def lang(self) -> str: ...

    def check_syntax(self, code: str) -> tuple[bool, str]:
        """Return (ok, error_msg)."""
        ...

    def run_test(self, impl_code: str, test_code: str) -> tuple[bool, str]:
        """Execute one test against impl code.  Return (passed, error_msg)."""
        ...


class PythonRunner:
    """Execute Python code via compile() + exec() with stdout suppression."""

    lang = "python"

    def check_syntax(self, code: str) -> tuple[bool, str]:
        try:
            compile(code, "<tournament>", "exec")
            return True, ""
        except SyntaxError as e:
            return False, str(e)

    def run_test(self, impl_code: str, test_code: str) -> tuple[bool, str]:
        try:
            ns: dict = {}
            with contextlib.redirect_stdout(io.StringIO()):
                exec(compile(impl_code, "<impl>", "exec"), ns)
                exec(compile(test_code, "<test>", "exec"), ns)
            return True, ""
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


class RustRunner:
    """Compile and run Rust code via rustc subprocess."""

    lang = "rust"

    def check_syntax(self, code: str) -> tuple[bool, str]:
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(suffix=".rs", mode="w", delete=False) as f:
            f.write(code)
            f.flush()
            out_path = f.name + ".check"
        try:
            r = subprocess.run(
                ["rustc", "--edition", "2021", "--crate-type", "lib",
                 "-A", "warnings", f.name, "-o", out_path],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                return True, ""
            return False, r.stderr[:500]
        except Exception as e:
            return False, str(e)
        finally:
            for p in (f.name, out_path):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

    def run_test(self, impl_code: str, test_code: str) -> tuple[bool, str]:
        import subprocess, tempfile
        full = impl_code + "\n" + test_code
        with tempfile.NamedTemporaryFile(suffix=".rs", mode="w", delete=False) as f:
            f.write(full)
            f.flush()
            src = f.name
            binary = f.name + ".bin"
        try:
            comp = subprocess.run(
                ["rustc", "--edition", "2021", "-A", "warnings",
                 src, "-o", binary],
                capture_output=True, text=True, timeout=30,
            )
            if comp.returncode != 0:
                return False, comp.stderr[:500]
            run = subprocess.run(
                [binary], capture_output=True, text=True, timeout=10,
            )
            if run.returncode != 0:
                err = run.stderr[:500] or f"exit code {run.returncode}"
                return False, err
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "timeout"
        except Exception as e:
            return False, str(e)
        finally:
            for p in (src, binary):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass


RUNNERS: dict[str, LanguageRunner] = {
    "python": PythonRunner(),
    "rust": RustRunner(),
}


# ═══════════════════════════════════════════════════════════════════════════════
# CLUB SMASH — universal model×task routing on two axes
# ═══════════════════════════════════════════════════════════════════════════════
#
# Every task×role maps to a point on the difficulty × clarity plane.
# Every model covers a region.  Routing = geometry: does the task point
# fall inside the model's region?
#
#   DIFFICULTY (0–100) — how hard is the logic/algorithm?
#     0  ─── barely writes a word
#    20  ─── simple getters/setters, basic CRUD
#    40  ─── standard algorithms, pattern matching
#    60  ─── state machines, recursive descent, tricky edge cases
#    80  ─── complex architecture, multi-file coordination
#   100  ─── whole-project generation from a single prompt
#
#   CLARITY (0–100) — how well-specified is the input?
#   100  ─── skeleton with exact signatures and types
#    80  ─── precise spec with examples and edge cases listed
#    60  ─── good description but model must infer some design
#    40  ─── vague requirements, multiple valid interpretations
#    20  ─── "make it work" with minimal context
#
# Roles are just reference points on this plane — not special code paths.
# New roles (compress, test-write, document) are just new coordinates.
#

@dataclass
class SmashCoord:
    """A point on the difficulty × clarity plane."""
    difficulty: int
    clarity: int

    def __repr__(self) -> str:
        return f"({self.difficulty}d, {self.clarity}c)"


# Role defaults: how each role shifts a task's base difficulty,
# and what clarity level the role's input typically provides.
#
# diff_offset: fill is easier (skeleton helps), oneshot is harder (no guidance)
# clarity:     fill gets a skeleton (very clear), oneshot gets a spec (less clear)
ROLE_DEFAULTS: dict[str, dict] = {
    "fill":      {"diff_offset": -10, "clarity": 90},
    "expand":    {"diff_offset":  -5, "clarity": 80},
    "compress":  {"diff_offset": -15, "clarity": 85},
    "review":    {"diff_offset":  -5, "clarity": 75},
    "map":       {"diff_offset":   0, "clarity": 70},
    "testgen":   {"diff_offset":  +5, "clarity": 65},
    "generate":  {"diff_offset":  +5, "clarity": 60},
    "oneshot":   {"diff_offset": +10, "clarity": 65},
}


def role_coord(base_difficulty: int, role: str) -> SmashCoord:
    """Derive a task's (difficulty, clarity) from its base difficulty and role."""
    defaults = ROLE_DEFAULTS.get(role, {"diff_offset": 0, "clarity": 70})
    return SmashCoord(
        difficulty=max(0, min(100, base_difficulty + defaults["diff_offset"])),
        clarity=defaults["clarity"],
    )


@dataclass
class SmashRange:
    """Model capability region on the difficulty × clarity plane."""
    # Difficulty axis
    low: int          # minimum task difficulty it can handle
    sweet: int        # ideal task difficulty (peak efficiency)
    high: int         # maximum task difficulty it can attempt
    # Clarity axis
    min_clarity: int  # minimum task clarity the model needs to succeed

    def fit(self, coord: SmashCoord) -> float:
        """
        How well-fitted is this model to a task at the given coordinates?
        Returns 0.0–1.0 where 1.0 = perfectly right-sized.

        Two gates multiplied:
        - Difficulty: oversized = slight penalty, undersized = steep, above high = 0
        - Clarity: below min_clarity = steep penalty (model can't handle ambiguity)
        """
        difficulty, clarity = coord.difficulty, coord.clarity

        # Difficulty fit
        if difficulty > self.high:
            diff_fit = 0.0
        elif difficulty < self.low:
            diff_fit = max(0.2, 1.0 - (self.low - difficulty) / 50.0)
        elif difficulty <= self.sweet:
            diff_fit = 0.8 + 0.2 * (difficulty / max(self.sweet, 1))
        else:
            span = self.high - self.sweet
            diff_fit = max(0.3, 1.0 - 0.7 * (difficulty - self.sweet) / max(span, 1))

        # Clarity fit
        if clarity >= self.min_clarity:
            clar_fit = 1.0
        else:
            gap = self.min_clarity - clarity
            clar_fit = max(0.1, 1.0 - gap / 40.0)

        return diff_fit * clar_fit

    def covers(self, coord: SmashCoord, threshold: float = 0.5) -> bool:
        """Does this model's region cover the given task point?"""
        return self.fit(coord) >= threshold


def estimate_smash_range(
    params_b: float,
    active_params_b: float | None = None,
    is_moe: bool = False,
    quant: str = "",
) -> SmashRange:
    """
    Estimate a model's Club Smash range from its specs.

    This is the cold-start estimator — used before we have an empirical
    efficiency map. Once real benchmark data exists, the map is the truth
    and architecture (MoE, dense, etc.) becomes irrelevant.

    MoE models use active params for estimation since per-token capability
    tracks the active parameter count, not total.
    """
    effective = active_params_b if (is_moe and active_params_b) else params_b

    quant_penalty = {
        "f16": 1.0, "bf16": 1.0, "q8_0": 0.95,
        "q6_k": 0.90, "q5_k_m": 0.85, "q5_k_s": 0.83,
        "q4_k_m": 0.80, "iq4_xs": 0.78, "q4_k_s": 0.77, "q4_0": 0.75,
        "q3_k_m": 0.70, "q3_k_l": 0.68, "q3_k_s": 0.65,
        "q2_k": 0.55,
    }.get(quant.lower(), 0.85)

    adj = effective * quant_penalty

    if adj < 2.0:
        return SmashRange(low=5, sweet=15, high=25, min_clarity=85)
    elif adj < 5.0:
        return SmashRange(low=10, sweet=25, high=40, min_clarity=75)
    elif adj < 10.0:
        return SmashRange(low=15, sweet=35, high=55, min_clarity=65)
    elif adj < 20.0:
        return SmashRange(low=20, sweet=45, high=65, min_clarity=55)
    elif adj < 40.0:
        return SmashRange(low=25, sweet=50, high=75, min_clarity=45)
    elif adj < 80.0:
        return SmashRange(low=30, sweet=55, high=85, min_clarity=35)
    else:
        return SmashRange(low=35, sweet=60, high=95, min_clarity=25)


def estimate_token_load(coord: SmashCoord) -> int:
    """
    Estimate total tokens (in + out) a task at this coordinate will need.

    Harder tasks need more code, more reasoning. Lower clarity means the model
    must infer more, generating more exploratory tokens.

    Returns approximate total token count.
    """
    # Base: 200 tokens for trivial, scaling quadratically with difficulty
    base = 200 + int(15 * coord.difficulty + 0.1 * coord.difficulty ** 2)
    # Low clarity → model has to reason/explore more (+30% at clarity 0)
    clarity_factor = 1.0 + 0.3 * (1.0 - coord.clarity / 100.0)
    return int(base * clarity_factor)


def estimate_tok_s(params_b: float, active_params_b: float | None,
                   is_moe: bool, quant: str, is_local: bool,
                   is_gpu: bool) -> float:
    """
    Cold-start throughput estimate before real measurement exists.

    Once tok_s is measured, this is never called. MoE active params
    matter here because memory-bandwidth scales with active params.
    """
    effective = active_params_b if (is_moe and active_params_b) else params_b

    # Quant affects speed (less memory → faster)
    quant_speed = {
        "f16": 1.0, "bf16": 1.0, "q8_0": 1.3, "q6_k": 1.5,
        "q5_k_m": 1.7, "q4_k_m": 2.0, "q4_0": 2.1,
        "q3_k_m": 2.3, "q2_k": 2.5,
    }.get(quant.lower(), 1.5)

    if is_local and is_gpu:
        # GPU: rough baseline 200 tok/s for 7B q4, scales inversely
        return 200.0 * (7.0 / max(effective, 0.5)) * quant_speed
    elif is_local:
        # CPU: much slower, ~15 tok/s for 7B q4
        return 15.0 * (7.0 / max(effective, 0.5)) * quant_speed
    else:
        # Cloud: assume 80-120 tok/s, bigger models slightly slower
        return max(40.0, 120.0 - effective * 0.3)


def compute_time_to_complete(tok_s: float, coord: SmashCoord) -> float:
    """Estimated seconds to complete a task at this coordinate."""
    tokens = estimate_token_load(coord)
    return tokens / max(tok_s, 0.1)


def compute_energy_cost(time_s: float, power_w: float | None) -> float | None:
    """Energy in joules. None if power unknown (cloud models)."""
    if power_w is None:
        return None
    return time_s * power_w


def compute_dollar_cost(
    coord: SmashCoord, cost_input: float, cost_output: float,
) -> float:
    """Estimated dollar cost for a task at this coordinate."""
    tokens = estimate_token_load(coord)
    # Rough split: 40% input, 60% output
    return (tokens * 0.4 * cost_input + tokens * 0.6 * cost_output) / 1e6


def compute_efficiency_surface(
    smash: SmashRange,
    tok_s: float,
    d_range: tuple[int, int] = (0, 100),
    c_range: tuple[int, int] = (0, 100),
    resolution: int = 200,
) -> tuple:
    """
    Compute a 2D efficiency surface: time-to-complete across task space.

    Returns (difficulties, clarities, time_grid, efficiency_grid) where:
    - time_grid[c, d] = estimated seconds to complete (0 if model can't do it)
    - efficiency_grid[c, d] = normalised efficiency 0-1 (capability × speed)

    This is the TRUE compressor map: it shows not just where the model CAN
    work, but where it's FAST. The peak efficiency island is the sweet spot.
    """
    import numpy as np
    difficulties = np.linspace(d_range[0], d_range[1], resolution)
    clarities = np.linspace(c_range[0], c_range[1], resolution)
    time_grid = np.zeros((resolution, resolution))
    eff_grid = np.zeros((resolution, resolution))

    for ci, c in enumerate(clarities):
        for di, d in enumerate(difficulties):
            coord = SmashCoord(difficulty=int(d), clarity=int(c))
            capability = smash.fit(coord)
            if capability < 0.1:
                time_grid[ci, di] = 0.0
                eff_grid[ci, di] = 0.0
            else:
                t = compute_time_to_complete(tok_s, coord)
                time_grid[ci, di] = t
                # Efficiency = capability / time, normalised
                # Fast + capable = high efficiency
                # Slow or incapable = low efficiency
                eff_grid[ci, di] = capability / max(t, 0.01)

    # Normalise efficiency to 0-1 for visualisation
    max_eff = eff_grid.max()
    if max_eff > 0:
        eff_grid /= max_eff

    return difficulties, clarities, time_grid, eff_grid


def estimate_query_coords(
    description: str,
    role: str = "oneshot",
    has_tests: bool = False,
    has_examples: bool = False,
    has_signatures: bool = False,
) -> SmashCoord:
    """
    Heuristic: estimate (difficulty, clarity) for an arbitrary query.
    This is the universal router's entry point — no benchmarks needed,
    just approximate where the query falls on the plane.
    """
    # Clarity heuristics
    clarity = 50
    if has_signatures:
        clarity += 15
    if has_tests:
        clarity += 15
    if has_examples:
        clarity += 10
    words = len(description.split())
    if words > 200:
        clarity -= 10       # verbose ≠ clear
    elif words > 50:
        clarity += 5        # detailed spec

    # Difficulty heuristics (rough baseline)
    difficulty = 35
    complexity_signals = [
        "async", "concurrent", "recursive", "parser", "state machine",
        "tree", "graph", "cache", "protocol", "distributed",
    ]
    for signal in complexity_signals:
        if signal in description.lower():
            difficulty += 5
    if words < 20:
        difficulty -= 10    # short = probably simple
    elif words > 100:
        difficulty += 10    # long spec = complex ask

    # Apply role offset
    defaults = ROLE_DEFAULTS.get(role, {"diff_offset": 0, "clarity": 70})
    difficulty = max(0, min(100, difficulty + defaults["diff_offset"]))
    # Role clarity overrides heuristic if role is well-structured
    if defaults["clarity"] > clarity:
        clarity = defaults["clarity"]
    clarity = max(0, min(100, clarity))

    return SmashCoord(difficulty=difficulty, clarity=clarity)


def measured_smash(quality: float, elapsed_s: float, right_fit: float) -> int:
    """
    Compute measured Club Smash (0–100) from fight results.

    - quality < 1.0 → undersized, smash drops with quality
    - quality = 1.0 + fast + good fit → right-sized, high smash
    - quality = 1.0 but slow / oversized → works but wasteful
    """
    if quality < 1.0:
        # Failed: smash reflects how close it got
        return max(0, int(quality * 50))

    # Passed — score based on efficiency
    speed_bonus = min(1.0, 30.0 / max(elapsed_s, 0.1))  # faster = better
    return min(100, int(50 + 30 * right_fit + 20 * speed_bonus))


# ═══════════════════════════════════════════════════════════════════════════════
# EFFICIENCY MAP — turbo compressor–style visualisation
# ═══════════════════════════════════════════════════════════════════════════════

# Contour thresholds and glyphs — from peak island outward
_EFF_CONTOURS = [
    (0.95, "█"),  # peak — sweet spot island
    (0.85, "▓"),  # high efficiency
    (0.70, "▒"),  # usable
    (0.50, "░"),  # marginal
    (0.30, "·"),  # wasteful / struggling
    (0.00, " "),  # out of range
]


def _eff_glyph(fit: float) -> str:
    for threshold, glyph in _EFF_CONTOURS:
        if fit >= threshold:
            return glyph
    return " "


def render_efficiency_map(
    smash: SmashRange,
    name: str = "",
    *,
    d_range: tuple[int, int] = (0, 100),
    c_range: tuple[int, int] = (0, 100),
    d_step: int = 2,
    c_step: int = 3,
    task_coords: dict[str, SmashCoord] | None = None,
) -> str:
    """
    Render an ASCII efficiency map for a model — like a turbo compressor map.

    X-axis: Task difficulty (→)
    Y-axis: Task clarity (↑)
    Glyphs: █ peak  ▓ high  ▒ usable  ░ marginal  · wasteful

    Optional task_coords overlay: marks where specific tasks fall on the map.
    """
    d_lo, d_hi = d_range
    c_lo, c_hi = c_range

    # Build coordinate grid: rows from high clarity (top) to low (bottom)
    clarities = list(range(c_hi, c_lo - 1, -c_step))
    difficulties = list(range(d_lo, d_hi + 1, d_step))

    # Assign unique markers to tasks
    _MARKER_GLYPHS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"
    task_markers: dict[str, str] = {}
    marker_legend: list[tuple[str, str, float]] = []
    if task_coords:
        for i, (tid, coord) in enumerate(task_coords.items()):
            glyph = _MARKER_GLYPHS[i] if i < len(_MARKER_GLYPHS) else chr(65 + i)
            task_markers[tid] = glyph
            marker_legend.append((glyph, tid, smash.fit(coord)))

    # Pre-compute task positions (snap to nearest grid cell)
    markers: dict[tuple[int, int], str] = {}
    if task_coords:
        for tid, coord in task_coords.items():
            best_d = min(difficulties, key=lambda d: abs(d - coord.difficulty))
            best_c = min(clarities, key=lambda c: abs(c - coord.clarity))
            markers[(best_d, best_c)] = task_markers[tid]

    # Header
    tag = f"  {name}" if name else ""
    lines = [f"  🏏 Efficiency Map{tag}"]
    lines.append(f"  █ peak  ▓ high  ▒ ok  ░ weak  · waste")
    lines.append("")

    # Y-axis label + grid
    for c in clarities:
        label = f"  {c:>3}c │"
        cells = []
        for d in difficulties:
            coord = SmashCoord(difficulty=d, clarity=c)
            marker = markers.get((d, c))
            if marker:
                cells.append(marker)
            else:
                cells.append(_eff_glyph(smash.fit(coord)))
        lines.append(label + "".join(cells) + "│")

    # X-axis
    width = len(difficulties)
    lines.append("       └" + "─" * width + "┘")
    tick_line = "        "
    for d in difficulties:
        if d % 10 == 0:
            tick_line += str(d // 10) if d < 100 else "X"
        else:
            tick_line += " "
    lines.append(tick_line)
    lines.append("        " + " " * (width // 2 - 5) + "difficulty →")

    # Model stats
    lines.append(f"  Sweet spot: {smash.sweet}d, needs {smash.min_clarity}c+ clarity")
    lines.append(f"  Range: {smash.low}–{smash.high} difficulty")

    # Task legend
    if marker_legend:
        lines.append("")
        for glyph, tid, fit_val in marker_legend:
            pct = int(fit_val * 100)
            bar = "█" * (pct // 5)
            lines.append(f"  {glyph} {tid:<18} fit:{pct:>3}% {bar}")

    return "\n".join(lines)


def print_efficiency_maps(
    contenders: list,
    task_coords: dict[str, SmashCoord] | None = None,
) -> None:
    """Print efficiency maps for all contenders, with optional task overlays."""
    for c in contenders:
        print()
        print(render_efficiency_map(
            c.smash, f"{c.club} {c.name}",
            task_coords=task_coords,
        ))
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# FITNESS — configurable multi-objective optimisation
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class FitnessWeights:
    """
    Tunable knobs.  Quality is always a hard gate (must be 100%).
    These weights rank *champions* — models that already pass every test.
    """
    cost: float = 0.30
    speed: float = 0.30
    power: float = 0.20
    locality: float = 0.20

    def as_dict(self) -> dict[str, float]:
        return {
            "cost": self.cost, "speed": self.speed,
            "power": self.power, "locality": self.locality,
        }


PRESETS: dict[str, FitnessWeights] = {
    "balanced":    FitnessWeights(0.30, 0.30, 0.20, 0.20),
    "cheapest":    FitnessWeights(0.60, 0.15, 0.10, 0.15),
    "fastest":     FitnessWeights(0.10, 0.55, 0.10, 0.25),
    "greenest":    FitnessWeights(0.10, 0.15, 0.55, 0.20),
    "local-first": FitnessWeights(0.10, 0.25, 0.20, 0.45),
}


def compute_fitness(
    results: list["FightResult"],
    weights: FitnessWeights,
) -> list[tuple["FightResult", float]]:
    """
    Score every result.  quality < 1.0 → fitness = -1 (disqualified).
    Among champions, metrics with zero variance get their weight
    redistributed proportionally to the remaining active metrics.
    """
    if not results:
        return []

    passed = [r for r in results if r.quality >= 1.0]
    if not passed:
        return [(r, -1.0) for r in results]

    # Collect per-metric values for variance check
    vals: dict[str, list[float]] = {
        "cost":     [r.cost for r in passed],
        "speed":    [r.elapsed_s for r in passed],
        "power":    [r.energy_j for r in passed if r.energy_j is not None],
        "locality": [r.locality for r in passed],
    }

    raw = weights.as_dict()
    active: dict[str, float] = {}
    for k, w in raw.items():
        v = vals.get(k, [])
        if len(v) >= 2 and (max(v) - min(v)) > 1e-9:
            active[k] = w

    if not active:
        return [(r, 1.0 if r.quality >= 1.0 else -1.0) for r in results]

    total = sum(active.values())
    norm_w = {k: v / total for k, v in active.items()}

    ranges: dict[str, tuple[float, float]] = {}
    for k in active:
        v = vals[k]
        ranges[k] = (min(v), max(v))

    def _lower_better(val: float, key: str) -> float:
        mn, mx = ranges[key]
        return 1.0 - (val - mn) / (mx - mn) if mx > mn else 1.0

    def _higher_better(val: float, key: str) -> float:
        mn, mx = ranges[key]
        return (val - mn) / (mx - mn) if mx > mn else 1.0

    scored: list[tuple["FightResult", float]] = []
    for r in results:
        if r.quality < 1.0:
            scored.append((r, -1.0))
            continue
        s = 0.0
        if "cost" in norm_w:
            s += norm_w["cost"] * _lower_better(r.cost, "cost")
        if "speed" in norm_w:
            s += norm_w["speed"] * _lower_better(r.elapsed_s, "speed")
        if "power" in norm_w:
            if r.energy_j is not None:
                s += norm_w["power"] * _lower_better(r.energy_j, "power")
            else:
                s += norm_w["power"] * 0.5   # neutral for unknown
        if "locality" in norm_w:
            s += norm_w["locality"] * _higher_better(r.locality, "locality")
        scored.append((r, round(s, 4)))

    return scored


# ═══════════════════════════════════════════════════════════════════════════════
# TASKS — coding challenges with executable test suites
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TournamentTask:
    """A coding challenge with language, rubric, and executable tests."""
    id: str
    name: str
    lang: str
    description: str
    expected_class: str
    methods: list[str]
    tests: list[tuple[str, str]]       # (test_name, test_code)

    # Club Smash — base difficulty of the underlying challenge (0–100)
    base_difficulty: int = 35

    # Spec clarity (0–100): how precisely the spec describes the expected output.
    # High clarity = exact method signatures, types, edge cases documented.
    # Low clarity = vague natural language, model must infer the design.
    # If set, overrides the role's default clarity in coord_for().
    spec_clarity: int | None = None

    # Optional per-role coordinate overrides (falls back to role_coord())
    role_overrides: dict[str, SmashCoord] = field(default_factory=dict)

    def coord_for(self, role: str) -> SmashCoord:
        """Get the (difficulty, clarity) point for this task in a given role."""
        if role in self.role_overrides:
            return self.role_overrides[role]
        coord = role_coord(self.base_difficulty, role)
        if self.spec_clarity is not None:
            coord = SmashCoord(coord.difficulty, self.spec_clarity)
        return coord

    @property
    def method_signatures(self) -> str:
        return "\n".join(f"  - {m}" for m in self.methods)

    @property
    def runner(self) -> LanguageRunner:
        return RUNNERS[self.lang]

    @property
    def content_hash(self) -> str:
        """Hash of spec + tests — changes when the task is meaningfully modified."""
        import hashlib
        parts = [self.id, self.lang, self.description, self.expected_class]
        parts += self.methods
        for name, code in self.tests:
            parts += [name, code]
        parts.append(str(self.base_difficulty))
        parts.append(str(self.spec_clarity))
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


TASKS: dict[str, TournamentTask] = {}

# ---------------------------------------------------------------------------
TASKS["rate-limiter"] = TournamentTask(
    id="rate-limiter",
    name="RateLimiter",
    lang="python",
    description=(
        "A sliding-window rate limiter that tracks call timestamps and "
        "allows or denies calls based on a max_calls/period window. "
        "max_calls=0 is valid and means all calls are denied."
    ),
    expected_class="RateLimiter",
    methods=[
        "__init__(self, max_calls: int, period: float)",
        "allow(self) -> bool",
        "remaining(self) -> int",
        "reset(self) -> None",
        "time_until_reset(self) -> float",
    ],
    tests=[
        ("basic_limiting", textwrap.dedent("""\
            rl = RateLimiter(max_calls=2, period=1.0)
            assert rl.allow() is True
            assert rl.allow() is True
            assert rl.allow() is False
        """)),
        ("reset_after_period", textwrap.dedent("""\
            import time
            rl = RateLimiter(max_calls=1, period=0.1)
            assert rl.allow() is True
            assert rl.allow() is False
            time.sleep(0.15)
            assert rl.allow() is True
        """)),
        ("remaining_count", textwrap.dedent("""\
            rl = RateLimiter(max_calls=3, period=1.0)
            assert rl.remaining() == 3
            rl.allow()
            assert rl.remaining() == 2
        """)),
        ("reset_method", textwrap.dedent("""\
            rl = RateLimiter(max_calls=2, period=1.0)
            rl.allow(); rl.allow()
            assert rl.allow() is False
            rl.reset()
            assert rl.allow() is True
        """)),
        ("time_until_reset", textwrap.dedent("""\
            rl = RateLimiter(max_calls=1, period=1.0)
            rl.allow()
            t = rl.time_until_reset()
            assert 0.0 < t <= 1.0
        """)),
        ("zero_max_calls", textwrap.dedent("""\
            rl = RateLimiter(max_calls=0, period=1.0)
            assert rl.allow() is False
        """)),
        ("high_throughput", textwrap.dedent("""\
            rl = RateLimiter(max_calls=100, period=1.0)
            results = [rl.allow() for _ in range(100)]
            assert all(results)
            assert rl.allow() is False
        """)),
    ],
    base_difficulty=20,     # simple sliding window + time tracking
    spec_clarity=85,
)

# ---------------------------------------------------------------------------
TASKS["lru-cache"] = TournamentTask(
    id="lru-cache",
    name="LRUCache",
    lang="python",
    description=(
        "A Least Recently Used cache with fixed capacity. "
        "get() returns None on miss. put() evicts LRU entry on overflow."
    ),
    expected_class="LRUCache",
    methods=[
        "__init__(self, capacity: int)",
        "get(self, key: str) -> any",
        "put(self, key: str, value: any) -> None",
        "size(self) -> int",
        "clear(self) -> None",
    ],
    tests=[
        ("basic_get_put", textwrap.dedent("""\
            c = LRUCache(capacity=2)
            c.put("a", 1)
            assert c.get("a") == 1
        """)),
        ("eviction", textwrap.dedent("""\
            c = LRUCache(capacity=2)
            c.put("a", 1); c.put("b", 2); c.put("c", 3)
            assert c.get("a") is None
            assert c.get("b") == 2
            assert c.get("c") == 3
        """)),
        ("lru_ordering", textwrap.dedent("""\
            c = LRUCache(capacity=2)
            c.put("a", 1); c.put("b", 2)
            c.get("a")
            c.put("c", 3)
            assert c.get("a") == 1
            assert c.get("b") is None
        """)),
        ("update_key", textwrap.dedent("""\
            c = LRUCache(capacity=2)
            c.put("a", 1); c.put("a", 10)
            assert c.get("a") == 10
            assert c.size() == 1
        """)),
        ("size_tracking", textwrap.dedent("""\
            c = LRUCache(capacity=3)
            assert c.size() == 0
            c.put("a", 1); c.put("b", 2); c.put("c", 3); c.put("d", 4)
            assert c.size() == 3
        """)),
        ("clear_all", textwrap.dedent("""\
            c = LRUCache(capacity=2)
            c.put("a", 1); c.put("b", 2)
            c.clear()
            assert c.size() == 0
            assert c.get("a") is None
        """)),
    ],
    base_difficulty=25,     # LRU ordering is the main challenge
    spec_clarity=85,
)

# ---------------------------------------------------------------------------
TASKS["event-emitter"] = TournamentTask(
    id="event-emitter",
    name="EventEmitter",
    lang="python",
    description=(
        "A pub/sub event system supporting on, off, once, and emit. "
        "once() listeners fire exactly once then auto-remove."
    ),
    expected_class="EventEmitter",
    methods=[
        "__init__(self)",
        "on(self, event: str, fn) -> None",
        "off(self, event: str, fn) -> None",
        "once(self, event: str, fn) -> None",
        "emit(self, event: str, *args) -> None",
        "listener_count(self, event: str) -> int",
    ],
    tests=[
        ("basic_emit", textwrap.dedent("""\
            ee = EventEmitter()
            results = []
            ee.on("data", lambda x: results.append(x))
            ee.emit("data", 42)
            assert results == [42]
        """)),
        ("multiple_listeners", textwrap.dedent("""\
            ee = EventEmitter()
            a, b = [], []
            ee.on("x", lambda v: a.append(v))
            ee.on("x", lambda v: b.append(v))
            ee.emit("x", 1)
            assert a == [1] and b == [1]
        """)),
        ("remove_listener", textwrap.dedent("""\
            ee = EventEmitter()
            results = []
            fn = lambda v: results.append(v)
            ee.on("x", fn)
            ee.off("x", fn)
            ee.emit("x", 1)
            assert results == []
        """)),
        ("once_fires_once", textwrap.dedent("""\
            ee = EventEmitter()
            results = []
            ee.once("x", lambda v: results.append(v))
            ee.emit("x", 1)
            ee.emit("x", 2)
            assert results == [1]
        """)),
        ("multiple_events", textwrap.dedent("""\
            ee = EventEmitter()
            a, b = [], []
            ee.on("a", lambda v: a.append(v))
            ee.on("b", lambda v: b.append(v))
            ee.emit("a", 1); ee.emit("b", 2)
            assert a == [1] and b == [2]
        """)),
        ("listener_count", textwrap.dedent("""\
            ee = EventEmitter()
            ee.on("x", lambda: None)
            ee.on("x", lambda: None)
            ee.on("y", lambda: None)
            assert ee.listener_count("x") == 2
            assert ee.listener_count("y") == 1
        """)),
    ],
    base_difficulty=40,     # once() wrapper is the tricky part
    spec_clarity=85,
    role_overrides={
        "fill": SmashCoord(difficulty=45, clarity=90),   # once() is hard even with skeleton
    },
)

# ---------------------------------------------------------------------------
# HARDER PYTHON — difficulty 60+
# ---------------------------------------------------------------------------
TASKS["expr-parser"] = TournamentTask(
    id="expr-parser",
    name="ExprParser",
    lang="python",
    description=(
        "A recursive-descent expression parser and evaluator. "
        "Supports +, -, *, / with correct precedence, parentheses, "
        "unary minus, and variable bindings via an env dict. "
        "Raises ValueError on parse errors."
    ),
    expected_class="ExprParser",
    methods=[
        "__init__(self, env: dict[str, float] | None = None)",
        "parse(self, expr: str) -> float",
    ],
    tests=[
        ("simple_add", textwrap.dedent("""\
            p = ExprParser()
            assert p.parse("2 + 3") == 5.0
        """)),
        ("precedence", textwrap.dedent("""\
            p = ExprParser()
            assert p.parse("2 + 3 * 4") == 14.0
        """)),
        ("parens", textwrap.dedent("""\
            p = ExprParser()
            assert p.parse("(2 + 3) * 4") == 20.0
        """)),
        ("unary_minus", textwrap.dedent("""\
            p = ExprParser()
            assert p.parse("-3 + 5") == 2.0
            assert p.parse("-(2 + 3)") == -5.0
        """)),
        ("nested_parens", textwrap.dedent("""\
            p = ExprParser()
            assert p.parse("((1 + 2) * (3 + 4))") == 21.0
        """)),
        ("division", textwrap.dedent("""\
            p = ExprParser()
            assert abs(p.parse("10 / 4") - 2.5) < 1e-9
        """)),
        ("variables", textwrap.dedent("""\
            p = ExprParser(env={"x": 10, "y": 3})
            assert p.parse("x + y * 2") == 16.0
        """)),
        ("complex_expr", textwrap.dedent("""\
            p = ExprParser(env={"a": 2})
            assert abs(p.parse("a * (3 + 4) / 2 - 1") - 6.0) < 1e-9
        """)),
        ("error_handling", textwrap.dedent("""\
            p = ExprParser()
            try:
                p.parse("2 +")
                assert False, "should have raised"
            except ValueError:
                pass
        """)),
    ],
    base_difficulty=55,     # recursive descent with precedence, unary, vars
    spec_clarity=85,
)

# ---------------------------------------------------------------------------
TASKS["fsm"] = TournamentTask(
    id="fsm",
    name="FSM",
    lang="python",
    description=(
        "A finite state machine with states, transitions, guards, and "
        "entry/exit actions. Transitions can have guard functions that "
        "return bool. Entry/exit actions fire on state changes. "
        "Raises ValueError on invalid transitions."
    ),
    expected_class="FSM",
    methods=[
        "__init__(self, initial_state: str)",
        "add_transition(self, src: str, event: str, dst: str, "
        "guard=None, action=None) -> None",
        "on_enter(self, state: str, fn) -> None",
        "on_exit(self, state: str, fn) -> None",
        "send(self, event: str, **ctx) -> str",
        "state(self) -> str",
    ],
    tests=[
        ("basic_transition", textwrap.dedent("""\
            m = FSM("idle")
            m.add_transition("idle", "start", "running")
            assert m.send("start") == "running"
            assert m.state() == "running"
        """)),
        ("invalid_transition", textwrap.dedent("""\
            m = FSM("idle")
            m.add_transition("idle", "start", "running")
            try:
                m.send("stop")
                assert False, "should raise"
            except ValueError:
                pass
        """)),
        ("guard_blocks", textwrap.dedent("""\
            m = FSM("idle")
            m.add_transition("idle", "start", "running",
                             guard=lambda **ctx: ctx.get("auth", False))
            try:
                m.send("start", auth=False)
                assert False, "guard should block"
            except ValueError:
                pass
            assert m.state() == "idle"
        """)),
        ("guard_allows", textwrap.dedent("""\
            m = FSM("idle")
            m.add_transition("idle", "start", "running",
                             guard=lambda **ctx: ctx.get("auth", False))
            assert m.send("start", auth=True) == "running"
        """)),
        ("entry_exit_actions", textwrap.dedent("""\
            log = []
            m = FSM("a")
            m.add_transition("a", "go", "b")
            m.on_exit("a", lambda: log.append("exit_a"))
            m.on_enter("b", lambda: log.append("enter_b"))
            m.send("go")
            assert log == ["exit_a", "enter_b"]
        """)),
        ("multi_step", textwrap.dedent("""\
            m = FSM("off")
            m.add_transition("off", "power", "on")
            m.add_transition("on", "power", "off")
            m.send("power")
            assert m.state() == "on"
            m.send("power")
            assert m.state() == "off"
        """)),
        ("action_on_transition", textwrap.dedent("""\
            results = []
            m = FSM("s1")
            m.add_transition("s1", "go", "s2",
                             action=lambda **ctx: results.append(ctx.get("val")))
            m.send("go", val=42)
            assert results == [42]
        """)),
    ],
    base_difficulty=55,     # guards + actions + entry/exit ordering
    spec_clarity=85,
    role_overrides={
        "oneshot": SmashCoord(difficulty=65, clarity=60),  # must infer ordering conventions
    },
)

# ---------------------------------------------------------------------------
# RUST TASKS — test the Rust runner at medium-high difficulty
# ---------------------------------------------------------------------------
TASKS["rust-stack-calc"] = TournamentTask(
    id="rust-stack-calc",
    name="RustStackCalc",
    lang="rust",
    description=(
        "A stack-based calculator (RPN). Supports push, pop, and applying "
        "operations (+, -, *, /) to the top two stack elements. "
        "Returns Result<f64, String> for errors (underflow, division by zero, unknown op)."
    ),
    expected_class="StackCalc",
    methods=[
        "StackCalc::new() -> Self",
        "push(&mut self, val: f64)",
        "pop(&mut self) -> Result<f64, String>",
        "apply(&mut self, op: &str) -> Result<f64, String>",
        "peek(&self) -> Result<f64, String>",
        "size(&self) -> usize",
    ],
    tests=[
        ("push_pop", textwrap.dedent("""\
            fn main() {
                let mut c = StackCalc::new();
                c.push(42.0);
                assert_eq!(c.pop().unwrap(), 42.0);
                assert!(c.pop().is_err());
            }
        """)),
        ("add", textwrap.dedent("""\
            fn main() {
                let mut c = StackCalc::new();
                c.push(3.0); c.push(4.0);
                let r = c.apply("+").unwrap();
                assert!((r - 7.0).abs() < 1e-9);
                assert_eq!(c.size(), 1);
            }
        """)),
        ("subtract", textwrap.dedent("""\
            fn main() {
                let mut c = StackCalc::new();
                c.push(10.0); c.push(3.0);
                let r = c.apply("-").unwrap();
                assert!((r - 7.0).abs() < 1e-9);
            }
        """)),
        ("multiply_divide", textwrap.dedent("""\
            fn main() {
                let mut c = StackCalc::new();
                c.push(6.0); c.push(3.0);
                assert!((c.apply("*").unwrap() - 18.0).abs() < 1e-9);
                c.push(2.0);
                assert!((c.apply("/").unwrap() - 9.0).abs() < 1e-9);
            }
        """)),
        ("div_by_zero", textwrap.dedent("""\
            fn main() {
                let mut c = StackCalc::new();
                c.push(5.0); c.push(0.0);
                assert!(c.apply("/").is_err());
            }
        """)),
        ("underflow", textwrap.dedent("""\
            fn main() {
                let mut c = StackCalc::new();
                c.push(1.0);
                assert!(c.apply("+").is_err());
            }
        """)),
        ("peek_and_size", textwrap.dedent("""\
            fn main() {
                let mut c = StackCalc::new();
                assert_eq!(c.size(), 0);
                c.push(5.0);
                assert!((c.peek().unwrap() - 5.0).abs() < 1e-9);
                assert_eq!(c.size(), 1);
            }
        """)),
        ("chained_ops", textwrap.dedent("""\
            fn main() {
                let mut c = StackCalc::new();
                // compute (3 + 4) * 2 = 14
                c.push(3.0); c.push(4.0);
                c.apply("+").unwrap();
                c.push(2.0);
                c.apply("*").unwrap();
                assert!((c.peek().unwrap() - 14.0).abs() < 1e-9);
            }
        """)),
    ],
    base_difficulty=45,     # straightforward but needs Rust fluency
    spec_clarity=85,
)

# ---------------------------------------------------------------------------
TASKS["rust-trie"] = TournamentTask(
    id="rust-trie",
    name="RustTrie",
    lang="rust",
    description=(
        "A prefix trie for string storage with insert, search, starts_with, "
        "and delete. Delete must clean up childless nodes. "
        "All methods take &str keys."
    ),
    expected_class="Trie",
    methods=[
        "Trie::new() -> Self",
        "insert(&mut self, word: &str)",
        "search(&self, word: &str) -> bool",
        "starts_with(&self, prefix: &str) -> bool",
        "delete(&mut self, word: &str) -> bool",
        "count(&self) -> usize",
    ],
    tests=[
        ("insert_search", textwrap.dedent("""\
            fn main() {
                let mut t = Trie::new();
                t.insert("hello");
                assert!(t.search("hello"));
                assert!(!t.search("hell"));
                assert!(!t.search("helloo"));
            }
        """)),
        ("starts_with", textwrap.dedent("""\
            fn main() {
                let mut t = Trie::new();
                t.insert("hello");
                assert!(t.starts_with("hel"));
                assert!(t.starts_with("hello"));
                assert!(!t.starts_with("helloo"));
            }
        """)),
        ("multiple_words", textwrap.dedent("""\
            fn main() {
                let mut t = Trie::new();
                t.insert("abc"); t.insert("abd"); t.insert("xyz");
                assert!(t.search("abc"));
                assert!(t.search("abd"));
                assert!(t.search("xyz"));
                assert_eq!(t.count(), 3);
            }
        """)),
        ("prefix_overlap", textwrap.dedent("""\
            fn main() {
                let mut t = Trie::new();
                t.insert("app"); t.insert("apple");
                assert!(t.search("app"));
                assert!(t.search("apple"));
                assert_eq!(t.count(), 2);
            }
        """)),
        ("delete_basic", textwrap.dedent("""\
            fn main() {
                let mut t = Trie::new();
                t.insert("hello");
                assert!(t.delete("hello"));
                assert!(!t.search("hello"));
                assert_eq!(t.count(), 0);
            }
        """)),
        ("delete_preserves_prefix", textwrap.dedent("""\
            fn main() {
                let mut t = Trie::new();
                t.insert("app"); t.insert("apple");
                t.delete("apple");
                assert!(t.search("app"));
                assert!(!t.search("apple"));
                assert_eq!(t.count(), 1);
            }
        """)),
        ("delete_nonexistent", textwrap.dedent("""\
            fn main() {
                let mut t = Trie::new();
                t.insert("hello");
                assert!(!t.delete("world"));
                assert!(t.search("hello"));
            }
        """)),
        ("empty_string", textwrap.dedent("""\
            fn main() {
                let mut t = Trie::new();
                t.insert("");
                assert!(t.search(""));
                assert_eq!(t.count(), 1);
                assert!(t.starts_with(""));
            }
        """)),
    ],
    base_difficulty=60,     # Rust ownership + recursive delete + trie cleanup
    spec_clarity=85,
    role_overrides={
        "fill": SmashCoord(difficulty=65, clarity=85),  # recursive delete in Rust is hard
    },
)


# ---------------------------------------------------------------------------
TASKS["counter"] = TournamentTask(
    id="counter",
    name="Counter",
    lang="python",
    description=(
        "A counter that increments, decrements, and resets. "
        "Starts at an initial value (default 0). "
        "Cannot go below zero (floor at 0)."
    ),
    expected_class="Counter",
    methods=[
        "__init__(self, initial=0)",
        "increment(self, n=1) -> int",
        "decrement(self, n=1) -> int",
        "reset(self) -> None",
        "value (property) -> int",
    ],
    tests=[
        ("basic_increment", textwrap.dedent("""\
            c = Counter()
            assert c.value == 0
            assert c.increment() == 1
            assert c.value == 1
        """)),
        ("basic_decrement", textwrap.dedent("""\
            c = Counter(5)
            assert c.decrement() == 4
            assert c.value == 4
        """)),
        ("floor_at_zero", textwrap.dedent("""\
            c = Counter(1)
            c.decrement()
            assert c.value == 0
            c.decrement()
            assert c.value == 0
            c.decrement(10)
            assert c.value == 0
        """)),
        ("reset_works", textwrap.dedent("""\
            c = Counter(3)
            c.increment(10)
            assert c.value == 13
            c.reset()
            assert c.value == 3
        """)),
        ("custom_initial", textwrap.dedent("""\
            c = Counter(42)
            assert c.value == 42
            c.increment()
            assert c.value == 43
        """)),
        ("increment_by_n", textwrap.dedent("""\
            c = Counter()
            assert c.increment(5) == 5
            assert c.increment(3) == 8
            assert c.decrement(2) == 6
        """)),
    ],
    base_difficulty=8,
    spec_clarity=85,
)

# ---------------------------------------------------------------------------
TASKS["rust-hello-struct"] = TournamentTask(
    id="rust-hello-struct",
    name="Greeter",
    lang="rust",
    description=(
        "A Greeter struct that stores a name and produces formatted greetings. "
        "Supports formal and informal modes."
    ),
    expected_class="Greeter",
    methods=[
        "Greeter::new(name: &str) -> Self",
        "greet(&self) -> String",
        "formal_greet(&self) -> String",
        "set_name(&mut self, name: &str)",
        "name(&self) -> &str",
    ],
    tests=[
        ("basic_greet", textwrap.dedent("""\
            fn main() {
                let g = Greeter::new("Alice");
                assert_eq!(g.greet(), "Hello, Alice!");
            }
        """)),
        ("formal_greet", textwrap.dedent("""\
            fn main() {
                let g = Greeter::new("Alice");
                assert_eq!(g.formal_greet(), "Good day, Alice. How do you do?");
            }
        """)),
        ("set_name", textwrap.dedent("""\
            fn main() {
                let mut g = Greeter::new("Alice");
                g.set_name("Bob");
                assert_eq!(g.name(), "Bob");
                assert_eq!(g.greet(), "Hello, Bob!");
            }
        """)),
        ("name_accessor", textwrap.dedent("""\
            fn main() {
                let g = Greeter::new("Charlie");
                assert_eq!(g.name(), "Charlie");
            }
        """)),
        ("empty_name", textwrap.dedent("""\
            fn main() {
                let g = Greeter::new("");
                assert_eq!(g.greet(), "Hello, !");
                assert_eq!(g.formal_greet(), "Good day, . How do you do?");
            }
        """)),
    ],
    base_difficulty=12,
    spec_clarity=85,
)

# ---------------------------------------------------------------------------
TASKS["min-stack"] = TournamentTask(
    id="min-stack",
    name="MinStack",
    lang="python",
    description=(
        "A stack that supports push, pop, top, and get_min, all in O(1) time. "
        "get_min returns the minimum value currently in the stack. "
        "Raises ValueError on empty stack operations."
    ),
    expected_class="MinStack",
    methods=[
        "__init__(self)",
        "push(self, val: int) -> None",
        "pop(self) -> int",
        "top(self) -> int",
        "get_min(self) -> int",
        "size(self) -> int",
        "is_empty(self) -> bool",
    ],
    tests=[
        ("push_pop", textwrap.dedent("""\
            s = MinStack()
            s.push(10)
            s.push(20)
            assert s.top() == 20
            assert s.pop() == 20
            assert s.top() == 10
            assert s.size() == 1
        """)),
        ("min_tracking", textwrap.dedent("""\
            s = MinStack()
            s.push(3)
            s.push(5)
            s.push(1)
            assert s.get_min() == 1
            s.pop()
            assert s.get_min() == 3
        """)),
        ("min_after_pops", textwrap.dedent("""\
            s = MinStack()
            s.push(2)
            s.push(4)
            s.push(1)
            s.push(3)
            assert s.get_min() == 1
            s.pop()
            assert s.get_min() == 1
            s.pop()
            assert s.get_min() == 2
        """)),
        ("empty_raises", textwrap.dedent("""\
            s = MinStack()
            raised = False
            try:
                s.pop()
            except ValueError:
                raised = True
            assert raised
            raised = False
            try:
                s.top()
            except ValueError:
                raised = True
            assert raised
            raised = False
            try:
                s.get_min()
            except ValueError:
                raised = True
            assert raised
        """)),
        ("duplicate_mins", textwrap.dedent("""\
            s = MinStack()
            s.push(1)
            s.push(1)
            s.push(2)
            assert s.get_min() == 1
            s.pop()
            assert s.get_min() == 1
            s.pop()
            assert s.get_min() == 1
        """)),
        ("is_empty_check", textwrap.dedent("""\
            s = MinStack()
            assert s.is_empty() is True
            s.push(42)
            assert s.is_empty() is False
            s.pop()
            assert s.is_empty() is True
        """)),
        ("large_stack", textwrap.dedent("""\
            s = MinStack()
            for i in range(1000, 0, -1):
                s.push(i)
            assert s.get_min() == 1
            assert s.size() == 1000
            s.pop()
            assert s.get_min() == 2
        """)),
    ],
    base_difficulty=30,
    spec_clarity=85,
)

# ---------------------------------------------------------------------------
TASKS["csv-parser"] = TournamentTask(
    id="csv-parser",
    name="CsvParser",
    lang="rust",
    description=(
        "A CSV parser that handles quoted fields, escaped quotes (doubled \\\"\\\"), "
        "commas inside quotes, and newlines inside quotes. "
        "Returns Vec<Vec<String>> of records. Skips empty lines."
    ),
    expected_class="CsvParser",
    methods=[
        "CsvParser::new() -> Self",
        "parse(&self, input: &str) -> Vec<Vec<String>>",
        "parse_line(&self, line: &str) -> Vec<String>",
    ],
    tests=[
        ("simple_csv", textwrap.dedent("""\
            fn main() {
                let p = CsvParser::new();
                let rows = p.parse("a,b,c\\n1,2,3");
                assert_eq!(rows.len(), 2);
                assert_eq!(rows[0], vec!["a", "b", "c"]);
                assert_eq!(rows[1], vec!["1", "2", "3"]);
            }
        """)),
        ("quoted_fields", textwrap.dedent("""\
            fn main() {
                let p = CsvParser::new();
                let rows = p.parse("name,desc\\n\\"Alice\\",\\"A+ student\\"");
                assert_eq!(rows[1][0], "Alice");
                assert_eq!(rows[1][1], "A+ student");
            }
        """)),
        ("escaped_quotes", textwrap.dedent("""\
            fn main() {
                let p = CsvParser::new();
                let row = p.parse_line("say,\\"He said \\"\\"hi\\"\\"\\",end");
                assert_eq!(row[1], "He said \\"hi\\"");
                assert_eq!(row.len(), 3);
            }
        """)),
        ("comma_in_quotes", textwrap.dedent("""\
            fn main() {
                let p = CsvParser::new();
                let row = p.parse_line("\\"a,b\\",c");
                assert_eq!(row.len(), 2);
                assert_eq!(row[0], "a,b");
                assert_eq!(row[1], "c");
            }
        """)),
        ("empty_fields", textwrap.dedent("""\
            fn main() {
                let p = CsvParser::new();
                let row = p.parse_line(",a,,b,");
                assert_eq!(row, vec!["", "a", "", "b", ""]);
            }
        """)),
        ("empty_input", textwrap.dedent("""\
            fn main() {
                let p = CsvParser::new();
                let rows = p.parse("");
                assert!(rows.is_empty());
            }
        """)),
        ("skip_empty_lines", textwrap.dedent("""\
            fn main() {
                let p = CsvParser::new();
                let rows = p.parse("a,b\\n\\nc,d\\n");
                assert_eq!(rows.len(), 2);
                assert_eq!(rows[0], vec!["a", "b"]);
                assert_eq!(rows[1], vec!["c", "d"]);
            }
        """)),
    ],
    base_difficulty=35,
    spec_clarity=85,
    role_overrides={
        "fill": SmashCoord(difficulty=40, clarity=85),  # Rust string handling is fiddly
    },
)

# ---------------------------------------------------------------------------
TASKS["json-path"] = TournamentTask(
    id="json-path",
    name="JsonPath",
    lang="python",
    description=(
        "A JSONPath evaluator supporting: root ($), dot notation ($.store.book), "
        "bracket notation ($['store']), array indexing ($[0], $[-1]), wildcards "
        "($[*], $.store.*), array slicing ($[0:2]), and recursive descent ($..*). "
        "Takes a parsed JSON object (dict/list) and a JSONPath expression string, "
        "returns a list of matching values."
    ),
    expected_class="JsonPath",
    methods=[
        "__init__(self)",
        "query(self, data: Any, path: str) -> list",
        "compile(self, path: str) -> list[str]",
    ],
    tests=[
        ("root_query", textwrap.dedent("""\
            jp = JsonPath()
            data = {"a": 1, "b": 2}
            result = jp.query(data, "$")
            assert result == [{"a": 1, "b": 2}]
        """)),
        ("dot_notation", textwrap.dedent("""\
            jp = JsonPath()
            data = {"a": {"b": {"c": 42}}}
            result = jp.query(data, "$.a.b.c")
            assert result == [42]
        """)),
        ("bracket_notation", textwrap.dedent("""\
            jp = JsonPath()
            data = {"store": {"name": "Books R Us"}}
            result = jp.query(data, "$['store']['name']")
            assert result == ["Books R Us"]
        """)),
        ("array_index", textwrap.dedent("""\
            jp = JsonPath()
            data = {"items": [10, 20, 30, 40]}
            assert jp.query(data, "$.items[0]") == [10]
            assert jp.query(data, "$.items[-1]") == [40]
        """)),
        ("wildcard", textwrap.dedent("""\
            jp = JsonPath()
            data = {"items": [1, 2, 3]}
            result = jp.query(data, "$.items[*]")
            assert result == [1, 2, 3]
        """)),
        ("array_slice", textwrap.dedent("""\
            jp = JsonPath()
            data = {"items": [10, 20, 30, 40, 50]}
            result = jp.query(data, "$.items[0:2]")
            assert result == [10, 20]
        """)),
        ("recursive_descent", textwrap.dedent("""\
            jp = JsonPath()
            data = {"a": {"b": 1, "c": {"b": 2}}, "b": 3}
            result = jp.query(data, "$..b")
            assert sorted(result) == [1, 2, 3]
        """)),
        ("nested_complex", textwrap.dedent("""\
            jp = JsonPath()
            data = {"store": {"book": [
                {"title": "A", "price": 10},
                {"title": "B", "price": 20},
            ]}}
            result = jp.query(data, "$.store.book[0].title")
            assert result == ["A"]
        """)),
    ],
    base_difficulty=70,
    spec_clarity=85,
    role_overrides={
        "fill": SmashCoord(difficulty=75, clarity=80),
    },
)

# ---------------------------------------------------------------------------
TASKS["mini-regex"] = TournamentTask(
    id="mini-regex",
    name="MiniRegex",
    lang="python",
    description=(
        "A minimal regex engine supporting: literal chars, . (any char), "
        "* (zero or more), + (one or more), ? (zero or one), character classes "
        "[abc], negated classes [^abc], ranges [a-z], anchors ^ and $. "
        "Returns bool for full match. Does NOT need groups, backrefs, or lookahead."
    ),
    expected_class="MiniRegex",
    methods=[
        "__init__(self, pattern: str)",
        "match(self, text: str) -> bool",
        "find(self, text: str) -> str | None",
    ],
    tests=[
        ("literal", textwrap.dedent("""\
            r = MiniRegex("hello")
            assert r.match("hello") is True
            assert r.match("world") is False
        """)),
        ("dot_any", textwrap.dedent("""\
            r = MiniRegex("h.llo")
            assert r.match("hello") is True
            assert r.match("hallo") is True
            assert r.match("hllo") is False
        """)),
        ("star_repeat", textwrap.dedent("""\
            r = MiniRegex("ab*c")
            assert r.match("ac") is True
            assert r.match("abc") is True
            assert r.match("abbbbc") is True
        """)),
        ("plus_repeat", textwrap.dedent("""\
            r = MiniRegex("ab+c")
            assert r.match("ac") is False
            assert r.match("abc") is True
            assert r.match("abbbc") is True
        """)),
        ("question_optional", textwrap.dedent("""\
            r = MiniRegex("colou?r")
            assert r.match("color") is True
            assert r.match("colour") is True
        """)),
        ("char_class", textwrap.dedent("""\
            r = MiniRegex("[abc]x")
            assert r.match("ax") is True
            assert r.match("bx") is True
            assert r.match("dx") is False
        """)),
        ("negated_class", textwrap.dedent("""\
            r = MiniRegex("[^abc]x")
            assert r.match("dx") is True
            assert r.match("ax") is False
        """)),
        ("anchors", textwrap.dedent("""\
            r = MiniRegex("^hello$")
            assert r.match("hello") is True
            assert r.match("hello world") is False
            r2 = MiniRegex("he.*ld")
            assert r2.find("say hello world ok") == "hello world"
        """)),
    ],
    base_difficulty=78,
    spec_clarity=85,
    role_overrides={
        "fill": SmashCoord(difficulty=82, clarity=75),
    },
)

# ---------------------------------------------------------------------------
TASKS["rust-json-parser"] = TournamentTask(
    id="rust-json-parser",
    name="RustJsonParser",
    lang="rust",
    description=(
        "A JSON parser that converts a JSON string into a Value enum. "
        "The model must define both the Value enum and the JsonParser struct. "
        "Value enum variants: Null, Bool(bool), Number(f64), Str(String), "
        "Array(Vec<Value>), Object(Vec<(String, Value)>). "
        "Supports: null, booleans, numbers (integer, float, negative), "
        "strings (with escapes \\n \\t \\\\ \\\"), arrays, and nested objects. "
        "Returns Result<Value, String> for parse errors. "
        "Value should implement Display for pretty printing."
    ),
    expected_class="JsonParser",
    methods=[
        "JsonParser::new() -> Self",
        "parse(&self, input: &str) -> Result<Value, String>",
    ],
    tests=[
        ("parse_null", textwrap.dedent("""\
            fn main() {
                let p = JsonParser::new();
                let v = p.parse("null").unwrap();
                match v {
                    Value::Null => {},
                    _ => panic!("expected null"),
                }
            }
        """)),
        ("parse_bool", textwrap.dedent("""\
            fn main() {
                let p = JsonParser::new();
                match p.parse("true").unwrap() {
                    Value::Bool(b) => assert!(b),
                    _ => panic!("expected true"),
                }
                match p.parse("false").unwrap() {
                    Value::Bool(b) => assert!(!b),
                    _ => panic!("expected false"),
                }
            }
        """)),
        ("parse_number", textwrap.dedent("""\
            fn main() {
                let p = JsonParser::new();
                match p.parse("42").unwrap() {
                    Value::Number(n) => assert!((n - 42.0).abs() < 1e-9),
                    _ => panic!("expected number"),
                }
                match p.parse("-3.14").unwrap() {
                    Value::Number(n) => assert!((n + 3.14).abs() < 1e-9),
                    _ => panic!("expected negative float"),
                }
            }
        """)),
        ("parse_string", textwrap.dedent("""\
            fn main() {
                let p = JsonParser::new();
                match p.parse(r#""hello""#).unwrap() {
                    Value::Str(s) => assert_eq!(s, "hello"),
                    _ => panic!("expected string"),
                }
                match p.parse(r#""line\\nnext""#).unwrap() {
                    Value::Str(s) => assert_eq!(s, "line\\nnext"),
                    _ => panic!("expected escaped string"),
                }
            }
        """)),
        ("parse_array", textwrap.dedent("""\
            fn main() {
                let p = JsonParser::new();
                match p.parse("[1, 2, 3]").unwrap() {
                    Value::Array(arr) => assert_eq!(arr.len(), 3),
                    _ => panic!("expected array"),
                }
            }
        """)),
        ("parse_object", textwrap.dedent("""\
            fn main() {
                let p = JsonParser::new();
                match p.parse(r#"{"a": 1, "b": 2}"#).unwrap() {
                    Value::Object(obj) => {
                        assert_eq!(obj.len(), 2);
                        assert_eq!(obj[0].0, "a");
                    },
                    _ => panic!("expected object"),
                }
            }
        """)),
        ("parse_nested", textwrap.dedent("""\
            fn main() {
                let p = JsonParser::new();
                let input = r#"{"list": [1, {"nested": true}], "val": null}"#;
                match p.parse(input).unwrap() {
                    Value::Object(obj) => assert_eq!(obj.len(), 2),
                    _ => panic!("expected nested object"),
                }
            }
        """)),
        ("parse_whitespace", textwrap.dedent("""\
            fn main() {
                let p = JsonParser::new();
                let input = "  {  \"a\"  :  1  }  ";
                match p.parse(input).unwrap() {
                    Value::Object(obj) => assert_eq!(obj.len(), 1),
                    _ => panic!("expected object with whitespace"),
                }
            }
        """)),
        ("parse_error", textwrap.dedent("""\
            fn main() {
                let p = JsonParser::new();
                assert!(p.parse("{invalid}").is_err());
                assert!(p.parse("[1, 2,]").is_err());
                assert!(p.parse("").is_err());
            }
        """)),
    ],
    base_difficulty=75,
    spec_clarity=85,
    role_overrides={
        "fill": SmashCoord(difficulty=80, clarity=80),
    },
)

# ---------------------------------------------------------------------------
TASKS["rust-arena-alloc"] = TournamentTask(
    id="rust-arena-alloc",
    name="RustArenaAlloc",
    lang="rust",
    description=(
        "A simple arena (bump) allocator. Pre-allocates a fixed-size byte buffer, "
        "then hands out slices from it via alloc(). Supports reset() to free all "
        "allocations at once. Does NOT support individual free. "
        "Returns Result<*mut u8, String> for allocation, Err when full. "
        "Tracks bytes used and remaining."
    ),
    expected_class="Arena",
    methods=[
        "Arena::new(capacity: usize) -> Self",
        "alloc(&mut self, size: usize) -> Result<*mut u8, String>",
        "reset(&mut self)",
        "used(&self) -> usize",
        "remaining(&self) -> usize",
        "capacity(&self) -> usize",
    ],
    tests=[
        ("alloc_basic", textwrap.dedent("""\
            fn main() {
                let mut arena = Arena::new(1024);
                let ptr = arena.alloc(64).unwrap();
                assert!(!ptr.is_null());
                unsafe { *ptr = 42; }
                unsafe { assert_eq!(*ptr, 42); }
            }
        """)),
        ("alloc_fill", textwrap.dedent("""\
            fn main() {
                let mut arena = Arena::new(128);
                arena.alloc(128).unwrap();
                assert!(arena.alloc(1).is_err());
            }
        """)),
        ("reset_reuses", textwrap.dedent("""\
            fn main() {
                let mut arena = Arena::new(64);
                arena.alloc(64).unwrap();
                assert_eq!(arena.remaining(), 0);
                arena.reset();
                assert_eq!(arena.remaining(), 64);
                arena.alloc(32).unwrap();
                assert_eq!(arena.used(), 32);
            }
        """)),
        ("zero_alloc", textwrap.dedent("""\
            fn main() {
                let mut arena = Arena::new(64);
                let ptr = arena.alloc(0).unwrap();
                assert!(!ptr.is_null());
                assert_eq!(arena.used(), 0);
            }
        """)),
        ("remaining_tracking", textwrap.dedent("""\
            fn main() {
                let mut arena = Arena::new(100);
                assert_eq!(arena.remaining(), 100);
                arena.alloc(30).unwrap();
                assert_eq!(arena.remaining(), 70);
                assert_eq!(arena.used(), 30);
            }
        """)),
        ("multiple_allocs", textwrap.dedent("""\
            fn main() {
                let mut arena = Arena::new(256);
                let p1 = arena.alloc(64).unwrap();
                let p2 = arena.alloc(64).unwrap();
                assert_ne!(p1, p2);
                unsafe { *p1 = 1; *p2 = 2; }
                unsafe { assert_eq!(*p1, 1); assert_eq!(*p2, 2); }
            }
        """)),
        ("capacity_check", textwrap.dedent("""\
            fn main() {
                let arena = Arena::new(512);
                assert_eq!(arena.capacity(), 512);
                assert_eq!(arena.used(), 0);
                assert_eq!(arena.remaining(), 512);
            }
        """)),
        ("alloc_after_reset", textwrap.dedent("""\
            fn main() {
                let mut arena = Arena::new(128);
                arena.alloc(100).unwrap();
                arena.alloc(28).unwrap();
                assert!(arena.alloc(1).is_err());
                arena.reset();
                let ptr = arena.alloc(128).unwrap();
                unsafe { *ptr = 99; }
                unsafe { assert_eq!(*ptr, 99); }
                assert_eq!(arena.used(), 128);
            }
        """)),
    ],
    base_difficulty=82,
    spec_clarity=85,
    role_overrides={
        "fill": SmashCoord(difficulty=85, clarity=75),
    },
)


# ---------------------------------------------------------------------------
# LOW-CLARITY & MID-CLARITY TASK VARIANTS
# Same underlying challenge, but the spec is vague or ambiguous.
# This lets us measure the clarity axis independently of difficulty.
# ---------------------------------------------------------------------------

# --- Counter: vague (clarity 30) ----------------------------------------
# Same difficulty as counter (8), but the spec is intentionally ambiguous.
# "reset" could mean reset-to-zero or reset-to-initial.  "Count things" is
# all the model gets.  The tests still expect initial-value reset.
TASKS["counter-vague"] = TournamentTask(
    id="counter-vague",
    name="Counter",
    lang="python",
    description="Make a counter. It should count things and be resettable.",
    expected_class="Counter",
    methods=[],  # no method hints at all
    tests=[
        ("basic_increment", textwrap.dedent("""\
            c = Counter()
            c.increment()
            c.increment()
            assert c.value == 2
        """)),
        ("decrement", textwrap.dedent("""\
            c = Counter()
            c.increment()
            c.increment()
            c.decrement()
            assert c.value == 1
        """)),
        ("custom_start", textwrap.dedent("""\
            c = Counter(10)
            c.increment()
            assert c.value == 11
        """)),
        ("reset_to_initial", textwrap.dedent("""\
            c = Counter(5)
            c.increment()
            c.increment()
            c.reset()
            assert c.value == 5
        """)),
        ("no_negative", textwrap.dedent("""\
            c = Counter()
            c.decrement()
            assert c.value == 0
        """)),
    ],
    base_difficulty=8,
    spec_clarity=30,
)

# --- Counter: mid-clarity (clarity 55) ------------------------------------
# Method names given but no types, edge cases not mentioned.
TASKS["counter-mid"] = TournamentTask(
    id="counter-mid",
    name="Counter",
    lang="python",
    description=(
        "A counter class with increment, decrement, reset, and a value property. "
        "It should support starting from a custom initial value."
    ),
    expected_class="Counter",
    methods=["increment", "decrement", "reset", "value"],
    tests=[
        ("basic_increment", textwrap.dedent("""\
            c = Counter()
            c.increment()
            c.increment()
            assert c.value == 2
        """)),
        ("decrement", textwrap.dedent("""\
            c = Counter()
            c.increment()
            c.increment()
            c.decrement()
            assert c.value == 1
        """)),
        ("custom_start", textwrap.dedent("""\
            c = Counter(10)
            c.increment()
            assert c.value == 11
        """)),
        ("reset_to_initial", textwrap.dedent("""\
            c = Counter(5)
            c.increment()
            c.increment()
            c.reset()
            assert c.value == 5
        """)),
        ("no_negative", textwrap.dedent("""\
            c = Counter()
            c.decrement()
            assert c.value == 0
        """)),
    ],
    base_difficulty=8,
    spec_clarity=55,
)

# --- Rate limiter: vague (clarity 25) ------------------------------------
# No method signatures, no mention of sliding window or time tracking.
TASKS["rate-limiter-vague"] = TournamentTask(
    id="rate-limiter-vague",
    name="RateLimiter",
    lang="python",
    description=(
        "Limit how often something can be called. Should support setting "
        "a maximum number of calls in a time window."
    ),
    expected_class="RateLimiter",
    methods=[],
    tests=[
        ("basic_limiting", textwrap.dedent("""\
            rl = RateLimiter(max_calls=2, period=1.0)
            assert rl.allow() is True
            assert rl.allow() is True
            assert rl.allow() is False
        """)),
        ("reset_after_period", textwrap.dedent("""\
            import time
            rl = RateLimiter(max_calls=1, period=0.1)
            assert rl.allow() is True
            assert rl.allow() is False
            time.sleep(0.15)
            assert rl.allow() is True
        """)),
        ("remaining_count", textwrap.dedent("""\
            rl = RateLimiter(max_calls=3, period=1.0)
            assert rl.remaining() == 3
            rl.allow()
            assert rl.remaining() == 2
        """)),
        ("reset_method", textwrap.dedent("""\
            rl = RateLimiter(max_calls=2, period=1.0)
            rl.allow(); rl.allow()
            assert rl.allow() is False
            rl.reset()
            assert rl.allow() is True
        """)),
    ],
    base_difficulty=20,
    spec_clarity=25,
)

# --- Expr parser: vague (clarity 20) -------------------------------------
# Notoriously hard when vague: precedence, unary, variables — none mentioned.
TASKS["expr-parser-vague"] = TournamentTask(
    id="expr-parser-vague",
    name="ExprParser",
    lang="python",
    description="Parse and evaluate math expressions from strings.",
    expected_class="ExprParser",
    methods=[],
    tests=[
        ("basic_add", textwrap.dedent("""\
            p = ExprParser()
            assert p.evaluate("2 + 3") == 5.0
        """)),
        ("precedence", textwrap.dedent("""\
            p = ExprParser()
            assert p.evaluate("2 + 3 * 4") == 14.0
        """)),
        ("parentheses", textwrap.dedent("""\
            p = ExprParser()
            assert p.evaluate("(2 + 3) * 4") == 20.0
        """)),
        ("unary_neg", textwrap.dedent("""\
            p = ExprParser()
            assert p.evaluate("-3 + 5") == 2.0
        """)),
        ("variables", textwrap.dedent("""\
            p = ExprParser()
            p.set_variable("x", 10)
            assert p.evaluate("x * 2 + 1") == 21.0
        """)),
        ("nested_parens", textwrap.dedent("""\
            p = ExprParser()
            assert p.evaluate("((2 + 3) * (4 - 1))") == 15.0
        """)),
        ("division", textwrap.dedent("""\
            p = ExprParser()
            assert p.evaluate("10 / 4") == 2.5
        """)),
    ],
    base_difficulty=55,
    spec_clarity=20,
)

# --- Expr parser: mid-clarity (clarity 50) --------------------------------
# Operations listed, set_variable mentioned, but no types or precedence rules.
TASKS["expr-parser-mid"] = TournamentTask(
    id="expr-parser-mid",
    name="ExprParser",
    lang="python",
    description=(
        "A math expression parser that supports +, -, *, / with parentheses. "
        "Should handle variables via set_variable. Unary minus should work."
    ),
    expected_class="ExprParser",
    methods=["evaluate", "set_variable"],
    tests=[
        ("basic_add", textwrap.dedent("""\
            p = ExprParser()
            assert p.evaluate("2 + 3") == 5.0
        """)),
        ("precedence", textwrap.dedent("""\
            p = ExprParser()
            assert p.evaluate("2 + 3 * 4") == 14.0
        """)),
        ("parentheses", textwrap.dedent("""\
            p = ExprParser()
            assert p.evaluate("(2 + 3) * 4") == 20.0
        """)),
        ("unary_neg", textwrap.dedent("""\
            p = ExprParser()
            assert p.evaluate("-3 + 5") == 2.0
        """)),
        ("variables", textwrap.dedent("""\
            p = ExprParser()
            p.set_variable("x", 10)
            assert p.evaluate("x * 2 + 1") == 21.0
        """)),
        ("nested_parens", textwrap.dedent("""\
            p = ExprParser()
            assert p.evaluate("((2 + 3) * (4 - 1))") == 15.0
        """)),
        ("division", textwrap.dedent("""\
            p = ExprParser()
            assert p.evaluate("10 / 4") == 2.5
        """)),
    ],
    base_difficulty=55,
    spec_clarity=50,
)

# --- LRU cache: vague (clarity 30) ----------------------------------------
# Just "make a cache" — no mention of LRU eviction, capacity, get/put.
TASKS["lru-cache-vague"] = TournamentTask(
    id="lru-cache-vague",
    name="LRUCache",
    lang="python",
    description="A cache with a size limit. Old stuff should get evicted.",
    expected_class="LRUCache",
    methods=[],
    tests=[
        ("basic_store", textwrap.dedent("""\
            c = LRUCache(capacity=2)
            c.put("a", 1)
            assert c.get("a") == 1
        """)),
        ("eviction", textwrap.dedent("""\
            c = LRUCache(capacity=2)
            c.put("a", 1)
            c.put("b", 2)
            c.put("c", 3)
            assert c.get("a") is None
            assert c.get("b") == 2
            assert c.get("c") == 3
        """)),
        ("access_refreshes", textwrap.dedent("""\
            c = LRUCache(capacity=2)
            c.put("a", 1)
            c.put("b", 2)
            c.get("a")  # refresh a
            c.put("c", 3)  # should evict b, not a
            assert c.get("a") == 1
            assert c.get("b") is None
        """)),
        ("overwrite", textwrap.dedent("""\
            c = LRUCache(capacity=2)
            c.put("a", 1)
            c.put("a", 99)
            assert c.get("a") == 99
        """)),
        ("size_tracking", textwrap.dedent("""\
            c = LRUCache(capacity=3)
            c.put("a", 1)
            c.put("b", 2)
            assert len(c) == 2
        """)),
    ],
    base_difficulty=25,
    spec_clarity=30,
)

# --- Rust greeter: vague (clarity 25) ------------------------------------
# No mention of struct, capitalize, or language.  Just "say hello."
TASKS["rust-hello-vague"] = TournamentTask(
    id="rust-hello-vague",
    name="Greeter",
    lang="rust",
    description="A thing that greets people. It should be polite.",
    expected_class="Greeter",
    methods=[],
    tests=[
        ("basic_greet", textwrap.dedent("""\
            fn main() {
                let g = Greeter::new("English");
                assert_eq!(g.greet("Alice"), "Hello, Alice!");
            }
        """)),
        ("uppercase_name", textwrap.dedent("""\
            fn main() {
                let g = Greeter::new("English");
                assert_eq!(g.greet("bob"), "Hello, Bob!");
            }
        """)),
        ("spanish", textwrap.dedent("""\
            fn main() {
                let g = Greeter::new("Spanish");
                assert_eq!(g.greet("Carlos"), "Hola, Carlos!");
            }
        """)),
        ("french", textwrap.dedent("""\
            fn main() {
                let g = Greeter::new("French");
                assert_eq!(g.greet("Marie"), "Bonjour, Marie!");
            }
        """)),
    ],
    base_difficulty=12,
    spec_clarity=25,
)

# --- Event emitter: mid-clarity (clarity 50) ------------------------------
# Mentions on/emit/off but not once() or return values.
TASKS["event-emitter-mid"] = TournamentTask(
    id="event-emitter-mid",
    name="EventEmitter",
    lang="python",
    description=(
        "An event system. Register handlers with on(), fire them with emit(), "
        "remove with off(). Also support one-time handlers."
    ),
    expected_class="EventEmitter",
    methods=["on", "emit", "off", "once"],
    tests=[
        ("basic_emit", textwrap.dedent("""\
            results = []
            ee = EventEmitter()
            ee.on("click", lambda data: results.append(data))
            ee.emit("click", "hello")
            assert results == ["hello"]
        """)),
        ("multiple_handlers", textwrap.dedent("""\
            results = []
            ee = EventEmitter()
            ee.on("x", lambda d: results.append(1))
            ee.on("x", lambda d: results.append(2))
            ee.emit("x", None)
            assert results == [1, 2]
        """)),
        ("off_removes", textwrap.dedent("""\
            results = []
            ee = EventEmitter()
            handler = lambda d: results.append(d)
            ee.on("x", handler)
            ee.off("x", handler)
            ee.emit("x", "nope")
            assert results == []
        """)),
        ("once_fires_once", textwrap.dedent("""\
            results = []
            ee = EventEmitter()
            ee.once("x", lambda d: results.append(d))
            ee.emit("x", "a")
            ee.emit("x", "b")
            assert results == ["a"]
        """)),
        ("emit_returns_count", textwrap.dedent("""\
            ee = EventEmitter()
            ee.on("x", lambda d: None)
            ee.on("x", lambda d: None)
            assert ee.emit("x", None) == 2
        """)),
    ],
    base_difficulty=40,
    spec_clarity=50,
)

# --- CSV parser: vague (clarity 20) --------------------------------------
# "Parse CSV" is all you get.  Quoting, escaping, headers — not mentioned.
TASKS["csv-parser-vague"] = TournamentTask(
    id="csv-parser-vague",
    name="CsvParser",
    lang="python",
    description="Parse CSV data.",
    expected_class="CsvParser",
    methods=[],
    tests=[
        ("basic_parse", textwrap.dedent("""\
            p = CsvParser()
            rows = p.parse("a,b,c\\n1,2,3\\n4,5,6")
            assert rows == [["a","b","c"],["1","2","3"],["4","5","6"]]
        """)),
        ("quoted_fields", textwrap.dedent("""\
            p = CsvParser()
            rows = p.parse("name,desc\\nAlice,\\"has,comma\\"")
            assert rows[1] == ["Alice", "has,comma"]
        """)),
        ("empty_fields", textwrap.dedent("""\
            p = CsvParser()
            rows = p.parse("a,,c\\n,,")
            assert rows == [["a","","c"],["","",""]]
        """)),
        ("with_headers", textwrap.dedent("""\
            p = CsvParser(has_header=True)
            rows = p.parse("name,age\\nAlice,30\\nBob,25")
            assert rows == [{"name":"Alice","age":"30"},{"name":"Bob","age":"25"}]
        """)),
        ("custom_delimiter", textwrap.dedent("""\
            p = CsvParser(delimiter="\\t")
            rows = p.parse("a\\tb\\n1\\t2")
            assert rows == [["a","b"],["1","2"]]
        """)),
    ],
    base_difficulty=35,
    spec_clarity=20,
)

# --- MinStack: mid-clarity (clarity 55) -----------------------------------
# Methods named but no hint about O(1) min or the tricky push/pop tracking.
TASKS["min-stack-mid"] = TournamentTask(
    id="min-stack-mid",
    name="MinStack",
    lang="python",
    description=(
        "A stack that also tracks the minimum value. "
        "Should support push, pop, top, and get_min."
    ),
    expected_class="MinStack",
    methods=["push", "pop", "top", "get_min"],
    tests=[
        ("basic_push_pop", textwrap.dedent("""\
            s = MinStack()
            s.push(3)
            s.push(1)
            s.push(2)
            assert s.top() == 2
            assert s.get_min() == 1
        """)),
        ("min_after_pop", textwrap.dedent("""\
            s = MinStack()
            s.push(2)
            s.push(1)
            s.pop()
            assert s.get_min() == 2
        """)),
        ("duplicate_min", textwrap.dedent("""\
            s = MinStack()
            s.push(1)
            s.push(1)
            s.pop()
            assert s.get_min() == 1
        """)),
        ("single_element", textwrap.dedent("""\
            s = MinStack()
            s.push(42)
            assert s.top() == 42
            assert s.get_min() == 42
        """)),
        ("descending_push", textwrap.dedent("""\
            s = MinStack()
            for v in [5, 4, 3, 2, 1]:
                s.push(v)
            assert s.get_min() == 1
            s.pop(); s.pop()
            assert s.get_min() == 3
        """)),
        ("empty_raises", textwrap.dedent("""\
            s = MinStack()
            try:
                s.pop()
                assert False, "should have raised"
            except (IndexError, Exception):
                pass
        """)),
    ],
    base_difficulty=30,
    spec_clarity=55,
)


# ---------------------------------------------------------------------------
# FRONTIER TASKS — wildly underspecified, massively difficult.
#
# These separate frontier models from good-enough models.  The spec is
# intentionally one or two sentences — the model must infer the entire
# design.  Tests check structural/behavioral properties, not exact output.
#
# Assessment strategy:
#   We can't test "does this make $1bn" — but we CAN test whether the
#   model produced something *architecturally sound* for the domain:
#     - Does it compile / parse?
#     - Does it have the right abstractions? (routes, models, handlers)
#     - Do the basics actually work? (CRUD, auth, persistence)
#     - Did it handle the obvious edge case the spec didn't mention?
#
#   This is graduated scoring: 2/8 tests passing is still useful data.
#   A model that produces a working CRUD app from "build me a SaaS" is
#   clearly more capable than one that produces a hello-world.
#
# Token/time caps:
#   These tasks get max_tokens=12000 and wall_time cutoff in the runner.
#   We're testing capability, not willingness to burn money.
# ---------------------------------------------------------------------------

# --- URL shortener: vague frontier (diff=65, clarity=15) -----------------
# Well-understood problem but zero guidance on API, storage, collision handling.
TASKS["url-shortener-vague"] = TournamentTask(
    id="url-shortener-vague",
    name="URLShortener",
    lang="python",
    description="Make a URL shortener.",
    expected_class="URLShortener",
    methods=[],
    tests=[
        ("shorten_and_resolve", textwrap.dedent("""\
            s = URLShortener()
            short = s.shorten("https://example.com/very/long/path")
            assert isinstance(short, str)
            assert len(short) < 20
            assert s.resolve(short) == "https://example.com/very/long/path"
        """)),
        ("same_url_same_code", textwrap.dedent("""\
            s = URLShortener()
            a = s.shorten("https://example.com")
            b = s.shorten("https://example.com")
            assert a == b
        """)),
        ("different_urls_different_codes", textwrap.dedent("""\
            s = URLShortener()
            a = s.shorten("https://a.com")
            b = s.shorten("https://b.com")
            assert a != b
        """)),
        ("resolve_unknown", textwrap.dedent("""\
            s = URLShortener()
            result = s.resolve("nonexistent")
            assert result is None
        """)),
        ("stats_tracking", textwrap.dedent("""\
            s = URLShortener()
            code = s.shorten("https://example.com")
            s.resolve(code)
            s.resolve(code)
            stats = s.stats(code)
            assert stats["clicks"] >= 2
        """)),
        ("custom_alias", textwrap.dedent("""\
            s = URLShortener()
            code = s.shorten("https://example.com", alias="my-link")
            assert code == "my-link"
            assert s.resolve("my-link") == "https://example.com"
        """)),
    ],
    base_difficulty=65,
    spec_clarity=15,
)

# --- Task queue: vague frontier (diff=72, clarity=15) --------------------
# Needs priority, retry logic, concurrency awareness — none mentioned.
TASKS["task-queue-vague"] = TournamentTask(
    id="task-queue-vague",
    name="TaskQueue",
    lang="python",
    description="Build a task queue with priorities and retries.",
    expected_class="TaskQueue",
    methods=[],
    tests=[
        ("enqueue_dequeue", textwrap.dedent("""\
            q = TaskQueue()
            q.enqueue("task-1", payload={"x": 1})
            task = q.dequeue()
            assert task["id"] == "task-1"
            assert task["payload"] == {"x": 1}
        """)),
        ("priority_ordering", textwrap.dedent("""\
            q = TaskQueue()
            q.enqueue("low", priority=1)
            q.enqueue("high", priority=10)
            q.enqueue("mid", priority=5)
            assert q.dequeue()["id"] == "high"
            assert q.dequeue()["id"] == "mid"
            assert q.dequeue()["id"] == "low"
        """)),
        ("empty_returns_none", textwrap.dedent("""\
            q = TaskQueue()
            assert q.dequeue() is None
        """)),
        ("retry_on_fail", textwrap.dedent("""\
            q = TaskQueue()
            q.enqueue("flaky", max_retries=2)
            task = q.dequeue()
            q.fail(task["id"])
            task2 = q.dequeue()
            assert task2["id"] == "flaky"
            assert task2["retries"] == 1
        """)),
        ("max_retries_exhausted", textwrap.dedent("""\
            q = TaskQueue()
            q.enqueue("doomed", max_retries=1)
            task = q.dequeue()
            q.fail(task["id"])
            task = q.dequeue()
            q.fail(task["id"])
            # After max retries, task should be dead-lettered
            assert q.dequeue() is None
            assert len(q.dead_letter()) >= 1
        """)),
        ("complete_removes", textwrap.dedent("""\
            q = TaskQueue()
            q.enqueue("done-task")
            task = q.dequeue()
            q.complete(task["id"])
            assert q.dequeue() is None
            assert q.size() == 0
        """)),
        ("size_tracking", textwrap.dedent("""\
            q = TaskQueue()
            q.enqueue("a")
            q.enqueue("b")
            assert q.size() == 2
            q.dequeue()
            assert q.size() == 1
        """)),
    ],
    base_difficulty=72,
    spec_clarity=15,
)

# --- KV store with TTL: vague frontier (diff=60, clarity=20) -------------
# "Store stuff with expiry" — must infer TTL semantics, cleanup, defaults.
TASKS["kv-store-vague"] = TournamentTask(
    id="kv-store-vague",
    name="KVStore",
    lang="python",
    description="A key-value store where entries can expire.",
    expected_class="KVStore",
    methods=[],
    tests=[
        ("basic_set_get", textwrap.dedent("""\
            kv = KVStore()
            kv.set("key1", "value1")
            assert kv.get("key1") == "value1"
        """)),
        ("missing_key", textwrap.dedent("""\
            kv = KVStore()
            assert kv.get("nope") is None
        """)),
        ("ttl_expiry", textwrap.dedent("""\
            import time
            kv = KVStore()
            kv.set("temp", "data", ttl=0.1)
            assert kv.get("temp") == "data"
            time.sleep(0.15)
            assert kv.get("temp") is None
        """)),
        ("overwrite", textwrap.dedent("""\
            kv = KVStore()
            kv.set("k", "v1")
            kv.set("k", "v2")
            assert kv.get("k") == "v2"
        """)),
        ("delete", textwrap.dedent("""\
            kv = KVStore()
            kv.set("k", "v")
            kv.delete("k")
            assert kv.get("k") is None
        """)),
        ("keys_listing", textwrap.dedent("""\
            kv = KVStore()
            kv.set("a", 1)
            kv.set("b", 2)
            assert sorted(kv.keys()) == ["a", "b"]
        """)),
        ("no_ttl_persists", textwrap.dedent("""\
            import time
            kv = KVStore()
            kv.set("permanent", "data")
            time.sleep(0.1)
            assert kv.get("permanent") == "data"
        """)),
    ],
    base_difficulty=60,
    spec_clarity=20,
)

# --- Markdown to HTML: vague frontier (diff=68, clarity=10) ---------------
# "Convert markdown" — must figure out which subset, edge cases, nesting.
TASKS["markdown-vague"] = TournamentTask(
    id="markdown-vague",
    name="MarkdownParser",
    lang="python",
    description="Convert markdown text to HTML.",
    expected_class="MarkdownParser",
    methods=[],
    tests=[
        ("headings", textwrap.dedent("""\
            p = MarkdownParser()
            assert "<h1>Title</h1>" in p.render("# Title")
            assert "<h2>Sub</h2>" in p.render("## Sub")
        """)),
        ("bold_italic", textwrap.dedent("""\
            p = MarkdownParser()
            html = p.render("**bold** and *italic*")
            assert "<strong>bold</strong>" in html
            assert "<em>italic</em>" in html
        """)),
        ("links", textwrap.dedent("""\
            p = MarkdownParser()
            html = p.render("[click](https://example.com)")
            assert '<a href="https://example.com">click</a>' in html
        """)),
        ("code_blocks", textwrap.dedent("""\
            p = MarkdownParser()
            html = p.render("```\\ncode here\\n```")
            assert "<code>" in html or "<pre>" in html
            assert "code here" in html
        """)),
        ("unordered_list", textwrap.dedent("""\
            p = MarkdownParser()
            html = p.render("- one\\n- two\\n- three")
            assert "<ul>" in html
            assert "<li>" in html
            assert "one" in html
        """)),
        ("inline_code", textwrap.dedent("""\
            p = MarkdownParser()
            html = p.render("use `print()` here")
            assert "<code>print()</code>" in html
        """)),
        ("paragraphs", textwrap.dedent("""\
            p = MarkdownParser()
            html = p.render("first\\n\\nsecond")
            assert html.count("<p>") >= 2 or html.count("<p ") >= 2
        """)),
    ],
    base_difficulty=68,
    spec_clarity=10,
)

# --- The big one: "Build me a SaaS" (diff=95, clarity=5) -----------------
# This is deliberately absurd.  The spec gives almost nothing.
# We test: did you produce anything that remotely resembles a web app?
# This is the ultimate frontier separator — only the best models will
# produce something that passes even 2-3 of these structural checks.
TASKS["saas-vague"] = TournamentTask(
    id="saas-vague",
    name="SaaSApp",
    lang="python",
    description="Build a SaaS application backend.",
    expected_class="SaaSApp",
    methods=[],
    tests=[
        ("instantiates", textwrap.dedent("""\
            app = SaaSApp()
            assert app is not None
        """)),
        ("has_users", textwrap.dedent("""\
            app = SaaSApp()
            user = app.create_user(email="test@example.com", name="Test")
            assert user is not None
            assert "id" in user or hasattr(user, "id")
        """)),
        ("user_lookup", textwrap.dedent("""\
            app = SaaSApp()
            app.create_user(email="a@b.com", name="Alice")
            found = app.get_user(email="a@b.com")
            assert found is not None
        """)),
        ("duplicate_user_rejected", textwrap.dedent("""\
            app = SaaSApp()
            app.create_user(email="dup@test.com", name="First")
            try:
                app.create_user(email="dup@test.com", name="Second")
                assert False, "should reject duplicate email"
            except Exception:
                pass
        """)),
        ("has_resource_crud", textwrap.dedent("""\
            app = SaaSApp()
            user = app.create_user(email="u@t.com", name="U")
            uid = user["id"] if isinstance(user, dict) else user.id
            item = app.create_item(user_id=uid, name="Widget", data={"price": 9.99})
            assert item is not None
            items = app.list_items(user_id=uid)
            assert len(items) >= 1
        """)),
        ("resource_isolation", textwrap.dedent("""\
            app = SaaSApp()
            u1 = app.create_user(email="u1@t.com", name="U1")
            u2 = app.create_user(email="u2@t.com", name="U2")
            uid1 = u1["id"] if isinstance(u1, dict) else u1.id
            uid2 = u2["id"] if isinstance(u2, dict) else u2.id
            app.create_item(user_id=uid1, name="Private", data={})
            items = app.list_items(user_id=uid2)
            assert len(items) == 0
        """)),
        ("delete_item", textwrap.dedent("""\
            app = SaaSApp()
            user = app.create_user(email="d@t.com", name="D")
            uid = user["id"] if isinstance(user, dict) else user.id
            item = app.create_item(user_id=uid, name="Gone", data={})
            iid = item["id"] if isinstance(item, dict) else item.id
            app.delete_item(item_id=iid)
            items = app.list_items(user_id=uid)
            assert len(items) == 0
        """)),
    ],
    base_difficulty=95,
    spec_clarity=5,
)

# --- Rust: build me a database (diff=90, clarity=10) ----------------------
# In-memory DB with SQL-ish queries.  Absurdly underspecified.
TASKS["rust-minidb-vague"] = TournamentTask(
    id="rust-minidb-vague",
    name="MiniDB",
    lang="rust",
    description="Build a simple in-memory database that supports basic queries.",
    expected_class="MiniDB",
    methods=[],
    tests=[
        ("create_table_insert", textwrap.dedent("""\
            fn main() {
                let mut db = MiniDB::new();
                db.create_table("users", &["name", "age"]);
                db.insert("users", &["Alice", "30"]);
                db.insert("users", &["Bob", "25"]);
                let rows = db.select("users", None);
                assert_eq!(rows.len(), 2);
            }
        """)),
        ("select_with_filter", textwrap.dedent("""\
            fn main() {
                let mut db = MiniDB::new();
                db.create_table("items", &["name", "price"]);
                db.insert("items", &["Apple", "1"]);
                db.insert("items", &["Banana", "2"]);
                db.insert("items", &["Cherry", "3"]);
                let rows = db.select("items", Some(("price", "2")));
                assert_eq!(rows.len(), 1);
                assert_eq!(rows[0][0], "Banana");
            }
        """)),
        ("delete_rows", textwrap.dedent("""\
            fn main() {
                let mut db = MiniDB::new();
                db.create_table("t", &["x"]);
                db.insert("t", &["1"]);
                db.insert("t", &["2"]);
                db.delete("t", ("x", "1"));
                let rows = db.select("t", None);
                assert_eq!(rows.len(), 1);
            }
        """)),
        ("multiple_tables", textwrap.dedent("""\
            fn main() {
                let mut db = MiniDB::new();
                db.create_table("a", &["col"]);
                db.create_table("b", &["col"]);
                db.insert("a", &["x"]);
                db.insert("b", &["y"]);
                assert_eq!(db.select("a", None).len(), 1);
                assert_eq!(db.select("b", None).len(), 1);
            }
        """)),
        ("count", textwrap.dedent("""\
            fn main() {
                let mut db = MiniDB::new();
                db.create_table("t", &["v"]);
                for i in 0..10 {
                    db.insert("t", &[&i.to_string()]);
                }
                assert_eq!(db.count("t"), 10);
            }
        """)),
    ],
    base_difficulty=90,
    spec_clarity=10,
)


# ═══════════════════════════════════════════════════════════════════════════════
# CONTENDERS — models with hardware / cost / MoE metadata
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Contender:
    """A warrior entering the tournament."""
    name: str
    kind: str                          # llama-server | ollama | openrouter
    model_id: str
    endpoint: str = ""

    # Cost (per 1M tokens)
    cost_input: float = 0.0
    cost_output: float = 0.0

    # Model sizing
    params_b: float = 0.0
    active_params_b: float | None = None    # MoE: active < total
    quant: str = ""
    is_moe: bool = False
    context_k: int = 128                    # context window in thousands of tokens

    # Hardware placement
    is_local: bool = False
    is_gpu: bool = False
    power_w: float | None = None            # watts during inference

    # Throughput — measured tok/s on reference hardware (None = not yet measured)
    tok_s: float | None = None

    # Tournament roles
    roles: list[str] = field(default_factory=lambda: ["map", "fill", "oneshot"])

    club: str = "🪨"

    # Club Smash capability (computed on init)
    smash: SmashRange = field(default_factory=lambda: SmashRange(10, 30, 50, 65))

    def __post_init__(self):
        self.smash = estimate_smash_range(
            self.params_b, self.active_params_b,
            self.is_moe, self.quant,
        )
        # Estimate throughput if not explicitly measured
        if self.tok_s is None:
            self.tok_s = estimate_tok_s(
                self.params_b, self.active_params_b,
                self.is_moe, self.quant,
                self.is_local, self.is_gpu,
            )

    @property
    def effective_params(self) -> float:
        return self.active_params_b if self.active_params_b else self.params_b

    @property
    def locality(self) -> float:
        if not self.is_local:
            return 0.0
        return 1.0 if self.is_gpu else 0.7

    @property
    def cost_per_call(self) -> float:
        """Rough estimate: ~800 input + ~1200 output tokens per call."""
        return (800 * self.cost_input + 1200 * self.cost_output) / 1e6

    def tag(self) -> str:
        parts = []
        if self.is_local:
            parts.append("GPU" if self.is_gpu else "CPU")
        else:
            parts.append("cloud")
        parts.append(f"{self.params_b:.0f}B")
        if self.quant:
            parts.append(self.quant)
        if self.tok_s:
            parts.append(f"{self.tok_s:.0f}t/s")
        if self.cost_per_call == 0:
            parts.append("FREE")
        else:
            parts.append(f"${self.cost_per_call:.4f}/call")
        parts.append(f"🏏{self.smash.low}-{self.smash.sweet}-{self.smash.high} ✨{self.smash.min_clarity}+")
        return " · ".join(parts)

    def adjusted_coord(self, coord: SmashCoord) -> SmashCoord:
        """
        Adjust a task coordinate for this model's context window.

        Larger context makes the same task effectively easier — the model
        can see all code + tests + spec at once. Smaller context forces
        truncation, which increases effective difficulty.

        Baseline is 128k. Clarity stays the same — context doesn't help
        with ambiguous specs, only with fitting the whole task in view.
        """
        # token load for this task
        tokens = estimate_token_load(coord)
        ctx_tokens = self.context_k * 1024

        if tokens >= ctx_tokens * 0.9:
            # Task barely fits or overflows — difficulty spikes
            penalty = min(20, int(10 * tokens / ctx_tokens))
        elif ctx_tokens >= 512_000:
            # Very large context (512k+) — slight ease
            bonus = -5
            penalty = bonus
        elif ctx_tokens >= 128_000:
            # Standard context — no adjustment
            penalty = 0
        else:
            # Small context (<128k) — harder
            penalty = max(0, int(5 * (128_000 - ctx_tokens) / 128_000))

        return SmashCoord(
            difficulty=max(0, min(100, coord.difficulty + penalty)),
            clarity=coord.clarity,
        )

    def time_for(self, coord: SmashCoord) -> float:
        """Estimated seconds to complete a task at this coordinate."""
        return compute_time_to_complete(self.tok_s or 1.0, coord)

    def cost_for(self, coord: SmashCoord) -> dict:
        """Full cost breakdown for a task at this coordinate."""
        t = self.time_for(coord)
        energy = compute_energy_cost(t, self.power_w)
        dollar = compute_dollar_cost(coord, self.cost_input, self.cost_output)
        return {
            "time_s": t,
            "energy_j": energy,
            "cost_usd": dollar if not self.is_local else 0.0,
            "energy_cost_usd": (energy / 3_600_000 * 0.15) if energy else None,  # $0.15/kWh
        }


def build_contenders() -> list[Contender]:
    """Build the contender roster."""
    return [
        # ── Local GPU (free, fast) ──
        Contender(
            name="rnj-1:8b",
            kind="llama-server",
            model_id="EssentialAI_rnj-1-instruct-Q6_K.gguf",
            endpoint="http://127.0.0.1:8081",
            params_b=8.0, quant="q6_k", context_k=32,
            is_local=True, is_gpu=True, power_w=150,
            tok_s=45.0,
            club="⚡", roles=["map", "fill", "oneshot"],
        ),
        Contender(
            name="gemma4-26b-a4b",
            kind="llama-server",
            model_id="gemma-4-26B-A4B-it-Q8_0.gguf",
            endpoint="http://192.168.40.253:8080",
            params_b=26.0, active_params_b=4.0, quant="q8_0", context_k=128,
            is_moe=True, is_local=True, is_gpu=True,
            power_w=500,
            tok_s=85.0,
            club="🔥", roles=["map", "fill", "oneshot"],
        ),
        # ── Local CPU (free, slower) ──
        Contender(
            name="qwen2.5-coder:1.5b",
            kind="ollama",
            model_id="qwen2.5-coder:1.5b",
            endpoint="http://127.0.0.1:11434",
            params_b=1.5, quant="q4_k_m", context_k=32,
            is_local=True, is_gpu=False, power_w=100,
            club="🦴", roles=["fill"],
        ),
        Contender(
            name="qwen3-coder:30b",
            kind="ollama",
            model_id="qwen3-coder:30b",
            endpoint="http://127.0.0.1:11434",
            params_b=30.0, quant="q4_k_m", context_k=128,
            is_local=True, is_gpu=False, power_w=100,
            club="🧠", roles=["map", "fill", "oneshot"],
        ),
        # ── Cloud paid ──
        Contender(
            name="deepseek-v3",
            kind="openrouter",
            model_id="deepseek/deepseek-chat-v3-0324",
            endpoint="https://openrouter.ai/api/v1",
            cost_input=0.20, cost_output=0.77,
            params_b=685.0, active_params_b=37.0,
            is_moe=True, quant="bf16", context_k=128,
            club="💎", roles=["map", "fill", "oneshot"],
        ),
        Contender(
            name="gpt-5.4-nano",
            kind="openrouter",
            model_id="openai/gpt-5.4-nano",
            endpoint="https://openrouter.ai/api/v1",
            cost_input=0.20, cost_output=1.25,
            params_b=8.0, context_k=1024,
            club="⚛️", roles=["fill", "oneshot"],
        ),
        Contender(
            name="gpt-5.4-mini",
            kind="openrouter",
            model_id="openai/gpt-5.4-mini",
            endpoint="https://openrouter.ai/api/v1",
            cost_input=0.75, cost_output=4.50,
            params_b=30.0, context_k=1024,
            club="🔬", roles=["map", "fill", "oneshot"],
        ),
        Contender(
            name="gemini-2.5-flash",
            kind="openrouter",
            model_id="google/gemini-2.5-flash",
            endpoint="https://openrouter.ai/api/v1",
            cost_input=0.30, cost_output=2.50,
            params_b=30.0, context_k=1024,
            club="⚡", roles=["map", "fill", "oneshot"],
        ),
        Contender(
            name="codestral-2508",
            kind="openrouter",
            model_id="mistralai/codestral-2508",
            endpoint="https://openrouter.ai/api/v1",
            cost_input=0.30, cost_output=0.90,
            params_b=22.0, quant="bf16", context_k=256,
            club="🗡️", roles=["map", "fill", "oneshot"],
        ),
        Contender(
            name="llama-4-maverick",
            kind="openrouter",
            model_id="meta-llama/llama-4-maverick",
            endpoint="https://openrouter.ai/api/v1",
            cost_input=0.15, cost_output=0.60,
            params_b=400.0, active_params_b=17.0,
            is_moe=True, quant="bf16", context_k=1024,
            club="🦙", roles=["map", "fill", "oneshot"],
        ),
        # ── Cloud paid – high-end ──
        Contender(
            name="claude-sonnet-4.6",
            kind="openrouter",
            model_id="anthropic/claude-sonnet-4.6",
            endpoint="https://openrouter.ai/api/v1",
            cost_input=3.0, cost_output=15.0,
            params_b=70.0, context_k=1024,
            club="🎭", roles=["map", "fill", "oneshot"],
        ),
        Contender(
            name="claude-haiku-4.5",
            kind="openrouter",
            model_id="anthropic/claude-haiku-4.5",
            endpoint="https://openrouter.ai/api/v1",
            cost_input=1.0, cost_output=5.0,
            params_b=20.0, context_k=200,
            club="🎋", roles=["map", "fill", "oneshot"],
        ),
        Contender(
            name="gpt-5.4",
            kind="openrouter",
            model_id="openai/gpt-5.4",
            endpoint="https://openrouter.ai/api/v1",
            cost_input=2.50, cost_output=15.0,
            params_b=200.0, context_k=1024,
            club="🏛️", roles=["map", "fill", "oneshot"],
        ),
        Contender(
            name="gemini-2.5-pro",
            kind="openrouter",
            model_id="google/gemini-2.5-pro",
            endpoint="https://openrouter.ai/api/v1",
            cost_input=1.25, cost_output=10.0,
            params_b=175.0, active_params_b=50.0,
            is_moe=True, context_k=1024,
            club="🌟", roles=["map", "fill", "oneshot"],
        ),
        # ── Cloud paid – small / efficient ──
        Contender(
            name="phi-4",
            kind="openrouter",
            model_id="microsoft/phi-4",
            endpoint="https://openrouter.ai/api/v1",
            cost_input=0.07, cost_output=0.14,
            params_b=14.0, context_k=16,
            club="🔮", roles=["fill", "oneshot"],
        ),
        Contender(
            name="devstral-small",
            kind="openrouter",
            model_id="mistralai/devstral-small",
            endpoint="https://openrouter.ai/api/v1",
            cost_input=0.10, cost_output=0.30,
            params_b=24.0, context_k=128,
            club="🗡️", roles=["fill", "oneshot"],
        ),
        # ── Cloud paid – reasoning ──
        Contender(
            name="deepseek-r1",
            kind="openrouter",
            model_id="deepseek/deepseek-r1-0528",
            endpoint="https://openrouter.ai/api/v1",
            cost_input=0.50, cost_output=2.15,
            params_b=685.0, active_params_b=37.0,
            is_moe=True, quant="bf16", context_k=128,
            club="🧩", roles=["map", "oneshot"],
        ),
        Contender(
            name="deepseek-v3.1",
            kind="openrouter",
            model_id="deepseek/deepseek-chat-v3.1",
            endpoint="https://openrouter.ai/api/v1",
            cost_input=0.15, cost_output=0.75,
            params_b=685.0, active_params_b=37.0,
            is_moe=True, quant="bf16", context_k=128,
            club="💎", roles=["map", "fill", "oneshot"],
        ),
    ]


# Anthropic pricing for cost projection (per 1M tokens)
ANTHROPIC_PRICING = {
    "opus-4":     {"input": 15.0,  "output": 75.0,  "label": "Claude Opus 4"},
    "sonnet-4.6": {"input": 3.0,   "output": 15.0,  "label": "Claude Sonnet 4.6"},
    "haiku-4.5":  {"input": 1.0,   "output": 5.0,   "label": "Claude Haiku 4.5"},
}


# ═══════════════════════════════════════════════════════════════════════════════
# API CALL LAYER
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class CallResult:
    """Result of a single LLM API call."""
    content: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    elapsed_s: float = 0.0
    error: str = ""


def _openai_chat(
    endpoint: str,
    model_id: str,
    messages: list[dict],
    max_tokens: int = 2000,
    temperature: float = 0.0,
    headers: dict | None = None,
    timeout: int = 120,
    retries: int = 3,
) -> CallResult:
    """Generic OpenAI-compatible chat completion with retry/backoff."""
    base = endpoint.rstrip("/")
    if base.endswith("/v1"):
        url = f"{base}/chat/completions"
    else:
        url = f"{base}/v1/chat/completions"
    t0 = time.monotonic()
    last_err = ""
    for attempt in range(retries):
        try:
            if attempt > 0:
                time.sleep(2 ** attempt)  # 2s, 4s backoff
            hdrs = {"Content-Type": "application/json"}
            if headers:
                hdrs.update(headers)
            r = requests.post(
                url, headers=hdrs, timeout=timeout,
                json={
                    "model": model_id,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
            if r.status_code == 429 and attempt < retries - 1:
                last_err = f"429 rate-limited (attempt {attempt + 1})"
                continue
            r.raise_for_status()
            data = r.json()
            choice = data["choices"][0]
            content = choice.get("message", {}).get("content") or ""
            usage = data.get("usage", {})
            return CallResult(
                content=content,
                tokens_in=usage.get("prompt_tokens", 0),
                tokens_out=usage.get("completion_tokens", 0),
                elapsed_s=time.monotonic() - t0,
            )
        except Exception as e:
            last_err = str(e)
            if "429" not in last_err or attempt >= retries - 1:
                break
    return CallResult(error=last_err, elapsed_s=time.monotonic() - t0)


def call_model(
    contender: Contender,
    messages: list[dict],
    max_tokens: int = 2000,
) -> CallResult:
    """Route to the right backend."""
    if contender.kind == "llama-server":
        return _openai_chat(
            contender.endpoint, contender.model_id, messages,
            max_tokens=max_tokens, timeout=180,
        )
    elif contender.kind == "ollama":
        return _openai_chat(
            contender.endpoint, contender.model_id, messages,
            max_tokens=max_tokens, timeout=300,
        )
    elif contender.kind == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY", "")
        return _openai_chat(
            contender.endpoint, contender.model_id, messages,
            max_tokens=max_tokens,
            headers={
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": "https://github.com/codeclub",
            },
            timeout=120,
        )
    return CallResult(error=f"Unknown kind: {contender.kind}")


# ═══════════════════════════════════════════════════════════════════════════════
# CODE EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_code(response: str) -> str:
    """Extract code from LLM response, strip fences and thinking blocks."""
    text = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
    # Closed fence: ```python ... ``` or ```rust ... ```
    for lang in ("python", "rust", ""):
        pat = f"```{lang}\\s*\\n(.*?)```" if lang else "```\\s*\\n(.*?)```"
        m = re.search(pat, text, re.DOTALL)
        if m:
            return m.group(1).strip()
    # Unclosed fence (truncated response)
    for lang in ("python", "rust", ""):
        pat = f"```{lang}\\s*\\n(.*)" if lang else "```\\s*\\n(.*)"
        m = re.search(pat, text, re.DOTALL)
        if m:
            return m.group(1).strip()
    return text.strip()


# ═══════════════════════════════════════════════════════════════════════════════
# SCORING
# ═══════════════════════════════════════════════════════════════════════════════

def score_map(code: str, task: TournamentTask) -> dict[str, bool]:
    """Score a map (skeleton) output against the structural rubric."""
    ok_syntax, _ = task.runner.check_syntax(code)
    stub = _lang_stub(task.lang)
    return {
        "syntax":      ok_syntax,
        "has_class":   task.expected_class in code,
        "has_init":    "__init__" in code or "new(" in code or "fn new" in code,
        "has_methods": all(
            m.split("(")[0].split(".")[-1].split("::")[-1].strip() in code
            for m in task.methods
        ),
        "has_stub":    stub in code,
        "has_types":   "->" in code or ": " in code,
    }


def run_tests(
    code: str, task: TournamentTask,
) -> list[tuple[str, bool, str]]:
    """Run every test independently.  Returns [(name, passed, error)]."""
    results = []
    for name, test_code in task.tests:
        passed, err = task.runner.run_test(code, test_code)
        results.append((name, passed, err))
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# FIGHT RESULT
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class FightResult:
    """Complete record of one fight."""
    task_id: str
    mode: str                                    # "tiered" | "oneshot"
    map_model: str = ""
    fill_model: str = ""
    model: str = ""                              # oneshot

    # Quality
    tests_passed: int = 0
    tests_total: int = 0
    test_details: list[tuple[str, bool, str]] = field(default_factory=list)
    map_checks: dict = field(default_factory=dict)

    # Measured / estimated metrics
    quality: float = 0.0
    elapsed_s: float = 0.0
    cost: float = 0.0
    energy_j: float | None = None                # power_w × elapsed_s
    locality: float = 0.0

    # Token counts (for Anthropic projection)
    tokens_in: int = 0
    tokens_out: int = 0

    # Generated code
    map_code: str = ""
    fill_code: str = ""
    final_code: str = ""

    # Diagnostics
    error: str = ""                    # failure reason if quality == 0

    # Club Smash
    smash_fit: float = 0.0             # right-sizing score (0.0–1.0)
    smash_measured: int = 0            # measured smash (0–100)

    @property
    def label(self) -> str:
        if self.mode == "tiered":
            return f"{self.map_model} → {self.fill_model}"
        return self.model

    @property
    def quality_str(self) -> str:
        return f"{int(self.quality * 100)}% ({self.tests_passed}/{self.tests_total})"

    def to_dict(self) -> dict:
        return {
            "task": self.task_id, "mode": self.mode, "label": self.label,
            "quality": self.quality,
            "tests_passed": self.tests_passed, "tests_total": self.tests_total,
            "elapsed_s": round(self.elapsed_s, 2),
            "cost": round(self.cost, 6),
            "energy_j": round(self.energy_j, 1) if self.energy_j else None,
            "locality": self.locality,
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "smash_fit": round(self.smash_fit, 3),
            "smash_measured": self.smash_measured,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# PROMPTS
# ═══════════════════════════════════════════════════════════════════════════════

def _lang_label(lang: str) -> str:
    return {"python": "Python", "rust": "Rust"}.get(lang, lang.title())


def _lang_stub(lang: str) -> str:
    """Placeholder body for skeleton methods."""
    return {"python": "pass", "rust": "todo!()"}.get(lang, "pass")


def _lang_fence(lang: str) -> str:
    return {"python": "python", "rust": "rust"}.get(lang, lang)


def _map_messages(task: TournamentTask) -> list[dict]:
    lang = _lang_label(task.lang)
    stub = _lang_stub(task.lang)
    return [
        {"role": "system", "content":
         f"You are a senior software architect.  "
         f"Output ONLY a {lang} skeleton — signatures, type annotations, "
         f"and `{stub}` bodies.  NO implementation logic."},
        {"role": "user", "content": textwrap.dedent(f"""\
            Design a {lang} skeleton:

            Struct/Class: {task.expected_class}
            {task.description}

            Required methods:
            {task.method_signatures}

            Rules:
            - Include necessary stdlib imports/use statements
            - Every method body must be `{stub}` only
            - Include type annotations on all parameters and return types
            - Include a brief doc comment per method
            - Output ONLY the {lang} code
        """)},
    ]


def _fill_messages(skeleton: str, lang: str = "python") -> list[dict]:
    label = _lang_label(lang)
    stub = _lang_stub(lang)
    fence = _lang_fence(lang)
    return [
        {"role": "system", "content":
         f"You are a senior {label} developer.  "
         f"Complete the implementation.  Replace every `{stub}` with "
         f"working code.  Do NOT change signatures or add methods."},
        {"role": "user", "content":
         f"Complete this implementation.  Output ONLY the {label} code.\n\n"
         f"```{fence}\n{skeleton}\n```"},
    ]


def _oneshot_messages(task: TournamentTask) -> list[dict]:
    lang = _lang_label(task.lang)
    stub = _lang_stub(task.lang)
    return [
        {"role": "system", "content":
         f"You are a senior {lang} developer.  "
         f"Write a complete, fully-implemented {lang} struct/class."},
        {"role": "user", "content": textwrap.dedent(f"""\
            Write a complete {lang} implementation:

            Struct/Class: {task.expected_class}
            {task.description}

            Required methods:
            {task.method_signatures}

            Rules:
            - All methods must be fully implemented (no {stub}, no TODO)
            - Include necessary stdlib imports/use statements
            - No external dependencies
            - Output ONLY the {lang} code
        """)},
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# FIGHTS
# ═══════════════════════════════════════════════════════════════════════════════

def fight_tiered(
    map_c: Contender, fill_c: Contender, task: TournamentTask,
) -> FightResult:
    """Two-phase fight: map generates skeleton, fill implements."""
    result = FightResult(
        task_id=task.id, mode="tiered",
        map_model=map_c.name, fill_model=fill_c.name,
        tests_total=len(task.tests),
        locality=min(map_c.locality, fill_c.locality),
    )

    # Phase 1 — Map
    map_res = call_model(map_c, _map_messages(task), max_tokens=800)
    if map_res.error:
        result.map_checks = {"error": map_res.error}
        return result

    skeleton = extract_code(map_res.content)
    result.map_code = skeleton
    result.map_checks = score_map(skeleton, task)
    result.tokens_in += map_res.tokens_in
    result.tokens_out += map_res.tokens_out
    result.elapsed_s += map_res.elapsed_s
    result.cost += (
        map_res.tokens_in * map_c.cost_input
        + map_res.tokens_out * map_c.cost_output
    ) / 1e6

    if not result.map_checks.get("syntax"):
        return result

    # Phase 2 — Fill
    fill_res = call_model(fill_c, _fill_messages(skeleton, task.lang), max_tokens=2500)
    if fill_res.error:
        return result

    code = extract_code(fill_res.content)
    result.fill_code = code
    result.final_code = code
    result.tokens_in += fill_res.tokens_in
    result.tokens_out += fill_res.tokens_out
    result.elapsed_s += fill_res.elapsed_s
    result.cost += (
        fill_res.tokens_in * fill_c.cost_input
        + fill_res.tokens_out * fill_c.cost_output
    ) / 1e6

    ok, _ = task.runner.check_syntax(code)
    if not ok:
        return result

    test_results = run_tests(code, task)
    result.test_details = test_results
    result.tests_passed = sum(1 for _, p, _ in test_results if p)
    result.quality = (
        result.tests_passed / result.tests_total if result.tests_total else 0
    )

    # Energy
    powers = [w for w in [map_c.power_w, fill_c.power_w] if w is not None]
    if powers:
        result.energy_j = (sum(powers) / len(powers)) * result.elapsed_s

    # Club Smash — right-sizing
    map_fit = map_c.smash.fit(task.coord_for("map"))
    fill_fit = fill_c.smash.fit(task.coord_for("fill"))
    result.smash_fit = min(map_fit, fill_fit)
    result.smash_measured = measured_smash(
        result.quality, result.elapsed_s, result.smash_fit,
    )

    return result


def fight_oneshot(contender: Contender, task: TournamentTask,
                  max_tokens: int = 4000) -> FightResult:
    """Single model does everything in one call."""
    result = FightResult(
        task_id=task.id, mode="oneshot",
        model=contender.name,
        tests_total=len(task.tests),
        locality=contender.locality,
    )

    res = call_model(contender, _oneshot_messages(task), max_tokens=max_tokens)
    if res.error:
        result.error = f"api_error: {res.error}"
        return result

    code = extract_code(res.content)
    result.final_code = code
    result.tokens_in = res.tokens_in
    result.tokens_out = res.tokens_out
    result.elapsed_s = res.elapsed_s
    result.cost = (
        res.tokens_in * contender.cost_input
        + res.tokens_out * contender.cost_output
    ) / 1e6

    ok, syntax_err = task.runner.check_syntax(code)
    if not ok:
        result.error = f"syntax_error: {syntax_err[:200]}"
        return result

    test_results = run_tests(code, task)
    result.test_details = test_results
    result.tests_passed = sum(1 for _, p, _ in test_results if p)
    result.quality = (
        result.tests_passed / result.tests_total if result.tests_total else 0
    )

    if contender.power_w is not None:
        result.energy_j = contender.power_w * result.elapsed_s

    # Club Smash — right-sizing
    result.smash_fit = contender.smash.fit(task.coord_for("oneshot"))
    result.smash_measured = measured_smash(
        result.quality, result.elapsed_s, result.smash_fit,
    )

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINT HEALTH CHECK
# ═══════════════════════════════════════════════════════════════════════════════

def check_endpoints(contenders: list[Contender]) -> list[Contender]:
    """Ping each endpoint, return only reachable contenders."""
    available = []
    for c in contenders:
        try:
            if c.kind in ("llama-server", "ollama"):
                base = c.endpoint.rstrip("/")
                url = f"{base}/v1/models" if not base.endswith("/v1") else f"{base}/models"
                r = requests.get(url, timeout=3)
                if r.ok:
                    available.append(c)
                    continue
            elif c.kind == "openrouter":
                if os.environ.get("OPENROUTER_API_KEY"):
                    available.append(c)
                    continue
        except Exception:
            pass
        print(f"  ⚠️  {c.name} — unreachable, skipping")
    return available


# ═══════════════════════════════════════════════════════════════════════════════
# TOURNAMENT RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

def _sort_contenders(contenders: list[Contender]) -> list[Contender]:
    """Sort contenders: local GPU free → local CPU free → cloud free → cloud paid."""
    def key(c: Contender) -> tuple:
        return (
            0 if c.is_local and c.is_gpu else 1 if c.is_local else 2,
            c.cost_per_call,
            c.effective_params,    # smaller = faster for same-tier
        )
    return sorted(contenders, key=key)


def run_task(
    task: TournamentTask,
    contenders: list[Contender],
    weights: FitnessWeights,
    quick: bool = False,
) -> list[tuple[FightResult, float]]:
    """Run all fights for one task, return scored+ranked results."""
    results: list[FightResult] = []

    mc = task.coord_for("map")
    fc = task.coord_for("fill")
    oc = task.coord_for("oneshot")
    print(f"\n{'━' * 60}")
    print(f"  🏔️  TASK: {task.name}  ({task.lang}, {len(task.tests)} tests)")
    print(f"  🏏  base={task.base_difficulty}"
          f"  map={mc}  fill={fc}  oneshot={oc}")
    print(f"{'━' * 60}")

    # Sort by fitness preference
    ordered = _sort_contenders(contenders)
    map_models = [c for c in ordered if "map" in c.roles]
    fill_models = [c for c in ordered if "fill" in c.roles]

    # ── Tiered fights (smart pairing) ──
    champion_found = False
    for mc in map_models:
        for fc in fill_models:
            print(f"\n  ⚔️  Tiered: {mc.club} {mc.name} → {fc.club} {fc.name}")
            r = fight_tiered(mc, fc, task)
            results.append(r)
            _print_fight_result(r)
            if quick and r.quality >= 1.0:
                champion_found = True
                break
        if quick and champion_found:
            break

    # ── Oneshot fights ──
    oneshot_models = [c for c in ordered if "oneshot" in c.roles]
    for c in oneshot_models:
        print(f"\n  🗡️  Oneshot: {c.club} {c.name}")
        r = fight_oneshot(c, task)
        results.append(r)
        _print_fight_result(r)

    # Score and rank
    scored = compute_fitness(results, weights)
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _print_fight_result(r: FightResult) -> None:
    """Print result of a single fight with error details."""
    if r.map_checks.get("error"):
        print(f"     💥 API error: {str(r.map_checks['error'])[:80]}")
        return
    icon = "🏆" if r.quality >= 1.0 else "💀"
    smash_icon = "🏏" if r.smash_measured >= 70 else "🪵" if r.smash_measured >= 40 else "💤"
    print(f"     {icon} {r.quality_str}  ⏱ {r.elapsed_s:.1f}s"
          f"  💰${r.cost:.6f}  {smash_icon}{r.smash_measured}")
    if r.quality < 1.0 and r.test_details:
        failed = [(n, e) for n, p, e in r.test_details if not p]
        if failed:
            names = [n for n, _ in failed]
            print(f"     ❌ Failed: {', '.join(names)}")
            # Show first error for debugging
            print(f"        → {failed[0][1][:100]}")
    elif r.quality < 1.0 and r.tests_passed == 0 and r.tests_total > 0:
        # No tests ran at all — likely syntax/extraction issue
        if r.final_code:
            ok, err = r.test_details[0][2] if r.test_details else ("", "")
            print(f"     ⚠️  Code generated but no tests passed (syntax or extraction issue)")


def print_results_table(
    task: TournamentTask,
    scored: list[tuple[FightResult, float]],
) -> None:
    """Pretty-print the ranked results table."""
    print(f"\n{'═' * 86}")
    print(f"  📊  RESULTS: {task.name}")
    print(f"{'═' * 86}")
    hdr = (f"  {'Mode':<8} {'Model(s)':<28} {'Quality':>8}"
           f" {'Speed':>7} {'Cost':>9} {'🏏':>4} {'Energy':>8} {'Fit':>6}")
    print(hdr)
    print(f"  {'─' * 8} {'─' * 28} {'─' * 8}"
          f" {'─' * 7} {'─' * 9} {'─' * 4} {'─' * 8} {'─' * 6}")

    for i, (r, fit) in enumerate(scored):
        crown = "🏆" if i == 0 and fit > 0 else "  "
        mode = r.mode[:7]
        label = r.label[:27]
        qual = r.quality_str
        speed = f"{r.elapsed_s:.1f}s"
        cost = f"${r.cost:.4f}" if r.cost > 0 else "FREE"
        smash = f"{r.smash_measured:>3}"
        energy = f"{r.energy_j:.0f}J" if r.energy_j else "n/a"
        fitness = f"{fit:.3f}" if fit >= 0 else "FAIL"
        print(f"{crown}{mode:<8} {label:<28} {qual:>8}"
              f" {speed:>7} {cost:>9} {smash:>4} {energy:>8} {fitness:>6}")


# ═══════════════════════════════════════════════════════════════════════════════
# ANTHROPIC COST PROJECTION
# ═══════════════════════════════════════════════════════════════════════════════

def anthropic_projection(results: list[FightResult]) -> None:
    """Project measured token counts onto Anthropic pricing."""
    tiered = [r for r in results if r.mode == "tiered" and r.quality >= 1.0]
    if not tiered:
        print("\n  ⚠️  No tiered champions — can't project Anthropic costs")
        return

    # Use champion's average token counts as reference
    avg_in = sum(r.tokens_in for r in tiered) // len(tiered)
    avg_out = sum(r.tokens_out for r in tiered) // len(tiered)
    map_in, map_out = avg_in // 2, avg_out // 2
    fill_in, fill_out = avg_in - map_in, avg_out - map_out

    haiku = ANTHROPIC_PRICING["haiku-3.5"]
    sonnet = ANTHROPIC_PRICING["sonnet-4.6"]
    opus = ANTHROPIC_PRICING["opus-4"]

    rows = []
    # codeclub tiered: sonnet map + haiku fill
    cc = (
        (map_in * sonnet["input"] + map_out * sonnet["output"]
         + fill_in * haiku["input"] + fill_out * haiku["output"]) / 1e6
    )
    rows.append(("codeclub (sonnet→haiku)", cc))

    # Sonnet oneshot
    s_cost = (avg_in * sonnet["input"] + avg_out * sonnet["output"]) / 1e6
    rows.append(("Sonnet 4.6 oneshot", s_cost))

    # Opus oneshot
    o_cost = (avg_in * opus["input"] + avg_out * opus["output"]) / 1e6
    rows.append(("Opus 4 oneshot", o_cost))

    base = rows[0][1] if rows[0][1] > 0 else 1e-9

    print(f"\n{'═' * 72}")
    print(f"  💰  ANTHROPIC COST PROJECTION  ({avg_in}+{avg_out} tokens)")
    print(f"{'═' * 72}")
    print(f"  {'Approach':<36} {'Cost/call':>10} {'Relative':>10}")
    print(f"  {'─' * 36} {'─' * 10} {'─' * 10}")
    for label, cost in rows:
        rel = f"{cost / base:.1f}×"
        print(f"  {label:<36} ${cost:>8.4f} {rel:>10}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="🏔️ Caveman Model Tournament — models fight with clubs",
    )
    parser.add_argument(
        "--optimize", choices=list(PRESETS), default="balanced",
        help="Fitness preset (default: balanced)",
    )
    parser.add_argument(
        "--task", choices=list(TASKS),
        help="Run single task (default: all)",
    )
    parser.add_argument(
        "--quick", action="store_true",
        help="Stop at first champion per task",
    )
    parser.add_argument(
        "--json", metavar="FILE",
        help="Export results to JSON",
    )
    parser.add_argument(
        "--anthropic", action="store_true",
        help="Show Anthropic cost projection",
    )
    parser.add_argument(
        "--map", action="store_true",
        help="Show efficiency maps (turbo compressor–style) for each contender",
    )
    args = parser.parse_args()

    weights = PRESETS[args.optimize]

    # Map-only mode: just show the efficiency maps, no fights
    if args.map and not args.task and not args.json:
        contenders = check_endpoints(build_contenders())
        # Collect all task oneshot coords for overlay
        tc: dict[str, SmashCoord] = {}
        for tid, task in TASKS.items():
            tc[tid] = task.coord_for("oneshot")
        print_efficiency_maps(contenders, task_coords=tc)
        return

    print("\n🏔️  CAVEMAN MODEL TOURNAMENT  🏔️")
    print(f"   Optimising for: {args.optimize}")
    print(f"   Weights: cost={weights.cost}  speed={weights.speed}"
          f"  power={weights.power}  locality={weights.locality}")

    # Health check
    print("\n📡  Checking endpoints...")
    contenders = check_endpoints(build_contenders())
    print(f"   {len(contenders)} warriors ready\n")
    for c in contenders:
        print(f"   {c.club} {c.name:<25} {c.tag()}")

    # Select tasks
    tasks = [TASKS[args.task]] if args.task else list(TASKS.values())

    all_scored: list[tuple[FightResult, float]] = []
    all_results: list[FightResult] = []

    for task in tasks:
        scored = run_task(task, contenders, weights, quick=args.quick)
        print_results_table(task, scored)
        all_scored.extend(scored)
        all_results.extend(r for r, _ in scored)

    # Efficiency maps
    if args.map:
        tc: dict[str, SmashCoord] = {}
        for task in tasks:
            tc[task.id] = task.coord_for("oneshot")
        print_efficiency_maps(contenders, task_coords=tc)

    # Anthropic projection
    if args.anthropic or not args.task:
        anthropic_projection(all_results)

    # JSON export
    if args.json:
        export = {
            "optimize": args.optimize,
            "weights": weights.as_dict(),
            "results": [r.to_dict() for r, _ in all_scored],
        }
        with open(args.json, "w") as f:
            json.dump(export, f, indent=2)
        print(f"\n  📁  Exported to {args.json}")

    # Summary
    champions = [(r, s) for r, s in all_scored if r.quality >= 1.0]
    print(f"\n{'═' * 72}")
    print(f"  🏆  TOURNAMENT COMPLETE")
    print(f"  {len(champions)} champions from {len(all_scored)} fights")
    if champions:
        best = champions[0]
        print(f"  👑  Overall best: {best[0].label}  (fitness {best[1]:.3f})")
    print(f"{'═' * 72}\n")


if __name__ == "__main__":
    main()
