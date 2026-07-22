# ABOUTME: Tests for the per-story token + rework predictor (Story 28.2-002).
# ABOUTME: Model, cohort ladder, thin-history fallback, reconcile round-trip, metrics.

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sdlc.build import (
    BuildOptions,
    Ledger,
    _log_predictor_posture,
    _predict_story_cost,
    _reconcile_story_prediction,
    parse_build_args,
    run_build,
)
from sdlc.cohort import Story
from sdlc.predictor import (
    MIN_COHORT_SAMPLE,
    MIN_GLOBAL_SAMPLE,
    PREDICTOR_VERSION,
    PredictorConfig,
    PredictorHistory,
    StoryFeatures,
    TrainingRow,
    prediction_quality,
    predict_story,
    risk_flag,
    scope_band,
)

from test_build import FakeDispatcher, _sample_queue


# ---------------------------------------------------------------------------
# Feature keying: bands, risk flags, unknown-is-not-zero
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "proxy,expected",
    [(None, "unknown"), (1, "s"), (3, "s"), (4, "m"), (8, "m"), (9, "l"), (40, "l")],
)
def test_scope_band_edges(proxy, expected) -> None:
    assert scope_band(proxy) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "unknown"),
        ("", "unknown"),
        ("Low", "low"),
        ("medium", "med"),
        ("HIGH", "high"),
        ("bananas", "unknown"),
    ],
)
def test_risk_flag_normalises(raw, expected) -> None:
    assert risk_flag(raw) == expected


def test_features_unknown_flag_only_counts_discovery_features() -> None:
    # Risk comes from the inventory, not discovery — an unknown risk alone does
    # not make the feature vector incomplete.
    assert StoryFeatures(ac_count=5, dep_depth=1, scope_proxy=4).has_unknown is False
    assert StoryFeatures(ac_count=None, dep_depth=1, scope_proxy=4).has_unknown is True
    assert StoryFeatures(ac_count=5, dep_depth=None, scope_proxy=4).has_unknown is True
    assert StoryFeatures(ac_count=5, dep_depth=1, scope_proxy=None).has_unknown is True


def test_zero_feature_is_not_unknown() -> None:
    # A genuine zero is data. Only None is missing.
    f = StoryFeatures(ac_count=0, dep_depth=0, scope_proxy=0)
    assert f.has_unknown is False
    assert f.band == "s"


# ---------------------------------------------------------------------------
# History + the cohort ladder
# ---------------------------------------------------------------------------

def _rows(n, *, tokens, rework, scope=5, risk="Medium", ac=5, dep=1):
    return [
        TrainingRow(
            features=StoryFeatures(
                ac_count=ac, dep_depth=dep, scope_proxy=scope, risk=risk
            ),
            actual_tokens=tokens,
            rework=rework,
        )
        for _ in range(n)
    ]


def test_cohort_prefers_band_plus_risk_when_it_has_enough_history() -> None:
    history = PredictorHistory(
        tuple(
            _rows(MIN_COHORT_SAMPLE, tokens=100_000, rework=False, scope=5, risk="High")
            + _rows(MIN_COHORT_SAMPLE, tokens=10_000, rework=False, scope=5, risk="Low")
        )
    )
    cohort = history.cohort(StoryFeatures(ac_count=5, dep_depth=1, scope_proxy=5, risk="High"))
    assert cohort is not None
    assert cohort.tier == "band+risk"
    assert cohort.mean_tokens == pytest.approx(100_000)
    assert cohort.sample == MIN_COHORT_SAMPLE


def test_cohort_falls_to_band_when_risk_bucket_is_thin() -> None:
    history = PredictorHistory(
        tuple(
            _rows(1, tokens=100_000, rework=False, scope=5, risk="High")
            + _rows(MIN_COHORT_SAMPLE, tokens=20_000, rework=False, scope=5, risk="Low")
        )
    )
    cohort = history.cohort(
        StoryFeatures(ac_count=5, dep_depth=1, scope_proxy=5, risk="High")
    )
    assert cohort is not None
    assert cohort.tier == "band"
    assert cohort.sample == MIN_COHORT_SAMPLE + 1


def test_cohort_falls_to_global_when_band_is_thin() -> None:
    history = PredictorHistory(
        tuple(
            _rows(1, tokens=100_000, rework=False, scope=20)          # band 'l'
            + _rows(MIN_GLOBAL_SAMPLE, tokens=10_000, rework=False, scope=2)  # band 's'
        )
    )
    cohort = history.cohort(StoryFeatures(ac_count=5, dep_depth=1, scope_proxy=20))
    assert cohort is not None
    assert cohort.tier == "global"
    assert cohort.sample == MIN_GLOBAL_SAMPLE + 1


def test_cohort_is_none_without_any_history() -> None:
    assert PredictorHistory(()).cohort(StoryFeatures()) is None


