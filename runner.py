"""
runner.py — Test execution + failure compression for the dev loop.

Runs pytest on generated code+tests, returns structured results.
On failure, compresses the error context for efficient re-fill:
  - Strips passing function bodies (stub them) to reduce token count
  - Preserves only the failing function bodies (full text)
  - Appends the traceback + assertion errors

This keeps re-fill prompts small — typically < 600 tokens even for
complex failures — so the 1.5b fill model sees focused context.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import os
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    passed: bool
    output: str                      # Full pytest output
    failed_tests: list[str]          # Test function names that failed
    errors: list[str]                # Error messages / assertion text
    traceback: str                   # Compressed traceback (key lines only)
    num_passed: int = 0
    num_failed: int = 0
    num_errors: int = 0

    @property
    def total(self) -> int:
        return self.num_passed + self.num_failed + self.num_errors

    def summary(self) -> str:
        if self.passed:
            return f"PASS  {self.num_passed}/{self.total} tests passed"
        return (
            f"FAIL  {self.num_passed}/{self.total} passed, "
            f"{self.num_failed} failed, {self.num_errors} errors"
        )


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def run_tests(
    code: str,
    tests: str,
    *,
    timeout: int = 30,
    python: str | None = None,
) -> TestResult:
    """
    Write code+tests to temp files and run pytest.

    Parameters
    ----------
    code:     Generated Python code (the module under test).
    tests:    Generated pytest test code.
    timeout:  Max seconds to allow pytest to run.
    python:   Python executable path (defaults to sys.executable).

    Returns a TestResult with pass/fail status and compressed error context.
    """
    python = python or sys.executable

    with tempfile.TemporaryDirectory(prefix="codeclub_") as tmpdir:
        code_path = os.path.join(tmpdir, "generated.py")
        test_path = os.path.join(tmpdir, "test_generated.py")

        with open(code_path, "w") as f:
            f.write(code)

        test_header = f"""\
import sys
sys.path.insert(0, {repr(tmpdir)})
from generated import *
import pytest

