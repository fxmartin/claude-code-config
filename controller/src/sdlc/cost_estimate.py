# ABOUTME: Pre-dispatch usage/cost estimation (Story 14.1-002) — guess a stage's
# ABOUTME: tokens + notional-$ before the agent runs, for warning and gating.

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

# Heuristic: ~4 characters per token for mixed English+code prompts. Deliberately
# crude — the estimate is *guidance*; the authoritative figure remains the
# post-stage `--output-format` usage envelope reconciled at completion. Tuning
# this never has to be exact, only good enough to flag an unusually large prompt.
CHARS_PER_TOKEN = 4

# Notional API-equivalent price (mirrors build.NOTIONAL_USD_PER_MILLION_TOKENS).
# On a Claude Max subscription the dollar figure is an API-list-price equivalent
# computed from tokens — never real spend on the flat monthly fee — so this is a
# documented convenience constant, not a billing fact. A blended ~$15/Mtok keeps
# the conversion easy to reason about ($15 ⇒ 1M tokens).
DEFAULT_USD_PER_MILLION_TOKENS = 15.0

# Blended notional list-price equivalents per Claude tier alias (simple average
# of published input/output USD per Mtok, 2026-07: haiku $1/$5, sonnet $3/$15,
# opus $5/$25). Guidance only — both harnesses bill by subscription, so this
# stays an API-equivalent signal, never real spend. Unknown/unlabeled model ids
# (Codex free-form ids, routing-off None) fall back to the opus-equivalent
# DEFAULT_USD_PER_MILLION_TOKENS, preserving pre-#427 behavior.
MODEL_USD_PER_MILLION_TOKENS: dict[str, float] = {
    "haiku": 3.0,
    "sonnet": 9.0,
    "opus": 15.0,
}

# Per-stage multiplier: estimated *total* tokens (assembled prompt + the agent's
# generated output + its tool round-trips) as a multiple of the prompt's own
# tokens. A `build` turns a short prompt into a long edit/test session with many
# tool calls, so its factor is high; a mechanical `merge` stays close to its
# prompt. These are coarse priors used only until the ledger has historical
# per-stage usage to calibrate against (see :func:`estimate_stage`).
DEFAULT_STAGE_FACTORS: dict[str, float] = {
    "discovery": 4.0,
    "build": 12.0,
    "coverage": 10.0,
    "review": 6.0,
    "adversarial": 6.0,
    "merge": 3.0,
    "bugfix": 8.0,
    "reask": 2.0,
}

# Fallback multiplier for a stage absent from the map (a future / custom stage),
# so estimation never raises on an unrecognised stage name.
DEFAULT_STAGE_FACTOR = 6.0


@dataclass(frozen=True)
class CostEstimateConfig:
    """Tunables for the pre-dispatch estimate (Story 14.1-002).

    All fields default to the documented constants so a caller that wants the
    shipped heuristic just constructs ``CostEstimateConfig()``. Frozen so a
    shared default can be passed around without a caller mutating it.
    """

    stage_factors: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_STAGE_FACTORS)
    )
    default_factor: float = DEFAULT_STAGE_FACTOR
    usd_per_million_tokens: float = DEFAULT_USD_PER_MILLION_TOKENS
    chars_per_token: int = CHARS_PER_TOKEN


@dataclass(frozen=True)
class StageEstimate:
    """A pre-dispatch estimate for one stage.

    ``prompt_tokens`` is the heuristic token count of the assembled prompt;
    ``estimated_tokens`` is the projected *total* usage (prompt + output + tool
    round-trips); ``estimated_cost_usd`` is the notional API-equivalent dollars
    for that token count. ``calibrated`` is True when a historical per-stage
    average refined the projection rather than the crude factor.
    """

    stage: str
    prompt_tokens: int
    estimated_tokens: int
    estimated_cost_usd: float
    calibrated: bool = False


