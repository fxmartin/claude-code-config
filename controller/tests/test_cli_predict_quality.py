# ABOUTME: CLI tests for `sdlc predict-quality` — the prediction-scoring verb.
# ABOUTME: Story 28.2-002. Covers the metrics, --json, clean absence, unknown run.

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sdlc.build import Ledger
from sdlc.cli import app
from sdlc.predictor import PREDICTOR_VERSION

runner = CliRunner()


def _seed(tmp_path: Path, stories: list[tuple[str, int, int, float, int, bool]]) -> tuple[Path, str]:
    """Seed a ledger with `(story_id, predicted, actual, p_rework, reworked, low)` rows."""
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "auto")
    for sid, predicted, actual, prob, reworked, low in stories:
        ledger.story_upsert(
            run_id, sid, "99", sid, "P1", 2, "py", "", None, "DONE",
            ac_count=5, dep_depth=1, scope_proxy=4,
        )
        ledger.story_set_prediction(
            run_id, sid,
            predicted_tokens=predicted, predicted_rework_prob=prob,
            predictor_version=PREDICTOR_VERSION, low_confidence=low,
        )
        ledger.stage_start(run_id, sid, "build", 1)
        ledger.stage_set_usage(
            run_id, sid, "build", 1, session_id="s", input_tokens=actual,
            output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0,
            cost_usd=0.1,
        )
        ledger.stage_finish(run_id, sid, "build", 1, "DONE")
        if reworked:
            ledger.stage_start(run_id, sid, "bugfix", 1)
            ledger.stage_finish(run_id, sid, "bugfix", 1, "DONE")
        ledger.story_reconcile_prediction(run_id, sid)
    return db, run_id


_SAMPLE = [
    ("s1-001", 1_000, 1_100, 0.10, False, False),   # |Δ| 100, 10%
    ("s1-002", 1_000, 1_300, 0.10, False, False),   # |Δ| 300, 30%
    ("s1-003", 1_000, 1_500, 0.90, True, True),     # |Δ| 500, 50%
]


def test_predict_quality_reports_error_and_calibration_with_sample_sizes(
    tmp_path: Path,
) -> None:
    db, _ = _seed(tmp_path, _SAMPLE)
    result = runner.invoke(app, ["predict-quality", "--db", str(db)])
    assert result.exit_code == 0
    assert "median absolute error 300" in result.stdout
    assert "n=3" in result.stdout
    assert "rework calibration" in result.stdout
    assert "brier score" in result.stdout
    # The low-confidence share is always surfaced, never suppressed.
    assert "low-confidence share: 33%" in result.stdout
    assert PREDICTOR_VERSION in result.stdout


def test_predict_quality_json_shape(tmp_path: Path) -> None:
    db, _ = _seed(tmp_path, _SAMPLE)
    result = runner.invoke(app, ["predict-quality", "--db", str(db), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["token_median_abs_error"] == 300
    assert payload["token_sample"] == 3
    assert payload["rework_sample"] == 3
    assert payload["token_median_abs_pct_error"] == 30.0
    assert payload["versions"] == [PREDICTOR_VERSION]
    assert {b["sample"] for b in payload["rework_bins"]} == {1, 2}


def test_predict_quality_scopes_to_one_run(tmp_path: Path) -> None:
    db, run_id = _seed(tmp_path, _SAMPLE)
    other = Ledger(db).run_create("epic-98", "auto")
    result = runner.invoke(app, ["predict-quality", other, "--db", str(db), "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["token_sample"] == 0
    scoped = runner.invoke(app, ["predict-quality", run_id, "--db", str(db), "--json"])
    assert json.loads(scoped.stdout)["token_sample"] == 3


def test_predict_quality_reports_clean_absence(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(app, ["predict-quality", "--db", str(db)])
    assert result.exit_code == 0
    assert "no recorded predictions" in result.stdout
    # Never materialises a spurious empty ledger.
    assert not db.exists()


def test_predict_quality_reports_an_honest_zero_sample_before_reconciliation(
    tmp_path: Path,
) -> None:
    """Predicted but never reconciled scores as "no sample", not a vacuous 0 error."""
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "auto")
    ledger.story_upsert(
        run_id, "s1-001", "99", "Story one", "P1", 2, "py", "", None, "IN_PROGRESS",
        ac_count=5, dep_depth=1, scope_proxy=4,
    )
    ledger.story_set_prediction(
        run_id, "s1-001",
        predicted_tokens=1_000, predicted_rework_prob=0.1,
        predictor_version=PREDICTOR_VERSION, low_confidence=False,
    )
    result = runner.invoke(app, ["predict-quality", "--db", str(db)])
    assert result.exit_code == 0
    assert "tokens: no reconciled prediction yet (n=0)" in result.stdout
    assert "rework: no reconciled prediction yet (n=0)" in result.stdout
    # The prediction itself is still on the books — absent actuals, not absent rows.
    assert "median absolute error" not in result.stdout


def test_predict_quality_rejects_an_unknown_run(tmp_path: Path) -> None:
    db, _ = _seed(tmp_path, _SAMPLE)
    result = runner.invoke(app, ["predict-quality", "nope", "--db", str(db)])
    assert result.exit_code == 2