def test_from_rows_drops_rows_with_no_measured_cost() -> None:
    # A story whose usage was never recorded is not a zero-cost story — it is no
    # training signal at all, and must not drag the mean down.
    history = PredictorHistory.from_rows([
        {"ac_count": 5, "dep_depth": 1, "scope_proxy": 4,
         "actual_tokens": None, "actual_rework": 0},
        {"ac_count": 5, "dep_depth": 1, "scope_proxy": 4,
         "actual_tokens": 900, "actual_rework": 1, "risk": "High"},
    ])
    assert len(history.rows) == 1
    assert history.rows[0].actual_tokens == 900
    assert history.rows[0].rework is True
    assert history.rows[0].features.risk_key == "high"


def test_cohort_rework_rate_is_the_observed_share() -> None:
    history = PredictorHistory(
        tuple(
            _rows(6, tokens=10_000, rework=True, scope=5)
            + _rows(2, tokens=10_000, rework=False, scope=5)
            + _rows(MIN_COHORT_SAMPLE, tokens=10_000, rework=False, scope=20)
        )
    )
    cohort = history.cohort(StoryFeatures(ac_count=5, dep_depth=1, scope_proxy=5))
    assert cohort.sample == 8
    assert cohort.rework_rate == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# predict_story — the crude, inspectable model
# ---------------------------------------------------------------------------

def _flat_history(n=MIN_COHORT_SAMPLE * 2, tokens=100_000, rework=False, scope=5):
    return PredictorHistory(tuple(_rows(n, tokens=tokens, rework=rework, scope=scope)))


def test_prediction_is_none_without_history() -> None:
    # No reconciled history at all → the caller degrades to the 14.1-002 estimate
    # rather than the predictor inventing a number.
    assert predict_story(StoryFeatures(ac_count=5, dep_depth=1, scope_proxy=5),
                         PredictorHistory(())) is None


def test_prediction_at_the_reference_features_is_the_cohort_mean() -> None:
    cfg = PredictorConfig()
    pred = predict_story(
        StoryFeatures(ac_count=cfg.ac_ref, dep_depth=0, scope_proxy=cfg.scope_ref),
        _flat_history(scope=cfg.scope_ref),
        config=cfg,
    )
    assert pred is not None
    assert pred.predicted_tokens == 100_000
    assert pred.low_confidence is False
    assert pred.version == PREDICTOR_VERSION


def test_more_acceptance_criteria_predicts_more_tokens() -> None:
    cfg = PredictorConfig()
    history = _flat_history(scope=cfg.scope_ref)
    base = predict_story(
        StoryFeatures(ac_count=cfg.ac_ref, dep_depth=0, scope_proxy=cfg.scope_ref),
        history, config=cfg,
    )
    bigger = predict_story(
        StoryFeatures(ac_count=cfg.ac_ref * 2, dep_depth=0, scope_proxy=cfg.scope_ref),
        history, config=cfg,
    )
    assert base is not None and bigger is not None
    assert bigger.predicted_tokens > base.predicted_tokens


def test_wider_scope_and_deeper_dependencies_predict_more_tokens() -> None:
    cfg = PredictorConfig()
    history = _flat_history(scope=cfg.scope_ref)
    base = predict_story(
        StoryFeatures(ac_count=cfg.ac_ref, dep_depth=0, scope_proxy=cfg.scope_ref),
        history, config=cfg,
    )
    wide = predict_story(
        StoryFeatures(ac_count=cfg.ac_ref, dep_depth=3, scope_proxy=cfg.scope_ref * 3),
        history, config=cfg,
    )
    assert base is not None and wide is not None
    assert wide.predicted_tokens > base.predicted_tokens


def test_token_adjustment_is_clamped_both_ways() -> None:
    # Exaggerated weights so both guards are actually reachable — they exist to
    # bound a *retuned* model, not just the shipped one.
    cfg = PredictorConfig(ac_weight=5.0, scope_weight=5.0)
    history = _flat_history(scope=cfg.scope_ref)
    huge = predict_story(
        StoryFeatures(ac_count=500, dep_depth=99, scope_proxy=900),
        history, config=cfg,
    )
    tiny = predict_story(
        StoryFeatures(ac_count=0, dep_depth=0, scope_proxy=0), history, config=cfg
    )
    assert huge is not None and tiny is not None
    assert huge.predicted_tokens == int(round(100_000 * cfg.max_adjustment))
    assert tiny.predicted_tokens == int(round(100_000 * cfg.min_adjustment))


def test_shipped_weights_never_predict_a_negative_or_zero_cost() -> None:
    # The floor is a guard, not the shipped model's operating point: with the
    # default weights the smallest possible story still predicts most of its
    # cohort's mean, because the cohort mean is the measured part.
    tiny = predict_story(
        StoryFeatures(ac_count=0, dep_depth=0, scope_proxy=0), _flat_history()
    )
    assert tiny is not None
    assert tiny.predicted_tokens > 0


