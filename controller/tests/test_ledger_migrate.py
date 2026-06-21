# ABOUTME: Tests for Ledger.ensure_migrated() and auto-migrate-at-launch wiring.
# ABOUTME: Stale-DB fixture is migrated by each read/recovery verb; no-DB stays absent.

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from typer.testing import CliRunner

from sdlc.build import Ledger
from sdlc.cli import app

runner = CliRunner()


# The six usage columns Migration 1 adds to `stages`, and the two progress
# columns Migration 2 adds to `events` — the signal that a stale DB has been
# brought up to date.
_USAGE_COLS = {
    "session_id", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens", "cost_usd",
}
_EVENT_COLS = {"stage", "kind"}


def _old_schema_db(db: Path, *, with_run: bool = False) -> None:
    """Create a pre-migration ledger: full schema minus the migrated columns.

    Mirrors a real ledger built before Migrations 1 and 2 shipped. With
    ``with_run`` it seeds one minimal run so the read verbs have something to
    report (and a row a stale-column query could trip over).
    """
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
    if with_run:
        conn.execute(
            "INSERT INTO runs(id, scope, mode, status) "
            "VALUES ('r1', 'all', 'serial', 'IN_PROGRESS')"
        )
        conn.execute(
            "INSERT INTO stories(run_id, story_id, status) VALUES ('r1', 's1', 'IN_PROGRESS')"
        )
    conn.commit()
    conn.close()


def _columns(db: Path, table: str) -> set[str]:
    # `with sqlite3.connect(...)` commits but never closes — close explicitly so
    # the test leaves no unclosed handle (the codebase's documented gotcha).
    conn = sqlite3.connect(db)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def _versions(db: Path) -> list[int]:
    conn = sqlite3.connect(db)
    try:
        return sorted(r[0] for r in conn.execute("SELECT version FROM _migrations").fetchall())
    finally:
        conn.close()


# --- ensure_migrated unit behaviour ----------------------------------------


def test_ensure_migrated_applies_pending_columns(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db)
    assert not (_USAGE_COLS & _columns(db, "stages"))
    assert not (_EVENT_COLS & _columns(db, "events"))

    Ledger(db).ensure_migrated()

    assert _USAGE_COLS <= _columns(db, "stages")
    assert _EVENT_COLS <= _columns(db, "events")


def test_ensure_migrated_ledger_predating_migrations_table(tmp_path: Path) -> None:
    # A ledger built *before* the migration framework shipped has neither the
    # token/progress columns nor the bookkeeping `_migrations` table — and that
    # is precisely the ledger Migration 1 targets. ensure_migrated must create
    # the table on the fly rather than crash with "no such table: _migrations".
    db = tmp_path / "ancient.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, scope TEXT, started_at TIMESTAMP, "
        "  finished_at TIMESTAMP, mode TEXT, total_stories INTEGER DEFAULT 0, "
        "  completed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, status TEXT NOT NULL);"
        "CREATE TABLE stages (run_id TEXT, story_id TEXT, stage_name TEXT, "
        "  attempt INTEGER DEFAULT 1, status TEXT NOT NULL, "
        "  PRIMARY KEY(run_id, story_id, stage_name, attempt));"
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, "
        "  story_id TEXT, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP, level TEXT NOT NULL, "
        "  source TEXT, message TEXT NOT NULL);"
    )
    conn.commit()
    conn.close()

    Ledger(db).ensure_migrated()  # must not raise

    assert _USAGE_COLS <= _columns(db, "stages")
    assert _EVENT_COLS <= _columns(db, "events")
    assert _versions(db) == [1, 2, 3]


def test_ensure_migrated_no_db_is_noop_and_creates_nothing(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    Ledger(db).ensure_migrated()  # must not raise
    assert not db.exists()  # never-built repo stays without a spurious ledger


def test_ensure_migrated_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db)
    Ledger(db).ensure_migrated()
    Ledger(db).ensure_migrated()  # second pass must not raise (no duplicate ALTER)
    assert _versions(db) == [1, 2, 3]


