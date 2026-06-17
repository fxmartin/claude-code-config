# ABOUTME: Tests for the `status` command and the Ledger read-only query helpers.
# ABOUTME: Seeds a ledger via the writers, then asserts human + --json snapshots.

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sdlc.build import Ledger
from sdlc.cli import app

runner = CliRunner()


def _seed(db_path: Path) -> str:
    """Build a small two-story run and return its run id.

    Story A: build+coverage DONE, PR #42, status DONE.
    Story B: build IN_PROGRESS, status IN_PROGRESS.
    """
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("all", "parallel")
    ledger.set_total(run_id, 2)
    ledger.event_log(run_id, "", "info", "controller", "run started: scope=all mode=parallel")

    ledger.story_upsert(run_id, "34.5-003", "34", "Build the thing", "high", 3, "backend", "", None, "TODO")
    ledger.story_upsert(run_id, "34.6-001", "34", "Wire the API", "medium", 2, "backend", "", None, "TODO")

    # Story A: build -> coverage, both done, PR created, story DONE.
    ledger.stage_start(run_id, "34.5-003", "build", 1)
    ledger.stage_finish(run_id, "34.5-003", "build", 1, "DONE")
    ledger.stage_start(run_id, "34.5-003", "coverage", 1)
    ledger.stage_finish(run_id, "34.5-003", "coverage", 1, "DONE")
    ledger.set_story_pr(run_id, "34.5-003", 42)
    ledger.set_story_status(run_id, "34.5-003", "DONE")

    # Story B: build in progress.
    ledger.stage_start(run_id, "34.6-001", "build", 1)
    ledger.set_story_status(run_id, "34.6-001", "IN_PROGRESS")
    return run_id


# --- read helpers ----------------------------------------------------------


def test_latest_run_id_none_when_no_db(tmp_path: Path) -> None:
    assert Ledger(tmp_path / ".sdlc-state.db").latest_run_id() is None


def test_read_helpers_after_seed(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed(db)
    ledger = Ledger(db)

    assert ledger.latest_run_id() == run_id
    assert ledger.run_row(run_id)["status"] == "IN_PROGRESS"

    rows = {r["story_id"]: r for r in ledger.story_rows(run_id)}
    # current_stage is derived from the stages table, newest attempt wins.
    assert rows["34.5-003"]["current_stage"] == "coverage"
    assert rows["34.5-003"]["status"] == "DONE"
    assert rows["34.5-003"]["pr_number"] == 42
    assert rows["34.6-001"]["current_stage"] == "build"
    assert rows["34.6-001"]["status"] == "IN_PROGRESS"

    events = ledger.recent_events(run_id, limit=5)
    assert events  # oldest-first; the run-started event is present
    assert events[0]["message"].startswith("run started")


# --- status command --------------------------------------------------------


def test_status_no_run_human(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0
    assert "no build run found" in result.stdout


def test_status_no_run_json(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(app, ["status", "--db", str(db), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run"] is None


def test_status_human_snapshot(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed(db)
    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0
    out = result.stdout
    assert run_id[:8] in out
    assert "IN_PROGRESS" in out
    assert "1/2 done" in out  # one DONE of two stories
    assert "34.5-003" in out and "34.6-001" in out
    assert "#42" in out  # PR rendered with the GitHub # prefix


def test_status_json_snapshot(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed(db)
    result = runner.invoke(app, ["status", "--db", str(db), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run"]["id"] == run_id
    assert payload["run"]["status"] == "IN_PROGRESS"
    assert payload["counts"]["done"] == 1
    assert payload["counts"]["total"] == 2
    assert payload["counts"]["in_progress"] == 1
    by_id = {s["story_id"]: s for s in payload["stories"]}
    assert by_id["34.5-003"]["pr_number"] == 42
    assert by_id["34.6-001"]["current_stage"] == "build"


def test_status_explicit_run_id(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed(db)
    result = runner.invoke(app, ["status", "--db", str(db), "--run", run_id, "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["run"]["id"] == run_id


# --- status_snapshot (shared by status + dashboard) ------------------------


def test_status_snapshot_no_run(tmp_path: Path) -> None:
    from sdlc.build import status_snapshot

    snap = status_snapshot(Ledger(tmp_path / ".sdlc-state.db"))
    assert snap["run"] is None
    assert snap["counts"]["total"] == 0
    assert snap["stories"] == [] and snap["events"] == []


def test_status_snapshot_seeded(tmp_path: Path) -> None:
    from sdlc.build import status_snapshot

    db = tmp_path / ".sdlc-state.db"
    run_id = _seed(db)
    snap = status_snapshot(Ledger(db))
    assert snap["run"]["id"] == run_id
    assert snap["counts"]["done"] == 1
    assert snap["counts"]["total"] == 2
    by_id = {s["story_id"]: s for s in snap["stories"]}
    assert by_id["34.5-003"]["pr_number"] == 42
    assert snap["events"]
