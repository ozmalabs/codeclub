"""
brevity.py — Scale-aware brevity constraints for LLM prompts.

Based on: arXiv:2604.00025 "Inverse Scaling Can Be Easily Overcome With Scale-Aware
Prompting" (Hakim et al., 2026)

Key findings:
  - Larger models suffer "spontaneous scale-dependent verbosity" — they over-explain
    and introduce errors through overelaboration.
  - Brevity constraints improve large-model accuracy by 26 percentage points.
  - On mathematical/scientific tasks, brevity constraints *reverse* the scaling
    hierarchy — small models (0.5B–3B) beat large ones without constraints.
  - Dataset-specific optimal scales range from 0.5B to 3.0B parameters.

How this interacts with our compression pipeline:
  - We compress INPUT tokens (stubs, symbol table, compact passes) → ~70–94% savings.
  - Brevity constraints compress OUTPUT tokens → ~75% savings (Caveman mode).
  - Together: smaller models receive focused, compressed input + are told to be brief.
    This unlocks the small-model advantage the paper identifies.

Constraint design — what our benchmark showed:
  - "Output only the changed code, no explanation." (too vague for SMALL)
    → gpt-5-mini produced a diff fragment, missed the merge body, quality 80%
  - No constraint: gpt-5-mini retrieved → 100% quality, 43s, verbose
  - Root issue: brevity and completeness are orthogonal — the constraint must
    suppress padding WITHOUT sacrificing the complete function body.

  The fix: for code-edit tasks, the SMALL constraint must:
    1. Name the exact output format ("complete function, runnable as-is")
    2. Prohibit padding explicitly ("no prose, no diff format")
    3. NOT say "only changed code" — that triggers diff-format responses

  MEDIUM/LARGE: over-elaboration is the failure mode → stronger suppression OK.
  SMALL: under-completion is the failure mode → constraint must guarantee output shape.

Usage:
    from brevity import BrevityPrompt, ModelTier

    # Wrap any task prompt with scale-aware brevity constraint
    prompt = BrevityPrompt.wrap(task, tier=ModelTier.SMALL)

    # For code-edit tasks, use the code-specific template
    prompt = BrevityPrompt.code_edit(file_context, task, tier=ModelTier.SMALL)

    # A/B test constraint wording variants
    prompt = BrevityPrompt.code_edit(ctx, task, tier=ModelTier.SMALL, variant="v2_structured")
"""

from __future__ import annotations

from enum import Enum


class ModelTier(Enum):
    """
    Model size tier for scale-aware prompt selection.

    Based on paper findings:
      SMALL  (0.5B–3B):  benefits most from brevity constraints on focused tasks
      MEDIUM (7B–13B):   moderate brevity helps
      LARGE  (30B+):     strong brevity constraint needed to overcome verbosity bias
    """
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


# ---------------------------------------------------------------------------
# Brevity constraint templates (from paper §4 — Causal Intervention)
# ---------------------------------------------------------------------------

# The paper shows that the specific wording matters — "brief" alone is weaker
# than constraints that specify the output format and prohibit overelaboration.

_BREVITY_SUFFIX = {
    ModelTier.SMALL: (
        "Be concise. Give only the answer, no explanation."
    ),
    ModelTier.MEDIUM: (
        "Be brief and direct. No preamble, no explanation, no summary. "
        "Output only what was asked."
    ),
    ModelTier.LARGE: (
        "IMPORTANT: Be maximally concise. Do NOT explain your reasoning. "
        "Do NOT repeat the question. Do NOT add commentary. "
        "Output ONLY the requested content, nothing else."
    ),
}

