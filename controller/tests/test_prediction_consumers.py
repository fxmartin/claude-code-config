# ABOUTME: Tests for Story 28.3-002 — budget gate, pre-dispatch warnings, and the
# ABOUTME: batch planner consuming the 28.2-002 per-story prediction.

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from sdlc.build import (
    BuildOptions,
    Ledger,
    _CostGatePause,
    _flag_budget_projection,
    _plan_batch,
    _prediction_cost_gate,
    _reconcile_batch_plan,
    run_build,
)
from sdlc.cost_estimate import project_batch
from sdlc.predictor import StoryPrediction

from test_build import FakeDispatcher, _SAMPLE_STAGE_TOKENS, _sample_queue

# Each sample story runs build+coverage+review+merge under the fake dispatcher.
_TOKENS_PER_STORY = 4 * _SAMPLE_STAGE_TOKENS


# ---------------------------------------------------------------------------
# project_batch — the pure batch-sum the planner and budget projection read
# ---------------------------------------------------------------------------

def _p(tokens: int, *, low: bool = False) -> SimpleNamespace:
    """A minimal prediction stand-in (duck-typed like StoryPrediction)."""
    return SimpleNamespace(predicted_tokens=tokens, low_confidence=low)


def test_project_batch_sums_predicted_tokens_and_counts() -> None:
    projection = project_batch([_p(10_000), _p(20_000, low=True), None])
    assert projection.predicted_tokens == 30_000
    assert projection.predicted_stories == 2
    assert projection.fallback_stories == 1
    assert projection.low_confidence_stories == 1


def test_project_batch_confidence_is_high_only_when_everything_is_predicted() -> None:
    assert project_batch([_p(1), _p(2)]).confidence == "high"
    assert project_batch([_p(1, low=True), _p(2)]).confidence == "low"
    # A story with no prediction at all makes the batch figure partial → low.
    assert project_batch([_p(1), None]).confidence == "low"


def test_project_batch_with_no_predictions_is_unusable() -> None:
    empty = project_batch([])
    assert empty.usable is False
    assert empty.confidence == "low"
    assert project_batch([None, None]).usable is False


def test_window_fit_boundary_is_inclusive() -> None:
    projection = project_batch([_p(30_000)])
    assert projection.fits_window(30_000) is True
    assert projection.fits_window(29_999) is False


def test_windows_needed_is_a_ceiling() -> None:
    projection = project_batch([_p(90_000)])
    assert projection.windows_needed(40_000) == 3
    assert projection.windows_needed(90_000) == 1
    assert projection.windows_needed(0) == 0  # no window configured


# ---------------------------------------------------------------------------
# Shared harness: a ledger seeded with reconciled history the predictor trains on
# ---------------------------------------------------------------------------

def _seed_history(db: Path, *, count: int = 12, tokens: int = 10_000) -> None:
    """``count`` DONE stories with measured usage → global cohort mean = ``tokens``."""
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-98", "auto")
    for n in range(count):
        sid = f"seed-{n:03d}"
        ledger.story_upsert(
            run_id, sid, "98", "Seed", "P1", 2, "py", "", None, "TODO",
            ac_count=6, dep_depth=1, scope_proxy=4,
        )
        ledger.stage_start(run_id, sid, "build", 1)
        ledger.stage_set_usage(
            run_id, sid, "build", 1, session_id="s", input_tokens=tokens,
            output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0,
            cost_usd=0.1,
        )
        ledger.stage_finish(run_id, sid, "build", 1, "DONE")
        ledger.set_story_status(run_id, sid, "DONE")


def _events(db: Path, run_id: str) -> list[str]:
    ledger = Ledger(db)
    with ledger._connect_ro() as conn:  # noqa: SLF001 — test reads the audit trail
        return [
            r["message"]
            for r in conn.execute(
                "SELECT message FROM events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        ]


def _run(db: Path, **opt_overrides):
    opt_overrides.setdefault("predict", True)
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True, **opt_overrides,
    )
    return run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )


