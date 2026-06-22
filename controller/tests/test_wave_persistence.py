# ABOUTME: Tests for wave (cohort) index + dependency persistence (Story 11.2-007).
# ABOUTME: Covers the additive migration, schedule-time recording, and build↔resume parity.

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sdlc.build import (
    BuildOptions,
    Ledger,
    persist_cohort_structure,
    run_build,
    status_snapshot,
)
from sdlc.cohort import Story, compute_cohorts
from sdlc.resume import run_resume

from test_build import FakeDispatcher
from test_resume import _make_project, _seed_interrupted


# ---------------------------------------------------------------------------
# Migration 4 — additive `wave` + `dependencies` columns on `stories`
# ---------------------------------------------------------------------------

def _old_schema_db(db: Path) -> None:
    """A pre-11.2-007 ledger: full schema minus wave/dependencies on `stories`."""
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
    conn.commit()
    conn.close()


def _story_columns(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {r[1] for r in conn.execute("PRAGMA table_info(stories)").fetchall()}


_WAVE_COLS = {"wave", "dependencies"}


def test_init_migrates_existing_db_adds_wave_columns(tmp_path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db)
    assert not (_WAVE_COLS & _story_columns(db))  # neither present initially
    Ledger(db).init()
    assert _WAVE_COLS <= _story_columns(db)


def test_init_migration_preserves_rows_and_degrades_to_null(tmp_path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO runs(id, status) VALUES ('r1','DONE')")
        conn.execute(
            "INSERT INTO stories(run_id, story_id, status) VALUES ('r1','s1','DONE')"
        )
    Ledger(db).init()
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status, wave, dependencies FROM stories WHERE run_id='r1'"
        ).fetchone()
    assert row[0] == "DONE"   # pre-existing data intact
    assert row[1] is None     # wave defaults NULL on an old row
    assert row[2] is None     # dependencies defaults NULL on an old row


def test_init_migration_is_idempotent(tmp_path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db)
    Ledger(db).init()
    Ledger(db).init()  # second run must not raise (duplicate-column ALTER avoided)
    with sqlite3.connect(db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM _migrations WHERE version=4"
        ).fetchone()[0]
    assert n == 1


def test_fresh_schema_has_wave_columns(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    Ledger(db).init()
    assert _WAVE_COLS <= _story_columns(db)


# ---------------------------------------------------------------------------
# set_story_wave + the query surface (story_rows)
# ---------------------------------------------------------------------------

def test_set_story_wave_persists_index_and_deps_json(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    rid = ledger.run_create("epic-99", "parallel")
    ledger.story_upsert(rid, "s1", "99", "S", "P1", 1, "py", "", None, "TODO")
    ledger.set_story_wave(rid, "s1", 2, ["a", "b"])
    with sqlite3.connect(tmp_path / "ledger.db") as conn:
        row = conn.execute(
            "SELECT wave, dependencies FROM stories WHERE story_id='s1'"
        ).fetchone()
    assert row[0] == 2
    assert json.loads(row[1]) == ["a", "b"]


def test_story_rows_exposes_wave_and_parsed_deps(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    rid = ledger.run_create("epic-99", "parallel")
    ledger.story_upsert(rid, "s1", "99", "S1", "P1", 1, "py", "", None, "TODO")
    ledger.story_upsert(rid, "s2", "99", "S2", "P1", 1, "py", "", None, "TODO")
    ledger.set_story_wave(rid, "s1", 0, [])
    ledger.set_story_wave(rid, "s2", 1, ["s1"])
    rows = {r["story_id"]: r for r in ledger.story_rows(rid)}
    assert rows["s1"]["wave"] == 0
    assert rows["s1"]["dependencies"] == []
    assert rows["s2"]["wave"] == 1
    assert rows["s2"]["dependencies"] == ["s1"]


def test_story_rows_unrecorded_story_degrades_to_empty_deps(tmp_path) -> None:
    """A story never passed through `set_story_wave` reads wave=None, deps=[]."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    rid = ledger.run_create("epic-99", "parallel")
    ledger.story_upsert(rid, "s1", "99", "S1", "P1", 1, "py", "", None, "SKIPPED")
    row = ledger.story_rows(rid)[0]
    assert row["wave"] is None
    assert row["dependencies"] == []


# ---------------------------------------------------------------------------
# persist_cohort_structure — wave assignment + intra-queue edge filtering
# ---------------------------------------------------------------------------

def _q(story_id: str, deps: list[str]) -> Story:
    return Story(story_id, story_id, "99", "sample", "epic-99.md", "P1", 1, "py", deps)


def test_persist_cohort_structure_records_waves(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    rid = ledger.run_create("epic-99", "parallel")
    queue = [_q("a", []), _q("b", []), _q("c", ["a"]), _q("d", ["c"])]
    for s in queue:
        ledger.story_upsert(rid, s.id, "99", s.title, "P1", 1, "py", "", None, "TODO")
    cohorts = compute_cohorts(queue)
    persist_cohort_structure(ledger, rid, cohorts)

    rows = {r["story_id"]: r for r in ledger.story_rows(rid)}
    assert rows["a"]["wave"] == 0 and rows["a"]["dependencies"] == []
    assert rows["b"]["wave"] == 0 and rows["b"]["dependencies"] == []
    assert rows["c"]["wave"] == 1 and rows["c"]["dependencies"] == ["a"]
    assert rows["d"]["wave"] == 2 and rows["d"]["dependencies"] == ["c"]


def test_persist_cohort_structure_drops_out_of_queue_deps(tmp_path) -> None:
    """A dependency on an already-merged (out-of-queue) story is not persisted —
    matching compute_cohorts, which treats it as satisfied from the outset."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    rid = ledger.run_create("epic-99", "parallel")
    # `merged` is NOT in the queue; `x` depends on it plus in-queue `y`.
    queue = [_q("x", ["merged", "y"]), _q("y", [])]
    for s in queue:
        ledger.story_upsert(rid, s.id, "99", s.title, "P1", 1, "py", "", None, "TODO")
    cohorts = compute_cohorts(queue)
    persist_cohort_structure(ledger, rid, cohorts)

    rows = {r["story_id"]: r for r in ledger.story_rows(rid)}
    # y has no in-queue blocker → wave 0; x waits only on the in-queue edge → wave 1.
    assert rows["y"]["wave"] == 0
    assert rows["x"]["wave"] == 1
    assert rows["x"]["dependencies"] == ["y"]  # "merged" dropped


# ---------------------------------------------------------------------------
# run_build records wave + deps at schedule time
# ---------------------------------------------------------------------------

def test_run_build_records_wave_and_deps(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    queue = [
        Story("s1-001", "One", "99", "sample", "epic-99.md", "P1", 2, "py", []),
        Story("s1-002", "Two", "99", "sample", "epic-99.md", "P1", 2, "py", ["s1-001"]),
    ]
    run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=queue,
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    snap = status_snapshot(Ledger(db))
    rows = {s["story_id"]: s for s in snap["stories"]}
    assert rows["s1-001"]["wave"] == 0
    assert rows["s1-001"]["dependencies"] == []
    assert rows["s1-002"]["wave"] == 1
    assert rows["s1-002"]["dependencies"] == ["s1-001"]


# ---------------------------------------------------------------------------
# build ↔ resume parity — both scheduling paths record identical waves
# ---------------------------------------------------------------------------

def test_build_and_resume_agree_on_waves(tmp_path: Path) -> None:
    """Resume recomputes cohorts for the same queue and persists the same waves
    a fresh build would (AC: the two scheduling paths agree)."""
    # --- run_build over the two-story epic-99 queue (002 depends on 001) ---
    build_db = tmp_path / "build.db"
    queue = [
        Story("99.1-001", "One", "99", "sample", "epic-99-sample.md", "P1", 1,
              "general-purpose", []),
        Story("99.1-002", "Two", "99", "sample", "epic-99-sample.md", "P2", 2,
              "general-purpose", ["99.1-001"]),
    ]
    run_build(
        BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True,
                     sequential=True),
        queue=queue,
        ledger=Ledger(build_db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    build_rows = {s["story_id"]: s for s in status_snapshot(Ledger(build_db))["stories"]}

    # --- run_resume over a seeded interrupted ledger for the same markdown epic ---
    project = _make_project(tmp_path)
    resume_db = tmp_path / ".sdlc-state.db"
    _seed_interrupted(resume_db)
    run_resume("epic-99", ledger=Ledger(resume_db), dispatcher=FakeDispatcher(),
               root=project)
    resume_rows = {s["story_id"]: s for s in status_snapshot(Ledger(resume_db))["stories"]}

    for sid in ("99.1-001", "99.1-002"):
        assert build_rows[sid]["wave"] == resume_rows[sid]["wave"]
        assert build_rows[sid]["dependencies"] == resume_rows[sid]["dependencies"]
    # And the values are the expected DAG: 001 in wave 0 with no deps, 002 in
    # wave 1 waiting on 001.
    assert resume_rows["99.1-001"]["wave"] == 0
    assert resume_rows["99.1-001"]["dependencies"] == []
    assert resume_rows["99.1-002"]["wave"] == 1
    assert resume_rows["99.1-002"]["dependencies"] == ["99.1-001"]