"""
        with open(test_path, "w") as f:
            f.write(test_header + tests)

        try:
            result = subprocess.run(
                [python, "-m", "pytest", test_path, "-v", "--tb=short", "--no-header", "-q"],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=tmpdir,
            )
            output = result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return TestResult(
                passed=False,
                output=f"[TIMEOUT after {timeout}s]",
                failed_tests=[],
                errors=["Test execution timed out"],
                traceback="[TIMEOUT]",
            )
        except Exception as e:
            return TestResult(
                passed=False,
                output=str(e),
                failed_tests=[],
                errors=[str(e)],
                traceback=str(e),
            )

    return _parse_pytest_output(output, result.returncode)


def _parse_pytest_output(output: str, returncode: int) -> TestResult:
    """Parse pytest -v --tb=short output into a TestResult."""
    passed = returncode == 0

    # Count results
    summary_m = re.search(r'(\d+) passed', output)
    failed_m = re.search(r'(\d+) failed', output)
    error_m = re.search(r'(\d+) error', output)
    num_passed = int(summary_m.group(1)) if summary_m else 0
    num_failed = int(failed_m.group(1)) if failed_m else 0
    num_errors = int(error_m.group(1)) if error_m else 0

    # Detect collection errors (the whole test module failed to import/parse)
    # These show as "1 error" with no FAILED lines — treat differently from test failures
    is_collection_error = (
        num_errors > 0 and num_failed == 0 and num_passed == 0
        and ("ERROR collecting" in output or "ImportError" in output
             or "SyntaxError" in output or "NameError" in output)
    )

    # Extract failed test function names — handle "FAILED module.py::test_fn_name"
    # and plain "FAILED test_fn_name"
    failed_tests = re.findall(r'FAILED\s+\S+::(test_\w+)', output)
    failed_tests += re.findall(r'ERROR\s+\S+::(test_\w+)', output)
    # Fallback: bare name (no module prefix)
    if not failed_tests:
        failed_tests = re.findall(r'FAILED\s+(test_\w+)\b(?!\.py)', output)
        failed_tests += re.findall(r'ERROR\s+(test_\w+)\b(?!\.py)', output)

    # Extract error messages — the short traceback sections
    errors: list[str] = []
    # Capture FAILED/ERROR blocks
    for m in re.finditer(r'_{5,}\s*(test_\w+)\s*_{5,}\n(.*?)(?=_{5,}|\Z)', output, re.DOTALL):
        block = m.group(2).strip()
        # Keep only the key lines (E assert, AssertionError, etc.)
        key_lines = [l for l in block.splitlines()
                     if l.strip().startswith(("E ", "FAILED", "assert", "Error", ">>"))]
        if key_lines:
            errors.append(f"{m.group(1)}:\n" + "\n".join(key_lines[:8]))

    # Compact traceback: drop stack frames in site-packages, keep project lines
    tb_lines = []
    for line in output.splitlines():
        if any(skip in line for skip in ["site-packages", "importlib", "_pytest"]):
            continue
        if line.strip().startswith(("E ", "FAILED", "assert", "AssertionError",
                                     "TypeError", "AttributeError", "ValueError",
                                     "NameError", "test_")):
            tb_lines.append(line)
    traceback = "\n".join(tb_lines[:40])

    # For collection errors, add a descriptive error entry
    if is_collection_error:
        # Extract the actual error from the output
        col_err = re.search(r'((?:ImportError|NameError|SyntaxError)[^\n]+)', output)
        if col_err and col_err.group(1) not in errors:
            errors.insert(0, f"[collection error] {col_err.group(1)}")

    return TestResult(
        passed=passed,
        output=output,
        failed_tests=list(dict.fromkeys(failed_tests)),  # deduplicate, preserve order
        errors=errors,
        traceback=traceback,
        num_passed=num_passed,
        num_failed=num_failed,
        num_errors=num_errors,
    )


# ---------------------------------------------------------------------------
# Failure compression — for re-fill context
# ---------------------------------------------------------------------------

def compress_failure(
    code: str,
    test_result: TestResult,
    stub_map: str | None = None,
) -> str:
    """
    Build a compressed re-fill context from a test failure.

    Strategy:
    - Identify which functions are implicated in the failing tests
      (by name match between test names and function names in code)
    - For implicated functions: include full body (model needs to fix them)
    - For passing functions: use stub form (reduce tokens)
    - Append the compressed traceback

    Returns a string to prepend to the fill prompt for a retry.
    This typically reduces context by 50-70% vs including full code.
    """
    from treefrag import stub_functions

    implicated = _identify_implicated_functions(test_result)

    if not implicated:
        # Can't identify specific functions — include full code + traceback
        return f"<failing_code>\n{code}\n</failing_code>\n\n<errors>\n{test_result.traceback}\n</errors>"

    # Stub everything, then splice back the full bodies of implicated functions
    stubbed, smap = stub_functions(code, language="python")

    # Re-inject full bodies for implicated functions
    lines_orig = code.splitlines(keepends=True)
    lines_stub = stubbed.splitlines(keepends=True)

    for stub in smap.stubs:
        if stub.name in implicated:
            orig_body = "".join(lines_orig[stub.orig_start: stub.orig_end + 1])
            replacement = orig_body.splitlines(keepends=True)
            # Find the stub's position in the stubbed output and replace
            lines_stub[stub.comp_start: stub.comp_end + 1] = replacement

    focused_code = "".join(lines_stub)

    return (
        f"<focused_code>\n"
        f"# Functions with `...` are passing — only fix the ones with full bodies.\n"
        f"{focused_code}\n"
        f"</focused_code>\n\n"
        f"<test_failures>\n{test_result.traceback}\n</test_failures>"
    )


def _identify_implicated_functions(test_result: TestResult) -> set[str]:
    """
    Extract function names implicated in test failures.

    Heuristic: extract names from:
    - Failed test function names (test_consume_tokens → consume_tokens)
    - Error message text (AttributeError: 'RateLimiter' has no attribute 'tokens')
    - Traceback lines mentioning specific function calls
    """
    names: set[str] = set()

    # From test names: test_consume_tokens_available → consume, consume_tokens, etc.
    for test_name in test_result.failed_tests:
        stem = re.sub(r'^test_', '', test_name)
        parts = stem.split('_')
        # Add each prefix: consume, consume_tokens, consume_tokens_available
        for i in range(1, len(parts) + 1):
            names.add('_'.join(parts[:i]))

    # From error text: look for `def name` or `name(` or AttributeError mentioning names
    for error in test_result.errors:
        # method names in tracebacks: "in consume" or "self.consume("
        names.update(re.findall(r'(?:in |self\.)(\w+)\(', error))
        # AttributeError: has no attribute 'foo'
        names.update(re.findall(r"no attribute '(\w+)'", error))

    # From traceback
    names.update(re.findall(r'(?:in |self\.)(\w+)\(', test_result.traceback))

    # Filter out pytest internals and common names
    _ignore = {'pytest', 'assert', 'main', 'setUp', 'tearDown', 'fixture',
               'conftest', 'setup', 'teardown', 'run', 'call', 'repr'}
    return names - _ignore
