# ABOUTME: Per-story token + rework-probability predictor (Story 28.2-002) — a
# ABOUTME: crude, inspectable model trained on the ledger's own reconciled history.

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from statistics import fmean, median

# Version stamped onto every recorded prediction. Bump it whenever the cohort
# ladder, the band edges, or any weight in :class:`PredictorConfig` changes, so a
# recalibration is auditable: `sdlc predict-quality` reports error *per version*
# rather than blending a re-tuned model's numbers into the old model's history.
PREDICTOR_VERSION = "v1"

# The label a feature the epic did not state resolves to. Kept distinct from any
# real band so an unknown can never be silently read as a small story.
UNKNOWN = "unknown"

# Scope-proxy band edges (inclusive upper bounds) → band label. The proxy is the
# count of distinct files/areas the story names (Story 28.2-001), which is the
# closest thing the epic states to "how much surface does this touch". These
# edges are priors, not fitted values — the n=76 reconciled dataset is far too
# thin to fit cut points on, and pretending otherwise would be the exact
# false-precision this epic exists to remove.
SCOPE_BAND_EDGES: tuple[tuple[int, str], ...] = ((3, "s"), (8, "m"))
SCOPE_BAND_LARGE = "l"

# `**Risk Level**:` values the epics actually use, normalised to a short flag.
_RISK_ALIASES: dict[str, str] = {
    "low": "low",
    "medium": "med",
    "med": "med",
    "high": "high",
}

# How many reconciled stories a keyed cohort needs before the predictor will use
# its mean instead of the global one. Below this the cohort mean is noise, so the
# prediction degrades to the global mean and is flagged low-confidence (AC4).
MIN_COHORT_SAMPLE = 5

# How much *total* reconciled history the global mean itself needs before a
# prediction can claim confidence. The factory's own dataset was n=76 when this
# shipped, so this bar is deliberately low — but a 2-story ledger must still read
# as "we barely know", never as a confident number.
MIN_GLOBAL_SAMPLE = 10

# Predicted-probability buckets the rework calibration summary reports over.
REWORK_BIN_EDGES: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)


def scope_band(scope_proxy: int | None) -> str:
    """The size band for a story's scope proxy, or ``unknown`` when unstated."""
    if scope_proxy is None:
        return UNKNOWN
    for edge, label in SCOPE_BAND_EDGES:
        if scope_proxy <= edge:
            return label
    return SCOPE_BAND_LARGE


def risk_flag(risk: str | None) -> str:
    """Normalise an inventory ``risk`` value to ``low``/``med``/``high``/``unknown``."""
    return _RISK_ALIASES.get((risk or "").strip().lower(), UNKNOWN)


@dataclass(frozen=True)
class StoryFeatures:
    """The predictor's inputs for one story.

    The first three are the Story 28.2-001 discovery features (``None`` means the
    epic did not state enough to compute it — *unknown*, never a real zero).
    ``risk`` is the ``**Risk Level**:`` the story inventory projected, used only
    as a cohort key; it is not a discovery feature, so an unknown risk alone does
    not make the vector incomplete.
    """

    ac_count: int | None = None
    dep_depth: int | None = None
    scope_proxy: int | None = None
    risk: str | None = None

    @property
    def band(self) -> str:
        return scope_band(self.scope_proxy)

    @property
    def risk_key(self) -> str:
        return risk_flag(self.risk)

    @property
    def has_unknown(self) -> bool:
        """True when any *discovery* feature is missing."""
        return (
            self.ac_count is None
            or self.dep_depth is None
            or self.scope_proxy is None
        )

    @classmethod
    def from_row(cls, row: Mapping) -> "StoryFeatures":
        """Build a feature vector from a ledger story row (NULL stays unknown)."""
        def _int(key: str) -> int | None:
            value = row.get(key)
            return None if value is None else int(value)

        risk = row.get("risk")
        return cls(
            ac_count=_int("ac_count"),
            dep_depth=_int("dep_depth"),
            scope_proxy=_int("scope_proxy"),
            risk=str(risk) if risk else None,
        )


@dataclass(frozen=True)
class PredictorConfig:
    """The model's tunables — every number the prediction depends on, in one place.

    The weights are **priors**, not fitted coefficients: each says "a story with
    twice the reference acceptance criteria costs ~15% more than its cohort's
    mean", and they are deliberately small so the cohort mean, which *is*
    measured, dominates. The clamps stop a crude linear adjustment from producing
    an absurd number for an outlier feature value. Changing any of these is a
    recalibration — bump :data:`PREDICTOR_VERSION` with it.
    """

    # Reference (mid) feature values the adjustments are relative to.
    ac_ref: int = 5
    scope_ref: int = 6
    # Token-adjustment weights (multiplicative on the cohort mean).
    ac_weight: float = 0.15
    scope_weight: float = 0.20
    dep_weight: float = 0.05
    min_adjustment: float = 0.5
    max_adjustment: float = 2.0
    # Rework-probability adjustments (additive on the cohort's observed rate).
    rework_ac_weight: float = 0.02
    rework_scope_weight: float = 0.01
    rework_dep_weight: float = 0.03
    # A never-reworked cohort is not proof rework is impossible, and an
    # always-reworked one is not proof it is certain; bound both away from the
    # absolutes the thin sample cannot support.
    min_rework: float = 0.02
    max_rework: float = 0.95