def test_rework_probability_tracks_the_cohort_rate_and_is_bounded() -> None:
    cfg = PredictorConfig()
    never = predict_story(
        StoryFeatures(ac_count=cfg.ac_ref, dep_depth=0, scope_proxy=cfg.scope_ref),
        _flat_history(rework=False, scope=cfg.scope_ref), config=cfg,
    )
    always = predict_story(
        StoryFeatures(ac_count=cfg.ac_ref, dep_depth=0, scope_proxy=cfg.scope_ref),
        _flat_history(rework=True, scope=cfg.scope_ref), config=cfg,
    )
    assert never is not None and always is not None
    # Never-reworked history floors at the configured minimum, not a false 0.
    assert never.predicted_rework_probability == pytest.approx(cfg.min_rework)
    assert always.predicted_rework_probability == pytest.approx(cfg.max_rework)
    assert 0.0 <= never.predicted_rework_probability <= 1.0


def test_thin_history_falls_back_to_global_mean_and_flags_low_confidence() -> None:
    history = PredictorHistory(tuple(_rows(2, tokens=50_000, rework=False, scope=5)))
    pred = predict_story(
        StoryFeatures(ac_count=5, dep_depth=1, scope_proxy=5), history
    )
    assert pred is not None
    assert pred.cohort_tier == "global"
    assert pred.low_confidence is True
    assert pred.sample_size == 2


def test_unknown_discovery_feature_forces_global_mean_and_low_confidence() -> None:
    # A rich per-band history exists, but this story's scope proxy is unknown —
    # so the predictor must not pretend to key on a band it cannot compute.
    history = PredictorHistory(
        tuple(
            _rows(MIN_COHORT_SAMPLE * 2, tokens=200_000, rework=False, scope=20)
            + _rows(MIN_COHORT_SAMPLE * 2, tokens=20_000, rework=False, scope=2)
        )
    )
    pred = predict_story(StoryFeatures(ac_count=5, dep_depth=1, scope_proxy=None), history)
    assert pred is not None
    assert pred.cohort_tier == "global"
    assert pred.low_confidence is True
    assert pred.predicted_tokens == pytest.approx(110_000, rel=0.5)


def test_prediction_basis_is_inspectable() -> None:
    pred = predict_story(
        StoryFeatures(ac_count=5, dep_depth=1, scope_proxy=5, risk="High"),
        _flat_history(scope=5),
    )
    assert pred is not None
    # The basis string names the cohort, its sample size and the applied
    # adjustment, so a number is always auditable back to its inputs.
    assert "n=" in pred.basis
    assert pred.cohort_tier in pred.basis
    assert "x" in pred.basis or "×" in pred.basis


# ---------------------------------------------------------------------------
# prediction_quality — measured, never asserted
# ---------------------------------------------------------------------------

def _record(pred_tokens, actual_tokens, pred_rework, actual_rework, conf="high"):
    return {
        "predicted_tokens": pred_tokens,
        "actual_tokens": actual_tokens,
        "predicted_rework_prob": pred_rework,
        "actual_rework": actual_rework,
        "prediction_confidence": conf,
        "predictor_version": PREDICTOR_VERSION,
    }


def test_quality_reports_median_absolute_error_with_sample_size() -> None:
    report = prediction_quality([
        _record(100, 110, 0.1, 0),   # |Δ| = 10
        _record(100, 130, 0.1, 0),   # |Δ| = 30
        _record(100, 150, 0.1, 0),   # |Δ| = 50
    ])
    assert report.token_median_abs_error == pytest.approx(30.0)
    assert report.token_sample == 3


def test_quality_reports_median_absolute_percentage_error() -> None:
    report = prediction_quality([
        _record(100, 110, 0.1, 0),
        _record(200, 260, 0.1, 0),
        _record(100, 200, 0.1, 0),
    ])
    # 10%, 30%, 100% → median 30%.
    assert report.token_median_abs_pct_error == pytest.approx(30.0)


def test_quality_is_empty_not_zero_without_reconciled_records() -> None:
    report = prediction_quality([])
    assert report.token_median_abs_error is None
    assert report.token_sample == 0
    assert report.rework_sample == 0
    assert report.rework_brier is None
    assert report.rework_bins == ()


def test_quality_ignores_records_without_an_actual() -> None:
    report = prediction_quality([
        _record(100, None, 0.1, None),
        _record(100, 120, 0.1, 0),
    ])
    assert report.token_sample == 1
    assert report.rework_sample == 1


