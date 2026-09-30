# ABOUTME: Pre-dispatch usage/cost estimation (Story 14.1-002) — guess a stage's
# ABOUTME: tokens + notional-$ before the agent runs, for warning and gating.

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import NamedTuple

from sdlc.model_routing import TIER_MODEL_IDS

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

# Date the price table below was read from the published pricing page. Rendered
# beside every `$` figure (dashboard run header, `sdlc status`) so a reader knows
# which list prices produced it. Bump together with the table (Story 34.2-001).
PRICE_TABLE_VINTAGE = "2026-09-30"


class ModelRate(NamedTuple):
    """List price in USD per million tokens for one model id."""

    input: float
    output: float
    cache_read: float


# Notional list-price equivalents keyed by exact model id (Story 34.2-001).
# Guidance only — both harnesses bill by subscription, so this stays an
# API-equivalent signal, never real spend. Tier aliases resolve through
# ``model_routing.TIER_MODEL_IDS`` (:func:`price_id`). An id absent here costs at
# the opus default (:data:`FALLBACK_PRICE_ID`) and `sdlc doctor` warns once per id.
MODEL_USD_PER_MILLION_TOKENS: dict[str, ModelRate] = {
    "claude-opus-5-5": ModelRate(4.0, 20.0, 0.20),
    "claude-opus-5": ModelRate(5.0, 25.0, 0.50),
    "claude-sonnet-5-5": ModelRate(2.0, 10.0, 0.20),
    "claude-sonnet-5": ModelRate(2.0, 10.0, 0.20),
    "claude-sonnet-4-6": ModelRate(3.0, 15.0, 0.30),
    "claude-haiku-4-5": ModelRate(1.0, 5.0, 0.10),
}

# The entry an unpriced id (Codex free-form ids, a future model) costs at.
FALLBACK_PRICE_ID = "claude-opus-5-5"

# Cache writes (5-minute TTL) list at 1.25x the input rate.
CACHE_WRITE_MULTIPLIER = 1.25


def price_id(model: str | None) -> str | None:
    """The :data:`MODEL_USD_PER_MILLION_TOKENS` key ``model`` prices under, or None.

    Resolves a tier alias through the routing map, then matches the id exactly or
    as a dated snapshot of a priced id (``claude-haiku-4-5-20251001``). None means
    the id has no list price (including an unlabeled ``None`` model).
    """
    if not model:
        return None
    resolved = TIER_MODEL_IDS.get(model, model)
    if resolved in MODEL_USD_PER_MILLION_TOKENS:
        return resolved
    matches = [k for k in MODEL_USD_PER_MILLION_TOKENS if resolved.startswith(k + "-")]
    return max(matches, key=len) if matches else None


def model_rate(model: str | None) -> ModelRate:
    """The list rate for ``model``, falling back to the opus default if unpriced."""
    return MODEL_USD_PER_MILLION_TOKENS[price_id(model) or FALLBACK_PRICE_ID]


def blended_usd_per_million(model: str | None) -> float:
    """Average of input/output rates, for estimates that only know a token total."""
    rate = model_rate(model)
    return (rate.input + rate.output) / 2


def usage_cost(
    model: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Notional dollars for one stage usage row, each token class at its own rate."""
    rate = model_rate(model)
    usd = (
        input_tokens * rate.input
        + output_tokens * rate.output
        + cache_read_tokens * rate.cache_read
        + cache_creation_tokens * rate.input * CACHE_WRITE_MULTIPLIER
    ) / 1_000_000
    return round(usd, 6)


def price_vintage_label(cost_usd: float) -> str:
    """``$0.231 · prices 2026-09-30`` — a `$` figure stamped with its price table."""
    digits = 3 if cost_usd < 1 else 2
    return f"${cost_usd:.{digits}f} · prices {PRICE_TABLE_VINTAGE}"


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
