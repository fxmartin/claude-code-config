# ABOUTME: Tests for rate-limit stall telemetry (Story 27.3-004).
# ABOUTME: Stall seconds land in their own ledger table, distinct from stage durations.

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from sdlc.build import BuildOptions, Ledger, run_build, status_snapshot
from sdlc.cli import app
from sdlc.rate_limit import RateLimitSignal

from test_build import FakeDispatcher, _SAMPLE_STAGE_TOKENS, _sample_queue
from test_rate_limit_gate import RateLimitingDispatcher, _Sleeps

runner = CliRunner()


def _opts(**kw) -> BuildOptions:
    base = dict(scope="epic-99", skip_preflight=True, sequential=True)
    base.update(kw)
    return BuildOptions(**base)


def _stall_rows(db: Path, run_id: str) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r) for r in conn.execute(
                "SELECT story_id, stage, source, waited_s FROM stalls "
                "WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema: the stalls table exists on fresh DBs and is migrated onto old ones
# ---------------------------------------------------------------------------


def test_fresh_ledger_has_stalls_table(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    Ledger(db).init()
    conn = sqlite3.connect(db)
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "stalls" in tables


def test_ensure_migrated_adds_stalls_table(tmp_path: Path) -> None:
    # A pre-27.3-004 ledger has no stalls table; ensure_migrated() adds it and
    # records the migration version so it runs at most once.
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, status TEXT NOT NULL);"
        "CREATE TABLE _migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "  applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);"
    )
    conn.commit()
    conn.close()
    Ledger(db).ensure_migrated()
    conn = sqlite3.connect(db)
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "stalls" in tables


# ---------------------------------------------------------------------------
# Ledger write + read helpers
# ---------------------------------------------------------------------------


def test_stall_log_and_run_stall_totals(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.stall_log(run_id, "s1-001", "build", "retry-after", 120)
    ledger.stall_log(run_id, "s1-001", None, "window", 60)
    ledger.stall_log(run_id, "s1-002", "coverage", "429", 30)
    ledger.stall_log(run_id, "", None, "window-reset", 15)  # run-level (resume)
    totals = ledger.run_stall_totals(run_id)
    assert totals["total_s"] == 225
    assert totals["by_story"] == {"s1-001": 180, "s1-002": 30}
    # Another run's stalls never leak into this run's totals.
    other = ledger.run_create("epic-99", "serial")
    assert ledger.run_stall_totals(other) == {"total_s": 0, "by_story": {}}


def test_run_stall_totals_degrades_without_table_or_db(tmp_path: Path) -> None:
    # Read-only viewers never migrate: a ledger predating the stalls table (and
    # a missing DB) must read as "no stalls", never crash.
    missing = Ledger(tmp_path / "absent.db")
    assert missing.run_stall_totals("r1") == {"total_s": 0, "by_story": {}}

    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, status TEXT NOT NULL);"
    )
    conn.commit()
    conn.close()
    assert Ledger(old).run_stall_totals("r1") == {"total_s": 0, "by_story": {}}


# ---------------------------------------------------------------------------
# AC1: the in-process rate-limit wait records its seconds per story/stage
# ---------------------------------------------------------------------------


def test_reactive_429_wait_records_stall_for_story_and_stage(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    dispatcher = RateLimitingDispatcher(
        trip_on=("build", "s1-001"),
        signal=RateLimitSignal(source="retry-after", retry_after_s=120),
    )
    result = run_build(
        _opts(), queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=dispatcher, preflight=lambda: True, sleep_fn=_Sleeps(),
    )
    assert result.completed == 3
    rows = _stall_rows(db, result.run_id)
    assert rows == [
        {"story_id": "s1-001", "stage": "build", "source": "retry-after", "waited_s": 120},
    ]
    totals = Ledger(db).run_stall_totals(result.run_id)
    assert totals["total_s"] == 120
    assert totals["by_story"] == {"s1-001": 120}


def test_window_budget_wait_records_stall_for_gated_story(tmp_path: Path) -> None:
    # The proactive window gate stalls *before* the next story dispatches: the
    # stall is attributed to that story with no stage (nothing dispatched yet).
    db = tmp_path / "ledger.db"
    budget = 2 * _SAMPLE_STAGE_TOKENS  # tripped after the first story's 4 stages
    result = run_build(
        _opts(window_budget=budget, window_s=100, rate_limit_max_wait_s=18000),
        queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=FakeDispatcher(), preflight=lambda: True,
        sleep_fn=_Sleeps(), clock=lambda: 0.0,
    )
    assert result.completed == 3
    rows = _stall_rows(db, result.run_id)
    assert rows, "expected the proactive window stall to be recorded"
    assert all(r["stage"] is None for r in rows)
    assert all(r["waited_s"] > 0 for r in rows)
    # Attributed to the stories that were gated, not the one that spent the window.
    assert all(r["story_id"] in ("s1-002", "s1-003") for r in rows)


# ---------------------------------------------------------------------------
# AC2: status snapshot + `sdlc status` show stall time apart from duration
# ---------------------------------------------------------------------------


def _seed_with_stalls(db: Path) -> str:
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 2)
    ledger.story_upsert(run_id, "s1-001", "99", "One", "P1", 2, "py", "", None, "DONE")
    ledger.story_upsert(run_id, "s1-002", "99", "Two", "P1", 2, "py", "", None, "DONE")
    ledger.stage_start(run_id, "s1-001", "build", 1)
    ledger.stage_finish(run_id, "s1-001", "build", 1, "DONE")
    ledger.stall_log(run_id, "s1-001", "build", "retry-after", 300)
    return run_id


def test_status_snapshot_surfaces_stall_seconds(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed_with_stalls(db)
    snap = status_snapshot(Ledger(db))
    assert snap["run"]["stall_seconds"] == 300
    stories = {s["story_id"]: s for s in snap["stories"]}
    assert stories["s1-001"]["stall_seconds"] == 300
    # A story that never stalled reads None — distinct from "stalled 0s".
    assert stories["s1-002"]["stall_seconds"] is None


def test_status_snapshot_zero_stalls(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "s1-001", "99", "One", "P1", 2, "py", "", None, "DONE")
    snap = status_snapshot(ledger)
    assert snap["run"]["stall_seconds"] == 0


def test_status_human_shows_stall_separately(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed_with_stalls(db)
    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0
    assert "stall" in result.stdout  # the stall line is present…
    assert "300s" in result.stdout   # …with the waited seconds


def test_status_human_omits_stall_line_when_none(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "s1-001", "99", "One", "P1", 2, "py", "", None, "DONE")
    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0
    assert "stall" not in result.stdout


def test_status_json_includes_stall_seconds(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed_with_stalls(db)
    result = runner.invoke(app, ["status", "--db", str(db), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run"]["stall_seconds"] == 300
    assert all("stall_seconds" in s for s in payload["stories"])


def test_dashboard_page_renders_stalls() -> None:
    # The dashboard reads the same snapshot; the page must render stall time
    # separately from the duration cell/head line rather than folding it in.
    from sdlc.dashboard import _PAGE

    assert "stall_seconds" in _PAGE
    assert "stalled" in _PAGE