# ---------------------------------------------------------------------------
# Consumer 1 — pre-dispatch warning/gate on the predicted story cost
# ---------------------------------------------------------------------------

def test_prediction_gate_warns_with_confidence_and_proceeds_on_auto(
    tmp_path: Path,
) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_history(db)  # global mean 10k → every sample story predicts ~10k tokens
    result = _run(db, auto=True, cost_estimate_threshold=5_000)
    assert result.completed == 3
    assert result.cost_gated is False
    warns = [
        m for m in _events(db, result.run_id)
        if "story predicted" in m and "exceeds --cost-threshold" in m
    ]
    assert len(warns) == 3  # every story's forecast crossed the threshold
    assert all("[confidence: low]" in m for m in warns)
    assert all("proceeding (--auto)" in m for m in warns)


def test_prediction_gate_pauses_interactively_before_any_dispatch(
    tmp_path: Path,
) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_history(db)
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True, predict=True,
        auto=False, cost_estimate_threshold=5_000,
    )
    dispatcher = FakeDispatcher()
    result = run_build(
        opts, queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=dispatcher, preflight=lambda: True,
    )
    assert result.cost_gated is True
    assert dispatcher.calls == []  # gated before the first dispatch
    ledger = Ledger(db)
    assert ledger.run_row(result.run_id)["status"] == "IN_PROGRESS"  # resumable
    statuses = {r["story_id"]: r["status"] for r in ledger.story_rows(result.run_id)}
    assert statuses["s1-001"] == "NEEDS_ATTENTION"
    # The story-level gate fires before any stage row exists — proving the gate
    # ran on the prediction, not on the 14.1-002 stage estimate.
    with ledger._connect_ro() as conn:  # noqa: SLF001
        stages = conn.execute(
            "SELECT COUNT(*) FROM stages WHERE run_id = ? AND story_id = ?",
            (result.run_id, "s1-001"),
        ).fetchone()[0]
    assert stages == 0


def test_prediction_gate_re_enforces_on_a_committed_forecast(tmp_path: Path) -> None:
    """A re-entered story (resume path) re-reads its committed forecast for the gate."""
    db = tmp_path / ".sdlc-state.db"
    _seed_history(db)
    ledger = Ledger(db)
    run_id = ledger.run_create("epic-99", "serial")
    story = _sample_queue()[0]
    ledger.story_upsert(
        run_id, story.id, "99", story.title, "P1", 2, "py", "", None, "TODO",
    )
    ledger.story_set_prediction(
        run_id, story.id, predicted_tokens=10_000, predicted_rework_prob=0.1,
        predictor_version="v1", low_confidence=True,
    )
    opts = BuildOptions(predict=True, auto=False, cost_estimate_threshold=5_000)
    # Fresh prediction is None (already committed) — the gate must still trip.
    with pytest.raises(_CostGatePause):
        _prediction_cost_gate(ledger, run_id, story, None, opts)
    # A raised threshold lets the same story through.
    raised = BuildOptions(predict=True, auto=False, cost_estimate_threshold=50_000)
    _prediction_cost_gate(ledger, run_id, story, None, raised)  # no raise


def test_prediction_gate_falls_back_to_stage_estimate_without_a_forecast(
    tmp_path: Path,
) -> None:
    """No reconciled history → no prediction → the 14.1-002 stage gate still holds."""
    db = tmp_path / ".sdlc-state.db"
    result = _run(db, auto=False, cost_estimate_threshold=1)
    assert result.cost_gated is True
    events = _events(db, result.run_id)
    # The predictor logged why it degraded (AC4: fallback is logged)...
    assert any("no reconciled story history yet" in m for m in events)
    # ...and the gate that fired was the stage-level one (a SKIPPED cost-gate row).
    ledger = Ledger(db)
    with ledger._connect_ro() as conn:  # noqa: SLF001
        gated = conn.execute(
            "SELECT COUNT(*) FROM stages WHERE run_id = ? "
            "AND status = 'SKIPPED' AND failure_category = 'cost-gate'",
            (result.run_id,),
        ).fetchone()[0]
    assert gated == 1


