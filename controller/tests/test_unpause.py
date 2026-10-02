# ABOUTME: Story 32.2-003 tests for `sdlc queue unpause` and the clear helper behind it.
# ABOUTME: Real ledgers, queue store and registry; no live scheduler (see test_scheduler.py).

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from sdlc.build import Ledger
from sdlc.cli import app
from sdlc.queue import QueueStore
from sdlc.registry import Registry, RunRecord
from sdlc.unpause import clear_rate_limit

runner = CliRunner()
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _dead_pid() -> int:
    """A pid no live process holds — the scheduler-gone shape a park leaves."""
    pid = 4_000_000
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pass
        pid += 1


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    store = QueueStore(tmp_path / "queue.db")
    return store, Registry(tmp_path / "registry.json")


def _parked_run(tmp_path, registry, name, *, reset_at=1_900_000_000.0, pid=None):
    repo = tmp_path / name
    repo.mkdir()
    db = str(repo / ".sdlc-state.db")
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-3", "build")
    ledger.event_log(run_id, "", "info", "config", json.dumps({"x": 1, "rate_limit_reset_at": reset_at}))
    ledger.run_update_status(run_id, "RATE_LIMITED")
    registry.register(
        RunRecord(run_id=run_id, repo=str(repo), db=db, scope="epic-3",
                  pid=pid or _dead_pid(), status="IN_PROGRESS", started_at="")
    )
    return run_id, db


def _pause(store):
    store.init()
    store.pause_dispatch(until=NOW + timedelta(hours=3), reason="rate limited", now=NOW)


def test_clears_pause_and_re_arms_the_parked_run(tmp_path, monkeypatch) -> None:
    store, registry = _env(tmp_path, monkeypatch)
    run_id, db = _parked_run(tmp_path, registry, "alpha")
    _pause(store)

    result = clear_rate_limit(store, registry, now=NOW)

    assert store.dispatch_pause() is None
    assert [r.run_id for r in result.runs] == [run_id]
    ledger = Ledger(db)
    assert "rate_limit_reset_at" not in ledger.run_config(run_id)
    assert ledger.run_config(run_id)["x"] == 1  # other config keys survive
    assert ledger.run_row(run_id)["status"] == "IN_PROGRESS"
    audit = ledger.events_by_source(run_id, "operator")
    assert len(audit) == 1 and "rate_limit_reset_at" in audit[0]
    clears = store.pause_clears()
    assert len(clears) == 1
    assert clears[0]["reason"] == "operator" and clears[0]["cleared_at"] == NOW.isoformat()


def test_re_arms_rate_limited_stories(tmp_path, monkeypatch) -> None:
    store, registry = _env(tmp_path, monkeypatch)
    run_id, db = _parked_run(tmp_path, registry, "alpha")
    ledger = Ledger(db)
    with ledger._connect() as conn:
        conn.execute(
            "INSERT INTO stories(run_id, story_id, title, status) VALUES (?, ?, ?, ?)",
            (run_id, "3.1-001", "One", "RATE_LIMITED"),
        )
        conn.execute(
            "INSERT INTO stories(run_id, story_id, title, status) VALUES (?, ?, ?, ?)",
            (run_id, "3.1-002", "Two", "DONE"),
        )

    result = clear_rate_limit(store, registry, now=NOW)

    assert result.runs[0].stories == ["3.1-001"]
    statuses = {s["story_id"]: s["status"] for s in ledger.story_rows(run_id)}
    assert statuses == {"3.1-001": "IN_PROGRESS", "3.1-002": "DONE"}


def test_covers_every_repo_and_skips_live_and_finished_runs(tmp_path, monkeypatch) -> None:
    store, registry = _env(tmp_path, monkeypatch)
    a, _ = _parked_run(tmp_path, registry, "alpha")
    b, _ = _parked_run(tmp_path, registry, "beta")
    live, live_db = _parked_run(tmp_path, registry, "gamma", pid=os.getpid())

    result = clear_rate_limit(store, registry, now=NOW)

    assert {r.run_id for r in result.runs} == {a, b}
    assert Ledger(live_db).run_row(live)["status"] == "RATE_LIMITED"


