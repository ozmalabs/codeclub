"""
testgen.py — LLM-driven test generation for assembled code.

Generates pytest tests for a given piece of code + task description.
Tests are:
  - Focused on the acceptance criteria from the spec
  - Structured as isolated unit tests
  - Designed to catch the specific bugs the fill model is likely to make
    (semantic ambiguity in return values, missing __init__ state, etc.)

The generated test file is standalone: it imports the module by writing
the code to a temp file, so no project-level setup is required.
"""

from __future__ import annotations

from typing import Callable


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_TESTGEN_PROMPT = """\
Write pytest unit tests for the following Python code.

<code>
{code}
</code>

<task>
{task}
</task>

{criteria_block}
Rules:
- Use pytest (import pytest). No unittest.
- Each test function tests ONE specific behaviour.
- Use EXACT concrete values from the task (e.g. capacity=100 means available() returns 100 after
init; consume(10) returns True when tokens available, False when bucket empty).
- Include at least one test per public method.
- Include edge cases: empty bucket, overfill on refill, zero consumption.
- The test module runs alongside the code — use direct instantiation, not imports.
- Do NOT reproduce or redefine any class or function from the code above.
- Do NOT add module-level code that is not a test function, fixture, or import.
- Output ONLY the test functions and fixtures. No class definitions. No prose.
"""

_CRITERIA_TEMPLATE = """\
<acceptance_criteria>
{criteria}
</acceptance_criteria>

"""


def _build_prompt(code: str, task: str, acceptance_criteria: list[str] | None = None) -> str:
    criteria_block = ""
    if acceptance_criteria:
        criteria_block = _CRITERIA_TEMPLATE.format(
            criteria="\n".join(f"- {c}" for c in acceptance_criteria)
        )
    return _TESTGEN_PROMPT.format(code=code, task=task, criteria_block=criteria_block)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_tests(
    code: str,
    task: str,
    call_fn: Callable[[str], str],
    *,
    acceptance_criteria: list[str] | None = None,
) -> str:
    """
    Generate pytest tests for assembled code.

    Parameters
    ----------
    code:                 The assembled Python code to test.
    task:                 Original task description (for domain context).
    call_fn:              LLM callable.
    acceptance_criteria:  Optional list of acceptance criteria strings from the spec.
                          Included in prompt to anchor test assertions.

    Returns the test file as a string (ready to write to a .py file).
    """
    from generator import _strip_fences
    prompt = _build_prompt(code, task, acceptance_criteria)
    raw = call_fn(prompt)
    extracted = _strip_fences(raw)
    return _clean_test_output(extracted, code)


def _clean_test_output(tests: str, original_code: str) -> str:
    """
    Strip any class/function definitions the model reproduced from the code.

    Small models often echo the code they were given. This removes:
    - class definitions that exist in the original code
    - non-test def lines before the first test_ function
    - trailing prose after the last test function

    Keeps: import statements, @pytest.fixture, def test_*, class Test*.
    """
    import re

    # Extract class names from original code
    code_classes = set(re.findall(r'^class\s+(\w+)', original_code, re.MULTILINE))

    lines = tests.splitlines()
    result: list[str] = []
    skip_indent: int | None = None  # skip block at this base indent

    prev_decorator = False  # was the previous non-blank line a @pytest.fixture?

    for line in lines:
        stripped = line.strip()

        # If we're skipping a block, check if we've exited it
        if skip_indent is not None:
            curr = len(line) - len(line.lstrip()) if stripped else skip_indent + 1
            if stripped and curr <= skip_indent:
                skip_indent = None
            else:
                prev_decorator = False
                continue

        # Track @pytest.fixture decorator
        is_decorator = stripped.startswith('@pytest.fixture') or stripped.startswith('@pytest.mark')
        if is_decorator:
            prev_decorator = True
            result.append(line)
            continue

        # Skip class definitions that are in the original code
        cls_m = re.match(r'^class\s+(\w+)', line)
        if cls_m and cls_m.group(1) in code_classes:
            # But if preceded by a fixture decorator, keep it (it's a test class)
            if not prev_decorator:
                skip_indent = 0
                prev_decorator = False
                continue

        # Skip non-test top-level defs that are echoed code — UNLESS preceded by
        # @pytest.fixture (those are fixture definitions the tests depend on)
        if not prev_decorator:
            has_test = any(re.match(r'\s*def test_', l) for l in result)
            if not has_test:
                top_def = re.match(r'^(def|class)\s+(\w+)', line)
                if top_def and not top_def.group(2).startswith('test_'):
                    skip_indent = 0
                    prev_decorator = False
                    continue

        prev_decorator = False
        result.append(line)

    # Strip trailing blank lines
    while result and not result[-1].strip():
        result.pop()

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Test file preamble injection
# ---------------------------------------------------------------------------

def make_test_module(code: str, tests: str) -> str:
    """
    Combine code + tests into a single executable test module.

    Since generated code is self-contained (no imports from project files),
    we exec() the code into the test module's namespace so pytest can find it.

    This approach works for standalone generated classes/functions.
    For code that imports project modules, use write_test_files() instead.
    """
    preamble = f'''\
# Auto-generated test module
# Code under test is exec'd into this module's namespace

_CODE = """{code.replace('"', '\\"').replace('"""', '\\"""')}"""
exec(compile(_CODE, "<generated>", "exec"), globals())

import pytest

'''
    return preamble + tests


def write_test_files(
    code: str,
    tests: str,
    code_path: "str | None" = None,
    test_path: "str | None" = None,
) -> tuple[str, str]:
    """
    Write code and tests to temp files. Returns (code_path, test_path).

    If paths are not provided, writes to /tmp/codeclub_*.py.
    The test file imports the code module by path.
    """
    import tempfile, os

    if code_path is None:
        fd, code_path = tempfile.mkstemp(suffix=".py", prefix="codeclub_code_")
        os.close(fd)
    if test_path is None:
        fd, test_path = tempfile.mkstemp(suffix=".py", prefix="codeclub_test_")
        os.close(fd)

    with open(code_path, "w") as f:
        f.write(code)

    # Test file: import the code module
    module_name = os.path.splitext(os.path.basename(code_path))[0]
    code_dir = os.path.dirname(code_path)
    test_header = f"""\
import sys, os
sys.path.insert(0, {repr(code_dir)})
from {module_name} import *
import pytest

"""
    with open(test_path, "w") as f:
        f.write(test_header + tests)

    return code_path, test_path
