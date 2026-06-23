# ABOUTME: Tests for concurrency-safe ledger writes (Story 17.1-002).
# ABOUTME: Many threads write different stories at once; no "database is locked", rows isolated.

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

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


def _hold_writer_lock(db: Path, acquired: threading.Event, release: threading.Event) -> None:
    """Grab SQLite's single writer lock via ``BEGIN IMMEDIATE`` and hold it until
    ``release`` is set, signalling ``acquired`` once the lock is genuinely held."""
    conn = sqlite3.connect(db)
    try:
        conn.execute("BEGIN IMMEDIATE;")  # takes the writer lock now, not lazily
        acquired.set()
        release.wait(timeout=5)
        conn.rollback()  # drop the lock without mutating any rows
    finally:
        conn.close()


def test_contended_writer_waits_out_lock_then_succeeds(tmp_path: Path) -> None:
    """The story's core guarantee: when the writer lock is already held, a Ledger
    write does NOT error with "database is locked" — the explicit busy_timeout
    makes it wait out the lock and then commit once the holder releases."""
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-17", "serial")
    ledger.story_upsert(run_id, "17.1-002", "17", "Story", "P2", 3, "py", "", None, "TODO")

    acquired = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=_hold_writer_lock, args=(db, acquired, release))
    holder.start()
    try:
        assert acquired.wait(timeout=5), "lock holder never acquired the writer lock"

        # Release the lock shortly after the Ledger write begins waiting, so the
        # write must wait out a real, held lock — but well within busy_timeout.
        hold_s = 0.4
        threading.Timer(hold_s, release.set).start()

        start = time.monotonic()
        ledger.set_story_status(run_id, "17.1-002", "DONE")  # must not raise
        elapsed = time.monotonic() - start
    finally:
        release.set()
        holder.join(timeout=5)

    # It waited (rather than the lock being instantly free) yet stayed far under
    # the 5000ms ceiling, and the write actually landed.
    assert elapsed >= hold_s / 2
    assert elapsed < LEDGER_BUSY_TIMEOUT_MS / 1000
    conn = _open(db)
    try:
        status = conn.execute(
            "SELECT status FROM stories WHERE run_id=? AND story_id='17.1-002'",
            (run_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "DONE"


def test_zero_timeout_writer_fails_proving_busy_timeout_is_what_saves_us(
    tmp_path: Path,
) -> None:
    """Control test: with no busy_timeout a writer hitting the same held lock
    errors immediately — demonstrating the explicit timeout, not luck, is what
    keeps the Ledger's contended writes from failing."""
    db = tmp_path / "ledger.db"
    Ledger(db).init()

    acquired = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=_hold_writer_lock, args=(db, acquired, release))
    holder.start()
    try:
        assert acquired.wait(timeout=5), "lock holder never acquired the writer lock"
        loser = sqlite3.connect(db, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                loser.execute("BEGIN IMMEDIATE;")
        finally:
            loser.close()
    finally:
        release.set()
        holder.join(timeout=5)
