# ABOUTME: Tests for the Epic-22 `story_inventory` ledger table + its migration.
# ABOUTME: Story 22.1-001 — fresh-create has the table; an old ledger gains it without data loss.

from __future__ import annotations

import sqlite3
from pathlib import Path

from sdlc.build import Ledger

# Every column Story 22.1-001 requires on the inventory cache. `host`+`issue_ref`
# together identify the remote item (GitHub number / GitLab iid); `harness` is the
# derived per-story harness summary; the rest project the MD spec.
_INVENTORY_COLS = {
    "story_id",
    "epic",
    "feature",
    "title",
    "points",
    "risk",
    "status",
    "owner",
    "host",
    "issue_ref",
    "harness",
    "updated_at",
}


def _old_schema_db(db: Path, *, with_run: bool = False) -> None:
    """A pre-inventory ledger: the original Epic-04 shape, no `story_inventory`.

    Mirrors a real ledger built before this story shipped. ``with_run`` seeds one
    minimal run so a data-loss check has a row to verify survives the migration.
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
    conn.commit()
    conn.close()


def _columns(db: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def _has_table(db: Path, table: str) -> bool:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _versions(db: Path) -> list[int]:
    conn = sqlite3.connect(db)
    try:
        return sorted(r[0] for r in conn.execute("SELECT version FROM _migrations").fetchall())
    finally:
        conn.close()


# --- fresh create ----------------------------------------------------------


def test_fresh_init_creates_story_inventory(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    Ledger(db).init()
    assert _has_table(db, "story_inventory")
    assert _INVENTORY_COLS <= _columns(db, "story_inventory")


def test_story_inventory_story_id_is_primary_key(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    Ledger(db).init()
    conn = sqlite3.connect(db)
    try:
        pk = [r[1] for r in conn.execute("PRAGMA table_info(story_inventory)").fetchall() if r[5]]
    finally:
        conn.close()
    assert pk == ["story_id"]


def test_story_inventory_round_trips_a_row(tmp_path: Path) -> None:
    # The schema must be usable end-to-end: a full projected+cached row inserts
    # and reads back unchanged (the sync/build/projector write these fields).
    db = tmp_path / "fresh.db"
    Ledger(db).init()
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO story_inventory "
            "(story_id, epic, feature, title, points, risk, status, owner, host, "
            " issue_ref, harness) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("22.1-001", "22", "22.1", "Story inventory schema + migration", 3,
             "Medium", "TODO", "fxmartin", "github", "42",
             "build:claude review:codex"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT epic, feature, points, host, issue_ref, harness "
            "FROM story_inventory WHERE story_id='22.1-001'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("22", "22.1", 3, "github", "42", "build:claude review:codex")


# --- backward-compat upgrade ----------------------------------------------


def test_old_ledger_gains_story_inventory_on_migrate(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db)
    assert not _has_table(db, "story_inventory")

    Ledger(db).ensure_migrated()

    assert _has_table(db, "story_inventory")
    assert _INVENTORY_COLS <= _columns(db, "story_inventory")


def test_migrate_preserves_existing_rows_when_adding_inventory(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db, with_run=True)
    Ledger(db).ensure_migrated()
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT status FROM runs WHERE id='r1'").fetchone()
    finally:
        conn.close()
    assert row[0] == "IN_PROGRESS"  # older ledger upgraded without data loss


def test_inventory_migration_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db)
    Ledger(db).ensure_migrated()
    Ledger(db).ensure_migrated()  # second pass must not raise (no duplicate CREATE)
    assert _has_table(db, "story_inventory")
    assert _versions(db).count(7) == 1  # the inventory migration recorded exactly once


def test_inventory_migration_noop_on_fresh_db(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    ledger = Ledger(db)
    ledger.init()  # DDL already creates story_inventory
    before = _columns(db, "story_inventory")
    ledger.ensure_migrated()  # must be a no-op, no regression
    assert before == _columns(db, "story_inventory")


def test_fresh_init_records_inventory_migration_version(tmp_path: Path) -> None:
    # A fresh DB gets the table from the base DDL, but Migration 7 must still be
    # *recorded* as applied — otherwise a later ensure_migrated would re-run its
    # CREATE (a no-op here, but the version bookkeeping must stay honest so the
    # idempotency guard holds across the init → ensure_migrated boundary).
    db = tmp_path / "fresh.db"
    Ledger(db).init()
    assert 7 in _versions(db)


def test_partially_migrated_ledger_gains_only_inventory(tmp_path: Path) -> None:
    # The realistic upgrade: a ledger already at Migrations 1-6 (the shape just
    # before this story) where Migration 7 is the *only* pending one. Recording
    # 1-6 as applied means the column-add migrations are skipped and only the
    # table-creation path runs — the branch a from-empty `_migrations` fixture
    # never isolates.
    db = tmp_path / "v6.db"
    _old_schema_db(db)
    conn = sqlite3.connect(db)
    try:
        conn.executemany(
            "INSERT INTO _migrations(version, name) VALUES (?, ?)",
            [(v, f"migration {v}") for v in range(1, 7)],
        )
        conn.commit()
    finally:
        conn.close()
    assert not _has_table(db, "story_inventory")

    Ledger(db).ensure_migrated()

    assert _has_table(db, "story_inventory")
    # Migration 7 creates the inventory table; Migration 8 adds its `human_status`
    # column (a no-op against the table the up-to-date DDL just created, but still
    # recorded). Both were the pending tail.
    assert _versions(db) == [1, 2, 3, 4, 5, 6, 7, 8]


def test_story_inventory_updated_at_is_autopopulated(tmp_path: Path) -> None:
    # `updated_at` is NOT NULL DEFAULT CURRENT_TIMESTAMP: a row inserted without
    # it must still get a non-null stamp, so the projector/sync never has to
    # supply one and a reader can always order by it.
    db = tmp_path / "fresh.db"
    Ledger(db).init()
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO story_inventory(story_id) VALUES ('22.1-001')"
        )
        conn.commit()
        updated_at = conn.execute(
            "SELECT updated_at FROM story_inventory WHERE story_id='22.1-001'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert updated_at is not None
