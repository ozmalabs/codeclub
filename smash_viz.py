#!/usr/bin/env python3
"""
🏏 Club Smash Efficiency Maps — turbo compressor–style visualisation.

Three renderers:
  • matplotlib  → publication-quality PNGs (single + overlay)
  • plotly      → interactive HTML with hover, toggle, overlay
  • ASCII       → terminal fallback (lives in tournament.py)

Usage:
    python smash_viz.py                     # all models, PNG + HTML
    python smash_viz.py --model rnj-1:8b    # single model
    python smash_viz.py --html              # HTML only
    python smash_viz.py --png               # PNG only
    python smash_viz.py --overlay           # combined overlay map
    python smash_viz.py --compare "rnj-1:8b,gpt-4.1-nano"  # compare specific models
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np

# Import the smash system from tournament
from tournament import (
    ROLE_DEFAULTS,
    TASKS,
    SmashCoord,
    SmashRange,
    build_contenders,
    check_endpoints,
    compute_efficiency_surface,
    estimate_smash_range,
    estimate_token_load,
)

# Output directory
OUT_DIR = Path("benchmarks/maps")


# ═══════════════════════════════════════════════════════════════════════════════
# GRID COMPUTATION — shared by all renderers
# ═══════════════════════════════════════════════════════════════════════════════

def compute_efficiency_grid(
    smash: SmashRange,
    d_range: tuple[int, int] = (0, 100),
    c_range: tuple[int, int] = (0, 100),
    resolution: int = 200,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Compute a 2D efficiency grid for a SmashRange.

    Returns (difficulties, clarities, efficiency_matrix) where
    efficiency_matrix[c_idx, d_idx] = fit score 0.0–1.0.
    """
    difficulties = np.linspace(d_range[0], d_range[1], resolution)
    clarities = np.linspace(c_range[0], c_range[1], resolution)
    grid = np.zeros((resolution, resolution))

    for ci, c in enumerate(clarities):
        for di, d in enumerate(difficulties):
            grid[ci, di] = smash.fit(SmashCoord(difficulty=int(d), clarity=int(c)))

    return difficulties, clarities, grid


def get_task_coords(role: str = "oneshot") -> dict[str, SmashCoord]:
    """Get all task coordinates for a given role."""
    return {tid: task.coord_for(role) for tid, task in TASKS.items()}


# ═══════════════════════════════════════════════════════════════════════════════
# MATPLOTLIB RENDERER — publication-quality PNGs
# ═══════════════════════════════════════════════════════════════════════════════

# Caveman-friendly colour palette
_CMAP_NAME = "RdYlGn"  # red (bad) → yellow (ok) → green (peak)
_CONTOUR_LEVELS = [0.3, 0.5, 0.7, 0.85, 0.95]
_CONTOUR_LABELS = ["waste", "weak", "ok", "high", "peak"]

# Distinct colours for multi-model overlay
_MODEL_COLOURS = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990",
    "#dcbeff", "#9A6324", "#800000", "#aaffc3", "#808000",
]


def _strip_emoji(text: str) -> str:
    """Strip emoji for matplotlib which can't render them in most fonts."""
    import re
    return re.sub(
        r'[\U0001F300-\U0001FAFF\U00002600-\U000027BF\u2B50\uFE0F]', '', text
    ).strip()


