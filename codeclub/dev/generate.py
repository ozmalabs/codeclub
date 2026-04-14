"""
generator.py — Two-phase new-code generation: map then fill (Skeleton-of-Thought).

Phase 1 (Map):  Give the model a task description + optional interface context.
                Model returns a stub map — signatures + `...` bodies only.
                One mid-tier call. Output is small (~150–300 tokens).

Phase 2 (Fill): For each function stub, one focused call fills the body.
                Each call is small (~350 in, ~250 out) AND fully isolated —
                no cross-function context, no architectural reasoning required.
                This means EACH FILL CALL CAN RUN ON THE SMALLEST AVAILABLE MODEL.

                Calls are independent → run in parallel with ThreadPoolExecutor.

Assembly:       Filled bodies splice back into the stub map via SourceMap.
                Output: complete, runnable Python file.

Based on: arXiv:2307.15337 "Skeleton-of-Thought: Prompting LLMs for Efficient
          Parallel Generation" + arXiv:2604.00025 "Scale-Aware Prompting"

The real cost advantage (not just token count):
  One-shot on gpt-4.1:
    1 call × 3000 in + 2500 out @ $2/$8 per 1M  →  ~$0.026

  Map+fill:
    Phase 1: 1 call × 400 in + 200 out on gpt-4.1  →  ~$0.0024   (architecture)
    Phase 2: 10 calls × 350 in + 250 out on gpt-5-mini  →  ~$0.0020  (bodies)
    Total: ~$0.0044  →  ~83% cost reduction, ~3x faster wall time

  With a self-hosted 3B model for fill (e.g. Qwen2.5-Coder-3B via Ollama):
    Phase 2 cost ≈ $0.000x  →  >95% cost reduction

  The key insight: once the stub map sets the interface contract, filling a
  single isolated function body is a task well within a 0.5B–3B model's reach.
  The paper (arXiv:2604.00025) shows these models match 30B+ on focused tasks
  with brevity constraints — exactly what fill_prompt provides.

Local model routing (map_call_fn / fill_call_fn):
  Use separate callables for each phase so you can route:
    map  → cloud (needs architecture reasoning)
    fill → local Ollama (tiny model, isolated body, no reasoning needed)

  Quick start:
    from generator import make_ollama_fn, recommend_local_fill_model, generate
    fill_fn = make_ollama_fn(recommend_local_fill_model())
    result = generate(task, context, map_call_fn=cloud_fn, fill_call_fn=fill_fn)
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from codeclub.compress.brevity import ModelTier


# ---------------------------------------------------------------------------
# Local model registry — Q4 RAM requirements (MiB) + Ollama tag
# ---------------------------------------------------------------------------

@dataclass
class LocalModel:
    tag: str             # Ollama model tag
    ram_mb: int          # Approximate Q4 RAM requirement in MiB
    active_mb: int       # Active params RAM (relevant for MoE models)
    tier: str            # "fill" | "map" | "both"
    notes: str = ""

# Models are ordered smallest → largest.
# active_mb == ram_mb for dense models; smaller for MoE.
_LOCAL_MODELS: list[LocalModel] = [
    LocalModel("qwen3:0.6b",           600,    600,  "fill", "Smallest thinking model; good for trivial fills"),
    LocalModel("qwen2.5-coder:1.5b",  1300,   1300,  "fill", "Code-tuned; reliable fill quality"),
    LocalModel("qwen3:1.7b",          1400,   1400,  "fill", "Best sub-2GB fill model; thinking mode available"),
    LocalModel("qwen2.5-coder:3b",    2200,   2200,  "fill", "Proven fill workhorse; strong code generation"),
    LocalModel("qwen3:4b",            3200,   3200,  "fill", "Good balance; thinking mode"),
    LocalModel("rnj-1:8b",            5200,   5200,  "both", "Essential AI; order-of-magnitude SWE-bench vs peers"),
    LocalModel("qwen3-coder:30b",     6000,   6000,  "both", "MoE 30B/3B-active; needs ~6GB loaded; coding-tuned agentic model"),
    LocalModel("devstral:24b",       16000,  16000,  "map",  "46.8% SWE-Bench; best local map model at 32GB+"),
]


def free_ram_mb() -> int:
    """Return available system RAM in MiB (Linux/macOS)."""
    # Linux: use MemAvailable (accounts for cache reclaim)
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except FileNotFoundError:
        pass
    # macOS fallback via vm_stat
    try:
        import subprocess
        out = subprocess.check_output(["vm_stat"], text=True)
        page_size = 4096
        free_pages = 0
        for line in out.splitlines():
            if "Pages free" in line or "Pages inactive" in line:
                free_pages += int(line.split()[-1].rstrip("."))
        return (free_pages * page_size) // (1024 * 1024)
    except Exception:
        return 0


def recommend_local_fill_model(min_free_mb: int | None = None) -> LocalModel | None:
    """
    Return the best fill-capable local model that fits in available RAM.

    Leaves a 1.5GB headroom for OS + Ollama server overhead.
    Returns None if no model fits (caller should fall back to cloud).
    """
    available = min_free_mb if min_free_mb is not None else free_ram_mb()
    headroom = 1536  # 1.5 GB overhead
    usable = available - headroom

    candidates = [m for m in _LOCAL_MODELS if m.tier in ("fill", "both")]
    # Pick largest model that fits (best quality within budget)
    fits = [m for m in candidates if m.active_mb <= usable]
    return fits[-1] if fits else None


def recommend_local_map_model(min_free_mb: int | None = None) -> LocalModel | None:
    """
    Return the best map-capable local model that fits in available RAM.

    Map phase needs more reasoning ability — prefers "map" or "both" tier.
    """
    available = min_free_mb if min_free_mb is not None else free_ram_mb()
    headroom = 1536
    usable = available - headroom

    candidates = [m for m in _LOCAL_MODELS if m.tier in ("map", "both")]
    fits = [m for m in candidates if m.active_mb <= usable]
    return fits[-1] if fits else None


def list_local_models(min_free_mb: int | None = None) -> list[tuple[LocalModel, bool]]:
    """
    Return all known local models with a boolean indicating if they fit in RAM.

    Useful for tooling / CLI display.
    """
    available = min_free_mb if min_free_mb is not None else free_ram_mb()
    headroom = 1536
    usable = available - headroom
    return [(m, m.active_mb <= usable) for m in _LOCAL_MODELS]


# ---------------------------------------------------------------------------
# Ollama call_fn factory
# ---------------------------------------------------------------------------

def make_ollama_fn(
    model: LocalModel | str,
    base_url: str = "http://localhost:11434",
    timeout: int = 120,
    check_ram: bool = True,
) -> Callable[[str], str]:
    """
    Return a call_fn that sends prompts to a local Ollama model.

    Parameters
    ----------
    model:      LocalModel instance or raw Ollama tag string (e.g. "qwen3:1.7b")
    base_url:   Ollama server URL (default: localhost)
    timeout:    Per-request timeout in seconds
    check_ram:  If True, raise RuntimeError when available RAM is insufficient

    Usage
    -----
        fill_fn = make_ollama_fn(recommend_local_fill_model())
        result  = generate(task, ctx, map_call_fn=cloud_fn, fill_call_fn=fill_fn)
    """
    import json
    import urllib.request

    tag = model.tag if isinstance(model, LocalModel) else model
    ram_needed = model.active_mb if isinstance(model, LocalModel) else 0

    if check_ram and ram_needed:
        available = free_ram_mb()
        headroom = 1536
        if available - headroom < ram_needed:
            raise RuntimeError(
                f"Insufficient RAM for {tag}: need ~{ram_needed}MB active, "
                f"have {available - headroom}MB usable "
                f"({available}MB available - {headroom}MB headroom). "
                f"Free more RAM or pick a smaller model."
            )

    url = f"{base_url}/api/chat"

    def _call(prompt: str) -> str:
        payload = json.dumps({
            "model": tag,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data["message"]["content"]

    _call.__name__ = f"ollama:{tag}"
    return _call


# ---------------------------------------------------------------------------
# Phase 1: Map prompts
# ---------------------------------------------------------------------------

_MAP_SUFFIX = {
    ModelTier.SMALL: (
        "Output a Python module skeleton only: class/function signatures with `...` bodies. "
        "No implementations. No prose."
    ),
    ModelTier.MEDIUM: (
        "Output a Python module skeleton. Rules:\n"
        "- All classes, methods, and top-level functions with type-hinted signatures and `...` bodies.\n"
        "- For every class, include `__init__` that declares all instance attributes with concrete "
        "domain constants as defaults (rates, limits, caps — embed the actual values from the task).\n"
        "- Docstrings must specify return semantics precisely: what the value represents and its unit "
        "(e.g. 'Returns number of tokens remaining, NOT a boolean').\n"
        "- Encode domain constants as parameter defaults or `Literal` types where applicable.\n"
        "- Output ONLY the module skeleton. No implementations. No prose. No extra functions."
    ),
    ModelTier.LARGE: (
        "Output a complete Python module skeleton with all classes, methods, and functions. "
        "For every class include `__init__` with all instance attributes and concrete domain "
        "constants declared. Docstrings must specify exact return value semantics. "
        "Use `...` for all bodies. No implementations."
    ),
}


def map_prompt(task: str, context: str = "", *, tier: ModelTier = ModelTier.MEDIUM) -> str:
    """
    Build the Phase 1 prompt.

    Model should return ONLY signatures + `...` bodies — no implementations.
    We use MEDIUM tier for map: enough detail for type hints + docstrings
    without over-generating bodies.
    """
    parts = []
    if context:
        parts.append(f"<context>\n{context}\n</context>\n")
    parts.append(f"<task>\n{task}\n</task>\n")
    parts.append(_MAP_SUFFIX[tier])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Phase 2: Fill prompts
# ---------------------------------------------------------------------------

# Per-function fill uses SMALL tier by default — tight context, short body.
# The stub map provides all interface info; the model only needs to implement
# one isolated function. This is where the small-model advantage activates.
_FILL_SUFFIX = {
    ModelTier.SMALL: "No explanation. Code only.",
    ModelTier.MEDIUM: "Output only the complete function. No prose.",
    ModelTier.LARGE: "Output only the complete function. No explanation.",
}


def fill_prompt(
    stub_map: str,
    fn_name: str,
    fn_sig: str,
    task: str,
    *,
    tier: ModelTier = ModelTier.SMALL,
    error_context: str = "",
) -> str:
    """
    Build the Phase 2 prompt for one function.

    The model sees:
      - The full stub map (all interfaces, as stubs — cheap context)
      - The original task (for intent/domain)
      - The specific signature to implement
      - A constraint listing only the callable methods (prevents hallucination)
      - Optional error context for fix-loop retries
    """
    # Extract all method/function names from the stub so the model
    # knows exactly what it can call — prevents inventing helpers like
    # `self.drain_bucket()` that don't exist in the stub.
    available = re.findall(r'def (\w+)\(', stub_map)
    available_note = (
        f"\nAvailable methods (ONLY call these — do not invent others): "
        f"{', '.join(available)}"
    ) if available else ""

    error_block = f"\n<previous_error>\n{error_context}\n</previous_error>\n" if error_context else ""

    return (
        f"<module_interface>\n{stub_map}\n</module_interface>\n\n"
        f"<task>\n{task}\n</task>\n\n"
        f"<implement>\n{fn_sig}\n</implement>\n"
        f"{available_note}\n{error_block}\n"
        f"{_FILL_SUFFIX[tier]}"
    )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class GenerationResult:
    stub_map: str                  # Phase 1 output
    filled_bodies: dict[str, str]  # Phase 2 outputs {fn_name: complete function string}
    assembled: str                 # Final spliced file
    map_tokens_in: int
    map_tokens_out: int
    fill_tokens_in: int
    fill_tokens_out: int
    errors: dict[str, str] = field(default_factory=dict)  # {fn_name: error}

    @property
    def total_tokens_in(self) -> int:
        return self.map_tokens_in + self.fill_tokens_in

    @property
    def total_tokens_out(self) -> int:
        return self.map_tokens_out + self.fill_tokens_out

    @property
    def num_functions(self) -> int:
        return len(self.filled_bodies)


# ---------------------------------------------------------------------------
# Stub map parser — finds functions with `...` bodies in an already-stubbed file
# ---------------------------------------------------------------------------

@dataclass
class StubSlot:
    """A function stub slot in a stub map (Phase 1 output)."""
    name: str
    start_line: int   # 0-indexed: `def` line
    end_line: int     # 0-indexed: `    ...` line (inclusive)
    sig_lines: list[str] = field(default_factory=list)

    @property
    def sig(self) -> str:
        return "\n".join(self.sig_lines)


def parse_stub_map(stub_map: str) -> list[StubSlot]:
    """
    Parse an already-stubbed file to find all StubSlot entries.

    stub_functions() in treefrag.py skips bodies that are already `...`
    (the non_trivial guard). This parser handles the Phase 1 stub-map format
    directly: find `def` lines, skip multi-line signatures and docstrings,
    locate the `...` body marker.
    """
    lines = stub_map.splitlines()
    slots: list[StubSlot] = []

    i = 0
    while i < len(lines):
        m = re.match(r'^(\s*)(?:async\s+)?def\s+(\w+)\b', lines[i])
        if not m:
            i += 1
            continue

        base_indent = len(m.group(1))
        fn_name = m.group(2)
        fn_start = i

        # Advance past multi-line signature (ends with ':')
        j = i
        while j < len(lines) and not lines[j].rstrip().endswith(':'):
            j += 1
        sig_lines = lines[fn_start: j + 1]

        # Scan body for `...`, skipping blank lines and docstrings
        k = j + 1
        in_docstring = False
        quote_char = ""
        ellipsis_line: int | None = None

        while k < len(lines):
            stripped = lines[k].strip()

            if not stripped:
                k += 1
                continue

            # Exited function scope (back to same or lower indent level)
            curr_indent = len(lines[k]) - len(lines[k].lstrip())
            if curr_indent <= base_indent:
                break

            if in_docstring:
                if quote_char in stripped:
                    in_docstring = False
                k += 1
                continue

            if stripped.startswith('"""') or stripped.startswith("'''"):
                quote_char = stripped[:3]
                rest = stripped[3:]
                if quote_char not in rest:
                    in_docstring = True
                k += 1
                continue

            if stripped == '...':
                ellipsis_line = k
                break

            # Anything else = real body, not a stub
            break

        if ellipsis_line is not None:
            slots.append(StubSlot(
                name=fn_name,
                start_line=fn_start,
                end_line=ellipsis_line,
                sig_lines=sig_lines,
            ))

        i += 1

    return slots


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def assemble(stub_map: str, slots: list[StubSlot], filled: dict[str, str]) -> str:
    """
    Splice filled function bodies back into the stub map.

    `filled[fn_name]` = complete function string (sig + body) from the model.
    Stubs absent from `filled` are left as `...` (partial result is valid).

    Bottom-up splice (reverse start_line order) prevents line-offset drift
    when earlier replacements change line counts.

    If the model returned a full module instead of just the function, we extract
    the target function before splicing (see _extract_fn).

    Re-indents filled functions to match the stub slot's indentation — small
    models often return top-level functions even when the stub is a class method.
    """
    lines = stub_map.splitlines(keepends=True)
    for slot in sorted(slots, key=lambda s: s.start_line, reverse=True):
        if slot.name not in filled:
            continue
        raw = _strip_fences(filled[slot.name])
        extracted = _extract_fn(raw, slot.name)
        fn_text = extracted.rstrip("\n") + "\n"
        # Re-indent to match stub slot (small models ignore class context)
        expected_indent = len(slot.sig_lines[0]) - len(slot.sig_lines[0].lstrip()) if slot.sig_lines else 0
        fn_text = _reindent(fn_text, expected_indent)
        fn_lines = fn_text.splitlines(keepends=True)
        lines[slot.start_line: slot.end_line + 1] = fn_lines

    return "".join(lines)