def test_prediction_gate_still_gates_when_the_ledger_cannot_log() -> None:
    """The warn event is best-effort; the pause itself must never be lost."""
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("ledger exploded")

    prediction = StoryPrediction(
        predicted_tokens=10_000, predicted_rework_probability=0.1,
        low_confidence=True, version="v1", cohort_key="global",
        cohort_tier="global", sample_size=12, basis="test",
    )
    story = _sample_queue()[0]
    opts = BuildOptions(predict=True, auto=False, cost_estimate_threshold=5_000)
    with pytest.raises(_CostGatePause):
        _prediction_cost_gate(Boom(), "run", story, prediction, opts)


def test_prediction_gate_survives_a_failing_committed_lookup() -> None:
    """A best-effort committed-forecast read that raises must not block dispatch."""
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("ledger exploded")

    story = _sample_queue()[0]
    opts = BuildOptions(predict=True, auto=False, cost_estimate_threshold=5_000)
    # No fresh prediction and a broken lookup → treated as no forecast at all.
    _prediction_cost_gate(Boom(), "run", story, None, opts)  # no raise


def test_story_prediction_is_none_without_a_ledger_db(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "absent.db")
    assert ledger.story_prediction("run", "s1-001") is None


def test_story_prediction_is_none_on_a_pre_prediction_schema(tmp_path: Path) -> None:
    """A ledger predating the 28.2-002 columns degrades to the stage estimate."""
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(
        run_id, "s1-001", "99", "Story one", "P1", 2, "py", "", None, "TODO",
    )
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE stories DROP COLUMN predicted_tokens")
    assert ledger.story_prediction(run_id, "s1-001") is None


# ---------------------------------------------------------------------------
# Consumer 2 — budget gate projects remaining from summed predictions
# ---------------------------------------------------------------------------

def test_budget_projection_flags_a_run_heading_for_the_ceiling(tmp_path: Path) -> None:
    """The projected view fires before the accrued-only gate ever would."""
    db = tmp_path / ".sdlc-state.db"
    _seed_history(db)  # 10k predicted per story; actual accrual is ~17.7k per story
    result = _run(db, auto=True, budget=40_000)
    # The accrued-only gate never trips (pre-dispatch accrual peaks at ~35.4k)...
    assert result.budget_stopped is False
    assert result.completed == 3
    # ...but the projection (accrued + predicted-for-pending) crossed the ceiling.
    flags = [
        m for m in _events(db, result.run_id)
        if "projected to cross the ceiling" in m
    ]
    assert len(flags) == 1  # flagged once, not per remaining story
    assert "[confidence: low]" in flags[0]
    assert "--budget=40,000" in flags[0]


def test_budget_projection_absent_when_predictor_disabled(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_history(db)
    result = _run(db, auto=True, budget=40_000, predict=False)
    assert result.completed == 3
    events = _events(db, result.run_id)
    assert not any("projected to cross the ceiling" in m for m in events)
    # The Epic-14 fallback is the already-logged disabled posture (AC4).
    assert any("predictor disabled" in m for m in events)


def test_budget_projection_never_blocks_dispatch(tmp_path: Path) -> None:
    """The flag is advisory: the hard gate still trips on accrued tokens only."""
    db = tmp_path / ".sdlc-state.db"
    _seed_history(db)
    # Predictions alone (30k) cross this ceiling at run start, but dispatch must
    # proceed until the *accrued* gate trips after the first story (~17.7k).
    result = _run(db, auto=True, budget=10_000)
    assert result.budget_stopped is True
    assert result.completed == 1


def test_budget_projection_flags_on_the_parallel_scheduler_path(
    tmp_path: Path,
) -> None:
    """The continuous scheduler re-checks the projected ceiling per submission."""
    db = tmp_path / ".sdlc-state.db"
    _seed_history(db)
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, predict=True, auto=True,
        budget=40_000, concurrency=2,
    )
    result = run_build(
        opts, queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
    )
    assert result.completed == 3
    flags = [
        m for m in _events(db, result.run_id)
        if "projected to cross the ceiling" in m
    ]
    assert len(flags) == 1  # latched after the first firing, same as serial