def _setup_matplotlib():
    """Configure matplotlib for dark-themed plots."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "figure.facecolor": "#1a1a2e",
        "axes.facecolor": "#16213e",
        "axes.edgecolor": "#e0e0e0",
        "axes.labelcolor": "#e0e0e0",
        "text.color": "#e0e0e0",
        "xtick.color": "#e0e0e0",
        "ytick.color": "#e0e0e0",
        "grid.color": "#2a2a4a",
        "grid.alpha": 0.5,
        "font.family": "monospace",
        "font.size": 11,
    })
    return plt


def render_single_png(
    smash: SmashRange,
    name: str,
    task_coords: dict[str, SmashCoord] | None = None,
    outfile: str | Path | None = None,
    resolution: int = 200,
) -> Path:
    """Render a single model's efficiency map as a PNG."""
    plt = _setup_matplotlib()
    from matplotlib.colors import LinearSegmentedColormap

    d, c, grid = compute_efficiency_grid(smash, resolution=resolution)

    fig, ax = plt.subplots(figsize=(10, 8))

    # Custom colourmap: black → red → orange → yellow → green → bright green
    colours = ["#0d0d0d", "#8b0000", "#cc4400", "#ddaa00", "#66bb6a", "#00e676"]
    cmap = LinearSegmentedColormap.from_list("smash", colours, N=256)

    # Filled contour
    cf = ax.contourf(d, c, grid, levels=50, cmap=cmap, vmin=0, vmax=1)
    cbar = fig.colorbar(cf, ax=ax, label="Efficiency", shrink=0.85)
    cbar.ax.yaxis.label.set_color("#e0e0e0")
    cbar.ax.tick_params(colors="#e0e0e0")

    # Contour lines
    cs = ax.contour(
        d, c, grid, levels=_CONTOUR_LEVELS,
        colors="white", linewidths=0.8, alpha=0.6,
    )
    ax.clabel(cs, inline=True, fontsize=8, fmt={
        lv: lb for lv, lb in zip(_CONTOUR_LEVELS, _CONTOUR_LABELS)
    })

    # Sweet spot marker
    ax.plot(
        smash.sweet, max(smash.min_clarity, 50),
        marker="*", markersize=18, color="#ffd700",
        markeredgecolor="white", markeredgewidth=1.5, zorder=10,
    )

    # Task overlays
    if task_coords:
        for tid, coord in task_coords.items():
            fit = smash.fit(coord)
            colour = "#00e676" if fit >= 0.7 else "#ffab00" if fit >= 0.5 else "#ff1744"
            ax.scatter(
                coord.difficulty, coord.clarity,
                s=80, color=colour, edgecolors="white",
                linewidths=1.2, zorder=8,
            )
            label = f"{tid} ({coord.difficulty}d,{coord.clarity}c)"
            ax.annotate(
                label, (coord.difficulty, coord.clarity),
                xytext=(6, 6), textcoords="offset points",
                fontsize=7, color="white", weight="bold",
                bbox=dict(boxstyle="round,pad=0.2", facecolor="#000000", alpha=0.6),
            )

    # Capability boundary box
    ax.axvline(smash.low, color="#ff6666", linestyle="--", alpha=0.5, linewidth=1)
    ax.axvline(smash.high, color="#ff6666", linestyle="--", alpha=0.5, linewidth=1)
    ax.axhline(smash.min_clarity, color="#6688ff", linestyle="--", alpha=0.5, linewidth=1)

    ax.set_xlabel("Task Difficulty →", fontsize=13)
    ax.set_ylabel("Task Clarity ↑", fontsize=13)
    ax.set_title(f"Club Smash: {_strip_emoji(name)}", fontsize=15, pad=12)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.grid(True, linewidth=0.3)

    # Annotations
    ax.text(
        2, 2, f"Range: {smash.low}–{smash.high}d  |  Min clarity: {smash.min_clarity}c",
        fontsize=9, color="#aaaaaa", va="bottom",
    )

    if outfile is None:
        safe_name = _strip_emoji(name).replace(" ", "_").replace(":", "-").replace("/", "-")
        outfile = OUT_DIR / f"{safe_name}.png"

    outfile = Path(outfile)
    outfile.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(outfile, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return outfile


def render_overlay_png(
    models: list[tuple[str, SmashRange]],
    task_coords: dict[str, SmashCoord] | None = None,
    outfile: str | Path | None = None,
    resolution: int = 200,
) -> Path:
    """Render overlaid contour boundaries for multiple models on one chart."""
    plt = _setup_matplotlib()

    fig, ax = plt.subplots(figsize=(12, 9))

    for i, (name, smash) in enumerate(models):
        d, c, grid = compute_efficiency_grid(smash, resolution=resolution)
        colour = _MODEL_COLOURS[i % len(_MODEL_COLOURS)]

        # 50% efficiency boundary (usable region)
        ax.contour(
            d, c, grid, levels=[0.5],
            colors=[colour], linewidths=2.0, alpha=0.9,
        )
        # 85% efficiency boundary (high-efficiency island)
        ax.contour(
            d, c, grid, levels=[0.85],
            colors=[colour], linewidths=1.2, linestyles="--", alpha=0.7,
        )
        # Sweet spot
        ax.plot(
            smash.sweet, max(smash.min_clarity, 50),
            marker="*", markersize=14, color=colour,
            markeredgecolor="white", markeredgewidth=1.0, zorder=10,
        )
        # Label at sweet spot
        ax.annotate(
            name, (smash.sweet, max(smash.min_clarity, 50)),
            xytext=(8, 8), textcoords="offset points",
            fontsize=9, color=colour, weight="bold",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="#000000", alpha=0.7),
        )

    # Task markers
    if task_coords:
        for tid, coord in task_coords.items():
            ax.scatter(
                coord.difficulty, coord.clarity,
                s=100, marker="D", color="#ffd700", edgecolors="white",
                linewidths=1.5, zorder=12,
            )
            ax.annotate(
                tid, (coord.difficulty, coord.clarity),
                xytext=(8, -12), textcoords="offset points",
                fontsize=9, color="#ffd700", weight="bold",
                bbox=dict(boxstyle="round,pad=0.2", facecolor="#000000", alpha=0.7),
            )

    # Role regions (background shading)
    for role, defaults in ROLE_DEFAULTS.items():
        clarity = defaults["clarity"]
        ax.axhline(
            clarity, color="#444466", linestyle=":", alpha=0.3, linewidth=0.5,
        )
        ax.text(98, clarity + 1, role, fontsize=7, color="#666688", ha="right")

    ax.set_xlabel("Task Difficulty →", fontsize=13)
    ax.set_ylabel("Task Clarity ↑", fontsize=13)
    ax.set_title("Club Smash — Model Efficiency Overlay", fontsize=15, pad=12)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.grid(True, linewidth=0.3)

    # Legend: solid = usable, dashed = high
    from matplotlib.lines import Line2D
    legend_elements = []
    for i, (name, _) in enumerate(models):
        colour = _MODEL_COLOURS[i % len(_MODEL_COLOURS)]
        legend_elements.append(Line2D([0], [0], color=colour, linewidth=2, label=name))
    legend_elements.append(Line2D(
        [0], [0], color="white", linewidth=1.2,
        linestyle="--", alpha=0.7, label="85% efficiency",
    ))
    legend_elements.append(Line2D(
        [0], [0], color="white", linewidth=2.0,
        label="50% efficiency",
    ))
    ax.legend(
        handles=legend_elements, loc="lower right",
        fontsize=9, facecolor="#1a1a2e", edgecolor="#444",
        labelcolor="#e0e0e0",
    )

    if outfile is None:
        outfile = OUT_DIR / "overlay.png"
    outfile = Path(outfile)
    outfile.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(outfile, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return outfile


def render_efficiency_png(
    name: str,
    smash: SmashRange,
    tok_s: float,
    power_w: float | None = None,
    task_coords: dict[str, SmashCoord] | None = None,
    outfile: str | Path | None = None,
    resolution: int = 200,
) -> Path:
    """
    Render a TRUE compressor map: time-to-complete as the heat dimension.

    Unlike the capability map (which just shows CAN it do it?), this shows
    HOW FAST it does it. The peak efficiency island is where the model is
    both capable AND fast — the real sweet spot for routing.
    """
    plt = _setup_matplotlib()
    from matplotlib.colors import LinearSegmentedColormap

    d, c, time_grid, eff_grid = compute_efficiency_surface(
        smash, tok_s, resolution=resolution,
    )

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(20, 8))

    # Left panel: Time-to-complete (lower = better, so invert colormap)
    time_cmap = LinearSegmentedColormap.from_list(
        "time", ["#00e676", "#66bb6a", "#ddaa00", "#cc4400", "#8b0000", "#2a0000"], N=256,
    )
    # Mask zero values (model can't do these)
    import numpy as np
    masked_time = np.ma.masked_equal(time_grid, 0.0)
    cf1 = ax1.contourf(d, c, masked_time, levels=30, cmap=time_cmap)
    cbar1 = fig.colorbar(cf1, ax=ax1, label="Time (seconds)", shrink=0.85)
    cbar1.ax.yaxis.label.set_color("#e0e0e0")
    cbar1.ax.tick_params(colors="#e0e0e0")

    # Right panel: Combined efficiency (capability × speed)
    eff_colours = ["#0d0d0d", "#1a0033", "#4400cc", "#0066ff", "#00ccaa", "#00e676"]
    eff_cmap = LinearSegmentedColormap.from_list("eff", eff_colours, N=256)
    cf2 = ax2.contourf(d, c, eff_grid, levels=50, cmap=eff_cmap, vmin=0, vmax=1)
    cbar2 = fig.colorbar(cf2, ax=ax2, label="Efficiency (capability / time)", shrink=0.85)
    cbar2.ax.yaxis.label.set_color("#e0e0e0")
    cbar2.ax.tick_params(colors="#e0e0e0")

    # Contour lines on efficiency panel
    cs = ax2.contour(d, c, eff_grid, levels=[0.3, 0.5, 0.7, 0.85, 0.95],
                     colors="white", linewidths=0.8, alpha=0.6)
    ax2.clabel(cs, inline=True, fontsize=8, fmt={
        0.3: "low", 0.5: "ok", 0.7: "good", 0.85: "high", 0.95: "peak",
    })

    for ax in (ax1, ax2):
        # Sweet spot
        ax.plot(smash.sweet, max(smash.min_clarity, 50),
                marker="*", markersize=16, color="#ffd700",
                markeredgecolor="white", markeredgewidth=1.5, zorder=10)
        # Capability boundaries
        ax.axvline(smash.low, color="#ff6666", linestyle="--", alpha=0.4, linewidth=1)
        ax.axvline(smash.high, color="#ff6666", linestyle="--", alpha=0.4, linewidth=1)
        ax.axhline(smash.min_clarity, color="#6688ff", linestyle="--", alpha=0.4, linewidth=1)
        ax.set_xlabel("Task Difficulty →", fontsize=12)
        ax.set_ylabel("Task Clarity ↑", fontsize=12)
        ax.set_xlim(0, 100)
        ax.set_ylim(0, 100)
        ax.grid(True, linewidth=0.3)

        # Task overlays
        if task_coords:
            for tid, coord in task_coords.items():
                fit = smash.fit(coord)
                colour = "#00e676" if fit >= 0.7 else "#ffab00" if fit >= 0.5 else "#ff1744"
                ax.scatter(coord.difficulty, coord.clarity,
                           s=60, color=colour, edgecolors="white",
                           linewidths=1.0, zorder=8)
                ax.annotate(tid, (coord.difficulty, coord.clarity),
                            xytext=(5, 5), textcoords="offset points",
                            fontsize=7, color="white", weight="bold",
                            bbox=dict(boxstyle="round,pad=0.2",
                                      facecolor="#000000", alpha=0.6))

    safe = _strip_emoji(name)
    ax1.set_title(f"{safe} — Time to Complete", fontsize=13, pad=10)
    ax2.set_title(f"{safe} — Efficiency (capability x speed)", fontsize=13, pad=10)

    # Throughput and power annotation
    info = f"{tok_s:.0f} tok/s"
    if power_w:
        info += f" · {power_w:.0f}W"
    fig.suptitle(f"Club Smash Compressor Map: {safe}  [{info}]",
                 fontsize=15, y=1.02, color="#e0e0e0")

    if outfile is None:
        safe_name = safe.replace(" ", "_").replace(":", "-").replace("/", "-")
        outfile = OUT_DIR / f"{safe_name}_efficiency.png"
    outfile = Path(outfile)
    outfile.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(outfile, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return outfile


# ═══════════════════════════════════════════════════════════════════════════════
# PLOTLY RENDERER — interactive HTML with hover + toggle
# ═══════════════════════════════════════════════════════════════════════════════

def render_single_html(
    smash: SmashRange,
    name: str,
    task_coords: dict[str, SmashCoord] | None = None,
    outfile: str | Path | None = None,
    resolution: int = 150,
) -> Path:
    """Render a single model's efficiency map as interactive HTML."""
    import plotly.graph_objects as go

    d, c, grid = compute_efficiency_grid(smash, resolution=resolution)

    fig = go.Figure()

    # Heatmap with contours
    fig.add_trace(go.Contour(
        z=grid, x=d, y=c,
        colorscale=[
            [0.0, "#0d0d0d"], [0.2, "#8b0000"], [0.4, "#cc4400"],
            [0.6, "#ddaa00"], [0.8, "#66bb6a"], [1.0, "#00e676"],
        ],
        contours=dict(
            showlines=True,
            start=0.1, end=1.0, size=0.05,
        ),
        line=dict(width=0.5, color="rgba(255,255,255,0.2)"),
        colorbar=dict(
            title=dict(text="Efficiency", font=dict(color="#e0e0e0")),
            tickfont=dict(color="#e0e0e0"),
        ),
        hovertemplate=(
            "Difficulty: %{x:.0f}<br>"
            "Clarity: %{y:.0f}<br>"
            "Efficiency: %{z:.2f}<extra></extra>"
        ),
        name="efficiency",
    ))

    # Contour lines at key thresholds
    for level, label in zip(_CONTOUR_LEVELS, _CONTOUR_LABELS):
        fig.add_trace(go.Contour(
            z=grid, x=d, y=c,
            contours=dict(
                type="constraint", operation="=", value=level,
            ),
            line=dict(width=1.5, color="rgba(255,255,255,0.5)"),
            showscale=False,
            name=label,
            hovertemplate=f"{label} boundary<extra></extra>",
        ))

    # Sweet spot
    fig.add_trace(go.Scatter(
        x=[smash.sweet], y=[max(smash.min_clarity, 50)],
        mode="markers+text",
        marker=dict(size=18, color="#ffd700", symbol="star",
                    line=dict(width=2, color="white")),
        text=["★ sweet"], textposition="top right",
        textfont=dict(color="#ffd700", size=11),
        name="sweet spot",
        hovertemplate=(
            f"Sweet spot<br>Difficulty: {smash.sweet}<br>"
            f"Min clarity: {smash.min_clarity}<extra></extra>"
        ),
    ))

    # Task markers
    if task_coords:
        for tid, coord in task_coords.items():
            fit = smash.fit(coord)
            colour = "#00e676" if fit >= 0.7 else "#ffab00" if fit >= 0.5 else "#ff1744"
            fig.add_trace(go.Scatter(
                x=[coord.difficulty], y=[coord.clarity],
                mode="markers+text",
                marker=dict(size=12, color=colour, symbol="diamond",
                            line=dict(width=1.5, color="white")),
                text=[tid], textposition="top right",
                textfont=dict(color="white", size=10),
                name=f"{tid} (fit:{fit:.0%})",
                hovertemplate=(
                    f"{tid}<br>"
                    f"Difficulty: {coord.difficulty}<br>"
                    f"Clarity: {coord.clarity}<br>"
                    f"Fit: {fit:.1%}<extra></extra>"
                ),
            ))

    # Capability boundaries
    fig.add_vline(x=smash.low, line=dict(color="#ff6666", dash="dash", width=1))
    fig.add_vline(x=smash.high, line=dict(color="#ff6666", dash="dash", width=1))
    fig.add_hline(y=smash.min_clarity, line=dict(color="#6688ff", dash="dash", width=1))

    fig.update_layout(
        title=dict(text=f"🏏 {name}", font=dict(size=18)),
        xaxis=dict(title="Task Difficulty →", range=[0, 100], gridcolor="#2a2a4a"),
        yaxis=dict(title="Task Clarity ↑", range=[0, 100], gridcolor="#2a2a4a"),
        plot_bgcolor="#16213e",
        paper_bgcolor="#1a1a2e",
        font=dict(color="#e0e0e0"),
        legend=dict(bgcolor="rgba(0,0,0,0.5)", font=dict(size=10)),
        width=900, height=750,
    )

    if outfile is None:
        safe_name = _strip_emoji(name).replace(" ", "_").replace(":", "-").replace("/", "-")
        outfile = OUT_DIR / f"{safe_name}.html"
    outfile = Path(outfile)
    outfile.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(outfile), include_plotlyjs="cdn")
    return outfile