def test_nothing_to_clear_changes_nothing(tmp_path, monkeypatch) -> None:
    _env(tmp_path, monkeypatch)
    result = runner.invoke(app, ["queue", "unpause"])
    assert result.exit_code == 0, result.output
    assert "nothing to clear" in result.output
    assert not (tmp_path / "queue.db").exists()


def test_dry_run_lists_and_writes_nothing(tmp_path, monkeypatch) -> None:
    store, registry = _env(tmp_path, monkeypatch)
    run_id, db = _parked_run(tmp_path, registry, "alpha")
    store.init()
    _pause(store)

    result = runner.invoke(app, ["queue", "unpause", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "would clear queue pause" in result.output
    assert run_id[:8] in result.output
    assert store.dispatch_pause() is not None
    assert store.pause_clears() == []
    ledger = Ledger(db)
    assert ledger.run_row(run_id)["status"] == "RATE_LIMITED"
    assert "rate_limit_reset_at" in ledger.run_config(run_id)
    assert ledger.events_by_source(run_id, "operator") == []


def test_cli_prints_what_it_cleared(tmp_path, monkeypatch) -> None:
    store, registry = _env(tmp_path, monkeypatch)
    run_id, _ = _parked_run(tmp_path, registry, "alpha")
    store.init()
    _pause(store)

    result = runner.invoke(app, ["queue", "unpause"])

    assert result.exit_code == 0, result.output
    assert "cleared queue pause" in result.output
    assert f"cleared run {run_id[:8]} (alpha)" in result.output
    assert store.dispatch_pause() is None


def test_pause_only_clear_is_audited(tmp_path, monkeypatch) -> None:
    store, registry = _env(tmp_path, monkeypatch)
    _pause(store)

    result = clear_rate_limit(store, registry, now=NOW)

    assert result.runs == [] and result.pause is not None
    assert store.dispatch_pause() is None
    assert store.pause_clears()[0]["runs_cleared"] == 0


def test_finished_run_is_skipped(tmp_path, monkeypatch) -> None:
    store, registry = _env(tmp_path, monkeypatch)
    run_id, db = _parked_run(tmp_path, registry, "alpha")
    record = registry.records()[0]
    registry.register(
        RunRecord(run_id=record.run_id, repo=record.repo, db=record.db, scope=record.scope,
                  pid=record.pid, status="DONE", started_at="", finished_at="2026-09-27T00:00:00")
    )

    result = clear_rate_limit(store, registry, now=NOW)

    assert result.nothing_to_clear
    assert Ledger(db).run_row(run_id)["status"] == "RATE_LIMITED"


def test_unreadable_ledger_does_not_block_other_runs(tmp_path, monkeypatch) -> None:
    store, registry = _env(tmp_path, monkeypatch)
    good, _ = _parked_run(tmp_path, registry, "alpha")
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / ".sdlc-state.db").write_text("not a sqlite database")
    registry.register(
        RunRecord(run_id="broken-run", repo=str(bad), db=str(bad / ".sdlc-state.db"),
                  scope="x", pid=_dead_pid(), status="IN_PROGRESS", started_at="")
    )

    result = clear_rate_limit(store, registry, now=NOW)

    assert [r.run_id for r in result.runs] == [good]


def test_run_without_a_valid_reset_epoch_is_still_re_armed(tmp_path, monkeypatch) -> None:
    store, registry = _env(tmp_path, monkeypatch)
    run_id, db = _parked_run(tmp_path, registry, "alpha", reset_at="soon")

    result = clear_rate_limit(store, registry, now=NOW)

    assert result.runs[0].reset_at is None
    assert result.runs[0].to_dict() == {
        "run_id": run_id, "repo": str(tmp_path / "alpha"), "reset_at": None, "stories": [],
    }
    assert Ledger(db).run_row(run_id)["status"] == "IN_PROGRESS"
    assert store.pause_clears()[0]["runs_cleared"] == 1


def test_pause_clears_is_empty_without_a_db_or_table(tmp_path) -> None:
    assert QueueStore(tmp_path / "missing.db").pause_clears() == []
    legacy = tmp_path / "legacy.db"
    import sqlite3
    sqlite3.connect(legacy).close()
    assert QueueStore(legacy).pause_clears() == []


# --- Story 35.2-003: unpause by pool ---------------------------------------


def _pool_pause(store, pool):
    store.pause_dispatch(until=NOW + timedelta(hours=3), reason="rate limited", pool=pool, now=NOW)


def test_unpause_with_a_pool_resumes_only_that_pool(tmp_path, monkeypatch) -> None:
    store, _ = _env(tmp_path, monkeypatch)
    store.init()
    _pool_pause(store, "claude-shared")
    _pool_pause(store, "codex-shared")

    result = runner.invoke(app, ["queue", "unpause", "--pool", "claude-shared"])

    assert result.exit_code == 0, result.output
    assert "cleared queue pause (claude-shared)" in result.output
    assert [p.pool for p in store.dispatch_pauses()] == ["codex-shared"]
    assert store.pause_clears()[0]["reason"] == "operator (pool claude-shared)"


def test_unpause_with_a_pool_re_arms_only_runs_of_that_pool(tmp_path, monkeypatch) -> None:
    store, registry = _env(tmp_path, monkeypatch)
    store.init()
    shared_run, shared_db = _parked_run(tmp_path, registry, "alpha")
    m3_run, m3_db = _parked_run(tmp_path, registry, "beta")
    for repo, run_id, pool in (("alpha", shared_run, "claude-shared"), ("beta", m3_run, "claude-m3")):
        job_id = store.add_job(repo=str(tmp_path / repo), kind="build", scope="epic-3", pool=pool)
        store.claim_job(job_id, claimed_by="w", lease_seconds=60, now=NOW)
        store.attach_run(job_id, run_id)
    _pool_pause(store, "claude-shared")
    _pool_pause(store, "claude-m3")

    result = clear_rate_limit(store, registry, pool="claude-shared", now=NOW)

    assert [r.run_id for r in result.runs] == [shared_run]
    assert Ledger(shared_db).run_row(shared_run)["status"] == "IN_PROGRESS"
    assert Ledger(m3_db).run_row(m3_run)["status"] == "RATE_LIMITED"
    assert [p.pool for p in store.dispatch_pauses()] == ["claude-m3"]


def test_unpause_without_a_pool_clears_every_window(tmp_path, monkeypatch) -> None:
    store, registry = _env(tmp_path, monkeypatch)
    store.init()
    _pool_pause(store, "claude-shared")
    _pool_pause(store, "codex-shared")

    result = clear_rate_limit(store, registry, now=NOW)

    assert {p.pool for p in result.pauses} == {"claude-shared", "codex-shared"}
    assert store.dispatch_pauses() == []
    assert len(store.pause_clears()) == 2


def test_unpausing_a_pool_that_is_not_paused_is_nothing_to_clear(tmp_path, monkeypatch) -> None:
    store, registry = _env(tmp_path, monkeypatch)
    store.init()
    _pool_pause(store, "codex-shared")

    result = runner.invoke(app, ["queue", "unpause", "--pool", "claude-shared"])

    assert "nothing to clear" in result.output
    assert [p.pool for p in store.dispatch_pauses()] == ["codex-shared"]


def test_unpause_by_pool_works_against_a_fleet_queue(tmp_path, monkeypatch) -> None:
    """From the XPS the pause lives behind the queue service, not a local file."""
    from sdlc.queue import QueueBackend

    class FleetQueue:
        """Just enough of the HTTP client: no audit route, like the real one."""

        def __init__(self, inner: QueueStore) -> None:
            self.inner = inner

        def __getattr__(self, name):
            if name == "record_pause_clear":
                raise AttributeError(name)
            return getattr(self.inner, name)

    store, registry = _env(tmp_path, monkeypatch)
    store.init()
    _pool_pause(store, "claude-shared")
    _pool_pause(store, "codex-shared")
    fleet: QueueBackend = FleetQueue(store)  # type: ignore[assignment]

    result = clear_rate_limit(fleet, registry, pool="claude-shared", now=NOW)

    assert [p.pool for p in result.pauses] == ["claude-shared"]
    assert [p.pool for p in store.dispatch_pauses()] == ["codex-shared"]
    assert store.pause_clears() == []  # the audit table is the service's, not ours