def estimate_prompt_tokens(prompt: str, *, chars_per_token: int = CHARS_PER_TOKEN) -> int:
    """Heuristic token count for ``prompt`` (≈ ``len / chars_per_token``).

    Returns 0 for an empty prompt and never less than 1 for a non-empty one, so
    a tiny prompt is not estimated as zero tokens.
    """
    if not prompt:
        return 0
    return max(1, len(prompt) // max(1, chars_per_token))


def notional_cost(
    tokens: int, *, usd_per_million_tokens: float = DEFAULT_USD_PER_MILLION_TOKENS
) -> float:
    """Notional API-equivalent dollars for ``tokens`` (never real subscription spend)."""
    return round(tokens / 1_000_000 * usd_per_million_tokens, 6)


def estimate_stage(
    stage: str,
    prompt: str,
    *,
    config: CostEstimateConfig | None = None,
    historical_tokens: float | None = None,
) -> StageEstimate:
    """Estimate ``stage``'s total usage + notional cost from its assembled prompt.

    When ``historical_tokens`` (a per-stage average from the ledger) is present
    and positive it is used directly as the projection — this is the "calibrate
    against historical per-stage usage" path from the story's technical note.
    Otherwise the crude ``prompt_tokens × stage_factor`` heuristic applies. The
    projection is floored at the prompt's own token count so it can never read as
    less than what we already know will be sent.
    """
    cfg = config or CostEstimateConfig()
    prompt_tokens = estimate_prompt_tokens(prompt, chars_per_token=cfg.chars_per_token)

    calibrated = historical_tokens is not None and historical_tokens > 0
    if calibrated and historical_tokens is not None:
        estimated = int(round(historical_tokens))
    else:
        factor = cfg.stage_factors.get(stage, cfg.default_factor)
        estimated = int(round(prompt_tokens * factor))

    estimated = max(estimated, prompt_tokens)
    cost = notional_cost(estimated, usd_per_million_tokens=cfg.usd_per_million_tokens)
    return StageEstimate(
        stage=stage,
        prompt_tokens=prompt_tokens,
        estimated_tokens=estimated,
        estimated_cost_usd=cost,
        calibrated=calibrated,
    )


# ---------------------------------------------------------------------------
# Story 28.3-002: batch projection — summed per-story predictions for the
# budget gate's projected-remaining view and the rate-limit window planner
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BatchProjection:
    """Summed 28.2-002 predictions over a batch of not-yet-run stories.

    The figure the budget gate (14.1-001) projects remaining spend from and the
    batch planner (14.1-003) checks against the rate-limit window budget.
    ``fallback_stories`` counts stories the predictor produced nothing for —
    they contribute zero tokens to the sum, so any fallback makes the projection
    partial and forces ``confidence`` to ``low`` rather than letting an
    undercount read as a tight forecast.
    """

    predicted_tokens: int
    predicted_stories: int
    fallback_stories: int
    low_confidence_stories: int

    @property
    def usable(self) -> bool:
        """Whether any story at all carries a prediction to project from."""
        return self.predicted_stories > 0

    @property
    def confidence(self) -> str:
        """``high`` only when every story predicted with high confidence."""
        if (
            not self.predicted_stories
            or self.fallback_stories
            or self.low_confidence_stories
        ):
            return "low"
        return "high"

    def fits_window(self, window_budget: int) -> bool:
        """Whether the summed prediction fits one rate-limit window (inclusive)."""
        return self.predicted_tokens <= window_budget

    def windows_needed(self, window_budget: int) -> int:
        """Rolling windows the batch is projected to span (0 = no window set)."""
        if window_budget <= 0:
            return 0
        return max(1, math.ceil(self.predicted_tokens / window_budget))


def project_batch(predictions: Iterable[object | None]) -> BatchProjection:
    """Sum per-story predictions into a :class:`BatchProjection` (Story 28.3-002).

    ``predictions`` holds one entry per story in the batch: a 28.2-002
    ``StoryPrediction``-shaped object (``predicted_tokens`` +
    ``low_confidence``), or ``None`` for a story the predictor degraded on.
    Duck-typed on those two attributes so this module stays free of an
    ``sdlc.predictor`` import — the estimate side consumes the prediction, it
    does not depend on how it was modelled.
    """
    total = predicted = fallback = low = 0
    for prediction in predictions:
        if prediction is None:
            fallback += 1
            continue
        predicted += 1
        total += int(prediction.predicted_tokens)  # type: ignore[attr-defined]
        if prediction.low_confidence:  # type: ignore[attr-defined]
            low += 1
    return BatchProjection(
        predicted_tokens=total,
        predicted_stories=predicted,
        fallback_stories=fallback,
        low_confidence_stories=low,
    )