def render_overlay_html(
    models: list[tuple[str, SmashRange]],
    task_coords: dict[str, SmashCoord] | None = None,
    outfile: str | Path | None = None,
    resolution: int = 150,
) -> Path:
    """Render an interactive overlay of multiple models with toggle visibility."""
    import plotly.graph_objects as go

    fig = go.Figure()

    for i, (name, smash) in enumerate(models):
        d, c, grid = compute_efficiency_grid(smash, resolution=resolution)
        colour = _MODEL_COLOURS[i % len(_MODEL_COLOURS)]

        # Filled region at 50% threshold
        fig.add_trace(go.Contour(
            z=grid, x=d, y=c,
            contours=dict(
                type="constraint", operation=">", value=0.50,
            ),
            fillcolor=f"rgba({_hex_to_rgb(colour)}, 0.15)",
            line=dict(width=2.5, color=colour),
            showscale=False,
            name=f"{name} (usable)",
            hovertemplate=(
                f"{name}<br>"
                "Difficulty: %{x:.0f}<br>"
                "Clarity: %{y:.0f}<br>"
                f"Efficiency: %{{z:.2f}}<extra></extra>"
            ),
        ))

        # 85% island boundary
        fig.add_trace(go.Contour(
            z=grid, x=d, y=c,
            contours=dict(
                type="constraint", operation=">", value=0.85,
            ),
            fillcolor=f"rgba({_hex_to_rgb(colour)}, 0.25)",
            line=dict(width=1.5, color=colour, dash="dash"),
            showscale=False,
            name=f"{name} (peak)",
            hovertemplate=(
                f"{name} peak zone<br>"
                "Difficulty: %{x:.0f}<br>"
                "Clarity: %{y:.0f}<br>"
                f"Efficiency: %{{z:.2f}}<extra></extra>"
            ),
        ))

        # Sweet spot
        fig.add_trace(go.Scatter(
            x=[smash.sweet], y=[max(smash.min_clarity, 50)],
            mode="markers",
            marker=dict(size=16, color=colour, symbol="star",
                        line=dict(width=2, color="white")),
            name=f"{name} ★",
            hovertemplate=(
                f"{name} sweet spot<br>"
                f"Difficulty: {smash.sweet}<br>"
                f"Min clarity: {smash.min_clarity}<extra></extra>"
            ),
            legendgroup=name,
        ))

    # Task diamonds
    if task_coords:
        for tid, coord in task_coords.items():
            fig.add_trace(go.Scatter(
                x=[coord.difficulty], y=[coord.clarity],
                mode="markers+text",
                marker=dict(size=14, color="#ffd700", symbol="diamond",
                            line=dict(width=2, color="white")),
                text=[tid], textposition="top right",
                textfont=dict(color="#ffd700", size=11),
                name=f"📋 {tid}",
                hovertemplate=(
                    f"{tid}<br>"
                    f"Difficulty: {coord.difficulty}<br>"
                    f"Clarity: {coord.clarity}<extra></extra>"
                ),
            ))

    # Role reference lines
    for role, defaults in ROLE_DEFAULTS.items():
        fig.add_hline(
            y=defaults["clarity"],
            line=dict(color="#444466", dash="dot", width=0.5),
            annotation_text=role, annotation_position="right",
            annotation_font=dict(size=9, color="#666688"),
        )

    fig.update_layout(
        title=dict(text="🏏 Model Efficiency Overlay — Compressor Map", font=dict(size=18)),
        xaxis=dict(title="Task Difficulty →", range=[0, 100], gridcolor="#2a2a4a"),
        yaxis=dict(title="Task Clarity ↑", range=[0, 100], gridcolor="#2a2a4a"),
        plot_bgcolor="#16213e",
        paper_bgcolor="#1a1a2e",
        font=dict(color="#e0e0e0"),
        legend=dict(
            bgcolor="rgba(0,0,0,0.6)", font=dict(size=10),
            itemclick="toggle", itemdoubleclick="toggleothers",
        ),
        width=1000, height=800,
    )

    if outfile is None:
        outfile = OUT_DIR / "overlay.html"
    outfile = Path(outfile)
    outfile.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(outfile), include_plotlyjs="cdn")
    return outfile