def test_flag_budget_projection_is_best_effort_on_a_ledger_fault() -> None:
    """A usage-totals read that raises must never fail (or halt) the run."""
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("ledger exploded")

    projection = project_batch([_p(30_000)])
    opts = BuildOptions(predict=True, budget=10_000)
    flagged = _flag_budget_projection(
        Boom(), "run", opts, _sample_queue(), {}, projection,
    )
    assert flagged is False


# ---------------------------------------------------------------------------
# Consumer 3 — batch planner: summed predicted tokens vs the rate-limit window
# ---------------------------------------------------------------------------

def test_batch_plan_reports_a_fitting_window(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_history(db, tokens=10_000)  # summed prediction 30k vs 50k window
    result = _run(db, auto=True, window_budget=50_000)
    assert result.completed == 3
    plans = [m for m in _events(db, result.run_id) if m.startswith("batch plan:")]
    assert len(plans) == 1
    assert "~30,000 predicted tokens across 3 stories" in plans[0]
    assert "projected to fit the current window" in plans[0]
    assert "[confidence: low]" in plans[0]


def test_batch_plan_warns_when_the_batch_exceeds_the_window(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_history(db, tokens=30_000)  # summed prediction 90k vs 40k window
    result = _run(db, auto=True, window_budget=40_000)
    assert result.completed == 3  # pacing still runs on actual accrual, unharmed
    plans = [m for m in _events(db, result.run_id) if m.startswith("batch plan:")]
    assert len(plans) == 1
    assert "projected to exceed the window" in plans[0]
    assert "~3 windows" in plans[0]


def test_plan_batch_is_best_effort_on_a_ledger_fault() -> None:
    """A planner fault degrades to the Epic-14 accrued view, never fails the run."""
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("ledger exploded")

    opts = BuildOptions(predict=True, window_budget=40_000)
    assert _plan_batch(Boom(), "run", _sample_queue(), opts) == ({}, None)


def test_batch_plan_falls_back_without_a_usable_prediction(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"  # no seeded history at all
    result = _run(db, auto=True, window_budget=40_000)
    assert result.completed == 3
    events = _events(db, result.run_id)
    assert any("no usable story prediction" in m for m in events)
    assert not any("projected to fit" in m for m in events)
    assert not any("projected to exceed" in m for m in events)


# ---------------------------------------------------------------------------
# AC5 — projected-vs-actual window fit reconciled after the run
# ---------------------------------------------------------------------------

def test_batch_plan_is_reconciled_against_actuals(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_history(db, tokens=10_000)
    result = _run(db, auto=True, window_budget=50_000)
    assert result.completed == 3
    recon = [
        m for m in _events(db, result.run_id) if "batch plan reconciled" in m
    ]
    assert len(recon) == 1
    assert "projected ~30,000" in recon[0]
    actual = 3 * _TOKENS_PER_STORY
    assert f"actual {actual:,} tokens" in recon[0]
    assert "+77%" in recon[0]  # (53,040 - 30,000) / 30,000


def test_no_batch_reconciliation_without_a_window_plan(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_history(db)
    result = _run(db, auto=True)  # predictor on, but no window budget configured
    assert result.completed == 3
    assert not any(
        "batch plan reconciled" in m for m in _events(db, result.run_id)
    )


def test_batch_reconciliation_is_best_effort_on_a_ledger_fault() -> None:
    """A reconciliation fault is swallowed — the run's close-out never breaks."""
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("ledger exploded")

    projection = project_batch([_p(30_000)])
    opts = BuildOptions(predict=True, window_budget=40_000)
    _reconcile_batch_plan(Boom(), "run", opts, projection)  # no raise
