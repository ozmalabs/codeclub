"""
dev_loop.py — Self-contained write → test → review → report cycle.

Takes an abstract task and runs the full development pipeline:

  1. Spec     — decompose task into user story, requirements, tasks
  2. Generate — map (stub design) + fill (parallel implementation)
  3. TestGen  — generate pytest tests from the spec + assembled code
  4. TestRun  — execute tests, capture results
  5. Fix loop — compress failures, re-fill implicated functions (up to N iterations)
  6. Review   — independent model reviews code against spec + test results
  7. Report   — light model summarises what was built, issues encountered

Model routing (each stage uses the right model for its complexity):
  spec/map:    mid-tier cloud or B580 SYCL (architecture reasoning needed)
  fill:        small local model (1.5b–3b, isolated function bodies)
  testgen:     fill_fn or dedicated (function-level reasoning, like fill)
  review:      DIFFERENT cloud model from map (independent perspective)
  report:      fill_fn or light cloud (summarisation only)

Usage
-----
    from dev_loop import run, make_openrouter_fn, make_ollama_fn

    result = run(
        "Build a RateLimiter class with token bucket algorithm ...",
        map_fn=make_openrouter_fn("google/gemma-4-26b-a4b-it"),
        fill_fn=make_ollama_fn("qwen2.5-coder:1.5b"),
        review_fn=make_openrouter_fn("meta-llama/llama-3.3-70b-instruct"),
    )
    print(result.report)

CLI
---
    python dev_loop.py "Build a RateLimiter class with token bucket algorithm"
        --map-model google/gemma-4-26b-a4b-it
        --fill-model qwen2.5-coder:1.5b
        --review-model meta-llama/llama-3.3-70b-instruct
        --max-iterations 3
        --output rate_limiter.py
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from codeclub.infra.hardware import HardwareSetup

from .generate import generate, make_ollama_fn, GenerationResult
from .spec import FeatureSpec, decompose, print_spec
from .testgen import generate_tests
from .runner import TestResult, run_tests, compress_failure
from .review import ReviewResult, review_code, print_review
from codeclub.compress.brevity import ModelTier
from codeclub.infra.models import (
    ModelSpec, ModelRouter, PerformanceTracker, PhaseOutcome,
    estimate_complexity, router_for_setup,
)
from codeclub.accounting.tracker import TaskLedger
from codeclub.accounting.power import read_energy
from codeclub.accounting.baseline import compute_savings, SavingsReport


# ---------------------------------------------------------------------------
# call_fn factories (re-exported here for convenience)
# ---------------------------------------------------------------------------

def _read_env_value(name: str) -> str:
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(name) and "=" in line:
                return line.split("=", 1)[1].strip()
    return os.environ.get(name, "")


def make_openrouter_fn(
    model_id: str,
    *,
    api_key: str | None = None,
    timeout: int = 120,
    max_tokens: int = 2048,
    temperature: float = 0.1,
) -> Callable[[str], str]:
    """
    OpenRouter call_fn factory.

    Reads OPENROUTER_API_KEY from .env if api_key not provided.
    """
    if api_key is None:
        api_key = _read_env_value("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not found in .env or environment")

    url = "https://openrouter.ai/api/v1/chat/completions"

    def _call(prompt: str) -> str:
        payload = json.dumps({
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/codeclub",
                "X-Title": "codeclub-devloop",
            },
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read())
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if e.code == 429 and attempt < 2:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise RuntimeError(f"HTTP {e.code}: {body[:300]}") from e
        if "error" in data:
            raise RuntimeError(str(data["error"])[:200])
        return data["choices"][0]["message"]["content"]

    _call.__name__ = f"openrouter:{model_id}"
    return _call


def make_llama_server_fn(
    base_url: str = "http://localhost:8081",
    *,
    timeout: int = 120,
    max_tokens: int = 1024,
    temperature: float = 0.1,
) -> Callable[[str], str]:
    """call_fn for llama.cpp server (OpenAI-compat /v1/chat/completions)."""
    url = f"{base_url}/v1/chat/completions"

    def _call(prompt: str) -> str:
        payload = json.dumps({
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]

    _call.__name__ = f"llama-server:{base_url}"
    return _call


# ---------------------------------------------------------------------------
# Anthropic SDK factory
# ---------------------------------------------------------------------------

def make_github_models_fn(
    model_id: str,
    *,
    api_key: str | None = None,
    timeout: int = 120,
    max_tokens: int = 2048,
    temperature: float = 0.1,
) -> Callable[[str], str]:
    """
    call_fn for GitHub Models inference endpoint (Copilot / GitHub Models access).

    Reads GITHUB_TOKEN from .env if api_key not provided.
    Endpoint: https://models.inference.ai.azure.com
    """
    if api_key is None:
        api_key = _read_env_value("GITHUB_TOKEN")
    if not api_key:
        raise RuntimeError("GITHUB_TOKEN not found in .env or environment")

    url = "https://models.inference.ai.azure.com/chat/completions"

    def _call(prompt: str) -> str:
        payload = json.dumps({
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read())
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if e.code == 429 and attempt < 2:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise RuntimeError(f"GitHub Models HTTP {e.code}: {body[:300]}") from e
        if "error" in data:
            raise RuntimeError(str(data["error"])[:200])
        return data["choices"][0]["message"]["content"]

    _call.__name__ = f"github:{model_id}"
    return _call


def _coerce_copilot_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "summary", "message"):
            inner = value.get(key)
            if isinstance(inner, str) and inner:
                return inner
        return json.dumps(value)
    return ""


def _extract_copilot_response_text(data: object) -> str:
    for attr in ("content", "transformed_content", "summary_content", "message"):
        text = _coerce_copilot_content(getattr(data, attr, None))
        if text:
            return text
    return ""


def make_copilot_sdk_fn(
    model_id: str,
    *,
    github_token: str | None = None,
    cli_path: str | None = None,
    cwd: str | None = None,
    timeout: int = 120,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    reasoning_effort: str | None = None,
) -> Callable[[str], str]:
    """
    call_fn for the GitHub Copilot SDK via the local Copilot CLI.

    Uses GITHUB_TOKEN when provided, otherwise falls back to the user's Copilot
    CLI login session. max_tokens and temperature are accepted for interface
    parity with the other providers but are controlled by the SDK/CLI runtime.
    """
    try:
        from copilot import CopilotClient, SubprocessConfig
        from copilot.session import PermissionHandler
    except ImportError:
        raise RuntimeError(
            "github-copilot-sdk package not installed — run: pip install github-copilot-sdk"
        )

    if github_token is None:
        github_token = _read_env_value("GITHUB_TOKEN") or None

    working_directory = cwd or os.getcwd()
    _ = max_tokens, temperature

    async def _call_async(prompt: str) -> str:
        config_kwargs = {
            "cwd": working_directory,
            "log_level": "error",
        }
        if cli_path is not None:
            config_kwargs["cli_path"] = cli_path
        if github_token:
            config_kwargs["github_token"] = github_token
            config_kwargs["use_logged_in_user"] = False

        async with CopilotClient(SubprocessConfig(**config_kwargs)) as client:
            session_kwargs = {
                "on_permission_request": PermissionHandler.approve_all,
                "model": model_id,
                "working_directory": working_directory,
            }
            if reasoning_effort is not None:
                session_kwargs["reasoning_effort"] = reasoning_effort

            async with await client.create_session(**session_kwargs) as session:
                response = await session.send_and_wait(prompt, timeout=timeout)

        if response is None:
            raise RuntimeError(f"Copilot SDK returned no assistant message for model {model_id}")

        text = _extract_copilot_response_text(response.data)
        if not text:
            raise RuntimeError(f"Copilot SDK returned an empty assistant message for model {model_id}")
        return text

    def _call(prompt: str) -> str:
        return asyncio.run(_call_async(prompt))

    _call.__name__ = f"copilot-sdk:{model_id}"
    return _call


def make_anthropic_fn(
    model_id: str,
    *,
    api_key: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.1,
) -> Callable[[str], str]:
    """call_fn for direct Anthropic API (requires `pip install anthropic`)."""
    try:
        import anthropic as _anthropic
    except ImportError:
        raise RuntimeError(
            "anthropic package not installed — run: pip install anthropic"
        )

    if api_key is None:
        api_key = _read_env_value("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not found in .env or environment")

    client = _anthropic.Anthropic(api_key=api_key)

    def _call(prompt: str) -> str:
        msg = client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    _call.__name__ = f"anthropic:{model_id}"
    return _call


# ---------------------------------------------------------------------------
# Model-spec → call_fn factory
# ---------------------------------------------------------------------------

def make_call_fn(
    model: ModelSpec,
    *,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    llama_server_url: str = "http://localhost:8081",
    hardware: "HardwareSetup | None" = None,
) -> Callable[[str], str]:
    """
    Build a call_fn from a ModelSpec.

    When hardware is provided, uses the best available endpoint URL for
    local models rather than the global llama_server_url fallback.
    """
    if model.provider == "openrouter":
        return make_openrouter_fn(model.id, max_tokens=max_tokens, temperature=temperature)
    elif model.provider == "ollama":
        ollama_url = (
            hardware.ollama_url_for(model) if hardware else "http://localhost:11434"
        )
        return make_ollama_fn(model.id, base_url=ollama_url, check_ram=False)
    elif model.provider == "llama-server":
        if hardware is not None:
            ep = hardware.best_endpoint_for(model)
            url = ep.url if ep else llama_server_url
        else:
            url = llama_server_url
        return make_llama_server_fn(url, max_tokens=max_tokens, temperature=temperature)
    elif model.provider == "anthropic":
        return make_anthropic_fn(model.id, max_tokens=max_tokens, temperature=temperature)
    elif model.provider == "copilot-sdk":
        return make_copilot_sdk_fn(model.id, cwd=os.getcwd(), max_tokens=max_tokens, temperature=temperature)
    elif model.provider == "github":
        return make_github_models_fn(model.id, max_tokens=max_tokens, temperature=temperature)
    else:
        raise ValueError(f"Unknown provider '{model.provider}' for model {model.id}")


# ---------------------------------------------------------------------------
# Loop result
# ---------------------------------------------------------------------------

@dataclass
class LoopResult:
    task: str
    spec: FeatureSpec | None
    gen_result: GenerationResult | None
    tests: str
    test_results: list[TestResult]       # one per iteration (last = final)
    review: ReviewResult | None
    report: str
    iterations: int
    total_time_s: float
    map_model: str = ""
    fill_model: str = ""
    review_model: str = ""
    complexity: str = ""
    # Mutable: caller can inspect / reconfigure between runs
    router: ModelRouter | None = None
    tracker: PerformanceTracker | None = None
    # Accounting
    ledger: TaskLedger | None = None
    savings: SavingsReport | None = None

    @property
    def passed(self) -> bool:
        return bool(self.test_results and self.test_results[-1].passed)

    @property
    def approved(self) -> bool:
        return self.review is not None and self.review.approved

    @property
    def final_code(self) -> str:
        return self.gen_result.assembled if self.gen_result else ""


# ---------------------------------------------------------------------------
# Report prompt
# ---------------------------------------------------------------------------

_REPORT_PROMPT = """\
Write a concise developer report for the following automated development session.

