# ABOUTME: Tests for per-story model-tier visibility (Story 27.2-002 AC4).
# ABOUTME: Covers stage_breakdown/state_rows reads, format_state render, and `sdlc status`.

from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from sdlc.build import Ledger, status_snapshot
from sdlc.cli import app
from sdlc.status import format_state, state_report

runner = CliRunner()


def _columns(db: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def _seed_run(ledger: Ledger) -> str:
    """A minimal run with one story so stage rows have a parent to join."""
    ledger.init()
    run_id = ledger.run_create("all", "serial")
    ledger.story_upsert(
        run_id, "27.2-002", "27", "Tier the skeptic", "should", 3, "backend", "", None, "TODO"
    )
    return run_id


def _old_schema_db(db: Path, *, with_stage: bool = False) -> None:
    """A pre-#427 ledger: stages table without the `model` column."""
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, scope TEXT, started_at TIMESTAMP, "
        "  finished_at TIMESTAMP, mode TEXT, total_stories INTEGER DEFAULT 0, "
        "  completed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, status TEXT NOT NULL);"
        "CREATE TABLE stories (run_id TEXT, story_id TEXT, epic_id TEXT, title TEXT, "
        "  priority TEXT, points INTEGER, agent_type TEXT, branch TEXT, "
        "  pr_number INTEGER, current_stage TEXT, status TEXT NOT NULL, "
        "  PRIMARY KEY(run_id, story_id));"
        "CREATE TABLE stages (run_id TEXT, story_id TEXT, stage_name TEXT, "
        "  attempt INTEGER DEFAULT 1, status TEXT NOT NULL, started_at TIMESTAMP, "
        "  finished_at TIMESTAMP, failure_category TEXT, output_path TEXT, "
        "  PRIMARY KEY(run_id, story_id, stage_name, attempt));"
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, "
        "  story_id TEXT, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP, level TEXT NOT NULL, "
        "  source TEXT, message TEXT NOT NULL);"
        "CREATE TABLE _migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "  applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);"
    )
    conn.execute(
        "INSERT INTO runs(id, scope, mode, status) VALUES ('r1', 'all', 'serial', 'IN_PROGRESS')"
    )
    conn.execute(
        "INSERT INTO stories(run_id, story_id, status) VALUES ('r1', 's1', 'IN_PROGRESS')"
    )
    if with_stage:
        conn.execute(
            "INSERT INTO stages(run_id, story_id, stage_name, attempt, status) "
            "VALUES ('r1', 's1', 'build', 1, 'DONE')"
        )
    conn.commit()
    conn.close()


# --- read side ---------------------------------------------------------------


def test_stage_breakdown_carries_model(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    run_id = _seed_run(ledger)
    ledger.stage_start(run_id, "27.2-002", "review", 1, model="sonnet")
    breakdown = ledger.stage_breakdown(run_id)
    assert breakdown["27.2-002"][0]["model"] == "sonnet"


def test_stage_breakdown_model_none_when_unrouted(tmp_path: Path) -> None:
    # Routing off records no model; the read side must surface None, not crash.
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    run_id = _seed_run(ledger)
    ledger.stage_start(run_id, "27.2-002", "build", 1)
    breakdown = ledger.stage_breakdown(run_id)
    assert breakdown["27.2-002"][0]["model"] is None


def test_state_rows_carry_model(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    run_id = _seed_run(ledger)
    ledger.stage_start(run_id, "27.2-002", "review", 1, model="opus")
    rows = {r["stage_name"]: r for r in ledger.state_rows(run_id)}
    assert rows["review"]["model"] == "opus"


# --- read-only viewer over a pre-migration ledger ----------------------------


def test_state_rows_on_unmigrated_ledger_model_none(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db, with_stage=True)

    rows = Ledger(db).state_rows("r1")

    assert rows and rows[0]["model"] is None
    # The read-only viewer must not have migrated the on-disk schema.
    assert "model" not in _columns(db, "stages")


def test_stage_breakdown_on_unmigrated_ledger_model_none(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db, with_stage=True)

    breakdown = Ledger(db).stage_breakdown("r1")

    assert breakdown["s1"][0]["model"] is None
    assert "model" not in _columns(db, "stages")


# --- render side --------------------------------------------------------------


def test_format_state_shows_model_column(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    run_id = _seed_run(ledger)
    ledger.stage_start(run_id, "27.2-002", "review", 1, model="sonnet")
    lines = format_state(state_report(ledger, run_id))
    assert "MODEL" in lines[0]
    assert any("sonnet" in line for line in lines[1:])


def test_format_state_model_dash_when_unrecorded(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    run_id = _seed_run(ledger)
    ledger.stage_start(run_id, "27.2-002", "build", 1)
    lines = format_state(state_report(ledger, run_id))
    assert "-" in lines[1]


def test_status_snapshot_rolls_up_models_per_story(tmp_path: Path) -> None:
    # AC4 (27.2-002): the tier used is visible per story — distinct models in
    # chronological first-use order across the story's stage attempts.
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    run_id = _seed_run(ledger)
    ledger.stage_start(run_id, "27.2-002", "build", 1, model="sonnet")
    ledger.stage_finish(run_id, "27.2-002", "build", 1, "DONE")
    ledger.stage_start(run_id, "27.2-002", "review", 1, model="sonnet")
    ledger.stage_finish(run_id, "27.2-002", "review", 1, "DONE")
    ledger.stage_start(run_id, "27.2-002", "merge", 1, model="opus")

    snap = status_snapshot(ledger, run_id)

    (story,) = snap["stories"]
    assert story["models"] == ["sonnet", "opus"]


def test_status_snapshot_models_empty_when_unrouted(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    run_id = _seed_run(ledger)
    ledger.stage_start(run_id, "27.2-002", "build", 1)

    snap = status_snapshot(ledger, run_id)

    (story,) = snap["stories"]
    assert story["models"] == []


def test_cli_status_human_shows_model_per_story(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    run_id = _seed_run(ledger)
    ledger.stage_start(run_id, "27.2-002", "review", 1, model="sonnet")
    ledger.set_story_status(run_id, "27.2-002", "IN_PROGRESS")

    result = runner.invoke(app, ["status", "--db", str(db)])

    assert result.exit_code == 0
    assert "MODEL" in result.output
    assert "sonnet" in result.output
