"""
accounting/power.py — Energy and power measurement.

Reads from the best available source for each hardware type:

  Intel Arc (xe driver)  → /sys/class/hwmon/hwmon*/energy2_input  (microjoules, no root needed)
  NVIDIA                 → nvidia-smi --query-gpu=power.draw       (watts, live)
  AMD CPU / Intel CPU    → RAPL via /sys/class/powercap/           (requires root — graceful skip)
  AMD GPU (amdgpu)       → hwmon power1_input if exposed
  Fallback               → TDP × utilisation estimate × duration

All public functions return floats in SI units (joules, watts). Callers
convert to kWh / cost as needed.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Snapshot — point-in-time energy reading across all sources
# ---------------------------------------------------------------------------

@dataclass
class EnergySnapshot:
    """
    Point-in-time energy readings in joules.

    Take two snapshots and subtract to get energy consumed in that interval.
    Sources that couldn't be read are None.
    """
    timestamp: float = field(default_factory=time.time)

    # GPU sources (joules since some epoch — diff two readings)
    gpu_xe_j:     Optional[float] = None   # Intel Arc via xe hwmon energy2 (pkg)
    gpu_nvidia_j: Optional[float] = None   # NVIDIA via nvidia-smi cumul energy
    gpu_amdgpu_j: Optional[float] = None   # AMD GPU via amdgpu hwmon

    # CPU sources
    cpu_rapl_j:   Optional[float] = None   # RAPL package energy (root needed)

    # Instantaneous power (watts, not cumulative — can't diff)
    gpu_nvidia_w: Optional[float] = None   # nvidia-smi power draw right now

    def delta(self, other: "EnergySnapshot") -> "EnergyDelta":
        """Return energy consumed between self (start) and other (end)."""
        def _diff(a, b):
            if a is None or b is None:
                return None
            d = b - a
            # Energy counters wrap on some hardware; ignore negative diffs
            return d if d >= 0 else None

        elapsed = other.timestamp - self.timestamp
        return EnergyDelta(
            elapsed_s=elapsed,
            gpu_xe_j=_diff(self.gpu_xe_j, other.gpu_xe_j),
            gpu_nvidia_j=_diff(self.gpu_nvidia_j, other.gpu_nvidia_j),
            gpu_amdgpu_j=_diff(self.gpu_amdgpu_j, other.gpu_amdgpu_j),
            cpu_rapl_j=_diff(self.cpu_rapl_j, other.cpu_rapl_j),
            gpu_nvidia_w=other.gpu_nvidia_w,
        )


@dataclass
class EnergyDelta:
    """Energy consumed between two EnergySnapshots."""
    elapsed_s: float = 0.0
    gpu_xe_j:     Optional[float] = None
    gpu_nvidia_j: Optional[float] = None
    gpu_amdgpu_j: Optional[float] = None
    cpu_rapl_j:   Optional[float] = None
    gpu_nvidia_w: Optional[float] = None  # last-known watts (not integrated)

    @property
    def gpu_j(self) -> Optional[float]:
        """Best available GPU energy in joules."""
        return self.gpu_xe_j or self.gpu_nvidia_j or self.gpu_amdgpu_j

    @property
    def cpu_j(self) -> Optional[float]:
        return self.cpu_rapl_j

    @property
    def total_j(self) -> Optional[float]:
        """Sum of all measured energy sources, or None if nothing was measured."""
        parts = [x for x in (self.gpu_j, self.cpu_j) if x is not None]
        return sum(parts) if parts else None

    @property
    def total_kwh(self) -> Optional[float]:
        j = self.total_j
        return j / 3_600_000 if j is not None else None

    def cost_usd(self, rate_per_kwh: float = 0.15) -> Optional[float]:
        """Energy cost in USD at the given electricity rate (default $0.15/kWh)."""
        kwh = self.total_kwh
        return kwh * rate_per_kwh if kwh is not None else None

    def avg_gpu_watts(self) -> Optional[float]:
        """Average GPU power draw over this interval."""
        j = self.gpu_j
        if j is not None and self.elapsed_s > 0:
            return j / self.elapsed_s
        return self.gpu_nvidia_w  # fallback to instantaneous

    def summary(self) -> str:
        parts = []
        if self.gpu_j is not None:
            parts.append(f"gpu={self.gpu_j:.1f}J")
        if self.cpu_j is not None:
            parts.append(f"cpu={self.cpu_j:.1f}J")
        total = self.total_j
        if total is not None:
            kwh = total / 3_600_000
            parts.append(f"total={total:.1f}J ({kwh*1000:.4f}Wh)")
        w = self.avg_gpu_watts()
        if w is not None:
            parts.append(f"~{w:.0f}W avg")
        return ", ".join(parts) if parts else "no measurement"


# ---------------------------------------------------------------------------
# Reader: hwmon discovery
# ---------------------------------------------------------------------------

_HWMON_ROOT = Path("/sys/class/hwmon")

def _hwmon_dirs() -> list[Path]:
    try:
        return sorted(_HWMON_ROOT.iterdir())
    except Exception:
        return []


def _read_uj(path: Path) -> Optional[float]:
    """Read a microjoule counter from a sysfs path, return joules."""
    try:
        return int(path.read_text().strip()) / 1_000_000
    except Exception:
        return None


def _read_uw(path: Path) -> Optional[float]:
    """Read a microwatt value from a sysfs path, return watts."""
    try:
        return int(path.read_text().strip()) / 1_000_000
    except Exception:
        return None


def _read_xe_energy() -> Optional[float]:
    """
    Intel Arc GPU (xe driver) — energy2_input labeled 'pkg' in microjoules.
    No root required on most distros.
    """
    for d in _hwmon_dirs():
        try:
            if (d / "name").read_text().strip() == "xe":
                # Prefer 'pkg' (energy2) over 'card' (energy1) — more comprehensive
                pkg = _read_uj(d / "energy2_input")
                if pkg is not None:
                    return pkg
                return _read_uj(d / "energy1_input")
        except Exception:
            continue
    return None


def _read_amdgpu_energy() -> Optional[float]:
    """AMD GPU (amdgpu driver) — energy1_input in microjoules if exposed."""
    for d in _hwmon_dirs():
        try:
            if (d / "name").read_text().strip() == "amdgpu":
                return _read_uj(d / "energy1_input")
        except Exception:
            continue
    return None


def _read_amdgpu_power_w() -> Optional[float]:
    """AMD GPU instantaneous power draw in watts."""
    for d in _hwmon_dirs():
        try:
            if (d / "name").read_text().strip() == "amdgpu":
                return _read_uw(d / "power1_input")
        except Exception:
            continue
    return None


def _read_rapl_energy() -> Optional[float]:
    """
    CPU RAPL package energy via powercap (requires read permission).
    Works on Intel CPUs natively; AMD exposes via same interface on some kernels.
    Returns None on permission error rather than raising.
    """
    try:
        p = Path("/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj")
        return _read_uj(p)
    except Exception:
        return None


def _read_nvidia_energy() -> Optional[float]:
    """
    NVIDIA GPU cumulative energy via nvidia-smi.
    NOTE: nvidia-smi reports in mWh; we convert to joules.
    Returns None if nvidia-smi unavailable or no GPU.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=energy.consumption", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            val = result.stdout.strip().split("\n")[0].strip()
            if val and val != "[N/A]":
                mwh = float(val)
                return mwh * 3.6  # mWh → J (1 Wh = 3600 J, 1 mWh = 3.6 J)
    except Exception:
        pass
    return None


