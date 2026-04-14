"""
accounting/tracker.py — Per-task ledger for all measurable quantities.

Records every phase of a codeclub task run:
  - wallclock time
  - tokens in/out per phase
  - energy consumed (measured or estimated)
  - cost in USD (cloud rate for remote models, energy cost for local)
  - model switching overhead

Attach a TaskLedger to LoopResult.ledger. The ledger is the authoritative
record for reporting and savings calculations.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .power import EnergySnapshot, EnergyDelta, read_energy, estimate_energy_j


# ---------------------------------------------------------------------------
# Per-phase record
# ---------------------------------------------------------------------------

@dataclass
class PhaseRecord:
    """One inference phase within a task run."""
    phase: str              # "spec" | "map" | "fill" | "testgen" | "review" | "report"
    model_id: str
    provider: str           # "openrouter" | "ollama" | "llama-server" | "anthropic" | "github"

    tokens_in:  int = 0
    tokens_out: int = 0

    wall_s:   float = 0.0   # elapsed wallclock seconds
    model_wait_s: float = 0.0  # time blocked on inference (≤ wall_s)

    # Energy (joules). Measured if hardware counters available, otherwise estimated.
    energy_j: float = 0.0
    energy_measured: bool = False  # True = from hwmon/RAPL, False = TDP estimate

    # Cloud cost (USD). 0 for local models.
    api_cost_usd: float = 0.0

    # Switching overhead: time to load this model if it wasn't already resident
    switch_cost_s: float = 0.0

    success: bool = True
    error: str = ""

    @property
    def is_local(self) -> bool:
        return self.provider in ("ollama", "llama-server")

    @property
    def energy_kwh(self) -> float:
        return self.energy_j / 3_600_000

    def energy_cost_usd(self, rate_per_kwh: float = 0.15) -> float:
        return self.energy_kwh * rate_per_kwh

    def total_cost_usd(self, electricity_rate: float = 0.15) -> float:
        """Cloud cost + energy cost (for local, only energy cost applies)."""
        return self.api_cost_usd + self.energy_cost_usd(electricity_rate)

    @property
    def tps(self) -> Optional[float]:
        """Observed tokens/sec during this phase."""
        if self.model_wait_s > 0 and self.tokens_out > 0:
            return self.tokens_out / self.model_wait_s
        return None


# ---------------------------------------------------------------------------
# Task ledger
# ---------------------------------------------------------------------------

class TaskLedger:
    """
    Accumulates all accounting data for a single dev loop run.

    Usage in loop.py:

        ledger = TaskLedger(task=task, electricity_rate=0.15)

        t0, e0 = time.time(), read_energy()
        spec = decompose(task, context, call_fn=_spec_fn)
        ledger.add(
            phase="spec", model_id=spec_model_id, provider=spec_provider,
            tokens_in=..., tokens_out=...,
            wall_s=time.time()-t0, energy_start=e0,
        )
    """

    def __init__(
        self,
        task: str = "",
        electricity_rate: float = 0.15,  # USD/kWh
        device_name: str = "",           # for TDP estimates
    ) -> None:
        self.task = task
        self.electricity_rate = electricity_rate
        self.device_name = device_name
        self.phases: list[PhaseRecord] = []
        self.started_at: float = time.time()
        self._last_model_id: str = ""  # for switch cost detection

    def add(
        self,
        phase: str,
        model_id: str,
        provider: str,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        wall_s: float = 0.0,
        model_wait_s: float = 0.0,
        energy_start: Optional[EnergySnapshot] = None,
        energy_end: Optional[EnergySnapshot] = None,
        api_cost_per_million_in: float = 0.0,
        api_cost_per_million_out: float = 0.0,
        success: bool = True,
        error: str = "",
    ) -> PhaseRecord:
        """
        Record a completed phase.

        If energy_start and energy_end are provided, actual energy is computed
        from the delta. Otherwise, falls back to TDP estimate.

        api_cost_per_million_in/out: provider rate for cloud models (0 for local).
        """
        # Energy
        energy_j = 0.0
        measured = False
        if energy_start is not None and energy_end is not None:
            delta: EnergyDelta = energy_start.delta(energy_end)
            total = delta.total_j
            if total is not None and total > 0:
                energy_j = total
                measured = True
        if not measured and wall_s > 0 and self.device_name:
            energy_j = estimate_energy_j(self.device_name, wall_s)

        # Cloud API cost
        api_cost = (
            tokens_in  * api_cost_per_million_in  / 1_000_000
          + tokens_out * api_cost_per_million_out / 1_000_000
        )

        # Model switch cost (if model changed since last phase)
        switch_s = 0.0
        if self._last_model_id and self._last_model_id != model_id:
            # We don't measure actual load time here — callers can pass it explicitly.
            # For now, switch_cost_s stays 0 unless patched by the caller.
            pass
        self._last_model_id = model_id

        rec = PhaseRecord(
            phase=phase,
            model_id=model_id,
            provider=provider,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            wall_s=wall_s,
            model_wait_s=model_wait_s or wall_s,
            energy_j=energy_j,
            energy_measured=measured,
            api_cost_usd=api_cost,
            switch_cost_s=switch_s,
            success=success,
            error=error,
        )
        self.phases.append(rec)
        return rec

    def record_switch(self, from_model: str, to_model: str, load_s: float) -> None:
        """Record model-switching overhead between phases."""
        if self.phases:
            self.phases[-1].switch_cost_s = load_s

    # ---------------------------------------------------------------------------
    # Aggregates
    # ---------------------------------------------------------------------------

    @property
    def total_wall_s(self) -> float:
        return time.time() - self.started_at

    @property
    def total_tokens_in(self) -> int:
        return sum(p.tokens_in for p in self.phases)

    @property
    def total_tokens_out(self) -> int:
        return sum(p.tokens_out for p in self.phases)

    @property
    def total_tokens(self) -> int:
        return self.total_tokens_in + self.total_tokens_out

    @property
    def total_energy_j(self) -> float:
        return sum(p.energy_j for p in self.phases)

    @property
    def total_energy_kwh(self) -> float:
        return self.total_energy_j / 3_600_000

    def total_api_cost_usd(self) -> float:
        return sum(p.api_cost_usd for p in self.phases)

    def total_energy_cost_usd(self) -> float:
        return self.total_energy_kwh * self.electricity_rate

    def total_cost_usd(self) -> float:
        return self.total_api_cost_usd() + self.total_energy_cost_usd()

    def total_switch_s(self) -> float:
        return sum(p.switch_cost_s for p in self.phases)

    def phases_by(self, phase: str) -> list[PhaseRecord]:
        return [p for p in self.phases if p.phase == phase]

    def any_measured(self) -> bool:
        """True if at least one phase had real energy measurement (not estimate)."""
        return any(p.energy_measured for p in self.phases)

    # ---------------------------------------------------------------------------
    # Display
    # ---------------------------------------------------------------------------

    def summary(self, *, verbose: bool = False) -> str:
        lines = []
        total_wall = self.total_wall_s
        total_tok = self.total_tokens
        api_cost = self.total_api_cost_usd()
        e_cost = self.total_energy_cost_usd()
        total_cost = api_cost + e_cost
        measured = self.any_measured()

        lines.append(f"  ┌─ Accounting {'─'*46}")
        lines.append(f"  │  Wallclock   {total_wall:.1f}s")
        lines.append(f"  │  Tokens      {total_tok:,} ({self.total_tokens_in:,} in + {self.total_tokens_out:,} out)")

        if self.total_energy_j > 0:
            energy_marker = "" if measured else " (est.)"
            lines.append(f"  │  Energy      {self.total_energy_kwh*1000:.4f} Wh{energy_marker}")
            lines.append(f"  │  Energy $    ${e_cost:.6f}  (@${self.electricity_rate}/kWh)")

        if api_cost > 0:
            lines.append(f"  │  API cost    ${api_cost:.6f}")

        lines.append(f"  │  Total $     ${total_cost:.6f}")

        sw = self.total_switch_s()
        if sw > 0:
            lines.append(f"  │  Switching   {sw:.1f}s model load overhead")

        if verbose and self.phases:
            lines.append(f"  │")
            lines.append(f"  │  {'Phase':<10} {'Model':<30} {'Tok↑':>6} {'Tok↓':>6} {'Wall':>6} {'Cost':>10}")
            for p in self.phases:
                cost_str = f"${p.total_cost_usd(self.electricity_rate):.5f}"
                model_short = p.model_id.split("/")[-1][:28]
                lines.append(
                    f"  │  {p.phase:<10} {model_short:<30} "
                    f"{p.tokens_in:>6} {p.tokens_out:>6} "
                    f"{p.wall_s:>5.1f}s {cost_str:>10}"
                )

        lines.append(f"  └{'─'*51}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialisable representation for storage/reporting."""
        return {
            "task": self.task,
            "total_wall_s": self.total_wall_s,
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "total_energy_kwh": self.total_energy_kwh,
            "total_api_cost_usd": self.total_api_cost_usd(),
            "total_energy_cost_usd": self.total_energy_cost_usd(),
            "total_cost_usd": self.total_cost_usd(),
            "electricity_rate": self.electricity_rate,
            "energy_measured": self.any_measured(),
            "phases": [
                {
                    "phase": p.phase,
                    "model_id": p.model_id,
                    "provider": p.provider,
                    "tokens_in": p.tokens_in,
                    "tokens_out": p.tokens_out,
                    "wall_s": p.wall_s,
                    "energy_j": p.energy_j,
                    "energy_measured": p.energy_measured,
                    "api_cost_usd": p.api_cost_usd,
                    "switch_cost_s": p.switch_cost_s,
                    "success": p.success,
                    "tps": p.tps,
                }
                for p in self.phases
            ],
        }