@dataclass(frozen=True)
class TrainingRow:
    """One reconciled story: its features, what it actually cost, whether it reworked."""

    features: StoryFeatures
    actual_tokens: int
    rework: bool


@dataclass(frozen=True)
class Cohort:
    """The historical bucket a prediction is drawn from, and how big it is."""

    key: str
    tier: str  # 'band+risk' | 'band' | 'global'
    mean_tokens: float
    rework_rate: float
    sample: int


@dataclass(frozen=True)
class StoryPrediction:
    """A pre-dispatch prediction, with everything needed to audit it later."""

    predicted_tokens: int
    predicted_rework_probability: float
    low_confidence: bool
    version: str
    cohort_key: str
    cohort_tier: str
    sample_size: int
    basis: str


@dataclass(frozen=True)
class PredictorHistory:
    """The reconciled training set the predictor draws its cohorts from."""

    rows: tuple[TrainingRow, ...] = ()

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping]) -> "PredictorHistory":
        """Build history from ledger training rows (see ``Ledger.prediction_training_rows``)."""
        training: list[TrainingRow] = []
        for row in rows:
            tokens = row.get("actual_tokens")
            if tokens is None:
                continue
            training.append(
                TrainingRow(
                    features=StoryFeatures.from_row(row),
                    actual_tokens=int(tokens),
                    rework=bool(row.get("actual_rework")),
                )
            )
        return cls(tuple(training))

    def cohort(self, features: StoryFeatures) -> Cohort | None:
        """The tightest cohort with enough history for ``features``, or None when empty.

        Walks a three-rung ladder — ``band+risk`` → ``band`` → ``global`` — and
        returns the first rung holding at least :data:`MIN_COHORT_SAMPLE` rows;
        ``global`` is the terminal rung and is returned with whatever it has (the
        caller flags a thin global as low-confidence). A story whose scope proxy
        is unknown has no band to key on, so both band rungs are skipped entirely
        rather than pooled into a fake "unknown" bucket (AC4).
        """
        if not self.rows:
            return None

        def _build(tier: str, key: str, matched: list[TrainingRow]) -> Cohort:
            return Cohort(
                key=key,
                tier=tier,
                mean_tokens=fmean(r.actual_tokens for r in matched),
                rework_rate=sum(1 for r in matched if r.rework) / len(matched),
                sample=len(matched),
            )

        if features.band != UNKNOWN:
            keyed: list[tuple[str, str, list[TrainingRow]]] = [
                (
                    "band+risk",
                    f"{features.band}/{features.risk_key}",
                    [
                        r for r in self.rows
                        if r.features.band == features.band
                        and r.features.risk_key == features.risk_key
                    ],
                ),
                (
                    "band",
                    features.band,
                    [r for r in self.rows if r.features.band == features.band],
                ),
            ]
            for tier, key, matched in keyed:
                if len(matched) >= MIN_COHORT_SAMPLE:
                    return _build(tier, key, matched)
        # The terminal rung: the whole reconciled history, taken with whatever it
        # has. `predict_story` flags any prediction that lands here low-confidence.
        return _build("global", "global", list(self.rows))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def predict_story(
    features: StoryFeatures,
    history: PredictorHistory,
    *,
    config: PredictorConfig | None = None,
) -> StoryPrediction | None:
    """Predict ``features``' token cost + rework probability from ``history``.

    The model is deliberately crude and fully inspectable: take the mean of the
    tightest historical cohort that has enough rows, then nudge it by the
    discovery features relative to their reference values. Returns ``None`` when
    there is no reconciled history at all — the caller then degrades to the
    Story 14.1-002 point-keyed stage estimate rather than the predictor inventing
    a number from nothing.

    ``low_confidence`` is set — and never suppressed — when the prediction fell
    back to the global mean, when the whole ledger holds less than
    :data:`MIN_GLOBAL_SAMPLE` reconciled stories, or when any discovery feature is
    unknown.
    """
    cfg = config or PredictorConfig()
    cohort = history.cohort(features)
    if cohort is None:
        return None

    adjustment = 1.0
    if features.ac_count is not None:
        adjustment += cfg.ac_weight * (features.ac_count - cfg.ac_ref) / cfg.ac_ref
    if features.scope_proxy is not None:
        adjustment += (
            cfg.scope_weight * (features.scope_proxy - cfg.scope_ref) / cfg.scope_ref
        )
    if features.dep_depth is not None:
        adjustment += cfg.dep_weight * features.dep_depth
    adjustment = _clamp(adjustment, cfg.min_adjustment, cfg.max_adjustment)

    rework = cohort.rework_rate
    if features.ac_count is not None:
        rework += cfg.rework_ac_weight * (features.ac_count - cfg.ac_ref)
    if features.scope_proxy is not None:
        rework += cfg.rework_scope_weight * (features.scope_proxy - cfg.scope_ref)
    if features.dep_depth is not None:
        rework += cfg.rework_dep_weight * features.dep_depth
    rework = _clamp(rework, cfg.min_rework, cfg.max_rework)

    low_confidence = (
        cohort.tier == "global"
        or len(history.rows) < MIN_GLOBAL_SAMPLE
        or features.has_unknown
    )
    basis = (
        f"cohort={cohort.key} tier={cohort.tier} n={cohort.sample} "
        f"mean={int(round(cohort.mean_tokens)):,} x{adjustment:.2f}"
    )
    return StoryPrediction(
        predicted_tokens=int(round(cohort.mean_tokens * adjustment)),
        predicted_rework_probability=round(rework, 4),
        low_confidence=low_confidence,
        version=PREDICTOR_VERSION,
        cohort_key=cohort.key,
        cohort_tier=cohort.tier,
        sample_size=cohort.sample,
        basis=basis,
    )