def _hex_to_rgb(hex_colour: str) -> str:
    """Convert #rrggbb to 'r, g, b' string for rgba()."""
    h = hex_colour.lstrip("#")
    return f"{int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}"


# ═══════════════════════════════════════════════════════════════════════════════
# QUANTIZATION COMPARISON — same architecture, different quants/sizes
# ═══════════════════════════════════════════════════════════════════════════════

def render_quant_comparison(
    base_name: str,
    params_b: float,
    quants: list[str],
    *,
    active_params_b: float | None = None,
    is_moe: bool = False,
    outfile_png: str | Path | None = None,
    outfile_html: str | Path | None = None,
) -> list[Path]:
    """
    Compare different quantizations of the same model.
    Renders both PNG overlay and interactive HTML.
    """
    models = []
    for q in quants:
        smash = estimate_smash_range(params_b, active_params_b, is_moe, q)
        label = f"{base_name} ({q})"
        models.append((label, smash))

    task_coords = get_task_coords("oneshot")
    files = []

    safe = base_name.replace(" ", "_").replace(":", "-").replace("/", "-")
    png_out = outfile_png or OUT_DIR / f"quant_{safe}.png"
    html_out = outfile_html or OUT_DIR / f"quant_{safe}.html"

    files.append(render_overlay_png(models, task_coords, png_out))
    files.append(render_overlay_html(models, task_coords, html_out))
    return files