# For code-editing tasks specifically — benchmark-tuned per tier.
#
# SMALL failure mode: under-completion (outputs diffs/fragments instead of full functions)
#   Fix: explicitly require "complete, runnable function" + prohibit diff format
#
# MEDIUM failure mode: mild over-explanation
#   Fix: permit brief inline comment, prohibit prose paragraphs
#
# LARGE failure mode: over-elaboration, padding, repetition
#   Fix: strong suppression of all non-code output
_CODE_BREVITY_SUFFIX = {
    ModelTier.SMALL: (
        # Benchmark winner: v4_minimal — consistent 100% quality, fewest output tokens.
        # Counter-intuitively, prescribing "complete function" bloated output (3698 tok)
        # or confused the model into outputting body-only fragments. Trust the model;
        # just suppress prose padding.
        "No explanation. Code only."
    ),
    ModelTier.MEDIUM: (
        "Output only the modified function(s), complete and runnable. "
        "A single inline comment explaining the key change is fine. "
        "No prose paragraphs, no diff format."
    ),
    ModelTier.LARGE: (
        "CRITICAL: Output ONLY the code change (unified diff or full modified function). "
        "No explanation. No commentary. No 'I changed X because Y'. Just the code."
    ),
}

# Variant templates for A/B testing SMALL tier wording.
# Each addresses the "complete but not padded" problem differently.
_CODE_BREVITY_VARIANTS: dict[str, str] = {
    # v0: original (baseline — caused 80% quality from diff-format responses)
    "v0_original": (
        "Output only the changed code, no explanation."
    ),
    # v1: explicit format + anti-diff rule (current default for SMALL)
    "v1_complete_function": (
        "Output ONLY a complete, runnable Python function (or functions) that fixes the issue. "
        "Include the full function body — not a diff, not a fragment. "
        "No prose before or after the code block."
    ),
    # v2: structured output template (format anchoring per paper §4)
    "v2_structured": (
        "Reply in this format only:\n"
        "```python\n"
        "# <one line: what changed>\n"
        "<complete fixed function>\n"
        "```\n"
        "Nothing else."
    ),
    # v3: anchor on correctness, not brevity
    "v3_correctness_first": (
        "Write the complete, corrected function body. "
        "It must be runnable as-is. "
        "Skip all explanation — just the code."
    ),
    # v4: minimal — no format prescription (control)
    "v4_minimal": (
        "No explanation. Code only."
    ),
}

# Caveman mode — maximum token efficiency, ~75% output reduction
# Pairs with JuliusBrussee/caveman for extreme compression
_CAVEMAN_SUFFIX = (
    "Respond in caveman-style compressed prose. "
    "Short words. No filler. Essential meaning only. "
    "For code: output only changed lines."
)


