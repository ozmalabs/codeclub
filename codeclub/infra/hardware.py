"""
hardware.py — Hardware inventory, endpoint registry, and model fitting.

Tells the router what is actually available on your machine (or across
machines). Works with any mix of GPUs, CPUs, and inference endpoints —
local or remote.

Declare your hardware once; the router handles the rest.

Declaration (recommended):
    setup = HardwareSetup.from_dict({
        "devices": [
            {"name": "Intel Arc B580", "vram_mb": 12288, "backend": "sycl",
             "endpoint": "http://localhost:8081"},
            {"name": "NVIDIA RTX 3080", "vram_mb": 10240, "backend": "cuda",
             "endpoint": "http://192.168.1.10:8081"},
        ],
        "ram_mb": 32768,
        "ollama_url": "http://localhost:11434",
        # Optional: bare remote endpoints not tied to a declared device
        "remote_endpoints": [
            {"url": "http://10.0.0.5:8081", "model": "devstral:24b"}
        ],
    })

Auto-detect (best-effort, Linux/Mac):
    setup = HardwareSetup.detect()
    setup.probe()   # HTTP health-check each endpoint, remove dead ones

The router uses HardwareSetup to:
  - Skip local models whose VRAM/RAM requirement exceeds what you have
  - Score GPU-backed endpoints higher than CPU (speed proxy)
  - Route different phases to different endpoints (map on B580, fill on CPU)
  - Fall back through quant tiers until something fits ("club until it fits")
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeclub.infra.models import ModelSpec


# ---------------------------------------------------------------------------
# Known GPU VRAM table (name substring → vram_mb)
# Used when detection can't read VRAM directly (Intel Arc, older AMD)
# ---------------------------------------------------------------------------

_KNOWN_VRAM: list[tuple[str, int]] = [
    # Intel Arc
    ("b580",  12_288), ("b770",  16_384),
    ("a770",  16_384), ("a750",   8_192), ("a580",   8_192),
    ("a380",   6_144), ("a310",   4_096),
    # NVIDIA (RTX 40xx)
    ("4090",  24_576), ("4080",  16_384), ("4070 ti", 12_288),
    ("4070 super", 12_288), ("4070", 12_288),
    ("4060 ti", 8_192), ("4060",  8_192),
    # NVIDIA (RTX 30xx)
    ("3090",  24_576), ("3080 ti", 12_288), ("3080", 10_240),
    ("3070 ti", 8_192), ("3070",   8_192),
    ("3060 ti", 8_192), ("3060",  12_288),
    # NVIDIA (RTX 20xx)
    ("2080 ti", 11_264), ("2080",  8_192), ("2070",  8_192),
    # AMD (RDNA 3)
    ("7900 xtx", 24_576), ("7900 xt", 20_480), ("7900 gre", 16_384),
    ("7800 xt", 16_384), ("7700 xt", 12_288), ("7600 xt", 16_384),
    ("7600",   8_192),
    # AMD (RDNA 2)
    ("6900 xt", 16_384), ("6800 xt", 16_384), ("6800",  16_384),
    ("6700 xt", 12_288), ("6650 xt",  8_192), ("6600 xt",  8_192),
    # Apple Silicon (unified memory — treat vram = total ram for sizing)
    ("m4 max", 128_000), ("m4 pro", 48_000), ("m4", 16_000),
    ("m3 max", 128_000), ("m3 pro", 36_000), ("m3", 18_000),
    ("m2 max",  96_000), ("m2 pro", 32_000), ("m2", 16_000),
    ("m1 max",  64_000), ("m1 pro", 32_000), ("m1", 16_000),
]

# Backend detection strings → backend label
_BACKEND_PATTERNS: list[tuple[str, str]] = [
    ("arc", "sycl"), ("intel", "sycl"),
    ("nvidia", "cuda"), ("geforce", "cuda"), ("quadro", "cuda"), ("tesla", "cuda"),
    ("amd", "rocm"), ("radeon", "rocm"),
    ("apple", "metal"), ("m1", "metal"), ("m2", "metal"), ("m3", "metal"), ("m4", "metal"),
]


def _guess_vram(name: str) -> int | None:
    n = name.lower()
    for substr, vram in _KNOWN_VRAM:
        if substr in n:
            return vram
    return None


def _guess_backend(name: str) -> str:
    n = name.lower()
    for substr, backend in _BACKEND_PATTERNS:
        if substr in n:
            return backend
    return "cpu"


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------

@dataclass
class GPUDevice:
    """A single GPU (or iGPU). Represents the physical device."""
    name: str
    vram_mb: int
    backend: str               # "cuda" | "sycl" | "metal" | "rocm" | "vulkan" | "cpu"
    device_index: int = 0
    # Rough throughput score — used for relative speed ordering.
    # Derived from tflops_fp16 when available; falls back to VRAM as proxy.
    tflops_fp16: float | None = None

    @property
    def speed_score(self) -> float:
        """Relative speed proxy (higher = faster). Not calibrated in absolute terms."""
        if self.tflops_fp16:
            return self.tflops_fp16
        # Rough heuristic: VRAM in GB as a proxy (8GB ≈ budget, 24GB ≈ high-end)
        return self.vram_mb / 1024.0

    @classmethod
    def from_dict(cls, d: dict) -> "GPUDevice":
        return cls(
            name=d["name"],
            vram_mb=d.get("vram_mb") or _guess_vram(d["name"]) or 0,
            backend=d.get("backend") or _guess_backend(d["name"]),
            device_index=d.get("device_index", 0),
            tflops_fp16=d.get("tflops_fp16"),
        )


@dataclass
class InferenceEndpoint:
    """
    A running (or configured) inference server.

    Can be backed by a GPUDevice (local llama-server), by Ollama (CPU or GPU),
    or by a remote machine over the network.
    """
    url: str
    provider: str              # "llama-server" | "ollama"
    # Which model this endpoint currently serves (None = Ollama, serves many)
    model_id: str | None = None
    # Physical device backing this endpoint (None = CPU / unknown)
    device: GPUDevice | None = None
    tps_observed: float | None = None
    # Alive status — updated by probe()
    alive: bool | None = None  # None = not yet checked

    @property
    def is_gpu(self) -> bool:
        return self.device is not None and self.device.backend != "cpu"

    @property
    def display(self) -> str:
        dev = self.device.name if self.device else "CPU"
        model = self.model_id or "any"
        return f"{self.provider}@{self.url} [{dev}] model={model}"

    def probe(self, timeout: int = 3) -> bool:
        """Check if this endpoint is alive. Updates self.alive."""
        try:
            if self.provider == "ollama":
                check_url = self.url.rstrip("/") + "/api/tags"
            else:
                check_url = self.url.rstrip("/") + "/v1/models"
            req = urllib.request.Request(check_url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout):
                pass
            self.alive = True
        except Exception:
            self.alive = False
        return bool(self.alive)

    def list_ollama_models(self) -> list[str]:
        """For Ollama endpoints: return list of pulled model tags."""
        if self.provider != "ollama":
            return []
        try:
            url = self.url.rstrip("/") + "/api/tags"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []


@dataclass
class HardwareSetup:
    """
    Complete hardware inventory for codeclub.

    Describes every device and inference endpoint available to the router.
    The router uses this to determine which local models can actually run,
    which endpoint serves each model, and how fast each path is.
    """
    devices: list[GPUDevice] = field(default_factory=list)
    ram_mb: int = 0
    endpoints: list[InferenceEndpoint] = field(default_factory=list)
    # Separate Ollama base URL — Ollama can serve many models, so it's not
    # tied to a single GPUDevice entry.
    ollama_url: str = "http://localhost:11434"

    # ---------------------------------------------------------------------------
    # Construction
    # ---------------------------------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict) -> "HardwareSetup":
        """
        Build from a user-supplied dict. Example:

            {
              "devices": [
                {"name": "Intel Arc B580", "vram_mb": 12288, "backend": "sycl",
                 "endpoint": "http://localhost:8081"},
                {"name": "NVIDIA RTX 3080", "vram_mb": 10240, "backend": "cuda",
                 "endpoint": "http://192.168.1.10:8081"},
              ],
              "ram_mb": 32768,
              "ollama_url": "http://localhost:11434",
              "remote_endpoints": [
                {"url": "http://10.0.0.5:8081", "model": "devstral:24b"}
              ]
            }
        """
        devices = []
        endpoints = []

        for dev_d in d.get("devices", []):
            dev = GPUDevice.from_dict(dev_d)
            devices.append(dev)
            if "endpoint" in dev_d:
                endpoints.append(InferenceEndpoint(
                    url=dev_d["endpoint"],
                    provider="llama-server",
                    device=dev,
                ))

        for ep_d in d.get("remote_endpoints", []):
            endpoints.append(InferenceEndpoint(
                url=ep_d["url"],
                provider=ep_d.get("provider", "llama-server"),
                model_id=ep_d.get("model"),
            ))

        return cls(
            devices=devices,
            ram_mb=d.get("ram_mb", 0) or _detect_ram_mb(),
            endpoints=endpoints,
            ollama_url=d.get("ollama_url", "http://localhost:11434"),
        )

    @classmethod
    def detect(cls) -> "HardwareSetup":
        """
        Best-effort hardware auto-detection.

        Tries: nvidia-smi → lspci → /proc/meminfo → Ollama probe.
        Falls back gracefully when tools are unavailable.
        """
        devices = (
            _detect_nvidia()
            or _detect_via_lspci()
            or []
        )
        ram_mb = _detect_ram_mb()

        # Default endpoints: probe common local ports
        endpoints: list[InferenceEndpoint] = []
        for port in (8081, 8082, 8083):
            url = f"http://localhost:{port}"
            ep = InferenceEndpoint(url=url, provider="llama-server")
            if ep.probe(timeout=1):
                # Try to find which device this might be
                ep.device = devices[0] if devices else None
                endpoints.append(ep)

        return cls(
            devices=devices,
            ram_mb=ram_mb,
            endpoints=endpoints,
            ollama_url="http://localhost:11434",
        )

    # ---------------------------------------------------------------------------
    # Queries
    # ---------------------------------------------------------------------------

    @property
    def total_vram_mb(self) -> int:
        return sum(d.vram_mb for d in self.devices)

    @property
    def has_gpu(self) -> bool:
        return bool(self.devices) and any(d.backend != "cpu" for d in self.devices)

    def probe(self) -> "HardwareSetup":
        """Health-check all configured endpoints. Returns self for chaining."""
        for ep in self.endpoints:
            ep.probe()
        return self

    def alive_endpoints(self) -> list[InferenceEndpoint]:
        """Return endpoints that have been probed and are alive."""
        return [ep for ep in self.endpoints if ep.alive is True]

    def endpoints_for_model(self, model: "ModelSpec") -> list[InferenceEndpoint]:
        """
        Return endpoints that can serve this model.

        For llama-server: endpoint must have enough VRAM on its device
        (or fall back to CPU if no device constraint set).
        For Ollama: always included if Ollama is reachable.
        """
        if not model.local:
            return []  # cloud model — no local endpoint needed

        result = []

        if model.provider == "ollama":
            # Ollama endpoint is implicit (handled separately via ollama_url)
            # Return a synthetic endpoint for scoring purposes
            result.append(InferenceEndpoint(
                url=self.ollama_url,
                provider="ollama",
                model_id=model.id,
                alive=True,  # assume alive; check with probe() if needed
            ))
            return result

        if model.provider == "llama-server":
            needed_vram = model.vram_mb or 0
            for ep in self.endpoints:
                if ep.provider != "llama-server":
                    continue
                if ep.alive is False:
                    continue
                if ep.model_id and ep.model_id != model.id:
                    continue
                # Check if endpoint's device has enough VRAM
                if needed_vram > 0 and ep.device:
                    if ep.device.vram_mb < needed_vram:
                        continue
                result.append(ep)
            return result

        return []

    def can_fit(self, model: "ModelSpec") -> bool:
        """Return True if this model can run on any available device/RAM."""
        if not model.local:
            return True  # cloud models always "fit"
        if model.vram_mb:
            # Needs GPU VRAM — check any single GPU (no multi-GPU split for now)
            if any(d.vram_mb >= model.vram_mb for d in self.devices):
                return True
            # Also check CPU RAM fallback (much slower, but it fits)
            if self.ram_mb >= model.vram_mb * 1.2:  # rough factor for CPU overhead
                return True
            return False
        if model.ram_mb:
            return self.ram_mb >= model.ram_mb
        return True  # no requirement declared, assume it fits

    def best_endpoint_for(self, model: "ModelSpec") -> InferenceEndpoint | None:
        """Return the best (fastest) endpoint for this model, or None."""
        candidates = self.endpoints_for_model(model)
        if not candidates:
            return None
        # Prefer GPU endpoints (higher speed_score)
        def ep_score(ep: InferenceEndpoint) -> float:
            if ep.tps_observed:
                return ep.tps_observed
            if ep.device:
                return ep.device.speed_score
            return 1.0  # CPU fallback
        return max(candidates, key=ep_score)

    def ollama_url_for(self, model: "ModelSpec") -> str:
        """Return the Ollama base URL to use for this model."""
        # Future: could route to different Ollama instances based on model size
        return self.ollama_url

    # ---------------------------------------------------------------------------
    # Display
    # ---------------------------------------------------------------------------

    def summary(self) -> str:
        lines = ["Hardware setup:"]
        if self.devices:
            for d in self.devices:
                lines.append(f"  GPU  {d.name} ({d.vram_mb//1024}GB {d.backend})")
        else:
            lines.append("  GPU  (none declared)")
        lines.append(f"  RAM  {self.ram_mb//1024}GB")
        lines.append(f"  Ollama  {self.ollama_url}")
        for ep in self.endpoints:
            status = {True: "alive", False: "dead", None: "unchecked"}[ep.alive]
            lines.append(f"  Endpoint  {ep.url} [{ep.provider}] {status}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto-detection helpers
# ---------------------------------------------------------------------------

def _detect_ram_mb() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb // 1024
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            return int(result.stdout.strip()) // (1024 * 1024)
    except Exception:
        pass
    return 0


def _detect_nvidia() -> list[GPUDevice]:
    try:
        result = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,index",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        devices = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            name, vram_mib, idx = parts[0], parts[1], parts[2]
            devices.append(GPUDevice(
                name=name,
                vram_mb=int(vram_mib),
                backend="cuda",
                device_index=int(idx),
            ))
        return devices
    except Exception:
        return []


def _detect_via_lspci() -> list[GPUDevice]:
    """Detect GPUs via lspci (Linux). Returns known GPUs from name lookup."""
    try:
        result = subprocess.run(
            ["lspci", "-mm"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        devices = []
        idx = 0
        for line in result.stdout.splitlines():
            line_lower = line.lower()
            # Match VGA, 3D, Display classes
            if not re.search(r'"vga|"3d|"display', line_lower):
                continue
            # Extract the device name from lspci -mm quoted fields
            # Format: "slot" "class" "vendor" "device" ...
            parts = re.findall(r'"([^"]*)"', line)
            if len(parts) < 4:
                continue
            vendor = parts[2]
            device = parts[3]
            name = f"{vendor} {device}".strip()
            vram = _guess_vram(name)
            backend = _guess_backend(name)
            if vram is None:
                # Unknown GPU — include with 0 VRAM so it's visible but won't fit models
                vram = 0
            devices.append(GPUDevice(
                name=name, vram_mb=vram, backend=backend, device_index=idx
            ))
            idx += 1
        return devices
    except Exception:
        return []


# ---------------------------------------------------------------------------
# VRAM requirement estimator (for models not in registry with explicit vram_mb)
# ---------------------------------------------------------------------------

# Bits-per-weight for common quantisation levels
_QUANT_BPW: dict[str, float] = {
    "f32": 32.0, "f16": 16.0, "bf16": 16.0,
    "q8_0": 8.5,
    "q6_k": 6.5, "q6_k_s": 6.5,
    "q5_k_m": 5.6, "q5_k_s": 5.5,
    "q4_k_m": 4.8, "q4_k_s": 4.6, "q4_0": 4.5,
    "iq4_xs": 4.3, "iq4_nl": 4.1,
    "q3_k_m": 3.9, "q3_k_s": 3.7, "q3_k_l": 4.0,
    "q2_k": 3.3, "q2_k_s": 3.1,
}

# Quant tiers ordered by quality (best first)
QUANT_QUALITY_ORDER = [
    "f16", "bf16", "q8_0",
    "q6_k", "q5_k_m", "q5_k_s",
    "q4_k_m", "iq4_xs", "q4_k_s", "q4_0",
    "q3_k_m", "q3_k_l", "q3_k_s",
    "q2_k",
]


def estimate_vram_mb(params_b: float, quant: str) -> int:
    """
    Estimate GPU VRAM required for a model.

    Parameters
    ----------
    params_b: Parameter count in billions (e.g. 8.0 for an 8B model)
    quant:    Quantisation level (e.g. "q6_k", "q4_k_m")

    Returns estimated VRAM in MB (includes ~15% overhead for KV cache
    and activations at moderate context length).
    """
    bpw = _QUANT_BPW.get(quant.lower(), 8.0)
    base_mb = params_b * 1e9 * bpw / 8.0 / (1024.0 ** 2)
    return int(base_mb * 1.15)


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def print_setup(setup: HardwareSetup) -> None:
    """Pretty-print a HardwareSetup."""
    print(setup.summary())


if __name__ == "__main__":
    import sys
    print("Detecting hardware...")
    hw = HardwareSetup.detect()
    print(hw.summary())

    print("\nProbing endpoints...")
    hw.probe()
    alive = hw.alive_endpoints()
    print(f"  {len(alive)} endpoint(s) alive")
    for ep in alive:
        if ep.provider == "ollama":
            models = ep.list_ollama_models()
            print(f"  Ollama models: {models}")