def test_rework_calibration_bins_carry_predicted_vs_observed_and_n() -> None:
    report = prediction_quality([
        _record(100, 100, 0.10, 0),
        _record(100, 100, 0.10, 0),
        _record(100, 100, 0.10, 1),
        _record(100, 100, 0.10, 0),
        _record(100, 100, 0.90, 1),
        _record(100, 100, 0.90, 1),
    ])
    bins = {(b.lower, b.upper): b for b in report.rework_bins}
    low = bins[(0.0, 0.25)]
    assert low.sample == 4
    assert low.predicted_mean == pytest.approx(0.10)
    assert low.observed_rate == pytest.approx(0.25)
    high = bins[(0.75, 1.0)]
    assert high.sample == 2
    assert high.observed_rate == pytest.approx(1.0)
    assert report.rework_sample == 6


def test_rework_brier_score_is_reported() -> None:
    report = prediction_quality([
        _record(100, 100, 1.0, 1),
        _record(100, 100, 0.0, 0),
    ])
    assert report.rework_brier == pytest.approx(0.0)


def test_quality_reports_the_low_confidence_share_and_never_suppresses_it() -> None:
    report = prediction_quality([
        _record(100, 100, 0.1, 0, conf="low"),
        _record(100, 100, 0.1, 0, conf="high"),
    ])
    assert report.low_confidence_share == pytest.approx(0.5)
    assert report.to_dict()["low_confidence_share"] == pytest.approx(0.5)


def test_quality_report_names_every_predictor_version_in_the_sample() -> None:
    records = [_record(100, 100, 0.1, 0)]
    records.append({**records[0], "predictor_version": "v0"})
    report = prediction_quality(records)
    assert set(report.versions) == {PREDICTOR_VERSION, "v0"}


# ---------------------------------------------------------------------------
# Ledger persistence: record → reconcile round-trip
# ---------------------------------------------------------------------------

