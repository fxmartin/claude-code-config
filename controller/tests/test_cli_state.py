# ABOUTME: Tests for `sdlc state` and `sdlc resume` CLI wiring (Story 10.1-001).
# ABOUTME: Seeds an interrupted ledger, then drives the commands via CliRunner.

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sdlc.cli import app

from test_resume import (
    _make_project,
    _seed_all_stages_done_unfinalised,
    _seed_complete,
    _seed_interrupted,
)

runner = CliRunner()


# --- state -----------------------------------------------------------------


def test_state_no_run(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(app, ["state", "--db", str(db)])
    assert result.exit_code == 0
    assert "no build run found" in result.stdout


def test_state_dumps_stage_rows(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)
    result = runner.invoke(app, ["state", "--db", str(db)])
    assert result.exit_code == 0
    out = result.stdout
    # Greppable: story id, stage, status, attempt all present.
    assert "99.1-002" in out
    assert "review" in out and "IN_PROGRESS" in out
    assert "build" in out and "DONE" in out
    assert "#100" in out  # PR rendered


def test_state_json(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)
    result = runner.invoke(app, ["state", "--db", str(db), "--json"])
    assert result.exit_code == 0
    rows = json.loads(result.stdout)
    assert any(r["story_id"] == "99.1-002" and r["stage_name"] == "review" for r in rows)


def test_state_json_no_run(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(app, ["state", "--db", str(db), "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []  # empty array, not the human message


def test_state_is_not_a_stub(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)
    result = runner.invoke(app, ["state", "--db", str(db)])
    assert "not yet implemented" not in result.stdout


# --- status reports the interrupted state ----------------------------------


def test_status_reports_interrupted_state(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)
    result = runner.invoke(app, ["status", "--db", str(db), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["counts"]["in_progress"] == 1
    assert payload["counts"]["done"] == 1
    by_id = {s["story_id"]: s for s in payload["stories"]}
    assert by_id["99.1-002"]["current_stage"] == "review"
    assert by_id["99.1-002"]["status"] == "IN_PROGRESS"


def test_status_human_shows_in_progress(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)
    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0
    assert "in progress" in result.stdout.lower()


# --- resume ----------------------------------------------------------------


def test_resume_command_finishes_run(tmp_path: Path, monkeypatch) -> None:
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)
    monkeypatch.chdir(tmp_path)

    # Inject the fake dispatcher so no real agent runs.
    import sdlc.resume as resume_mod
    from test_build import FakeDispatcher

    monkeypatch.setattr(resume_mod, "dispatch_agent", FakeDispatcher())
    result = runner.invoke(app, ["resume", "epic-99", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "resume finished" in result.output.lower()


def test_resume_command_nothing_to_resume(tmp_path: Path, monkeypatch) -> None:
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_complete(db)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["resume", "epic-99", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "nothing to resume" in result.output.lower()


def test_resume_command_run_with_no_incomplete_stories(tmp_path: Path, monkeypatch) -> None:
    """A resumable run whose stories are all (unfinalised but) complete is closed
    out rather than stranded: its end-crash story is finalised DONE without
    dispatching any stage (Story 17.2-002), so the run finalises coherently and
    its per-story worktree is reclaimed rather than leaked. Exits 0."""
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_all_stages_done_unfinalised(db)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["resume", "epic-99", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "1 done" in result.output.lower()
    assert "nothing to resume" not in result.output.lower()


def test_resume_is_not_a_stub(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(app, ["resume", "epic-99", "--db", str(db)])
    # No incomplete run → benign no-op, never the stub text.
    assert "not yet implemented" not in result.stdout