# ---------------------------------------------------------------------------
# Core generation
# ---------------------------------------------------------------------------

def generate_map(
    task: str,
    context: str,
    call_fn: Callable[[str], str],
    *,
    tier: ModelTier = ModelTier.MEDIUM,
) -> str:
    """Phase 1: call model to produce a stub map. Returns raw stub string."""
    prompt = map_prompt(task, context, tier=tier)
    raw = call_fn(prompt)
    return _strip_fences(raw)


def generate(
    task: str,
    context: str,
    map_call_fn: Callable[[str], str],
    fill_call_fn: Callable[[str], str] | None = None,
    *,
    map_tier: ModelTier = ModelTier.MEDIUM,
    fill_tier: ModelTier = ModelTier.SMALL,
    max_workers: int = 4,
    language: str = "python",
) -> GenerationResult:
    """
    Full two-phase generation pipeline.

    1. Map:      one mid-tier call via map_call_fn → stub map (all signatures, `...` bodies)
    2. Fill:     N parallel calls via fill_call_fn → one per function, fills body in isolation
    3. Assemble: splice bodies back into stub map

    The fill calls are intentionally routed to the cheapest/smallest available
    model — local Ollama preferred. Each call is ~350 tokens of tightly-scoped
    context, no architecture reasoning needed. A 0.5B–3B local model handles
    this well (arXiv:2604.00025 sec 5). Cost reduction vs one-shot on a large model:
    typically 80–95% depending on fill model choice; ~100% with local CPU model.

    Parameters
    ----------
    task:         Natural language description of what to build
    context:      Optional: existing code / interfaces to reference (stubs preferred)
    map_call_fn:  Callable(prompt) → response for Phase 1 (needs architecture reasoning)
    fill_call_fn: Callable(prompt) → response for Phase 2 (tiny/local model ideal).
                  Defaults to map_call_fn when not provided.
    map_tier:     Tier for Phase 1 — MEDIUM gives good type hints + docstrings
    fill_tier:    Tier for Phase 2 — SMALL; brevity constraints tuned for tiny models
    max_workers:  Parallel fill calls (set to 1 for sequential / rate-limit safety)
    language:     Source language for tree-sitter parsing

    Quick local usage
    -----------------
        from generator import make_ollama_fn, recommend_local_fill_model, generate
        fill_fn = make_ollama_fn(recommend_local_fill_model())
        result = generate(task, ctx, map_call_fn=cloud_fn, fill_call_fn=fill_fn)
    """
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")

    _fill_fn = fill_call_fn if fill_call_fn is not None else map_call_fn

    # --- Phase 1: Map ---
    map_p = map_prompt(task, context, tier=map_tier)
    map_tokens_in = len(enc.encode(map_p))
    stub_map = _strip_fences(map_call_fn(map_p))
    map_tokens_out = len(enc.encode(stub_map))

    # Parse stub map to enumerate stub slots (can't use stub_functions -- already stubbed)
    slots = parse_stub_map(stub_map)

    if not slots:
        return GenerationResult(
            stub_map=stub_map,
            filled_bodies={},
            assembled=stub_map,
            map_tokens_in=map_tokens_in,
            map_tokens_out=map_tokens_out,
            fill_tokens_in=0,
            fill_tokens_out=0,
        )

    # --- Phase 2: Fill (parallel) ---
    filled: dict[str, str] = {}
    errors: dict[str, str] = {}

    def _fill_one(slot: StubSlot) -> tuple[str, str, int, int]:
        p = fill_prompt(stub_map, slot.name, slot.sig, task, tier=fill_tier)
        ti = len(enc.encode(p))
        raw = _fill_fn(p)
        result = _strip_fences(raw)
        to = len(enc.encode(result))
        return slot.name, result, ti, to

    fill_stats: list[tuple[int, int]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fill_one, slot): slot for slot in slots}
        for future in as_completed(futures):
            slot = futures[future]
            try:
                fn_name, body, ti, to = future.result()
                filled[fn_name] = body
                fill_stats.append((ti, to))
            except Exception as exc:
                errors[slot.name] = str(exc)

    fill_tokens_in = sum(s[0] for s in fill_stats)
    fill_tokens_out = sum(s[1] for s in fill_stats)

    # --- Assembly ---
    assembled = assemble(stub_map, slots, filled)

    return GenerationResult(
        stub_map=stub_map,
        filled_bodies=filled,
        assembled=assembled,
        map_tokens_in=map_tokens_in,
        map_tokens_out=map_tokens_out,
        fill_tokens_in=fill_tokens_in,
        fill_tokens_out=fill_tokens_out,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reindent(fn_text: str, target_indent: int) -> str:
    """Re-indent a function to match the target indent level of its stub slot."""
    lines = fn_text.splitlines(keepends=True)
    if not lines:
        return fn_text
    current_indent = 0
    for line in lines:
        if line.strip():
            current_indent = len(line) - len(line.lstrip())
            break
    delta = target_indent - current_indent
    if delta == 0:
        return fn_text
    result = []
    for line in lines:
        if not line.strip():
            result.append(line)
        elif delta > 0:
            result.append(" " * delta + line)
        else:
            trimable = len(line) - len(line.lstrip())
            result.append(line[min(-delta, trimable):])
    return "".join(result)


def _strip_fences(text: str) -> str:
    """
    Extract first code block from model output, discarding surrounding prose.

    Handles:
    - Bare code block (``` at start)
    - Prose-wrapped: "Here is the code:\n```python\n...\n```\nThis works by..."
    - No fences but leading prose before first def/class/import
    - Multiple code blocks — returns the first one
    """
    text = text.strip()
    # Extract first fenced code block (with or without language tag)
    m = re.search(r'```(?:python|py)?\n(.*?)```', text, re.DOTALL)
    if not m:
        m = re.search(r'```(?:\w+)?\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).rstrip()
    # No fences — strip leading prose lines before the first code line
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r'\s*(?:(?:async\s+)?def\s|class\s|import\s|from\s|@)', line):
            return "\n".join(lines[i:])
    return text


def _extract_fn(text: str, fn_name: str) -> str:
    """
    Extract a specific function definition from model output.

    When a model returns a full module instead of just the target function,
    this locates `def fn_name` and returns just that function (up to the next
    top-level or same-indent definition, or end of text).

    If the text looks like just a function already, returns it as-is.
    """
    lines = text.splitlines()

    # Find the def line for this function
    start_idx: int | None = None
    fn_indent: int = 0
    for i, line in enumerate(lines):
        m = re.match(r'^(\s*)(?:async\s+)?def\s+' + re.escape(fn_name) + r'\b', line)
        if m:
            start_idx = i
            fn_indent = len(m.group(1))
            break

    if start_idx is None:
        # Function not found — return as-is (let caller handle it)
        return text

    # Collect lines until we hit another def/class at the same or lower indent
    result_lines = [lines[start_idx]]
    for line in lines[start_idx + 1:]:
        stripped = line.strip()
        if not stripped:
            result_lines.append(line)
            continue
        curr_indent = len(line) - len(line.lstrip())
        # Stop at same-or-lower indent non-empty line that starts a new def/class
        if curr_indent <= fn_indent and re.match(r'(?:async\s+)?def\s|class\s', stripped):
            break
        result_lines.append(line)

    return "\n".join(result_lines)
