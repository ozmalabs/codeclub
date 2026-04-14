"""
spec.py — Spec-kit-inspired task decomposition layer.

Converts an abstract task description into a structured FeatureSpec:
  Constitution  (implicit — governed by the task description)
  Specification → user story + requirements + acceptance criteria
  Plan          → architecture decisions + files to create/modify
  Tasks         → ordered, atomic implementation steps

Each phase uses a mid-tier model for reasoning. The output is structured
Markdown that is parsed into dataclasses and passed to the generation layer.

This is the top of the funnel:
  "Add a page that does XYZ"
    → FeatureSpec
      → [TaskSpec, TaskSpec, ...]
        → generator.generate() per task
          → dev_loop.run() for test/review/report
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TaskSpec:
    """One atomic implementation task derived from spec decomposition."""
    id: str                           # e.g. "T1"
    title: str
    description: str
    files: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)  # task IDs


@dataclass
class FeatureSpec:
    """Full structured specification for a feature or change."""
    title: str
    user_story: str
    requirements: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    architecture_notes: str = ""
    tasks: list[TaskSpec] = field(default_factory=list)
    raw: str = ""  # the full model output, for debugging


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SPEC_PROMPT = """\
You are a senior software architect. Decompose the following task into a \
structured specification.

<task>
{task}
</task>

{context_block}
Output a specification in exactly this Markdown format — no extra sections, \
no prose outside the format:

## Title
One-line feature title.

## User Story
As a [role], I want to [action] so that [benefit].

## Requirements
- REQ-1: ...
- REQ-2: ...

## Acceptance Criteria
- [ ] Given [condition], when [action], then [outcome]

## Architecture Notes
Brief notes on approach, patterns, or constraints. 2-4 sentences max.

## Tasks
### T1: [title]
Files: filename.py
Description: What to implement.
Done when: Concrete, testable completion condition.
Depends on: (none or T-IDs)

### T2: [title]
...

Rules:
- Each task must be independently implementable and testable.
- Files listed must be specific (no "various files").
- "Done when" must be objectively verifiable.
- Keep tasks atomic — one concern per task.
- Order tasks by dependency (independent first).
"""


def _build_prompt(task: str, context: str = "", stack_hints: str = "") -> str:
    context_block = f"<context>\n{context}\n</context>\n\n" if context.strip() else ""
    stack_block = f"\n{stack_hints}\n\n" if stack_hints else ""
    return _SPEC_PROMPT.format(task=task, context_block=context_block) + stack_block


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_spec(raw: str, task: str) -> FeatureSpec:
    """Parse structured Markdown spec output into a FeatureSpec."""

    def _section(text: str, header: str) -> str:
        m = re.search(rf'## {re.escape(header)}\n(.*?)(?=\n## |\Z)', text, re.DOTALL)
        return m.group(1).strip() if m else ""

    def _bullets(text: str) -> list[str]:
        lines = [re.sub(r'^[-*]\s*\[.?\]\s*', '', l.strip()).strip()
                 for l in text.splitlines() if re.match(r'\s*[-*]', l)]
        return [l for l in lines if l]

    title = _section(raw, "Title") or task[:60]
    user_story = _section(raw, "User Story")
    requirements = _bullets(_section(raw, "Requirements"))
    acceptance_criteria = _bullets(_section(raw, "Acceptance Criteria"))
    architecture_notes = _section(raw, "Architecture Notes")

    # Parse tasks
    tasks: list[TaskSpec] = []
    task_blocks = re.finditer(
        r'### (T\d+): (.+?)\n(.*?)(?=\n### T\d+|\Z)',
        raw, re.DOTALL
    )
    for m in task_blocks:
        tid = m.group(1)
        ttitle = m.group(2).strip()
        body = m.group(3).strip()

        files_m = re.search(r'Files:\s*(.+)', body)
        desc_m = re.search(r'Description:\s*(.+)', body)
        done_m = re.search(r'Done when:\s*(.+)', body)
        dep_m = re.search(r'Depends on:\s*(.+)', body)

        files = [f.strip() for f in (files_m.group(1) if files_m else "").split(",") if f.strip()]
        desc = desc_m.group(1).strip() if desc_m else ttitle
        done = done_m.group(1).strip() if done_m else ""
        deps_raw = dep_m.group(1).strip() if dep_m else ""
        deps = [d.strip() for d in re.findall(r'T\d+', deps_raw)]

        tasks.append(TaskSpec(
            id=tid,
            title=ttitle,
            description=desc,
            files=files,
            acceptance_criteria=[done] if done else [],
            depends_on=deps,
        ))

    return FeatureSpec(
        title=title,
        user_story=user_story,
        requirements=requirements,
        acceptance_criteria=acceptance_criteria,
        architecture_notes=architecture_notes,
        tasks=tasks,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def decompose(
    task: str,
    context: str = "",
    call_fn: Callable[[str], str] | None = None,
    stack_hints: str = "",
) -> FeatureSpec:
    """
    Decompose an abstract task description into a structured FeatureSpec.

    Parameters
    ----------
    task:         Natural language description of the feature or change.
    context:      Optional: existing code stubs or interface definitions.
                  Pass compressed/stubbed context (pipeline.run_stub) for token efficiency.
    call_fn:      LLM callable. Defaults to a no-op that returns a minimal spec
                  (useful for testing without a model).
    stack_hints:  Rendered stack hints block (from stacks.render_hints) to inject
                  library/architecture constraints into the decomposition prompt.

    Returns a FeatureSpec with user story, requirements, acceptance criteria,
    architecture notes, and a list of atomic TaskSpecs.
    """
    if call_fn is None:
        # Minimal fallback spec — one task = the full task description
        return FeatureSpec(
            title=task[:60],
            user_story=f"As a developer, I want to {task}.",
            tasks=[TaskSpec(id="T1", title=task[:60], description=task)],
        )

    prompt = _build_prompt(task, context, stack_hints)
    raw = call_fn(prompt)
    return _parse_spec(raw, task)


def print_spec(spec: FeatureSpec) -> None:
    """Pretty-print a FeatureSpec for debugging."""
    print(f"\n{'='*70}")
    print(f"  SPEC: {spec.title}")
    print(f"{'='*70}")
    print(f"  Story: {spec.user_story}")
    if spec.requirements:
        print(f"\n  Requirements:")
        for r in spec.requirements:
            print(f"    • {r}")
    if spec.acceptance_criteria:
        print(f"\n  Acceptance Criteria:")
        for ac in spec.acceptance_criteria:
            print(f"    ☐ {ac}")
    if spec.architecture_notes:
        print(f"\n  Architecture: {spec.architecture_notes}")
    if spec.tasks:
        print(f"\n  Tasks ({len(spec.tasks)}):")
        for t in spec.tasks:
            deps = f"  [after {', '.join(t.depends_on)}]" if t.depends_on else ""
            print(f"    {t.id}: {t.title}{deps}")
            print(f"         Files: {', '.join(t.files) or '(unspecified)'}")
            if t.acceptance_criteria:
                print(f"         Done:  {t.acceptance_criteria[0]}")
    print(f"{'='*70}\n")