def render_size_comparison(
    family_name: str,
    sizes: list[tuple[str, float]],
    quant: str = "bf16",
    outfile_png: str | Path | None = None,
    outfile_html: str | Path | None = None,
) -> list[Path]:
    """
    Compare different sizes of the same model family.
    sizes: list of (label, params_b) tuples.
    """
    models = []
    for label, params_b in sizes:
        smash = estimate_smash_range(params_b, quant=quant)
        models.append((label, smash))

    task_coords = get_task_coords("oneshot")
    files = []

    safe = family_name.replace(" ", "_").replace(":", "-").replace("/", "-")
    png_out = outfile_png or OUT_DIR / f"sizes_{safe}.png"
    html_out = outfile_html or OUT_DIR / f"sizes_{safe}.html"

    files.append(render_overlay_png(models, task_coords, png_out))
    files.append(render_overlay_html(models, task_coords, html_out))
    return files


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="🏏 Club Smash Efficiency Maps — turbo compressor–style",
    )
    parser.add_argument(
        "--model", help="Single model name to render",
    )
    parser.add_argument(
        "--compare", help="Comma-separated model names to compare",
    )
    parser.add_argument(
        "--png", action="store_true", help="PNG output only",
    )
    parser.add_argument(
        "--html", action="store_true", help="HTML output only",
    )
    parser.add_argument(
        "--overlay", action="store_true", help="Render overlay map",
    )
    parser.add_argument(
        "--quant-compare", metavar="MODEL",
        help="Compare quantizations of a model (e.g. 'gemma4-26b')",
    )
    parser.add_argument(
        "--no-endpoints", action="store_true",
        help="Skip endpoint health checks (use estimated specs only)",
    )
    parser.add_argument(
        "--efficiency", action="store_true",
        help="Render TRUE efficiency maps (time-to-complete + capability×speed)",
    )
    args = parser.parse_args()

    # Default: both formats
    do_png = args.png or (not args.png and not args.html)
    do_html = args.html or (not args.png and not args.html)

    # Build contenders
    contenders = build_contenders()
    if not args.no_endpoints:
        print("📡  Checking endpoints...")
        contenders = check_endpoints(contenders)
    print(f"   {len(contenders)} models loaded\n")

    task_coords = get_task_coords("oneshot")

    # Filter models
    if args.compare:
        names = [n.strip() for n in args.compare.split(",")]
        contenders = [c for c in contenders if c.name in names]
        if not contenders:
            print(f"❌  No matching models for: {args.compare}")
            sys.exit(1)
    elif args.model:
        contenders = [c for c in contenders if c.name == args.model]
        if not contenders:
            print(f"❌  Model not found: {args.model}")
            sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []

    # Quantization comparison
    if args.quant_compare:
        quants = ["bf16", "q8_0", "q6_k", "q5_k_m", "q4_k_m", "q3_k_m", "q2_k"]
        # Find the base model params
        match = [c for c in build_contenders() if args.quant_compare in c.name]
        if match:
            params = match[0].params_b
            active = match[0].active_params_b
            moe = match[0].is_moe
        else:
            print(f"❌  Model not found: {args.quant_compare}")
            sys.exit(1)

        print(f"  🏏  Quantization comparison: {args.quant_compare} ({params:.0f}B)")
        files = render_quant_comparison(
            args.quant_compare, params, quants,
            active_params_b=active, is_moe=moe,
        )
        generated.extend(files)
        for f in files:
            print(f"  ✅  {f}")
        print()

    # Individual model maps
    if not args.overlay or args.model:
        for c in contenders:
            label = f"{c.club} {c.name}"
            print(f"  🏏  Rendering {label}...")
            if do_png:
                f = render_single_png(c.smash, label, task_coords)
                generated.append(f)
                print(f"      PNG: {f}")
            if do_html:
                f = render_single_html(c.smash, label, task_coords)
                generated.append(f)
                print(f"      HTML: {f}")
            # Efficiency maps (true compressor maps)
            if args.efficiency and do_png:
                f = render_efficiency_png(
                    label, c.smash, c.tok_s or 50.0,
                    power_w=c.power_w, task_coords=task_coords,
                )
                generated.append(f)
                print(f"      EFF:  {f}")

    # Overlay map
    if args.overlay or (not args.model and not args.compare):
        models = [(c.name, c.smash) for c in contenders]
        print(f"\n  🏏  Rendering overlay ({len(models)} models)...")
        if do_png:
            f = render_overlay_png(models, task_coords)
            generated.append(f)
            print(f"      PNG: {f}")
        if do_html:
            f = render_overlay_html(models, task_coords)
            generated.append(f)
            print(f"      HTML: {f}")

    # Size comparison example: GPT-4.1 family
    if not args.model and not args.compare:
        print("\n  🏏  Bonus: GPT-4.1 size comparison...")
        files = render_size_comparison("GPT-4.1", [
            ("GPT-4.1 nano (8B)", 8),
            ("GPT-4.1 mini (30B)", 30),
            ("GPT-4.1 (200B est)", 200),
        ])
        generated.extend(files)
        for f in files:
            print(f"      {f}")

        print("\n  🏏  Bonus: Gemma4 quantization comparison...")
        files = render_quant_comparison(
            "gemma4-26b-a4b", 26, ["bf16", "q8_0", "q6_k", "q4_k_m", "q2_k"],
            active_params_b=4, is_moe=True,
        )
        generated.extend(files)
        for f in files:
            print(f"      {f}")

    print(f"\n  📁  {len(generated)} files generated in {OUT_DIR}/")


if __name__ == "__main__":
    main()
