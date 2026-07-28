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


# --- run-id prefixes (#546) -------------------------------------------------
#
# Every `--run` consumer matched on `WHERE run_id = ?`, so a truncated id — the
# form `sdlc status` and `sdlc runs` *print* — silently selected nothing. The
# miss then surfaced as a legitimate-looking result ("no incomplete stories",
# "0 stage rows"), which is indistinguishable from a genuinely finished or empty
# run. That is the worst possible failure for a command whose whole purpose is
# recovering work after a crash.


def test_state_resolves_a_run_id_prefix(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_interrupted(db)

    result = runner.invoke(app, ["state", "--db", str(db), "--run", run_id[:8]])
    assert result.exit_code == 0
    assert "99.1-002" in result.stdout
    assert "0 stage rows" not in result.stdout


def test_state_rejects_an_unknown_run_id(tmp_path: Path) -> None:
    """A miss must not render as an empty-but-valid run."""
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)

    result = runner.invoke(app, ["state", "--db", str(db), "--run", "nosuchrun"])
    assert result.exit_code == 2
    assert "unknown run id" in result.output
    assert "nosuchrun" in result.output


def test_resume_resolves_a_run_id_prefix(tmp_path: Path) -> None:
    """The reported case: the id `sdlc status` prints is not the id resume took."""
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_interrupted(db)

    result = runner.invoke(
        app, ["resume", "epic-99", "--db", str(db), "--run", run_id[:8]]
    )
    assert "no incomplete stories" not in result.stdout


def test_resume_rejects_an_unknown_run_id(tmp_path: Path) -> None:
    """`nothing to resume` must never be the answer to a bad `--run`.

    Reporting a lookup miss as a completed run tells an operator their work is
    gone when it is sitting intact in the ledger.
    """
    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)

    result = runner.invoke(
        app, ["resume", "epic-99", "--db", str(db), "--run", "deadbeef"]
    )
    assert result.exit_code == 2
    assert "unknown run id" in result.output
    assert "nothing to resume" not in result.output


def test_resume_rejects_an_ambiguous_run_id_prefix(tmp_path: Path) -> None:
    """Two runs sharing a prefix must error, never silently pick one."""
    from sdlc.build import Ledger

    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(db)
    ledger = Ledger(db)
    # Two ids sharing a prefix that matches neither exactly.
    for suffix in ("aaaa", "bbbb"):
        ledger.run_create("epic-99", "serial")
        with __import__("sqlite3").connect(db) as conn:
            conn.execute(
                "UPDATE runs SET id = ? WHERE id = ?",
                (f"dupe-{suffix}", ledger.latest_run_id()),
            )

    result = runner.invoke(
        app, ["resume", "epic-99", "--db", str(db), "--run", "dupe"]
    )
    assert result.exit_code == 2
    assert "ambiguous" in result.output
    assert "dupe" in result.output


def test_run_close_resolves_a_run_id_prefix(tmp_path: Path) -> None:
    """The fix-issue verbs take `--run` too and had the same silent miss."""
    from sdlc.build import Ledger

    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_interrupted(db)

    result = runner.invoke(
        app,
        ["run-close", "--db", str(db), "--run", run_id[:8], "--status", "ABORTED"],
    )
    assert result.exit_code == 0
    assert (Ledger(db).run_row(run_id) or {})["status"] == "ABORTED"
