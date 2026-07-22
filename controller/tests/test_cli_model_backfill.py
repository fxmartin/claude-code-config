# ABOUTME: CLI tests for `sdlc model-backfill` — the historical `stages.model` backfill.
# ABOUTME: Story 28.1-002. Covers apply, --dry-run, --all, --json, and clean absence.

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sdlc.build import Ledger
from sdlc.cli import app

runner = CliRunner()

_OPUS = "claude-opus-4-8"


def _result_line(model: str | None) -> str:
    event = {"type": "result", "result": "ok", "session_id": "sess-1"}
    if model is not None:
        event["modelUsage"] = {model: {"costUSD": 1.5, "outputTokens": 200}}
    return json.dumps(event) + "\n"


def _seed(tmp_path: Path, log_body: str, *, model: str | None = None) -> tuple[Path, str]:
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-28", "auto")
    ledger.story_upsert(
        run_id, "28.1-002", "epic-28", "t", "Must", 3, "python-backend-engineer",
        "feature/28.1-002", None, "DONE",
    )
    ledger.stage_start(run_id, "28.1-002", "build", 1, model=model)
    ledger.stage_finish(run_id, "28.1-002", "build", 1, "DONE")
    logs_dir = Path(f"{db}.logs") / run_id
    logs_dir.mkdir(parents=True)
    (logs_dir / "28.1-002-build-1.log").write_text(log_body, encoding="utf-8")
    return db, run_id


def _model(db: Path, run_id: str) -> str | None:
    return Ledger(db).stage_usage_rows(run_id)[0]["model"]


def test_model_backfill_writes_and_summarises(tmp_path: Path) -> None:
    db, run_id = _seed(tmp_path, _result_line(_OPUS))

    result = runner.invoke(app, ["model-backfill", "--db", str(db)])

    assert result.exit_code == 0, result.stdout
    assert "28.1-002/build#1" in result.stdout
    assert "1 row(s) updated" in result.stdout
    assert _model(db, run_id) == _OPUS


def test_model_backfill_dry_run_writes_nothing(tmp_path: Path) -> None:
    db, run_id = _seed(tmp_path, _result_line(_OPUS))

    result = runner.invoke(app, ["model-backfill", "--db", str(db), "--dry-run"])

    assert result.exit_code == 0
    assert "dry-run" in result.stdout
    assert _model(db, run_id) is None


def test_model_backfill_reports_unrecoverable_rows(tmp_path: Path) -> None:
    """AC2: an unrecoverable row is counted and named, never coerced."""
    db, run_id = _seed(tmp_path, "plain text output\n")

    result = runner.invoke(app, ["model-backfill", "--db", str(db)])

    assert result.exit_code == 0
    assert "unrecoverable" in result.stdout
    assert "coverage 0/1" in result.stdout
    assert _model(db, run_id) is None


def test_model_backfill_is_idempotent(tmp_path: Path) -> None:
    db, _ = _seed(tmp_path, _result_line(_OPUS))
    runner.invoke(app, ["model-backfill", "--db", str(db)])

    result = runner.invoke(app, ["model-backfill", "--db", str(db)])

    assert result.exit_code == 0
    assert "0 row(s) updated" in result.stdout


def test_model_backfill_json_report(tmp_path: Path) -> None:
    db, _ = _seed(tmp_path, _result_line(_OPUS))

    result = runner.invoke(app, ["model-backfill", "--db", str(db), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["coverage"] == 1.0
    assert payload["updated"][0]["log_model"] == _OPUS


def test_model_backfill_all_runs(tmp_path: Path) -> None:
    db, _ = _seed(tmp_path, _result_line(_OPUS))

    result = runner.invoke(app, ["model-backfill", "--db", str(db), "--all", "--json"])

    assert result.exit_code == 0
    assert len(json.loads(result.stdout)["run_ids"]) == 1


def test_model_backfill_rejects_an_unknown_run(tmp_path: Path) -> None:
    db, _ = _seed(tmp_path, _result_line(_OPUS))

    result = runner.invoke(app, ["model-backfill", "nope", "--db", str(db)])

    assert result.exit_code == 2


def test_model_backfill_caps_the_residual_list_and_says_how_many_it_hid(
    tmp_path: Path,
) -> None:
    """A long residual list is truncated, but the hidden count stays visible.

    AC2 requires unrecoverable rows to be *counted and reported*; truncating the
    per-row listing must not silently drop the tail.
    """
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-28", "auto")
    ledger.story_upsert(
        run_id, "28.1-002", "epic-28", "t", "Must", 3, "python-backend-engineer",
        "feature/28.1-002", None, "DONE",
    )
    # 22 dispatched attempts, no logs on disk → all unrecoverable, over the cap.
    for attempt in range(1, 23):
        ledger.stage_start(run_id, "28.1-002", "build", attempt, model=None)
        ledger.stage_finish(run_id, "28.1-002", "build", attempt, "DONE")

    result = runner.invoke(app, ["model-backfill", "--db", str(db)])

    assert result.exit_code == 0, result.stdout
    assert "+2 more row(s) without a model" in result.stdout
    assert "coverage 0/22" in result.stdout


def test_model_backfill_without_a_ledger_is_clean(tmp_path: Path) -> None:
    result = runner.invoke(app, ["model-backfill", "--db", str(tmp_path / "absent.db")])

    assert result.exit_code == 0
    assert "no build run found" in result.stdout
