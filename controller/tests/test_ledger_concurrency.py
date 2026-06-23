# ABOUTME: Tests for concurrency-safe ledger writes (Story 17.1-002).
# ABOUTME: Many threads write different stories at once; no "database is locked", rows isolated.

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from sdlc.build import LEDGER_BUSY_TIMEOUT_MS, Ledger


def _open(db: Path) -> sqlite3.Connection:
    # `with sqlite3.connect(...)` commits but never closes — close explicitly so
    # the test leaves no unclosed handle (the codebase's documented gotcha).
    return sqlite3.connect(db)


def test_write_connection_sets_explicit_busy_timeout(tmp_path: Path) -> None:
    """The write connection must set an *explicit*, intentional busy_timeout so
    contended writers wait out the brief WAL writer lock instead of erroring —
    rather than relying on Python's implicit ``sqlite3.connect`` default, which
    a future change to the connect call could silently drop. It must be at least
    as generous as the read connection's 2000ms."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    with ledger._connect() as conn:
        # PRAGMA busy_timeout returns the timeout in milliseconds.
        timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout_ms == LEDGER_BUSY_TIMEOUT_MS
    assert LEDGER_BUSY_TIMEOUT_MS >= 2000


def test_concurrent_multi_story_writes_never_lock_and_stay_isolated(
    tmp_path: Path,
) -> None:
    """Several workers writing rows for *different* stories at once must all
    succeed (no "database is locked") and leave each story's rows correct and
    isolated (no lost updates, no cross-story corruption)."""
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-17", "parallel")

    story_ids = [f"17.1-{i:03d}" for i in range(8)]
    for sid in story_ids:
        ledger.story_upsert(run_id, sid, "17", "Story", "P2", 3, "py", "", None, "TODO")

    # A barrier so every worker hits the ledger at the same instant — this is
    # what makes the write lock genuinely contended.
    start = threading.Barrier(len(story_ids))
    errors: list[BaseException] = []

    def _worker(sid: str) -> None:
        try:
            start.wait()
            # The full per-story write sequence a build worker performs: a stage
            # lifecycle, a status transition, and an audit event.
            ledger.stage_start(run_id, sid, "build", 1)
            ledger.event_log(run_id, sid, "info", "controller", f"{sid} building")
            ledger.stage_finish(run_id, sid, "build", 1, "DONE", "", "")
            ledger.set_story_status(run_id, sid, "DONE")
        except BaseException as exc:  # noqa: BLE001 - capture for the assert
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(sid,)) for sid in story_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No worker may have hit "database is locked" (or anything else).
    assert errors == [], f"concurrent writers raised: {errors!r}"

    # Every story's rows are present, correct, and isolated.
    conn = _open(db)
    try:
        for sid in story_ids:
            stage = conn.execute(
                "SELECT status FROM stages "
                "WHERE run_id=? AND story_id=? AND stage_name='build' AND attempt=1",
                (run_id, sid),
            ).fetchone()
            assert stage is not None and stage[0] == "DONE", sid

            story = conn.execute(
                "SELECT status FROM stories WHERE run_id=? AND story_id=?",
                (run_id, sid),
            ).fetchone()
            assert story is not None and story[0] == "DONE", sid

            events = conn.execute(
                "SELECT COUNT(*) FROM events WHERE run_id=? AND story_id=?",
                (run_id, sid),
            ).fetchone()[0]
            assert events == 1, sid

        # No spurious rows: exactly one stage and one event per story.
        total_stages = conn.execute(
            "SELECT COUNT(*) FROM stages WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        assert total_stages == len(story_ids)
    finally:
        conn.close()


def test_serial_writes_unchanged(tmp_path: Path) -> None:
    """A single-threaded write sequence behaves exactly as before — the
    concurrency guards must not alter the serial path."""
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-17", "serial")
    ledger.story_upsert(run_id, "17.1-002", "17", "Story", "P2", 3, "py", "", None, "TODO")
    ledger.stage_start(run_id, "17.1-002", "build", 1)
    ledger.stage_finish(run_id, "17.1-002", "build", 1, "DONE", "", "")

    conn = _open(db)
    try:
        row = conn.execute(
            "SELECT status, finished_at FROM stages "
            "WHERE run_id=? AND story_id='17.1-002' AND stage_name='build'",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "DONE"
    assert row[1] is not None
