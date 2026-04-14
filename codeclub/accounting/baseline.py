"""
accounting/baseline.py — Reference cost comparisons ("savings vs X").

Answers: "what would this task have cost without codeclub?"

Two dimensions of savings:
  1. Compression savings — fewer tokens sent because context was compressed.
     Baseline: sending full uncompressed files.
  2. Model savings — using local/cheap models instead of GPT-5.4/Claude Sonnet 4.6.
     Baseline: running the same task on a premium cloud model.

These are independent. You can have both, either, or neither.

Example output:
  Actual cost:       $0.00003  (local B580, energy only)
  vs GPT-5.4:        $0.1125   (3,750× more expensive)
  vs Sonnet 4.6:     $0.1050
  vs uncompressed:   $0.0024   (80× more, same model)
  Compression ratio: 94.2%
  Tokens saved:      12,400
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .tracker import TaskLedger


# ---------------------------------------------------------------------------
# Reference model pricing (USD per million tokens, input / output)
# Update as prices change — these are the main reference points.
# ---------------------------------------------------------------------------

BASELINE_MODELS: dict[str, tuple[float, float]] = {
    # model_id → (cost_in_per_M, cost_out_per_M)
    "gpt-5.4":             (2.50,  15.00),
    "gpt-5.3-codex":       (1.75,  14.00),
    "gpt-5":               (1.25,  10.00),
    "gpt-5-mini":          (0.25,   2.00),
    "gpt-4.1":             (2.00,   8.00),
    "gpt-4o":              (2.50,  10.00),
    "claude-opus-4-6":     (5.00,  25.00),
    "claude-sonnet-4-6":   (3.00,  15.00),
    "claude-haiku-4-5":    (1.00,   5.00),
    "deepseek-v3.1":       (0.15,   0.75),
    "llama-3.3-70b":       (0.10,   0.32),
}

# Default reference for "what would this have cost on a premium model"
DEFAULT_REFERENCE_MODEL = "gpt-5.4"


# ---------------------------------------------------------------------------
# Savings report
# ---------------------------------------------------------------------------

@dataclass
class SavingsReport:
    """
    Cost comparison for a completed task.

    actual_cost_usd:     what was actually spent (API + energy)
    reference_costs:     {model_id: cost_usd} for each baseline model
    compression_ratio:   fraction of tokens removed by compression (0–1)
    tokens_actual:       tokens actually sent
    tokens_uncompressed: estimated tokens without compression
    """
    actual_cost_usd: float
    reference_costs: dict[str, float] = field(default_factory=dict)
    compression_ratio: float = 0.0
    tokens_actual: int = 0
    tokens_uncompressed: int = 0

    @property
    def tokens_saved(self) -> int:
        return max(0, self.tokens_uncompressed - self.tokens_actual)

    def savings_vs(self, model_id: str) -> Optional[float]:
        """Absolute saving (USD) vs a reference model. Positive = we spent less."""
        ref = self.reference_costs.get(model_id)
        if ref is None:
            return None
        return ref - self.actual_cost_usd

    def multiplier_vs(self, model_id: str) -> Optional[float]:
        """How many times more expensive the reference model would have been."""
        ref = self.reference_costs.get(model_id)
        if ref is None or self.actual_cost_usd <= 0:
            return None
        return ref / self.actual_cost_usd

    def format(self, *, include_all: bool = False) -> str:
        lines = []
        lines.append(f"  ┌─ Cost comparison {'─'*41}")
        lines.append(f"  │  Actual cost      ${self.actual_cost_usd:.6f}")

        # Show key references
        show = (
            list(BASELINE_MODELS.keys())
            if include_all
            else [DEFAULT_REFERENCE_MODEL, "claude-sonnet-4-6", "gpt-5-mini"]
        )
        for model in show:
            ref = self.reference_costs.get(model)
            if ref is None:
                continue
            mult = self.multiplier_vs(model)
            mult_str = f"  ({mult:,.0f}× more)" if mult and mult > 1.5 else ""
            lines.append(f"  │  vs {model:<22} ${ref:.6f}{mult_str}")

        if self.compression_ratio > 0:
            lines.append(f"  │")
            lines.append(f"  │  Compression      {self.compression_ratio*100:.1f}% context removed")
            lines.append(f"  │  Tokens actual    {self.tokens_actual:,}")
            if self.tokens_uncompressed > 0:
                lines.append(f"  │  Tokens w/o compress {self.tokens_uncompressed:,}  "
                              f"(saved {self.tokens_saved:,})")

        lines.append(f"  └{'─'*51}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Compute savings
# ---------------------------------------------------------------------------

def compute_savings(
    ledger: TaskLedger,
    *,
    uncompressed_tokens: Optional[int] = None,
    reference_models: Optional[list[str]] = None,
) -> SavingsReport:
    """
    Compute what the same task would have cost on each reference model.

    Parameters
    ----------
    ledger:               Completed TaskLedger from a run.
    uncompressed_tokens:  Total tokens that would have been sent without
                          compression.  If None, assumes no compression was used
                          (compression_ratio = 0).
    reference_models:     Which baseline models to compare against.
                          Defaults to all models in BASELINE_MODELS.

    The reference cost is computed by assuming the reference model was used
    for ALL phases (spec + map + fill + testgen + review + report) at its
    published rate, with the same token counts as actually observed.
    """
    models = reference_models or list(BASELINE_MODELS.keys())
    actual_tokens_in  = ledger.total_tokens_in
    actual_tokens_out = ledger.total_tokens_out

    reference_costs: dict[str, float] = {}
    for model_id in models:
        rates = BASELINE_MODELS.get(model_id)
        if rates is None:
            continue
        rate_in, rate_out = rates
        cost = (
            actual_tokens_in  * rate_in  / 1_000_000
          + actual_tokens_out * rate_out / 1_000_000
        )
        reference_costs[model_id] = cost

    # Compression ratio
    compression_ratio = 0.0
    if uncompressed_tokens and uncompressed_tokens > actual_tokens_in:
        compression_ratio = 1.0 - actual_tokens_in / uncompressed_tokens

    return SavingsReport(
        actual_cost_usd=ledger.total_cost_usd(),
        reference_costs=reference_costs,
        compression_ratio=compression_ratio,
        tokens_actual=actual_tokens_in,
        tokens_uncompressed=uncompressed_tokens or 0,
    )


def format_full_report(ledger: TaskLedger, savings: Optional[SavingsReport] = None) -> str:
    """Combined accounting + savings report suitable for printing."""
    parts = [ledger.summary(verbose=True)]
    if savings is not None:
        parts.append(savings.format())
    return "\n".join(parts)