def _ledger_with_story(tmp_path: Path, **features) -> tuple[Ledger, str]:
    ledger = Ledger(tmp_path / ".sdlc-state.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "auto")
    ledger.story_upsert(
        run_id, "s1-001", "99", "Story one", "P1", 2, "py", "", None, "TODO",
        **features,
    )
    return ledger, run_id


def test_prediction_round_trips_through_the_story_row(tmp_path: Path) -> None:
    ledger, run_id = _ledger_with_story(tmp_path, ac_count=6, dep_depth=1, scope_proxy=4)
    ledger.story_set_prediction(
        run_id, "s1-001",
        predicted_tokens=123_456,
        predicted_rework_prob=0.42,
        predictor_version=PREDICTOR_VERSION,
        low_confidence=True,
    )
    row = ledger.story_prediction_rows()[0]
    assert row["predicted_tokens"] == 123_456
    assert row["predicted_rework_prob"] == pytest.approx(0.42)
    assert row["predictor_version"] == PREDICTOR_VERSION
    assert row["prediction_confidence"] == "low"
    # Not reconciled yet — the actuals stay unknown, never a fabricated zero.
    assert row["actual_tokens"] is None
    assert row["actual_rework"] is None


def test_reconcile_persists_actual_tokens_and_rework(tmp_path: Path) -> None:
    ledger, run_id = _ledger_with_story(tmp_path, ac_count=6, dep_depth=1, scope_proxy=4)
    ledger.story_set_prediction(
        run_id, "s1-001", predicted_tokens=1_000, predicted_rework_prob=0.1,
        predictor_version=PREDICTOR_VERSION, low_confidence=False,
    )
    for stage, attempt in (("build", 1), ("review", 1)):
        ledger.stage_start(run_id, "s1-001", stage, attempt)
        ledger.stage_set_usage(
            run_id, "s1-001", stage, attempt,
            session_id="s", input_tokens=100, output_tokens=200,
            cache_read_tokens=300, cache_creation_tokens=400, cost_usd=0.1,
        )
        ledger.stage_finish(run_id, "s1-001", stage, attempt, "DONE")

    outcome = ledger.story_reconcile_prediction(run_id, "s1-001")
    assert outcome is not None
    assert outcome["actual_tokens"] == 2_000
    assert outcome["actual_rework"] == 0
    row = ledger.story_prediction_rows()[0]
    assert row["actual_tokens"] == 2_000
    assert row["actual_rework"] == 0
    # Idempotent: a second pass assigns the same absolute figures.
    assert ledger.story_reconcile_prediction(run_id, "s1-001")["actual_tokens"] == 2_000


@pytest.mark.parametrize(
    "stage,attempt,status",
    [("bugfix", 1, "DONE"), ("review", 2, "DONE"), ("build", 1, "FAILED")],
)
def test_reconcile_detects_rework(tmp_path: Path, stage, attempt, status) -> None:
    ledger, run_id = _ledger_with_story(tmp_path, ac_count=6, dep_depth=1, scope_proxy=4)
    ledger.story_set_prediction(
        run_id, "s1-001", predicted_tokens=1_000, predicted_rework_prob=0.1,
        predictor_version=PREDICTOR_VERSION, low_confidence=False,
    )
    ledger.stage_start(run_id, "s1-001", stage, attempt)
    ledger.stage_finish(run_id, "s1-001", stage, attempt, status)
    outcome = ledger.story_reconcile_prediction(run_id, "s1-001")
    assert outcome is not None
    assert outcome["actual_rework"] == 1


@pytest.mark.parametrize(
    "stage,attempt,status",
    [
        # `bugfix`/`reask`/`commitlint` share one monotonic per-story sequence in
        # `attempt` (build.py's bugfix_seq; see resume.py's reconstruction of it),
        # so a second *successful* recovery dispatch anywhere in the story lands
        # at attempt 2 without any stage ever having been retried.
        ("commitlint", 2, "DONE"),
        ("reask", 3, "DONE"),
        # A recovery dispatch that fails is a failed *recovery*, not a review
        # retry: the pipeline stage it serves reports the rework on its own row.
        ("commitlint", 1, "FAILED"),
        ("reask", 2, "FAILED"),
    ],
)
def test_reconcile_ignores_recovery_dispatch_sequence_numbers(
    tmp_path: Path, stage, attempt, status
) -> None:
    """Recovery dispatches must not be read as rework (Story 28.2-002).

    `attempt` is a per-stage retry counter only for the pipeline stages. Reading
    it on a recovery row labels a clean story as reworked, and that label is the
    predictor's own target variable — it would train and score against noise it
    manufactured itself.
    """
    ledger, run_id = _ledger_with_story(tmp_path, ac_count=6, dep_depth=1, scope_proxy=4)
    ledger.story_set_prediction(
        run_id, "s1-001", predicted_tokens=1_000, predicted_rework_prob=0.1,
        predictor_version=PREDICTOR_VERSION, low_confidence=False,
    )
    for pipeline_stage in ("build", "review"):
        ledger.stage_start(run_id, "s1-001", pipeline_stage, 1)
        ledger.stage_finish(run_id, "s1-001", pipeline_stage, 1, "DONE")
    ledger.stage_start(run_id, "s1-001", stage, attempt)
    ledger.stage_finish(run_id, "s1-001", stage, attempt, status)

    outcome = ledger.story_reconcile_prediction(run_id, "s1-001")
    assert outcome is not None
    assert outcome["actual_rework"] == 0


def test_training_rows_ignore_recovery_dispatch_sequence_numbers(
    tmp_path: Path,
) -> None:
    """The same contamination, at the training end of the same predicate."""
    ledger, run_id = _ledger_with_story(tmp_path, ac_count=6, dep_depth=1, scope_proxy=4)
    for stage, attempt in (("build", 1), ("commitlint", 1), ("commitlint", 2)):
        ledger.stage_start(run_id, "s1-001", stage, attempt)
        ledger.stage_set_usage(
            run_id, "s1-001", stage, attempt, session_id="s", input_tokens=100,
            output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0,
            cost_usd=0.1,
        )
        ledger.stage_finish(run_id, "s1-001", stage, attempt, "DONE")
    ledger.set_story_status(run_id, "s1-001", "DONE")

    rows = ledger.prediction_training_rows()
    assert [r["actual_rework"] for r in rows] == [0]


def test_reconcile_is_a_noop_without_a_recorded_prediction(tmp_path: Path) -> None:
    ledger, run_id = _ledger_with_story(tmp_path)
    ledger.stage_start(run_id, "s1-001", "build", 1)
    ledger.stage_finish(run_id, "s1-001", "build", 1, "DONE")
    assert ledger.story_reconcile_prediction(run_id, "s1-001") is None
    assert ledger.story_prediction_rows() == []


def test_training_rows_read_terminal_stories_with_measured_usage(tmp_path: Path) -> None:
    ledger, run_id = _ledger_with_story(tmp_path, ac_count=6, dep_depth=1, scope_proxy=4)
    ledger.stage_start(run_id, "s1-001", "build", 1)
    ledger.stage_set_usage(
        run_id, "s1-001", "build", 1, session_id="s", input_tokens=1_000,
        output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0, cost_usd=0.1,
    )
    ledger.stage_finish(run_id, "s1-001", "build", 1, "DONE")
    ledger.set_story_status(run_id, "s1-001", "DONE")

    rows = ledger.prediction_training_rows()
    assert len(rows) == 1
    assert rows[0]["actual_tokens"] == 1_000
    assert rows[0]["ac_count"] == 6
    assert rows[0]["scope_proxy"] == 4
    history = PredictorHistory.from_rows(rows)
    assert len(history.rows) == 1
    assert history.rows[0].actual_tokens == 1_000


def test_training_rows_skip_stories_still_in_flight(tmp_path: Path) -> None:
    ledger, run_id = _ledger_with_story(tmp_path, ac_count=6, dep_depth=1, scope_proxy=4)
    ledger.stage_start(run_id, "s1-001", "build", 1)
    ledger.stage_set_usage(
        run_id, "s1-001", "build", 1, session_id="s", input_tokens=1_000,
        output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0, cost_usd=0.1,
    )
    ledger.set_story_status(run_id, "s1-001", "IN_PROGRESS")
    assert ledger.prediction_training_rows() == []


@pytest.mark.parametrize("status", ["NEEDS_ATTENTION", "AWAITING_APPROVAL", "FAILED"])
def test_training_rows_skip_resumable_parked_stories(
    tmp_path: Path, status
) -> None:
    """A parked story is not finished — `sdlc resume` re-enters it (Story 28.2-002).

    ``compute_resume_plan`` owes a next stage to any story whose pipeline is
    incomplete, and ``run_resume`` re-enters everything outside ``{DONE,
    SKIPPED}``. Training on a parked story presents a partial token total as the
    story's complete cost and biases the cohort mean low.
    """
    ledger, run_id = _ledger_with_story(tmp_path, ac_count=6, dep_depth=1, scope_proxy=4)
    ledger.stage_start(run_id, "s1-001", "build", 1)
    ledger.stage_set_usage(
        run_id, "s1-001", "build", 1, session_id="s", input_tokens=40_000,
        output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0, cost_usd=0.1,
    )
    ledger.stage_finish(run_id, "s1-001", "build", 1, "DONE")
    ledger.set_story_status(run_id, "s1-001", status)
    assert ledger.prediction_training_rows() == []


def test_training_rows_skip_stories_with_no_measured_usage(tmp_path: Path) -> None:
    ledger, run_id = _ledger_with_story(tmp_path, ac_count=6, dep_depth=1, scope_proxy=4)
    ledger.stage_start(run_id, "s1-001", "build", 1)
    ledger.stage_finish(run_id, "s1-001", "build", 1, "DONE")
    ledger.set_story_status(run_id, "s1-001", "DONE")
    # A story whose stages recorded no tokens would drag the mean toward zero.
    assert ledger.prediction_training_rows() == []


def _unmigrated_ledger(db: Path) -> Ledger:
    """A ledger predating both the 28.2-001 discovery columns and Migration 16.

    Built with raw sqlite rather than :meth:`Ledger.init`, precisely so the
    read-only prediction queries meet a `stories` table that lacks the columns
    they name. A real ledger reaches this state whenever a read verb runs against
    a DB written by an older controller and no migration has been applied yet.
    """
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE stories (run_id TEXT, story_id TEXT, status TEXT NOT NULL, "
            "  PRIMARY KEY(run_id, story_id))"
        )
        conn.execute(
            "INSERT INTO stories(run_id, story_id, status) VALUES ('r1', 's1-001', 'DONE')"
        )
        conn.commit()
    finally:
        conn.close()
    return Ledger(db)


def test_prediction_reads_degrade_cleanly_on_an_unmigrated_ledger(tmp_path: Path) -> None:
    # Read-only callers never migrate, so a missing column must read as "no
    # history" rather than raising OperationalError mid-build.
    ledger = _unmigrated_ledger(tmp_path / ".sdlc-state.db")
    assert ledger.story_prediction_rows() == []
    assert ledger.prediction_training_rows() == []


def test_training_rows_are_empty_without_a_ledger_file(tmp_path: Path) -> None:
    db = tmp_path / "absent.db"
    assert Ledger(db).prediction_training_rows() == []
    # Never materialises a spurious empty ledger just to answer the question.
    assert not db.exists()


def test_prediction_history_reads_the_inventory_risk_flag(tmp_path: Path) -> None:
    ledger, _ = _ledger_with_story(tmp_path, ac_count=6, dep_depth=1, scope_proxy=4)
    assert ledger.story_risk("s1-001") is None
    ledger.inventory_upsert_specs([("s1-001", "99", "99.1", "Story one", 2, "High")])
    assert ledger.story_risk("s1-001") == "High"


# ---------------------------------------------------------------------------
# Build integration: enabled records, disabled degrades
# ---------------------------------------------------------------------------

def _events(db: Path, run_id: str) -> list[str]:
    ledger = Ledger(db)
    with ledger._connect_ro() as conn:  # noqa: SLF001 — test reads the audit trail
        return [
            r["message"]
            for r in conn.execute(
                "SELECT message FROM events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        ]


def _run(db: Path, *, predict: bool):
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True, auto=True,
        predict=predict,
    )
    return run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )


def test_predict_flag_parses_and_defaults_off() -> None:
    assert parse_build_args(["epic-99"]).predict is False
    assert parse_build_args(["epic-99", "--predict"]).predict is True


def test_disabled_predictor_records_no_prediction_and_logs_the_note(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = _run(db, predict=False)
    assert Ledger(db).story_prediction_rows() == []
    assert any("predictor disabled" in m for m in _events(db, result.run_id))


def test_disabled_predictor_logs_the_note_once_per_run(tmp_path: Path) -> None:
    """AC5's note is per *run*, not per story — the default path must stay quiet.

    `--predict` is off by default, so a per-story note would add one advisory
    `events` row per story to every existing build, for a feature nobody enabled.
    """
    db = tmp_path / ".sdlc-state.db"
    result = _run(db, predict=False)
    notes = [m for m in _events(db, result.run_id) if "predictor disabled" in m]
    assert len(_sample_queue()) > 1  # the run really does hold several stories
    assert len(notes) == 1


def test_enabled_predictor_records_and_reconciles_each_story(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = _run(db, predict=True)
    rows = Ledger(db).story_prediction_rows()
    # The very first story has no reconciled history to train on, so it gets no
    # prediction at all — honestly absent rather than confidently invented. Every
    # story after it is predicted pre-dispatch and reconciled post-run.
    assert {r["story_id"] for r in rows} == {"s1-002", "s1-003"}
    assert all(r["predicted_tokens"] is not None for r in rows)
    assert all(r["actual_tokens"] is not None for r in rows)
    assert all(r["actual_rework"] == 0 for r in rows)
    assert all(r["predictor_version"] == PREDICTOR_VERSION for r in rows)
    # A ledger this thin can only ever produce low-confidence numbers.
    assert all(r["prediction_confidence"] == "low" for r in rows)
    assert any(
        "no reconciled story history yet" in m for m in _events(db, result.run_id)
    )


def test_enabled_predictor_persists_the_flag_for_resume(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = _run(db, predict=True)
    assert Ledger(db).run_config(result.run_id).get("predict") is True


def test_disabled_predictor_survives_a_ledger_that_cannot_log(tmp_path: Path) -> None:
    """The disabled-path note is advisory — a failing event_log must not fail a build."""
    class NoEvents:
        def event_log(self, *args, **kwargs):
            raise RuntimeError("events table gone")

    _log_predictor_posture(NoEvents(), "run", BuildOptions(predict=False))  # no raise
    story = Story("s1-001", "t", "99", "sample", "epic-99.md", "P1", 2, "py", [])
    assert _predict_story_cost(NoEvents(), "run", story, BuildOptions(predict=False)) is None


def test_enabled_predictor_notes_no_posture(tmp_path: Path) -> None:
    """An enabled predictor speaks through its per-story predictions, not a banner."""
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("ledger exploded")

    _log_predictor_posture(Boom(), "run", BuildOptions(predict=True))  # never touches it


def _seed_training_history(
    ledger: Ledger, *, count: int, tokens: int, prefix: str = "s2"
) -> None:
    """``count`` DONE stories with measured usage — history the predictor can train on."""
    run_id = ledger.run_create("epic-98", "auto")
    for n in range(count):
        story_id = f"{prefix}-{n:03d}"
        ledger.story_upsert(
            run_id, story_id, "98", "Seed", "P1", 2, "py", "", None, "TODO",
            ac_count=6, dep_depth=1, scope_proxy=4,
        )
        ledger.stage_start(run_id, story_id, "build", 1)
        ledger.stage_set_usage(
            run_id, story_id, "build", 1, session_id="s", input_tokens=tokens,
            output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0,
            cost_usd=0.1,
        )
        ledger.stage_finish(run_id, story_id, "build", 1, "DONE")
        ledger.set_story_status(run_id, story_id, "DONE")


def test_resume_keeps_the_forecast_committed_before_the_first_dispatch(
    tmp_path: Path,
) -> None:
    """A resumed story keeps its pre-dispatch forecast — never one refitted mid-flight.

    ``_predict_story_cost`` runs on the shared build/resume path, so a story parked
    by a rate limit re-enters it with part of its cost already spent and more
    history on the ledger. Re-predicting there would fit the forecast to work
    already done, which is exactly what committing it pre-dispatch prevents.
    """
    ledger, run_id = _ledger_with_story(tmp_path, ac_count=6, dep_depth=1, scope_proxy=4)
    opts = BuildOptions(predict=True)
    story = Story("s1-001", "t", "99", "Story one", "epic-99.md", "P1", 2, "py", [])
    # Enough reconciled history for the predictor to train on.
    _seed_training_history(ledger, count=12, tokens=10_000)

    assert _predict_story_cost(ledger, run_id, story, opts) is not None
    committed = ledger.story_prediction_rows(run_id)[0]["predicted_tokens"]

    # The story burns most of its cost, then parks; more history accrues meanwhile.
    ledger.stage_start(run_id, "s1-001", "build", 1)
    ledger.stage_set_usage(
        run_id, "s1-001", "build", 1,
        session_id="s", input_tokens=90_000, output_tokens=0,
        cache_read_tokens=0, cache_creation_tokens=0, cost_usd=1.0,
    )
    ledger.stage_finish(run_id, "s1-001", "build", 1, "DONE")
    _seed_training_history(ledger, count=8, tokens=10_000, prefix="s3")

    # Resume re-enters the same path — the committed forecast must stand.
    assert _predict_story_cost(ledger, run_id, story, opts) is None
    assert ledger.story_prediction_rows(run_id)[0]["predicted_tokens"] == committed


def test_rollback_clears_the_prediction_so_a_rebuild_predicts_afresh(
    tmp_path: Path,
) -> None:
    """The guard respects the one legitimate reset: `sdlc rollback`.

    Rollback discards the attempt and NULLs the prediction columns, so the story
    is genuinely unbuilt again and its next dispatch must commit a fresh forecast.
    """
    ledger, run_id = _ledger_with_story(tmp_path, ac_count=6, dep_depth=1, scope_proxy=4)
    opts = BuildOptions(predict=True)
    story = Story("s1-001", "t", "99", "Story one", "epic-99.md", "P1", 2, "py", [])
    _seed_training_history(ledger, count=12, tokens=10_000)

    assert _predict_story_cost(ledger, run_id, story, opts) is not None
    ledger.reset_story(run_id, "s1-001")
    assert _predict_story_cost(ledger, run_id, story, opts) is not None


def test_predictor_never_breaks_a_build_when_the_ledger_misbehaves(tmp_path: Path) -> None:
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("ledger exploded")

    story = Story("s1-001", "t", "99", "sample", "epic-99.md", "P1", 2, "py", [])
    opts = BuildOptions(predict=True)
    assert _predict_story_cost(Boom(), "run", story, opts) is None
    _reconcile_story_prediction(Boom(), "run", "s1-001")  # must not raise


# ---------------------------------------------------------------------------
# Rollback: a discarded attempt must not survive as prediction-vs-actual data
# ---------------------------------------------------------------------------

def _reconciled_story_with_stages(tmp_path: Path) -> tuple[Ledger, str]:
    """A story carrying a recorded prediction reconciled against real stage usage."""
    ledger, run_id = _ledger_with_story(
        tmp_path, ac_count=6, dep_depth=1, scope_proxy=4
    )
    ledger.story_set_prediction(
        run_id, "s1-001", predicted_tokens=1_000, predicted_rework_prob=0.1,
        predictor_version=PREDICTOR_VERSION, low_confidence=False,
    )
    for stage, attempt in (("build", 1), ("review", 1), ("bugfix", 1)):
        ledger.stage_start(run_id, "s1-001", stage, attempt)
        ledger.stage_set_usage(
            run_id, "s1-001", stage, attempt,
            session_id="s", input_tokens=100, output_tokens=200,
            cache_read_tokens=300, cache_creation_tokens=400, cost_usd=0.1,
        )
        ledger.stage_finish(run_id, "s1-001", stage, attempt, "DONE")
    assert ledger.story_reconcile_prediction(run_id, "s1-001") == {
        "actual_tokens": 3_000, "actual_rework": 1,
    }
    return ledger, run_id


def test_reset_story_clears_the_discarded_attempts_prediction(tmp_path: Path) -> None:
    """`sdlc rollback` deletes the stage rows, so the actuals derived from them go too.

    Otherwise the story row keeps `actual_tokens` measured from stages that no
    longer exist, and `predict-quality` scores a discarded attempt as if it were
    the story's real outcome.
    """
    ledger, run_id = _reconciled_story_with_stages(tmp_path)

    ledger.reset_story(run_id, "s1-001")

    # Back to a fresh, unbuilt state: never-predicted, never-reconciled.
    assert ledger.story_prediction_rows() == []


def test_reset_story_keeps_the_discovery_features(tmp_path: Path) -> None:
    """The features describe the *spec*, not the attempt — a rollback must keep them."""
    ledger, run_id = _reconciled_story_with_stages(tmp_path)

    ledger.reset_story(run_id, "s1-001")

    rows = ledger.prediction_training_rows()
    assert rows == []  # TODO is not terminal, and its stages are gone
    with sqlite3.connect(ledger.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ac_count, dep_depth, scope_proxy, title, status FROM stories "
            "WHERE run_id = ? AND story_id = ?",
            (run_id, "s1-001"),
        ).fetchone()
    assert (row["ac_count"], row["dep_depth"], row["scope_proxy"]) == (6, 1, 4)
    assert row["title"] == "Story one"
    assert row["status"] == "TODO"


def test_quality_report_ignores_a_rolled_back_story(tmp_path: Path) -> None:
    """The end-to-end consequence: a rolled-back attempt contributes no sample."""
    ledger, run_id = _reconciled_story_with_stages(tmp_path)
    scored = prediction_quality(ledger.story_prediction_rows())
    assert (scored.token_sample, scored.rework_sample) == (1, 1)

    ledger.reset_story(run_id, "s1-001")

    cleared = prediction_quality(ledger.story_prediction_rows())
    assert cleared.token_sample == 0
    assert cleared.token_median_abs_error is None
    assert cleared.rework_sample == 0
    assert cleared.low_confidence_share is None
