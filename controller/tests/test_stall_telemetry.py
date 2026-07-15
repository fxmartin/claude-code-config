# ABOUTME: Tests for rate-limit stall telemetry (Story 27.3-004).
# ABOUTME: Stall seconds land in their own ledger table, distinct from stage durations.

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdlc.build import (
    RATE_LIMIT_POLL_S,
    BuildOptions,
    Ledger,
    _rate_limit_wait,
    _run_story,
    apply_rate_limit_park,
    run_build,
    status_snapshot,
)
from sdlc.cli import app
from sdlc.dispatch import RateLimitError
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


# ---------------------------------------------------------------------------
# Coverage gate (27.3-004): residual edges of the wait/park path stall_log
# threads through — long-wait countdown, reset resolution, notify degradation,
# and the no-context / window-reopen branches around the stall recording.
# ---------------------------------------------------------------------------


def test_rate_limit_wait_logs_countdown_across_poll_chunks(tmp_path: Path) -> None:
    # A wait longer than one poll chunk emits the periodic countdown note and
    # still records the *total* waited seconds as a single stall row.
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    wait_s = RATE_LIMIT_POLL_S + 60  # two chunks → one mid-wait countdown note
    waited = _rate_limit_wait(
        ledger, run_id, RateLimitSignal(source="429"), wait_s,
        sleep_fn=_Sleeps(), story_id="s1-001", stage="build",
    )
    assert waited == wait_s
    assert _stall_rows(db, run_id) == [
        {"story_id": "s1-001", "stage": "build", "source": "429", "waited_s": wait_s},
    ]
    with ledger._connect_ro() as conn:  # noqa: SLF001 — test inspects the event log
        msgs = [
            r["message"] for r in conn.execute(
                "SELECT message FROM events WHERE run_id = ?", (run_id,)
            ).fetchall()
        ]
    assert any("until the window reopens" in m for m in msgs)


def test_park_resolves_reset_from_retry_after(tmp_path: Path) -> None:
    # A beyond-cap throttle carrying only a relative Retry-After (no reset
    # epoch) parks with reset_at = now + retry_after so resume honours it.
    db = tmp_path / "ledger.db"
    dispatcher = RateLimitingDispatcher(
        trip_on=("build", "s1-001"),
        signal=RateLimitSignal(source="retry-after", retry_after_s=50_000),
        times=99,
    )
    result = run_build(
        _opts(rate_limit_max_wait_s=300), queue=_sample_queue(), ledger=Ledger(db),
        dispatcher=dispatcher, preflight=lambda: True,
        sleep_fn=_Sleeps(), clock=lambda: 0.0,
    )
    assert result.rate_limited is True
    assert Ledger(db).run_config(result.run_id)["rate_limit_reset_at"] == 50_000.0


def test_park_notify_failure_never_fails_the_park(tmp_path: Path, monkeypatch) -> None:
    # The lifecycle notification is best-effort: a raising notifier must not
    # break the park — the run still ends RATE_LIMITED with its reset returned.
    def boom(*_a, **_k):
        raise RuntimeError("notifier down")

    monkeypatch.setattr("sdlc.build.notify", boom)
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    reset_at = apply_rate_limit_park(
        ledger, run_id, RateLimitSignal(source="usage-limit", reset_at=999.0),
        now=0.0, waited_s=60, window_s=18000,
    )
    assert reset_at == 999.0
    assert ledger.run_row(run_id)["status"] == "RATE_LIMITED"


def test_rate_limit_without_context_propagates(tmp_path: Path) -> None:
    # _run_story called without a rate-limit context (rl_ctx=None, the direct /
    # legacy path) must re-raise the throttle instead of absorbing it — only the
    # rate-limit-aware runner owns the wait/park (and stall_log) machinery.
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    story = _sample_queue()[0]
    ledger.story_upsert(
        run_id, story.id, "99", story.title, "P1", 2, "py", "", None, "PENDING"
    )
    dispatcher = RateLimitingDispatcher(
        trip_on=("build", story.id),
        signal=RateLimitSignal(source="429", retry_after_s=60),
    )
    with pytest.raises(RateLimitError):
        _run_story(story, _opts(), ledger, run_id, dispatcher, tmp_path / "logs")
    assert _stall_rows(db, run_id) == []  # nothing waited, so nothing recorded


def test_reactive_429_with_window_budget_reopens_window(tmp_path: Path) -> None:
    # A within-cap 429 while a window budget is configured reopens the window
    # after the wait (re-seeding its baseline at the post-wait accrual) and the
    # run completes with the stall attributed to the throttled story/stage.
    db = tmp_path / "ledger.db"
    sleeps = _Sleeps()
    dispatcher = RateLimitingDispatcher(
        trip_on=("build", "s1-001"),
        signal=RateLimitSignal(source="429", retry_after_s=60),
    )
    result = run_build(
        _opts(
            window_budget=100 * _SAMPLE_STAGE_TOKENS, window_s=100,
            rate_limit_max_wait_s=18000,
        ),
        queue=_sample_queue(), ledger=Ledger(db), dispatcher=dispatcher,
        preflight=lambda: True, sleep_fn=sleeps, clock=lambda: 0.0,
    )
    assert result.rate_limited is False
    assert result.completed == 3
    assert sum(sleeps.calls) == 60  # the ample budget itself never gated
    assert _stall_rows(db, result.run_id) == [
        {"story_id": "s1-001", "stage": "build", "source": "429", "waited_s": 60},
    ]