class BrevityPrompt:
    """
    Factory for scale-aware brevity-constrained prompts.

    The paper establishes that the brevity constraint must be:
      1. Explicit — not implied, not soft
      2. Format-specific — "give a diff" is better than "be brief"
      3. Placed at the END of the prompt (recency bias in attention)
    """

    @staticmethod
    def wrap(prompt: str, *, tier: ModelTier = ModelTier.LARGE) -> str:
        """Add a brevity constraint suffix to any prompt."""
        return f"{prompt}\n\n{_BREVITY_SUFFIX[tier]}"

    @staticmethod
    def code_edit(
        context: str,
        task: str,
        *,
        tier: ModelTier = ModelTier.LARGE,
        caveman: bool = False,
        variant: str | None = None,
    ) -> str:
        """
        Build a full code-edit prompt with compressed context and brevity constraint.

        Parameters
        ----------
        context:
            Compressed code context (output of pipeline.run_stub or retriever.render_retrieved_context)
        task:
            The task description / change request
        tier:
            Model size tier — affects verbosity constraint strength
        caveman:
            If True, apply Caveman-style maximum output compression (~75% output savings)
        variant:
            Optional A/B test variant key. One of: v0_original, v1_complete_function,
            v2_structured, v3_correctness_first, v4_minimal.
            If None, uses the default constraint for the tier.

        Returns
        -------
        Full prompt string ready to send to LLM
        """
        if caveman:
            suffix = _CAVEMAN_SUFFIX
        elif variant and variant in _CODE_BREVITY_VARIANTS:
            suffix = _CODE_BREVITY_VARIANTS[variant]
        else:
            suffix = _CODE_BREVITY_SUFFIX[tier]
        return (
            f"<context>\n{context}\n</context>\n\n"
            f"<task>\n{task}\n</task>\n\n"
            f"{suffix}"
        )

    @staticmethod
    def with_source_map_instruction(
        context: str,
        task: str,
        *,
        tier: ModelTier = ModelTier.LARGE,
        variant: str | None = None,
    ) -> str:
        """
        Code-edit prompt that tells the LLM it is working with stubs.

        The LLM is instructed to:
          - Keep '...' for functions it doesn't need to change
          - Replace '...' only for functions it needs to modify
          - Output the modified stub file (not the full expanded file)

        The expander.expand() call then restores original bodies for kept stubs.
        """
        if variant and variant in _CODE_BREVITY_VARIANTS:
            suffix = _CODE_BREVITY_VARIANTS[variant]
        else:
            suffix = _CODE_BREVITY_SUFFIX[tier]
        return (
            f"<context>\n"
            f"# NOTE: Function bodies are replaced with '...' to save tokens.\n"
            f"# Keep '...' for functions you are NOT changing.\n"
            f"# Replace '...' only for functions you ARE changing — write the full new body.\n"
            f"{context}\n"
            f"</context>\n\n"
            f"<task>\n{task}\n</task>\n\n"
            f"{suffix}"
        )


# ---------------------------------------------------------------------------
# A/B test runner for constraint variants
# ---------------------------------------------------------------------------

def ab_test_variants(
    context: str,
    task: str,
    call_fn,  # callable(prompt: str) -> str
    variants: list[str] | None = None,
) -> dict[str, str]:
    """
    Run constraint variants against a live LLM call function.

    Parameters
    ----------
    context:   compressed context string
    task:      task description
    call_fn:   callable(prompt) -> output_text
    variants:  list of variant keys to test (default: all)

    Returns
    -------
    {variant_key: output_text}
    """
    if variants is None:
        variants = list(_CODE_BREVITY_VARIANTS.keys())

    results: dict[str, str] = {}
    for v in variants:
        prompt = BrevityPrompt.code_edit(context, task, tier=ModelTier.SMALL, variant=v)
        try:
            results[v] = call_fn(prompt)
        except Exception as e:
            results[v] = f"ERROR: {e}"
    return results


# ---------------------------------------------------------------------------
# Model routing recommendation (paper §5 — Deployment Implications)
# ---------------------------------------------------------------------------

_CODE_ROUTING_THRESHOLDS = {
    # task complexity → recommended tier
    # Paper: dataset-specific optimal scales range from 0.5B to 3.0B
    "signature_only":    ModelTier.SMALL,   # renaming, type hint changes
    "single_function":   ModelTier.SMALL,   # one function body change
    "multi_function":    ModelTier.MEDIUM,  # coordinated changes across functions
    "cross_file":        ModelTier.LARGE,   # changes across multiple files
    "architecture":      ModelTier.LARGE,   # design-level changes
}


def recommend_tier(
    num_files_changed: int,
    num_functions_changed: int,
    has_cross_file_deps: bool = False,
) -> ModelTier:
    """
    Recommend a model tier based on task complexity.

    Based on paper finding: smaller models with brevity constraints are optimal
    for focused, well-scoped tasks. Use large models only when truly needed.

    With our input compression:
      SMALL tier + compressed context → equivalent to LARGE tier + full context
      (at 10-100x lower cost, per paper's parameter ratio findings)
    """
    if has_cross_file_deps or num_files_changed > 2:
        return ModelTier.LARGE
    if num_functions_changed > 3 or num_files_changed > 1:
        return ModelTier.MEDIUM
    return ModelTier.SMALL