def test_ensure_migrated_on_fresh_db_is_noop(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    ledger = Ledger(db)
    ledger.init()  # creates the schema from the current DDL + applies migrations
    before = _columns(db, "stages") | _columns(db, "events")
    ledger.ensure_migrated()  # must be a no-op, no regression
    after = _columns(db, "stages") | _columns(db, "events")
    assert before == after


def test_ensure_migrated_preserves_existing_rows(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db, with_run=True)
    Ledger(db).ensure_migrated()
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT status FROM runs WHERE id='r1'").fetchone()
    finally:
        conn.close()
    assert row[0] == "IN_PROGRESS"  # pre-existing data intact


def test_ensure_migrated_concurrent_launches_are_safe(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db)
    errors: list[BaseException] = []

    def _launch() -> None:
        try:
            Ledger(db).ensure_migrated()
        except BaseException as exc:  # noqa: BLE001 - capture for the assert
            errors.append(exc)

    threads = [threading.Thread(target=_launch) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []  # no double-apply ALTER crash, no busy-timeout failure
    assert _USAGE_COLS <= _columns(db, "stages")
    assert _versions(db) == [1, 2, 3]  # each migration recorded exactly once


# --- auto-migrate at verb launch -------------------------------------------


def test_status_migrates_stale_ledger_at_launch(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _old_schema_db(db, with_run=True)
    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0
    assert _USAGE_COLS <= _columns(db, "stages")
    assert _EVENT_COLS <= _columns(db, "events")


def test_status_no_db_creates_no_ledger(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0
    assert "no build run found" in result.stdout
    assert not db.exists()  # read verb must not materialise an empty ledger


def test_state_migrates_stale_ledger_at_launch(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _old_schema_db(db, with_run=True)
    result = runner.invoke(app, ["state", "--db", str(db)])
    assert result.exit_code == 0
    assert _USAGE_COLS <= _columns(db, "stages")


def test_resume_migrates_stale_ledger_at_launch(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _old_schema_db(db, with_run=True)
    result = runner.invoke(app, ["resume", "--db", str(db)])
    assert result.exit_code == 0
    assert _USAGE_COLS <= _columns(db, "stages")
    assert _EVENT_COLS <= _columns(db, "events")


def test_rollback_migrates_stale_ledger_at_launch(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _old_schema_db(db, with_run=True)
    # The migrate runs at launch, before run_rollback reads — a benign/no-op
    # rollback is enough to exercise the wiring, regardless of its exit code.
    runner.invoke(app, ["rollback", "r1", "--to", "s1", "--db", str(db)])
    assert _USAGE_COLS <= _columns(db, "stages")


def test_dashboard_make_server_migrates_stale_ledger(tmp_path: Path) -> None:
    from sdlc.dashboard import make_server

    db = tmp_path / ".sdlc-state.db"
    _old_schema_db(db, with_run=True)
    server = make_server(db_path=db, port=0)
    try:
        assert _USAGE_COLS <= _columns(db, "stages")
        assert _EVENT_COLS <= _columns(db, "events")
    finally:
        server.server_close()


def test_dashboard_registry_mode_migrates_every_run_ledger(tmp_path: Path) -> None:
    """Registry-discovery mode (no --db) reads each run's own ledger read-only;
    migrate them all at launch so a stale per-run DB never crashes a request."""
    import os

    from sdlc.dashboard import make_server
    from sdlc.registry import Registry, RunRecord

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    db_a = repo_a / ".sdlc-state.db"
    db_b = repo_b / ".sdlc-state.db"
    _old_schema_db(db_a, with_run=True)
    _old_schema_db(db_b, with_run=True)

    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord("run-a", str(repo_a), str(db_a), "epic-aaa", os.getpid(),
                  "IN_PROGRESS", "2026-01-01T00:00:00+00:00", total=1, completed=0)
    )
    registry.register(
        RunRecord("run-b", str(repo_b), str(db_b), "epic-bbb", os.getpid(),
                  "DONE", "2026-01-02T00:00:00+00:00", total=1, completed=1)
    )

    server = make_server(db_path=None, port=0, registry=registry)
    try:
        for db in (db_a, db_b):
            assert _USAGE_COLS <= _columns(db, "stages")
            assert _EVENT_COLS <= _columns(db, "events")
    finally:
        server.server_close()


def test_migrate_registry_ledgers_skips_unreachable(tmp_path: Path) -> None:
    """A missing/corrupt ledger in the registry is skipped, not fatal — and no
    spurious DB is materialised for a record whose file does not exist."""
    import os

    from sdlc.dashboard import _migrate_registry_ledgers
    from sdlc.registry import Registry, RunRecord

    good_db = tmp_path / "good.db"
    _old_schema_db(good_db)
    missing_db = tmp_path / "gone" / ".sdlc-state.db"  # parent dir absent

    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord("r1", str(tmp_path), str(good_db), "epic-aaa", os.getpid(),
                  "IN_PROGRESS", "2026-01-01T00:00:00+00:00", total=1, completed=0)
    )
    registry.register(
        RunRecord("r2", str(tmp_path), str(missing_db), "epic-bbb", os.getpid(),
                  "DONE", "2026-01-02T00:00:00+00:00", total=1, completed=1)
    )

    _migrate_registry_ledgers(registry)  # must not raise

    assert _USAGE_COLS <= _columns(good_db, "stages")  # reachable one migrated
    assert not missing_db.exists()  # unreachable one not materialised


def test_migrate_registry_ledgers_dedupes_shared_db(tmp_path: Path) -> None:
    """Two registry records pointing at the *same* ledger migrate it once: the
    second record is skipped by the ``seen`` guard, not migrated twice."""
    import os

    from sdlc.dashboard import _migrate_registry_ledgers
    from sdlc.registry import Registry, RunRecord

    shared_db = tmp_path / ".sdlc-state.db"
    _old_schema_db(shared_db)

    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord("r1", str(tmp_path), str(shared_db), "epic-aaa", os.getpid(),
                  "IN_PROGRESS", "2026-01-01T00:00:00+00:00", total=1, completed=0)
    )
    registry.register(
        RunRecord("r2", str(tmp_path), str(shared_db), "epic-bbb", os.getpid(),
                  "DONE", "2026-01-02T00:00:00+00:00", total=1, completed=1)
    )

    _migrate_registry_ledgers(registry)  # must not raise on the duplicate db

    assert _USAGE_COLS <= _columns(shared_db, "stages")  # migrated exactly once
    assert _versions(shared_db) == [1, 2, 3]  # no double-apply via the dupe record


def test_migrate_registry_ledgers_skips_corrupt_existing_db(tmp_path: Path) -> None:
    """An *existing but corrupt* ledger raises ``sqlite3.Error`` from
    ``ensure_migrated`` — the best-effort loop swallows it and keeps going,
    still migrating the reachable, valid ledger."""
    import os

    from sdlc.dashboard import _migrate_registry_ledgers
    from sdlc.registry import Registry, RunRecord

    corrupt_db = tmp_path / "corrupt.db"
    corrupt_db.write_bytes(b"this is not a sqlite database")  # exists, but unreadable
    good_db = tmp_path / "good.db"
    _old_schema_db(good_db)

    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord("r1", str(tmp_path), str(corrupt_db), "epic-aaa", os.getpid(),
                  "IN_PROGRESS", "2026-01-01T00:00:00+00:00", total=1, completed=0)
    )
    registry.register(
        RunRecord("r2", str(tmp_path), str(good_db), "epic-bbb", os.getpid(),
                  "DONE", "2026-01-02T00:00:00+00:00", total=1, completed=1)
    )

    _migrate_registry_ledgers(registry)  # corrupt db must not abort the loop

    assert _USAGE_COLS <= _columns(good_db, "stages")  # the valid ledger migrated
