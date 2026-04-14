"""
reviewer.py — Code review by a separate model.

Intentionally uses a DIFFERENT model from the one that generated the code.
Separation of concerns: the reviewer hasn't seen the generation process
and approaches the code with fresh eyes, catching bugs the generator
normalised over.

Review covers:
  - Correctness (does it satisfy the spec?)
  - Edge cases (what inputs would break it?)
  - Code quality (clarity, error handling, type safety)
  - Test coverage (are the generated tests sufficient?)

Output is structured so the dev_loop can decide whether to trigger
another generation cycle or accept the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ReviewResult:
    verdict: str                      # "APPROVE" | "REQUEST_CHANGES" | "COMMENT"
    summary: str
    issues: list[str] = field(default_factory=list)    # blocking issues
    suggestions: list[str] = field(default_factory=list)  # non-blocking
    score: float = 0.0                # 0.0–1.0 confidence in correctness

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVE"

    @property
    def needs_changes(self) -> bool:
        return self.verdict == "REQUEST_CHANGES"


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_REVIEW_PROMPT = """\
You are a senior code reviewer. Review the following code against the task \
specification and test results.

<task>
{task}
</task>

<code>
{code}
</code>

<test_results>
{test_results}
</test_results>

{spec_block}
Review the code and respond in exactly this format:

## Verdict
APPROVE | REQUEST_CHANGES | COMMENT

## Summary
One sentence.

## Issues
(blocking — must fix before shipping)
- ISSUE: ...
(or "none" if no blocking issues)

## Suggestions
(non-blocking — improvements to consider)
- SUGGEST: ...
(or "none")

## Score
0.0–1.0 — your confidence that this code is correct and complete.

Rules:
- APPROVE only if all acceptance criteria are met and no blocking issues exist.
- REQUEST_CHANGES if there are correctness bugs or unmet criteria.
- COMMENT for code quality concerns that don't affect correctness.
- BEFORE flagging any issue, quote the exact line(s) of code that demonstrate the problem.
  If you cannot quote the line, do not raise the issue.
- Do not flag things that are not present in the code above.
- Do not repeat what the code does — only flag problems.
"""

_SPEC_BLOCK_TEMPLATE = """\
<spec>
Requirements:
{requirements}

Acceptance Criteria:
{criteria}
</spec>

"""


def _build_prompt(
    code: str,
    task: str,
    test_result: "TestResult | None",
    spec: "FeatureSpec | None",
) -> str:
    from .runner import TestResult

    test_summary = "No tests run."
    if test_result is not None:
        test_summary = test_result.summary()
        if not test_result.passed and test_result.errors:
            test_summary += "\n" + "\n".join(test_result.errors[:5])

    spec_block = ""
    if spec is not None:
        reqs = "\n".join(f"- {r}" for r in spec.requirements) or "None specified."
        criteria = "\n".join(f"- {c}" for c in spec.acceptance_criteria) or "None specified."
        spec_block = _SPEC_BLOCK_TEMPLATE.format(requirements=reqs, criteria=criteria)

    return _REVIEW_PROMPT.format(
        task=task,
        code=code,
        test_results=test_summary,
        spec_block=spec_block,
    )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_review(raw: str) -> ReviewResult:
    def _section(text: str, header: str) -> str:
        m = re.search(rf'## {re.escape(header)}\n(.*?)(?=\n## |\Z)', text, re.DOTALL)
        return m.group(1).strip() if m else ""

    verdict_text = _section(raw, "Verdict").upper()
    if "APPROVE" in verdict_text:
        verdict = "APPROVE"
    elif "REQUEST" in verdict_text:
        verdict = "REQUEST_CHANGES"
    else:
        verdict = "COMMENT"

    summary = _section(raw, "Summary")

    issues_text = _section(raw, "Issues")
    issues = [re.sub(r'^[-*]\s*(ISSUE:\s*)?', '', l.strip())
              for l in issues_text.splitlines()
              if re.match(r'\s*[-*]', l) and l.strip().lower() not in ('- none', '- (none)')]

    suggestions_text = _section(raw, "Suggestions")
    suggestions = [re.sub(r'^[-*]\s*(SUGGEST:\s*)?', '', l.strip())
                   for l in suggestions_text.splitlines()
                   if re.match(r'\s*[-*]', l) and l.strip().lower() not in ('- none', '- (none)')]

    score_text = _section(raw, "Score")
    score_m = re.search(r'([0-9.]+)', score_text)
    score = float(score_m.group(1)) if score_m else (1.0 if verdict == "APPROVE" else 0.5)
    score = max(0.0, min(1.0, score))

    return ReviewResult(
        verdict=verdict,
        summary=summary,
        issues=issues,
        suggestions=suggestions,
        score=score,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def review_code(
    code: str,
    task: str,
    call_fn: Callable[[str], str],
    *,
    test_result: "TestResult | None" = None,
    spec: "FeatureSpec | None" = None,
) -> ReviewResult:
    """
    Review generated code against the task spec and test results.

    Should be called with a DIFFERENT model from the one used for generation
    to get an independent perspective.

    Parameters
    ----------
    code:         The assembled generated code.
    task:         Original task description.
    call_fn:      LLM callable (use a different model than the generator).
    test_result:  Optional TestResult from the test runner.
    spec:         Optional FeatureSpec for structured requirement checking.

    Returns a ReviewResult with verdict, issues, and suggestions.
    """
    prompt = _build_prompt(code, task, test_result, spec)
    raw = call_fn(prompt)
    return _parse_review(raw)


def print_review(review: ReviewResult) -> None:
    """Pretty-print a ReviewResult."""
    verdict_sym = {"APPROVE": "✓", "REQUEST_CHANGES": "✗", "COMMENT": "○"}.get(review.verdict, "?")
    print(f"\n  Review: {verdict_sym} {review.verdict}  (score={review.score:.1f})")
    print(f"  {review.summary}")
    if review.issues:
        print("  Issues:")
        for issue in review.issues:
            print(f"    ✗ {issue}")
    if review.suggestions:
        print("  Suggestions:")
        for s in review.suggestions:
            print(f"    → {s}")