<task>{task}</task>

<outcome>
Tests: {test_summary}
Review: {review_summary}
Iterations: {iterations}
</outcome>

<code_summary>
{code_summary}
</code_summary>

{issues_block}
Write the report in plain Markdown. Cover:
1. What was built (1-2 sentences)
2. Test results
3. Any issues encountered and how they were resolved (or not)
4. Review verdict and key suggestions

Be concise. No filler. Under 200 words.
"""


def _generate_report(
    task: str,
    loop_result: LoopResult,
    call_fn: Callable[[str], str],
) -> str:
    final_test = loop_result.test_results[-1] if loop_result.test_results else None
    test_summary = final_test.summary() if final_test else "No tests run"
    review_summary = (
        f"{loop_result.review.verdict} — {loop_result.review.summary}"
        if loop_result.review else "No review"
    )
    code_summary = (loop_result.final_code[:600] + "...") if loop_result.final_code else "(none)"
    issues_block = ""
    if loop_result.review and loop_result.review.issues:
        issues = "\n".join(f"- {i}" for i in loop_result.review.issues)
        issues_block = f"<issues>\n{issues}\n</issues>\n\n"

    prompt = _REPORT_PROMPT.format(
        task=task,
        test_summary=test_summary,
        review_summary=review_summary,
        iterations=loop_result.iterations,
        code_summary=code_summary,
        issues_block=issues_block,
    )
    return call_fn(prompt)


# ---------------------------------------------------------------------------
# Fix loop helpers
# ---------------------------------------------------------------------------

def _refill_failures(
    gen_result: GenerationResult,
    test_result: TestResult,
    fill_fn: Callable[[str], str],
    task: str,
    fill_hints: str = "",
) -> GenerationResult:
    """
    Re-fill only the functions implicated in test failures.

    Uses compress_failure() to build a focused context, then re-fills
    each implicated function with the error context prepended.
    """
    from codeclub.dev.generate import parse_stub_map, fill_prompt, assemble, _strip_fences, _extract_fn
    from codeclub.dev.runner import _identify_implicated_functions
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    implicated = _identify_implicated_functions(test_result)
    slots = parse_stub_map(gen_result.stub_map)

    # Only re-fill slots implicated in failures (or all if we can't identify)
    target_slots = [s for s in slots if s.name in implicated] if implicated else slots

    if not target_slots:
        return gen_result  # nothing to fix

    failure_context = compress_failure(
        gen_result.assembled, test_result, gen_result.stub_map
    )

    filled = dict(gen_result.filled_bodies)  # start from existing fills
    new_stats: list[tuple[int, int]] = []
    errors: dict[str, str] = {}

    # Build per-function error summaries for targeted feedback
    fn_errors: dict[str, str] = {}
    for error_str in test_result.errors:
        # Map error back to implicated function name
        for fn in implicated:
            if fn in error_str or fn.replace('_', '') in error_str.lower():
                fn_errors[fn] = error_str[:400]

    def _refill_one(slot):
        # Use per-function error if available, else use generic failure context
        fn_err = fn_errors.get(slot.name, "")
        if not fn_err:
            # Check if any error mentions this slot
            for err in test_result.errors:
                if slot.name in err:
                    fn_err = err[:400]
                    break
        if not fn_err and test_result.errors:
            fn_err = test_result.errors[0][:400]

        prompt = fill_prompt(
            gen_result.stub_map, slot.name, slot.sig, task,
            tier=ModelTier.SMALL,
            error_context=fn_err,
            fill_hints=fill_hints,
        )
        ti = len(enc.encode(prompt))
        raw = fill_fn(prompt)
        body = _strip_fences(raw)
        to = len(enc.encode(body))
        return slot.name, body, ti, to

    with ThreadPoolExecutor(max_workers=len(target_slots)) as executor:
        futures = {executor.submit(_refill_one, slot): slot for slot in target_slots}
        for future in as_completed(futures):
            slot = futures[future]
            try:
                fn_name, body, ti, to = future.result()
                filled[fn_name] = body
                new_stats.append((ti, to))
            except Exception as exc:
                errors[slot.name] = str(exc)

    new_assembled = assemble(gen_result.stub_map, slots, filled)

    return GenerationResult(
        stub_map=gen_result.stub_map,
        filled_bodies=filled,
        assembled=new_assembled,
        map_tokens_in=gen_result.map_tokens_in,
        map_tokens_out=gen_result.map_tokens_out,
        fill_tokens_in=gen_result.fill_tokens_in + sum(s[0] for s in new_stats),
        fill_tokens_out=gen_result.fill_tokens_out + sum(s[1] for s in new_stats),
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(
    task: str,
    context: str = "",
    *,
    map_fn: Callable[[str], str] | None = None,
    fill_fn: Callable[[str], str] | None = None,
    testgen_fn: Callable[[str], str] | None = None,
    review_fn: Callable[[str], str] | None = None,
    report_fn: Callable[[str], str] | None = None,
    spec_fn: Callable[[str], str] | None = None,
    router: ModelRouter | None = None,
    tracker: PerformanceTracker | None = None,
    hardware: "HardwareSetup | None" = None,
    max_fix_iterations: int = 3,
    run_review: bool = True,
    verbose: bool = True,
    llama_server_url: str = "http://localhost:8081",
    electricity_rate: float = 0.15,  # USD/kWh for energy cost calculation
    stack: str | None = None,
) -> LoopResult:
    """
    Full autonomous development loop.

    Parameters
    ----------
    task:               Natural language task description.
    context:            Optional existing code/stubs for context.
    map_fn:             call_fn for Phase 1 (stub design, architecture reasoning).
                        If not provided, resolved from ``router``.
    fill_fn:            call_fn for Phase 2 (body fill, local model preferred).
    review_fn:          call_fn for code review (independent model from map).
    report_fn:          call_fn for final report.
    spec_fn:            call_fn for task decomposition.
    router:             ModelRouter for automatic model selection.  If provided
                        and a phase fn is not explicitly given, the router picks
                        the model.  The router's budget/providers can be changed
                        at any point between calls (even mid-session) since it
                        is passed by reference.
    tracker:            PerformanceTracker shared across calls.  Records per-phase
                        outcomes and feeds dynamic escalation in the router.
                        If None, a fresh tracker is created (and returned in
                        LoopResult.tracker for reuse).
    max_fix_iterations: Max test-fix cycles before giving up.
    run_review:         Whether to run the code review phase.
    verbose:            Print progress to stdout.
    llama_server_url:   Base URL for llama-server (default localhost:8081).
    stack:              Stack name to use (e.g. 'web-api', 'cli', 'data').
                        If None, auto-detected from task keywords.
    """
    t_start = time.time()
    _tracker = tracker or PerformanceTracker()

    # ── Stack hints (data-driven, no LLM) ────────────────────────────────────
    from codeclub.stacks import resolve_stack, render_hints, render_fill_hints, render_test_hints
    _stack = resolve_stack(task, stack_name=stack)
    _stack_hints = render_hints(_stack)
    _fill_hints = render_fill_hints(_stack)
    _test_hints = render_test_hints(_stack)

    # ── Router-based model selection ─────────────────────────────────────────
    complexity = ""
    _suite: dict[str, ModelSpec | None] = {}

    if router is not None:
        # Wire the shared tracker and hardware so escalation + fit checks work
        if router.tracker is not _tracker:
            router.tracker = _tracker
        if hardware is not None and router.hardware is None:
            router.hardware = hardware

        complexity = estimate_complexity(task)
        _suite = router.select_suite(complexity)

        def _fn_from_suite(phase: str, fallback_fn=None, max_tok: int = 2048) -> Callable[[str], str] | None:
            if fallback_fn is not None:
                return fallback_fn
            m = _suite.get(phase)
            if m is None:
                return None
            return make_call_fn(m, max_tokens=max_tok,
                                llama_server_url=llama_server_url,
                                hardware=hardware)

        _spec_fn_r   = _fn_from_suite("spec",    spec_fn)
        _map_fn_r    = _fn_from_suite("map",     map_fn)
        _fill_fn_r   = _fn_from_suite("fill",    fill_fn)
        _testgen_fn_r = _fn_from_suite("testgen", testgen_fn, max_tok=2048)
        _review_fn_r = _fn_from_suite("review",  review_fn)
        _report_fn_r = _fn_from_suite("report",  report_fn)
    else:
        _spec_fn_r    = spec_fn    or map_fn
        _map_fn_r     = map_fn
        _fill_fn_r    = fill_fn    or map_fn
        _testgen_fn_r = testgen_fn or map_fn
        _review_fn_r  = review_fn  or map_fn
        _report_fn_r  = report_fn  or fill_fn or map_fn

    # Guard: map_fn is mandatory
    if _map_fn_r is None:
        raise ValueError("map_fn is required — either pass map_fn= or a router= with map phase models")

    _fill_fn    = _fill_fn_r or _map_fn_r
    _testgen_fn = _testgen_fn_r or _map_fn_r
    _review_fn  = _review_fn_r or _map_fn_r
    _report_fn  = _report_fn_r or _fill_fn
    _spec_fn    = _spec_fn_r or _map_fn_r

    map_model    = getattr(_map_fn_r,   '__name__', 'map')
    fill_model   = getattr(_fill_fn,    '__name__', 'fill')
    review_model = getattr(_review_fn,  '__name__', 'review')

    # ── Ledger ───────────────────────────────────────────────────────────────
    device_name = ""
    if hardware and hardware.devices:
        device_name = hardware.devices[0].name
    ledger = TaskLedger(
        task=task,
        electricity_rate=electricity_rate,
        device_name=device_name,
    )

    def _model_meta(fn_name: str) -> tuple[str, str]:
        """Extract (model_id, provider) from a call_fn __name__."""
        if ":" in fn_name:
            provider, model_id = fn_name.split(":", 1)
        else:
            provider, model_id = "unknown", fn_name
        return model_id, provider

    def _suite_rates(phase: str) -> tuple[float, float]:
        """Look up cost rates from the router suite for this phase."""
        m = _suite.get(phase) if _suite else None
        if m:
            return m.cost_in, m.cost_out
        return 0.0, 0.0

    # ── Outcome recording helper ─────────────────────────────────────────────
    def _record(phase: str, model_name: str, success: bool, t0: float, tokens_out: int = 0) -> None:
        # model_name is the __name__ attr which encodes provider:model_id
        model_id = model_name.split(":", 1)[-1] if ":" in model_name else model_name
        _tracker.record(PhaseOutcome(
            model_id=model_id,
            phase=phase,
            complexity=complexity or "moderate",
            success=success,
            latency_s=time.time() - t0,
            tokens_out=tokens_out,
        ))

    def _log(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    # ── 1. Spec ──────────────────────────────────────────────────────────────
    _log(f"\n{'━'*65}")
    _log(f"  codeclub dev loop")
    _log(f"  Task: {task[:70]}...")
    if complexity:
        _log(f"  Complexity: {complexity.upper()}")
    _log(f"  Map: {map_model}  |  Fill: {fill_model}")
    _log(f"  Stack: {_stack.name} ({_stack.description})")
    _log(f"{'━'*65}")

    _log("\n  [1/6] Decomposing task into spec ...")
    t_phase = time.time(); e_phase = read_energy()
    spec_fn_name = getattr(_spec_fn, '__name__', 'spec')
    try:
        spec = decompose(task, context, call_fn=_spec_fn, stack_hints=_stack_hints)
        _record("spec", spec_fn_name, True, t_phase)
        _mid, _prv = _model_meta(spec_fn_name); _ri, _ro = _suite_rates("spec")
        ledger.add("spec", _mid, _prv, wall_s=time.time()-t_phase,
                   energy_start=e_phase, energy_end=read_energy(),
                   api_cost_per_million_in=_ri, api_cost_per_million_out=_ro)
    except Exception as e:
        _record("spec", spec_fn_name, False, t_phase)
        ledger.add("spec", *_model_meta(spec_fn_name), wall_s=time.time()-t_phase,
                   success=False, error=str(e))
        raise
    if verbose:
        print_spec(spec)

    # ── 2. Generate ──────────────────────────────────────────────────────────
    _log("  [2/6] Generating code (map + fill) ...")
    t_gen = time.time(); e_gen = read_energy()
    try:
        gen_result = generate(
            task, context,
            map_call_fn=_map_fn_r,
            fill_call_fn=_fill_fn,
            max_workers=4,
            map_tier=ModelTier.MEDIUM,
            fill_tier=ModelTier.SMALL,
            stack_hints=_stack_hints,
            fill_hints=_fill_hints,
        )
        t_gen_end = time.time(); e_gen_end = read_energy()
        elapsed_gen = t_gen_end - t_gen
        _log(f"        {len(gen_result.filled_bodies)} functions filled in {elapsed_gen:.1f}s")
        _log(f"        {gen_result.map_tokens_in}t map-in, {gen_result.map_tokens_out}t map-out, "
             f"{gen_result.fill_tokens_in}t fill-in, {gen_result.fill_tokens_out}t fill-out")
        _record("map",  map_model,  True, t_gen, gen_result.map_tokens_out)
        _record("fill", fill_model, True, t_gen, gen_result.fill_tokens_out)
        _mri, _mro = _suite_rates("map"); _fri, _fro = _suite_rates("fill")
        _map_mid, _map_prv = _model_meta(map_model)
        _fill_mid, _fill_prv = _model_meta(fill_model)
        ledger.add("map", _map_mid, _map_prv,
                   tokens_in=gen_result.map_tokens_in,
                   tokens_out=gen_result.map_tokens_out,
                   wall_s=elapsed_gen, energy_start=e_gen, energy_end=e_gen_end,
                   api_cost_per_million_in=_mri, api_cost_per_million_out=_mro)
        ledger.add("fill", _fill_mid, _fill_prv,
                   tokens_in=gen_result.fill_tokens_in,
                   tokens_out=gen_result.fill_tokens_out,
                   wall_s=elapsed_gen,
                   api_cost_per_million_in=_fri, api_cost_per_million_out=_fro)
    except Exception as e:
        _record("map",  map_model,  False, t_gen)
        ledger.add("map", *_model_meta(map_model), wall_s=time.time()-t_gen,
                   success=False, error=str(e))
        _log(f"  [ERROR] Generation failed: {e}")
        return LoopResult(
            task=task, spec=spec, gen_result=None, tests="",
            test_results=[], review=None,
            report=f"Generation failed: {e}",
            iterations=0, total_time_s=time.time()-t_start,
            map_model=map_model, fill_model=fill_model, review_model=review_model,
            complexity=complexity, router=router, tracker=_tracker,
            ledger=ledger,
        )

    # ── 3. TestGen ───────────────────────────────────────────────────────────
    testgen_model = getattr(_testgen_fn, '__name__', 'testgen')
    _log(f"\n  [3/6] Generating tests ({testgen_model}) ...")
    t_phase = time.time(); e_phase = read_energy()
    try:
        tests = generate_tests(
            gen_result.assembled, task, _testgen_fn,
            acceptance_criteria=spec.acceptance_criteria,
            test_hints=_test_hints,
        )
        _tg_wall = time.time()-t_phase
        _log(f"        {tests.count('def test_')} test functions generated")
        _record("testgen", testgen_model, True, t_phase)
        _tgri, _tgro = _suite_rates("testgen")
        ledger.add("testgen", *_model_meta(testgen_model),
                   wall_s=_tg_wall, energy_start=e_phase, energy_end=read_energy(),
                   api_cost_per_million_in=_tgri, api_cost_per_million_out=_tgro)
    except Exception as e:
        _record("testgen", testgen_model, False, t_phase)
        ledger.add("testgen", *_model_meta(testgen_model),
                   wall_s=time.time()-t_phase, success=False, error=str(e))
        _log(f"  [WARN] Test generation failed: {e} — skipping test phase")
        tests = ""

    # ── 4+5. Test + fix loop ─────────────────────────────────────────────────
    test_results: list[TestResult] = []
    iteration = 0

    if tests:
        _log("\n  [4/6] Running tests ...")
        for iteration in range(max_fix_iterations + 1):
            test_result = run_tests(gen_result.assembled, tests)
            test_results.append(test_result)
            _log(f"        iter {iteration}: {test_result.summary()}")

            if test_result.passed:
                break

            if iteration == max_fix_iterations:
                _log(f"        max iterations reached — proceeding with failing tests")
                break

            # Record fill failure so router can escalate on next run
            _record("fill", fill_model, False, t_start, 0)

            # Fix loop: if router present, re-select fill model after recording failure
            if router is not None:
                new_fill_spec = router.select("fill", complexity or "moderate",
                                              exclude_ids=set())
                if new_fill_spec and new_fill_spec.id != _suite.get("fill", {}) and getattr(new_fill_spec, "id", None):
                    _fill_fn_new = make_call_fn(new_fill_spec, llama_server_url=llama_server_url)
                    if getattr(_fill_fn_new, '__name__', '') != fill_model:
                        fill_model = getattr(_fill_fn_new, '__name__', fill_model)
                        _fill_fn = _fill_fn_new
                        _log(f"        escalated fill model → {fill_model}")

            _log(f"\n  [5/6] Fixing failures (iteration {iteration+1}/{max_fix_iterations}) ...")
            _log(f"        Implicated: {test_result.failed_tests}")
            t_fix = time.time()
            gen_result = _refill_failures(gen_result, test_result, _fill_fn, task, fill_hints=_fill_hints)
            _record("fill", fill_model, True, t_fix, gen_result.fill_tokens_out)
    else:
        _log("\n  [4/6] Skipping tests (none generated)")
        _log("  [5/6] Skipping fix loop")

    # ── 6. Review ────────────────────────────────────────────────────────────
    review: ReviewResult | None = None
    if run_review:
        _log(f"\n  [6/6] Code review ({review_model}) ...")
        t_phase = time.time(); e_phase = read_energy()
        try:
            review = review_code(
                gen_result.assembled, task, _review_fn,
                test_result=test_results[-1] if test_results else None,
                spec=spec,
            )
            _rv_wall = time.time()-t_phase
            _record("review", review_model, True, t_phase)
            _rri, _rro = _suite_rates("review")
            ledger.add("review", *_model_meta(review_model),
                       wall_s=_rv_wall, energy_start=e_phase, energy_end=read_energy(),
                       api_cost_per_million_in=_rri, api_cost_per_million_out=_rro)
            if verbose:
                print_review(review)
        except Exception as e:
            _record("review", review_model, False, t_phase)
            ledger.add("review", *_model_meta(review_model),
                       wall_s=time.time()-t_phase, success=False, error=str(e))
            _log(f"  [WARN] Review failed: {e}")
    else:
        _log("\n  [6/6] Skipping review")

    # ── Report ────────────────────────────────────────────────────────────────
    _log("\n  Generating report ...")
    report_model = getattr(_report_fn, '__name__', 'report')
    t_phase = time.time(); e_phase = read_energy()
    partial = LoopResult(
        task=task, spec=spec, gen_result=gen_result, tests=tests,
        test_results=test_results, review=review, report="",
        iterations=iteration, total_time_s=time.time()-t_start,
        map_model=map_model, fill_model=fill_model, review_model=review_model,
        complexity=complexity, router=router, tracker=_tracker,
        ledger=ledger,
    )
    try:
        report = _generate_report(task, partial, _report_fn)
        _rp_wall = time.time()-t_phase
        _record("report", report_model, True, t_phase)
        _rpri, _rpro = _suite_rates("report")
        ledger.add("report", *_model_meta(report_model),
                   wall_s=_rp_wall, energy_start=e_phase, energy_end=read_energy(),
                   api_cost_per_million_in=_rpri, api_cost_per_million_out=_rpro)
    except Exception as e:
        _record("report", report_model, False, t_phase)
        ledger.add("report", *_model_meta(report_model),
                   wall_s=time.time()-t_phase, success=False, error=str(e))
        report = f"(report generation failed: {e})"
    partial.report = report
    partial.savings = compute_savings(ledger)

    total = time.time() - t_start
    _log(f"\n{'━'*65}")
    _log(f"  Done in {total:.1f}s  |  {iteration+1} iteration(s)  |  "
         f"tests={'PASS' if partial.passed else 'FAIL'}  |  "
         f"review={review.verdict if review else 'SKIPPED'}")
    _log(f"{'━'*65}")
    if verbose:
        _log(ledger.summary(verbose=False))
        if partial.savings:
            _log(partial.savings.format())
    _log(f"\n{report}")

    return partial


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    SETUP_CHOICES = [
        "local_only", "local_b580", "openrouter_free", "openrouter_cheap",
        "anthropic", "copilot", "github", "best_local_first",
    ]

    parser = argparse.ArgumentParser(
        description="codeclub autonomous dev loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Setup presets (--setup):
  local_only       No internet — Ollama + llama-server only
  local_b580       B580 SYCL for map/review, Ollama CPU for fill
  openrouter_free  Free-tier OpenRouter (rate-limited)
  openrouter_cheap Paid OpenRouter, cheap models (< $0.002/call)
  anthropic        Direct Anthropic API (Claude Opus/Sonnet/Haiku)
  copilot          GitHub Copilot SDK via local Copilot CLI
  github           GitHub Models inference endpoint
  best_local_first Local preferred, cloud fallback for complex phases

Dynamic levers (change mid-session without restarting):
  --budget         Cap model cost (free/cheap/medium/premium)
  --prefer-cloud   Don't prefer local models (useful when in a hurry)

Model overrides (bypass router for a specific phase):
  --map-model      OpenRouter model ID for map/spec
  --fill-model     Ollama model tag for fill
  --review-model   OpenRouter model ID for review
""",
    )
    parser.add_argument("task", help="Task description")
    parser.add_argument("--context", default="", help="Context code or file path")

    # ── Setup / routing ────────────────────────────────────────────────────
    parser.add_argument("--setup", default="best_local_first", choices=SETUP_CHOICES,
                        help="Hardware/provider setup preset (default: best_local_first)")
    parser.add_argument("--budget", default=None,
                        choices=["free", "cheap", "medium", "premium"],
                        help="Override model cost cap for this run")
    parser.add_argument("--prefer-cloud", action="store_true",
                        help="Do not boost local model score (use cloud when equal quality)")

    # ── Phase overrides (bypass router) ───────────────────────────────────
    parser.add_argument("--map-model", default=None,
                        help="OpenRouter model ID for map/spec phases (overrides router)")
    parser.add_argument("--fill-model", default=None,
                        help="Ollama tag for fill phases (overrides router)")
    parser.add_argument("--review-model", default=None,
                        help="OpenRouter model ID for review (overrides router)")
    parser.add_argument("--local-map", action="store_true",
                        help="Use llama-server on port 8081 for map (overrides router)")
    parser.add_argument("--llama-server", default="http://localhost:8081",
                        help="llama-server base URL (default: http://localhost:8081)")

    # ── Loop params ────────────────────────────────────────────────────────
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--no-review", action="store_true")
    parser.add_argument("--stack", default=None,
                        choices=["web-api", "cli", "data", "library", "async-service"],
                        help="Stack to use (auto-detected from task if omitted)")
    parser.add_argument("--electricity-rate", type=float, default=0.15,
                        help="Local electricity rate in USD/kWh (default: 0.15)")
    parser.add_argument("--output", help="Write final code to this file")
    parser.add_argument("--routing-table", action="store_true",
                        help="Print routing table for this setup and exit")
    parser.add_argument("--hardware", default=None,
                        help="Hardware config as JSON or path to JSON/YAML file "
                             "(see hardware.py for format). If omitted, uses detect.")
    parser.add_argument("--detect-hardware", action="store_true",
                        help="Auto-detect hardware and print summary, then exit")
    parser.add_argument("--probe", action="store_true",
                        help="Probe local endpoints and print which are alive, then exit")
    args = parser.parse_args()

    # Context: if it's a file path, read it
    context = args.context
    if context and Path(context).exists():
        context = Path(context).read_text()

    # Build hardware setup
    from hardware import HardwareSetup, print_setup
    _hardware: HardwareSetup | None = None
    if args.hardware:
        hw_src = args.hardware.strip()
        if Path(hw_src).exists():
            import yaml  # type: ignore[import]
            with open(hw_src) as f:
                hw_dict = yaml.safe_load(f) if hw_src.endswith((".yaml", ".yml")) else json.load(f)
        else:
            hw_dict = json.loads(hw_src)
        _hardware = HardwareSetup.from_dict(hw_dict)
        _hardware.probe()
    elif args.detect_hardware or args.probe:
        _hardware = HardwareSetup.detect()
        _hardware.probe()
        print_setup(_hardware)
        if args.detect_hardware:
            raise SystemExit(0)
    else:
        # Lightweight detect — just check local endpoints, don't enumerate GPUs
        _hardware = HardwareSetup.detect()

    if args.probe:
        print_setup(_hardware)
        raise SystemExit(0)

    # Build router
    router_kwargs: dict = {}
    if args.budget:
        router_kwargs["budget"] = args.budget
    if args.prefer_cloud:
        router_kwargs["prefer_local"] = False

    _router = router_for_setup(args.setup, hardware=_hardware, **router_kwargs)
    _tracker = PerformanceTracker()
    _router.tracker = _tracker

    if args.routing_table:
        from models import print_routing_table, print_complexity_suite
        print_routing_table(_router)
        print_complexity_suite(args.task, _router)
        raise SystemExit(0)

    # Phase overrides (bypass router for specific phases)
    map_fn    = None
    fill_fn   = None
    review_fn = None

    if args.local_map:
        map_fn = make_llama_server_fn(args.llama_server)
    elif args.map_model:
        map_fn = make_openrouter_fn(args.map_model)

    if args.fill_model:
        fill_fn = make_ollama_fn(args.fill_model, check_ram=False)

    if args.review_model:
        review_fn = make_openrouter_fn(args.review_model)

    result = run(
        args.task,
        context,
        map_fn=map_fn,
        fill_fn=fill_fn,
        review_fn=review_fn,
        router=_router,
        tracker=_tracker,
        hardware=_hardware,
        max_fix_iterations=args.max_iterations,
        run_review=not args.no_review,
        llama_server_url=args.llama_server,
        electricity_rate=args.electricity_rate,
        stack=args.stack,
    )

    if args.output and result.final_code:
        Path(args.output).write_text(result.final_code)
        print(f"\nCode written to {args.output}")

    # Print tracker summary if anything was recorded
    if _tracker.summary():
        print("\n  Performance summary:")
        for key, stats in sorted(_tracker.summary().items()):
            print(f"    {key:50s}  "
                  f"success={stats['success_rate']:.0%}  "
                  f"latency={stats['avg_latency']:.1f}s")