def _read_nvidia_power_w() -> Optional[float]:
    """NVIDIA instantaneous power draw in watts."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            val = result.stdout.strip().split("\n")[0].strip()
            if val and val != "[N/A]":
                return float(val)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_energy() -> EnergySnapshot:
    """
    Take a point-in-time energy snapshot from all available sources.

    Call twice (before and after an operation), then use `.delta()` to
    get energy consumed.

    Fast: reads from sysfs directly. The nvidia-smi call is skipped if
    no NVIDIA GPU was detected on the previous call (cached after first use).
    """
    return EnergySnapshot(
        timestamp=time.time(),
        gpu_xe_j=_read_xe_energy(),
        gpu_nvidia_j=_read_nvidia_energy(),
        gpu_amdgpu_j=_read_amdgpu_energy(),
        cpu_rapl_j=_read_rapl_energy(),
        gpu_nvidia_w=_read_nvidia_power_w(),
    )


# ---------------------------------------------------------------------------
# TDP estimates (fallback when no measurement available)
# ---------------------------------------------------------------------------

# Rough TDP estimates by GPU/CPU name substring (watts)
_TDP_TABLE: list[tuple[str, float]] = [
    # Intel Arc
    ("b580", 190.0), ("b770", 225.0),
    ("a770", 225.0), ("a750", 225.0), ("a580", 185.0),
    # NVIDIA
    ("4090", 450.0), ("4080", 320.0), ("4070 ti", 285.0),
    ("4070 super", 220.0), ("4070", 200.0),
    ("4060 ti", 165.0), ("4060", 115.0),
    ("3090", 350.0), ("3080", 320.0), ("3070", 220.0),
    ("3060", 170.0),
    # AMD GPU
    ("7900 xtx", 355.0), ("7900 xt", 315.0),
    ("7800 xt", 263.0), ("7700 xt", 245.0),
    ("6900 xt", 300.0), ("6800 xt", 300.0),
    # AMD CPU
    ("9950x", 170.0), ("9900x", 120.0), ("9700x", 65.0),
    ("9600x", 65.0), ("7950x", 170.0), ("7700x", 105.0),
    ("7600x", 105.0),
    # Intel CPU
    ("i9-14", 125.0), ("i7-14", 65.0), ("i5-14", 65.0),
    ("i9-13", 125.0), ("i7-13", 65.0), ("i5-13", 65.0),
]


def tdp_estimate_w(device_name: str) -> float:
    """Estimate TDP in watts from a device name string. Returns 100.0 if unknown."""
    name = device_name.lower()
    for substr, tdp in _TDP_TABLE:
        if substr in name:
            return tdp
    return 100.0  # conservative generic fallback


def estimate_energy_j(
    device_name: str,
    duration_s: float,
    utilisation: float = 0.70,
) -> float:
    """
    Estimate energy consumed when direct measurement isn't available.

    utilisation: fraction of TDP actually drawn (0.70 is a typical inference estimate).
    Returns joules.
    """
    tdp = tdp_estimate_w(device_name)
    return tdp * utilisation * duration_s


# ---------------------------------------------------------------------------
# Convenience: GPU power cap (maximum allowed power)
# ---------------------------------------------------------------------------

def gpu_power_cap_w() -> Optional[float]:
    """Read the configured GPU power cap from hwmon (xe driver)."""
    for d in _hwmon_dirs():
        try:
            if (d / "name").read_text().strip() == "xe":
                val = (d / "power1_cap").read_text().strip()
                return int(val) / 1_000_000  # μW → W
        except Exception:
            continue
    return None


if __name__ == "__main__":
    s1 = read_energy()
    time.sleep(1)
    s2 = read_energy()
    d = s1.delta(s2)
    print(f"1-second energy sample: {d.summary()}")
    cap = gpu_power_cap_w()
    if cap:
        print(f"GPU power cap: {cap:.0f}W")