# ---------------------------------------------------------------------------
# Prediction quality — measured, with its sample size, never asserted
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReworkBin:
    """One predicted-probability bucket of the rework calibration summary."""

    lower: float
    upper: float
    predicted_mean: float
    observed_rate: float
    sample: int

    def to_dict(self) -> dict:
        return {
            "lower": self.lower,
            "upper": self.upper,
            "predicted_mean": round(self.predicted_mean, 4),
            "observed_rate": round(self.observed_rate, 4),
            "sample": self.sample,
        }


@dataclass(frozen=True)
class QualityReport:
    """Prediction-vs-actual quality, every figure carrying its own sample size."""

    token_median_abs_error: float | None = None
    token_median_abs_pct_error: float | None = None
    token_sample: int = 0
    rework_bins: tuple[ReworkBin, ...] = ()
    rework_brier: float | None = None
    rework_sample: int = 0
    low_confidence_share: float | None = None
    versions: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "token_median_abs_error": self.token_median_abs_error,
            "token_median_abs_pct_error": self.token_median_abs_pct_error,
            "token_sample": self.token_sample,
            "rework_bins": [b.to_dict() for b in self.rework_bins],
            "rework_brier": self.rework_brier,
            "rework_sample": self.rework_sample,
            "low_confidence_share": self.low_confidence_share,
            "versions": list(self.versions),
        }


def _bin_index(probability: float) -> int:
    """The :data:`REWORK_BIN_EDGES` bucket ``probability`` falls in (top bin closed)."""
    for i in range(len(REWORK_BIN_EDGES) - 1):
        if probability < REWORK_BIN_EDGES[i + 1]:
            return i
    return len(REWORK_BIN_EDGES) - 2


def prediction_quality(records: Sequence[Mapping]) -> QualityReport:
    """Score recorded predictions against their reconciled actuals.

    ``records`` are the ledger's reconciled prediction rows (see
    ``Ledger.story_prediction_rows``). Rows whose actual is still unknown are
    excluded from *that* metric rather than counted as agreement, so a ledger with
    nothing reconciled reports an honest "no sample" instead of a vacuous zero
    error. Both headline figures carry their sample size, and the low-confidence
    share is reported alongside them — a small error over three low-confidence
    predictions is not a calibrated model.
    """
    abs_errors: list[float] = []
    abs_pct_errors: list[float] = []
    briers: list[float] = []
    buckets: list[list[tuple[float, int]]] = [
        [] for _ in range(len(REWORK_BIN_EDGES) - 1)
    ]
    versions: list[str] = []
    low_confidence = 0
    scored = 0

    for row in records:
        version = row.get("predictor_version")
        if version and version not in versions:
            versions.append(str(version))

        predicted = row.get("predicted_tokens")
        actual = row.get("actual_tokens")
        if predicted is not None and actual is not None:
            error = abs(float(actual) - float(predicted))
            abs_errors.append(error)
            if predicted:
                abs_pct_errors.append(error / float(predicted) * 100.0)

        prob = row.get("predicted_rework_prob")
        rework = row.get("actual_rework")
        if prob is not None and rework is not None:
            prob = float(prob)
            observed = 1 if int(rework) else 0
            buckets[_bin_index(prob)].append((prob, observed))
            briers.append((prob - observed) ** 2)

        if predicted is not None or prob is not None:
            scored += 1
            if str(row.get("prediction_confidence") or "") == "low":
                low_confidence += 1

    bins = tuple(
        ReworkBin(
            lower=REWORK_BIN_EDGES[i],
            upper=REWORK_BIN_EDGES[i + 1],
            predicted_mean=fmean(p for p, _ in bucket),
            observed_rate=sum(o for _, o in bucket) / len(bucket),
            sample=len(bucket),
        )
        for i, bucket in enumerate(buckets)
        if bucket
    )
    return QualityReport(
        token_median_abs_error=median(abs_errors) if abs_errors else None,
        token_median_abs_pct_error=(
            round(median(abs_pct_errors), 2) if abs_pct_errors else None
        ),
        token_sample=len(abs_errors),
        rework_bins=bins,
        rework_brier=round(fmean(briers), 4) if briers else None,
        rework_sample=len(briers),
        low_confidence_share=(low_confidence / scored) if scored else None,
        versions=tuple(versions),
    )
