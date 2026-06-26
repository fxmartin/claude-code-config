# ABOUTME: Tests for per-stage harness recording + surfacing in the ledger (Story 20.2-002).
# ABOUTME: Covers schema/migration, stage_start writes, state_rows reads, and format_state render.

from __future__ import annotations

import sqlite3
from pathlib import Path

from sdlc.build import BuildOptions, Ledger, _stage_harness
from sdlc.status import format_state, state_report


def _columns(db: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def _versions(db: Path) -> list[int]:
    conn = sqlite3.connect(db)
    try:
        return sorted(
            r[0] for r in conn.execute("SELECT version FROM _migrations").fetchall()
        )
    finally:
        conn.close()


def _seed_run(ledger: Ledger) -> str:
    """A minimal run with one story so stage rows have a parent to join."""
    ledger.init()
    run_id = ledger.run_create("all", "serial")
    ledger.story_upsert(
        run_id, "20.2-002", "20", "Record harness", "should", 3, "backend", "", None, "TODO"
    )
    return run_id


# --- schema + migration ----------------------------------------------------


def test_fresh_db_has_harness_column_and_migration_recorded(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    Ledger(db).init()
    assert "harness" in _columns(db, "stages")
    assert 6 in _versions(db)


def _old_schema_db(db: Path, *, with_stage: bool = False) -> None:
    """A pre-20.2-002 ledger: stages table without the `harness` column."""
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


def test_ensure_migrated_adds_harness_column(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db)
    assert "harness" not in _columns(db, "stages")

    Ledger(db).ensure_migrated()

    assert "harness" in _columns(db, "stages")
    assert 6 in _versions(db)


def test_pre_migration_ledger_still_loads_harness_defaults_to_claude(tmp_path: Path) -> None:
    # AC3: an existing ledger predating the harness column still loads; its stage
    # rows default to "claude" (everything before this story ran on Claude).
    db = tmp_path / "old.db"
    _old_schema_db(db, with_stage=True)
    Ledger(db).ensure_migrated()
    rows = state_report(Ledger(db), "r1")
    assert rows
    assert rows[0]["harness"] == "claude"


# --- write side ------------------------------------------------------------


def test_stage_start_defaults_to_claude(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    run_id = _seed_run(ledger)
    ledger.stage_start(run_id, "20.2-002", "build", 1)
    rows = ledger.state_rows(run_id)
    assert rows[0]["harness"] == "claude"


def test_stage_start_records_explicit_harness(tmp_path: Path) -> None:
    # AC1: a heterogeneous run records the harness that ran each stage.
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    run_id = _seed_run(ledger)
    ledger.stage_start(run_id, "20.2-002", "review", 1, harness="codex")
    rows = {r["stage_name"]: r for r in ledger.state_rows(run_id)}
    assert rows["review"]["harness"] == "codex"


def test_stage_breakdown_carries_harness(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    run_id = _seed_run(ledger)
    ledger.stage_start(run_id, "20.2-002", "build", 1, harness="codex")
    breakdown = ledger.stage_breakdown(run_id)
    assert breakdown["20.2-002"][0]["harness"] == "codex"


# --- role -> harness resolution -------------------------------------------


def test_stage_harness_resolves_role_from_map() -> None:
    opts = BuildOptions(harness_map={"review": "codex", "build": "claude"})
    assert _stage_harness("review", opts) == "codex"
    assert _stage_harness("build", opts) == "claude"
    # Unmapped role falls back to the built-in default.
    assert _stage_harness("coverage", opts) == "claude"


def test_stage_harness_default_when_no_map() -> None:
    opts = BuildOptions()
    assert _stage_harness("build", opts) == "claude"
    assert _stage_harness("merge", opts) == "claude"


# --- read/render side ------------------------------------------------------


def test_format_state_shows_harness_column(tmp_path: Path) -> None:
    # AC2: the per-stage state dump surfaces the harness for each stage.
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    run_id = _seed_run(ledger)
    ledger.stage_start(run_id, "20.2-002", "review", 1, harness="codex")
    lines = format_state(state_report(ledger, run_id))
    assert "HARNESS" in lines[0]
    assert any("codex" in line for line in lines[1:])
