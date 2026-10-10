# ABOUTME: Tests for Story 35.4-005 — preflight is a visible run phase, not a gate before the run.
# ABOUTME: The run row exists before preflight; a red gate stamps it FAILED; fleet/dashboard/status show it.

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest
from test_build import FakeDispatcher, _sample_queue
from test_fix_issue import (
    BatchProbeDispatcher,
    FakeBatchGh,
    FakeGh,
    RecordingDispatcher,
    _batch_issue,
    _issue_json,
)

import sdlc.build as build_mod
import sdlc.queue_client as qc
from sdlc.build import (
    BuildOptions,
    Ledger,
    default_preflight,
    run_build,
    status_snapshot,
)
from sdlc.dashboard import _PAGE, _registry_runs_view
from sdlc.fix_issue import (
    FixBatchOptions,
    FixOptions,
    resume_fix,
    run_fix,
    run_fix_batch,
)
from sdlc.queue import QueueStore
from sdlc.registry import Registry, RunRecord, live_record
from sdlc.resume import has_resumable_work, run_resume
from sdlc.scheduler import ledger_preflight_failure, ledger_run_terminal
from sdlc.status import format_preflight

RED = "PRE_FLIGHT_RED: 'uv run pytest' exited 1 — the suite is failing. Fix it, or bypass with --skip-preflight."
TIMEOUT = "PRE_FLIGHT_TIMEOUT: 'uv run pytest' exceeded 5s — aborting. Raise --preflight-timeout=N or bypass with --skip-preflight."


def _runs(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM runs").fetchall()
    finally:
        conn.close()


def _events(db: Path, source: str) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT level, message FROM events WHERE source = ? ORDER BY id", (source,)
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def fleet_pushes(monkeypatch):
    """A fleet is configured; every record pushed to it lands in the returned list."""
    pushed: list[RunRecord] = []
    monkeypatch.setattr(qc, "resolve_queue_url", lambda: "http://fleet.test")
    monkeypatch.setattr(qc, "push_fleet_run", lambda record: pushed.append(record))
    return pushed


@pytest.fixture
def notices(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(build_mod, "notify", lambda event, **fields: sent.append((event, fields)))
    return sent


# ---------------------------------------------------------------------------
# run_build: the run exists before preflight starts
# ---------------------------------------------------------------------------


def test_build_run_row_exists_before_preflight_and_reads_preflight(tmp_path, fleet_pushes) -> None:
    db = tmp_path / "ledger.db"
    registry = Registry(tmp_path / "registry.json")
    seen: dict = {}

    def preflight() -> bool:
        runs = _runs(db)
        seen["runs"] = [(r["status"]) for r in runs]
        snap = status_snapshot(Ledger(db))
        seen["phase"] = snap["run"]["phase"]
        seen["preflight"] = snap["run"]["preflight"]
        seen["total"] = snap["counts"]["total"]
        seen["local"] = [(r.run_id, r.phase) for r in registry.records()]
        seen["fleet"] = [(r.run_id, r.status, r.phase) for r in fleet_pushes]
        return True

    run_build(
        BuildOptions(scope="epic-99", sequential=True),
        queue=_sample_queue(), ledger=Ledger(db), dispatcher=FakeDispatcher(),
        preflight=preflight, registry=registry,
    )

    assert seen["runs"] == ["IN_PROGRESS"]
    assert seen["phase"] == "preflight"
    assert seen["preflight"]["state"] == "running"
    assert seen["total"] == 3  # the run is fully bootstrapped, not a hollow row
    run_id = _runs(db)[0]["id"]
    assert seen["local"] == [(run_id, "preflight")]
    # The fleet heard of the run — in preflight — before the gate finished.
    assert (run_id, "IN_PROGRESS", "preflight") in seen["fleet"]


def test_build_preflight_pass_changes_nothing_about_the_run(tmp_path, fleet_pushes) -> None:
    db = tmp_path / "ledger.db"
    registry = Registry(tmp_path / "registry.json")
    result = run_build(
        BuildOptions(scope="epic-99", sequential=True),
        queue=_sample_queue(), ledger=Ledger(db), dispatcher=FakeDispatcher(),
        preflight=lambda: True, registry=registry,
    )
    assert result.completed == 3
    runs = _runs(db)
    assert len(runs) == 1  # one run, one id, from preflight through close-out
    assert result.run_id == runs[0]["id"]
    assert [m for _lvl, m in _events(db, "preflight")][-1] == "passed"
    snap = status_snapshot(Ledger(db))
    assert snap["run"]["preflight"]["state"] == "passed"
    assert snap["run"]["phase"] is None  # finished
    assert snap["counts"]["done"] == 3
    assert all(r.run_id == runs[0]["id"] for r in registry.records())
    # The fleet's last word is the finished run, in no phase.
    assert fleet_pushes[-1].status == "DONE" and fleet_pushes[-1].phase is None


def test_build_preflight_red_stamps_run_failed_with_reason(tmp_path, fleet_pushes, notices) -> None:
    db = tmp_path / "ledger.db"
    registry = Registry(tmp_path / "registry.json")
    dispatcher = FakeDispatcher()
    result = run_build(
        BuildOptions(scope="epic-99", sequential=True),
        queue=_sample_queue(), ledger=Ledger(db), dispatcher=dispatcher,
        preflight=lambda: False, registry=registry,
    )
    assert result.preflight_failed is True
    assert dispatcher.calls == []  # still nothing dispatched
    [run] = _runs(db)
    assert run["status"] == "FAILED" and run["finished_at"]
    assert result.run_id == run["id"]
    [started, failed] = _events(db, "preflight")
    assert started[1].startswith("started: ")
    assert failed[0] == "error" and failed[1].startswith("PRE_FLIGHT_RED")
    # Local registry and fleet both finished FAILED.
    [rec] = registry.records()
    assert rec.status == "FAILED" and rec.finished_at and rec.phase is None
    assert (fleet_pushes[-1].status, fleet_pushes[-1].finished_at is not None) == ("FAILED", True)
    # Telegram carries the reason.
    [(event, fields)] = [n for n in notices if n[0] == "run_finished"]
    assert fields["terminal"] == "FAILED" and fields["reason"].startswith("PRE_FLIGHT_RED")
    # `sdlc status` shows it.
    snap = status_snapshot(Ledger(db))
    assert snap["run"]["status"] == "FAILED"
    assert snap["run"]["preflight"]["state"] == "failed"
    assert format_preflight(snap["run"]["preflight"]).startswith("preflight: failed — PRE_FLIGHT_RED")


@pytest.mark.parametrize("reason", [RED, TIMEOUT])
def test_build_default_preflight_reason_reaches_the_ledger(tmp_path, monkeypatch, reason) -> None:
    """The real gate's PRE_FLIGHT_RED / PRE_FLIGHT_TIMEOUT line is the run's error event."""
    db = tmp_path / "ledger.db"

    def fake_default(timeout=1800, on_failure=None, **_kw) -> bool:
        on_failure(reason)
        return False

    monkeypatch.setattr(build_mod, "default_preflight", fake_default)
    result = run_build(
        BuildOptions(scope="epic-99", sequential=True),
        queue=_sample_queue(), ledger=Ledger(db), dispatcher=FakeDispatcher(),
    )
    assert result.preflight_failed is True
    assert _events(db, "preflight")[-1] == ("error", reason)
    assert ledger_preflight_failure(str(db), result.run_id) == reason


# The suite itself is faked at the `subprocess.run` seam: a CI job image only
# guarantees `sh` (no `make`), and a real timed-out `make` would orphan its child.


def test_default_preflight_reports_the_reason_line(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "Makefile").write_text("test:\n\t@exit 1\n")

    class _Red:
        returncode = 1

    monkeypatch.setattr(build_mod.subprocess, "run", lambda *a, **k: _Red())
    lines: list[str] = []
    assert default_preflight(root=tmp_path, timeout=30, on_failure=lines.append) is False
    assert lines == [
        "PRE_FLIGHT_RED: 'make test' exited 1 — the suite is failing. "
        "Fix it, or bypass with --skip-preflight."
    ]
    assert lines[0] in capsys.readouterr().err  # still printed, as before


def test_default_preflight_timeout_reports_the_reason_line(tmp_path, monkeypatch) -> None:
    (tmp_path / "Makefile").write_text("test:\n\t@sleep 30\n")

    def _hang(cmd, **kwargs):
        raise build_mod.subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(build_mod.subprocess, "run", _hang)
    lines: list[str] = []
    assert default_preflight(root=tmp_path, timeout=1, on_failure=lines.append) is False
    assert len(lines) == 1 and lines[0].startswith("PRE_FLIGHT_TIMEOUT: 'make test' exceeded 1s")


def _make_is_missing(monkeypatch) -> None:
    """``make`` is not on this host's PATH; every other command runs for real."""
    real_run = build_mod.subprocess.run

    def _run(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and list(cmd[:1]) == ["make"]:
            raise FileNotFoundError(2, "No such file or directory", "make")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(build_mod.subprocess, "run", _run)


def test_default_preflight_reports_a_command_that_cannot_start(tmp_path, monkeypatch, capsys) -> None:
    # A worker without the repo's test tool: the gate must fail like a red suite,
    # not raise out of the run it now sits inside.
    (tmp_path / "Makefile").write_text("test:\n\t@exit 0\n")
    _make_is_missing(monkeypatch)
    lines: list[str] = []
    assert default_preflight(root=tmp_path, timeout=30, on_failure=lines.append) is False
    assert len(lines) == 1
    assert lines[0].startswith("PRE_FLIGHT_RED: 'make test' could not start")
    assert "No such file or directory" in lines[0]
    assert lines[0] in capsys.readouterr().err


def test_build_whose_preflight_command_cannot_start_fails_the_run(tmp_path, monkeypatch, fleet_pushes) -> None:
    # Before the fix the FileNotFoundError escaped `run_build`: the run sat
    # IN_PROGRESS in `preflight` for good and a fleet job parked as "resumable".
    (tmp_path / "Makefile").write_text("test:\n\t@exit 0\n")
    monkeypatch.chdir(tmp_path)
    _make_is_missing(monkeypatch)
    db = tmp_path / "ledger.db"
    registry = Registry(tmp_path / "registry.json")
    result = run_build(
        BuildOptions(scope="epic-99", sequential=True),
        queue=_sample_queue(), ledger=Ledger(db), dispatcher=FakeDispatcher(),
        registry=registry,
    )
    assert result.preflight_failed is True
    [run] = _runs(db)
    assert run["status"] == "FAILED" and Ledger(db).run_phase(run["id"]) is None
    level, reason = _events(db, "preflight")[-1]
    assert level == "error" and reason.startswith("PRE_FLIGHT_RED: 'make test' could not start")
    [rec] = registry.records()
    assert rec.status == "FAILED" and rec.finished_at
    assert fleet_pushes[-1].status == "FAILED"
    assert ledger_preflight_failure(str(db), run["id"]) == reason


def test_build_model_probe_runs_inside_the_preflight_phase(tmp_path, monkeypatch) -> None:
    # The live tier-model probe is part of preflight (`--skip-preflight` skips
    # it), so the run must already read `preflight` while it spends its seconds.
    db = tmp_path / "ledger.db"
    registry = Registry(tmp_path / "registry.json")
    seen: list[tuple] = []
    monkeypatch.setattr(
        build_mod, "_probe_tier_models",
        lambda ledger, run_id, opts: seen.append(
            (ledger.run_phase(run_id), [r.phase for r in registry.records()])
        ),
    )
    run_build(
        BuildOptions(scope="epic-99", sequential=True),
        queue=_sample_queue(), ledger=Ledger(db), dispatcher=FakeDispatcher(),
        preflight=lambda: True, registry=registry,
    )
    assert seen == [("preflight", ["preflight"])]
    # Opening the phase early does not open it twice: one gate, one start.
    assert [m for _, m in _events(db, "preflight")] == ["started: preflight", "passed"]


def test_skip_preflight_records_no_phase(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    run_build(
        BuildOptions(scope="epic-99", sequential=True, skip_preflight=True),
        queue=_sample_queue(), ledger=Ledger(db), dispatcher=FakeDispatcher(),
    )
    assert _events(db, "preflight") == []
    assert status_snapshot(Ledger(db))["run"]["preflight"] is None


def test_skip_preflight_never_detects_the_preflight_command(tmp_path, monkeypatch) -> None:
    # `--skip-preflight` is the bypass: it must not read the repo's Makefile /
    # pyproject / uv.lock for a gate command it will never run.
    def _must_not_detect(root=None) -> str:
        raise AssertionError("--skip-preflight detected the preflight command")

    monkeypatch.setattr(build_mod, "preflight_command_text", _must_not_detect)
    result = run_build(
        BuildOptions(scope="epic-99", sequential=True, skip_preflight=True),
        queue=_sample_queue(), ledger=Ledger(tmp_path / "ledger.db"), dispatcher=FakeDispatcher(),
    )
    assert result.completed == 3


# ---------------------------------------------------------------------------
# Ledger: the derived phase
# ---------------------------------------------------------------------------


def test_run_phase_walks_preflight_stories_closing_and_ends(tmp_path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    run_id = ledger.run_create("epic-1", "serial")
    ledger.story_upsert(run_id, "s-1", "1", "t", "P1", 1, "py", "", None, "TODO")
    assert ledger.run_phase(run_id) == "stories"
    ledger.event_log(run_id, "", "info", "preflight", "started: make test")
    assert ledger.run_phase(run_id) == "preflight"
    ledger.event_log(run_id, "", "info", "preflight", "passed")
    assert ledger.run_phase(run_id) == "stories"
    ledger.set_story_status(run_id, "s-1", "DONE")
    assert ledger.run_phase(run_id) == "closing"
    ledger.run_update_status(run_id, "DONE")
    assert ledger.run_phase(run_id) is None
    assert ledger.run_phase("no-such-run") is None


def test_run_phase_is_none_for_a_run_that_is_not_in_progress(tmp_path) -> None:
    # A parked run (RATE_LIMITED is resumable, so not terminal) is in no phase:
    # its settled stories must not read as `closing`, nor its TODO ones as `stories`.
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    run_id = ledger.run_create("epic-1", "serial")
    ledger.story_upsert(run_id, "s-1", "1", "t", "P1", 1, "py", "", None, "RATE_LIMITED")
    ledger.run_update_status(run_id, "RATE_LIMITED")
    assert ledger.run_phase(run_id) is None
    assert status_snapshot(ledger, run_id)["run"]["phase"] is None
    ledger.set_story_status(run_id, "s-1", "TODO")
    assert ledger.run_phase(run_id) is None
    assert status_snapshot(ledger, run_id)["run"]["phase"] is None


def test_preflight_state_reports_command_and_latest_attempt(tmp_path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    run_id = ledger.run_create("epic-1", "serial")
    assert ledger.preflight_state(run_id) is None
    ledger.event_log(run_id, "", "info", "preflight", "started: make test")
    state = ledger.preflight_state(run_id)
    assert state["state"] == "running" and state["command"] == "make test"
    assert state["finished_at"] is None and state["duration_seconds"] is not None
    ledger.event_log(run_id, "", "error", "preflight", RED)
    assert ledger.preflight_state(run_id)["state"] == "failed"
    assert ledger.preflight_state(run_id)["reason"] == RED
    # A resume's re-run is the latest attempt.
    ledger.event_log(run_id, "", "info", "preflight", "started: make test")
    assert ledger.preflight_state(run_id)["state"] == "running"


def test_list_runs_derives_phase_from_the_rows_it_already_read(tmp_path, monkeypatch) -> None:
    # `list_runs` already holds each run's status and story counts: a live run's
    # phase needs only its preflight events on top, not a `run_phase` re-read
    # (run row + events + stories) per run on a list polled every tick.
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    gate = ledger.run_create("epic-1", "serial")
    ledger.story_upsert(gate, "s-1", "1", "t", "P1", 1, "py", "", None, "TODO")
    ledger.event_log(gate, "", "info", "preflight", "started: make test")
    working = ledger.run_create("epic-2", "serial")
    ledger.story_upsert(working, "s-2", "1", "t", "P1", 1, "py", "", None, "IN_PROGRESS")
    closing = ledger.run_create("epic-3", "serial")
    ledger.story_upsert(closing, "s-3", "1", "t", "P1", 1, "py", "", None, "DONE")
    finished = ledger.run_create("epic-4", "serial")
    ledger.run_update_status(finished, "DONE")
    looked_up: list[str] = []
    real_preflight_state = Ledger.preflight_state

    def _counting(self, run_id, **kwargs):
        looked_up.append(run_id)
        return real_preflight_state(self, run_id, **kwargs)

    def _no_reread(self, run_id):
        raise AssertionError("list_runs re-read a run it already holds")

    monkeypatch.setattr(Ledger, "preflight_state", _counting)
    for name in ("run_phase", "run_row", "story_rows"):
        monkeypatch.setattr(Ledger, name, _no_reread)

    phases = {r["id"]: r["phase"] for r in ledger.list_runs()}

    assert phases == {gate: "preflight", working: "stories", closing: "closing", finished: None}
    assert sorted(looked_up) == sorted([gate, working, closing])  # a finished run pays nothing


def test_format_preflight_matches_the_header_contract() -> None:
    assert format_preflight(None) is None
    assert format_preflight({"state": "running", "command": "make test", "duration_seconds": 42}) == (
        "preflight: running (make test, 42s)"
    )
    assert format_preflight({"state": "passed", "duration_seconds": 7}) == "preflight: passed (7s)"
    assert format_preflight({"state": "failed", "reason": RED}) == f"preflight: failed — {RED}"
    assert format_preflight(None, phase="preflight") == "preflight: running"


# ---------------------------------------------------------------------------
# Registry + fleet: `phase` travels with the record
# ---------------------------------------------------------------------------


def _record(**overrides) -> RunRecord:
    fields = dict(
        run_id="run-1", repo="/r", db="/r/.sdlc-state.db", scope="epic-1", pid=1,
        status="IN_PROGRESS", started_at="2026-10-02T10:00:00+00:00", worker="xps",
    )
    fields.update(overrides)
    return RunRecord(**fields)


def test_registry_set_phase_and_finish_clears_it(tmp_path) -> None:
    registry = Registry(tmp_path / "registry.json")
    registry.register(_record())
    registry.set_phase("run-1", "preflight")
    assert registry.records()[0].phase == "preflight"
    registry.set_phase("unknown", "stories")  # a no-op, never an error
    registry.mark_finished("run-1", "FAILED")
    assert registry.records()[0].phase is None


def test_record_without_phase_still_parses() -> None:
    legacy = {
        "run_id": "r", "repo": "/r", "db": "/d", "scope": "s", "pid": 1,
        "status": "IN_PROGRESS", "started_at": "2026-10-02T10:00:00+00:00",
    }
    assert RunRecord.from_dict(legacy).phase is None


def test_live_record_reads_phase_from_the_ledger(tmp_path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    run_id = ledger.run_create("epic-1", "serial")
    ledger.event_log(run_id, "", "info", "preflight", "started: make test")
    record = _record(run_id=run_id, db=str(ledger.db_path))
    assert live_record(record).phase == "preflight"
    ledger.event_log(run_id, "", "info", "preflight", "passed")
    assert live_record(record).phase == "stories"
    # A finished record is in no phase, whatever the ledger says.
    assert live_record(_record(run_id=run_id, db=str(ledger.db_path), finished_at="2026-10-02T11:00:00+00:00")).phase is None


def test_live_record_reads_counts_and_phase_in_one_ledger_pass(tmp_path, monkeypatch) -> None:
    # Every worker heartbeat calls this: the phase rides on the `list_runs` row
    # the counts already come from, not on a second `run_phase` read.
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    run_id = ledger.run_create("epic-1", "serial")
    ledger.event_log(run_id, "", "info", "preflight", "started: make test")

    def _second_pass(self, rid):
        raise AssertionError("live_record re-read the ledger for the phase")

    monkeypatch.setattr(Ledger, "run_phase", _second_pass)
    assert live_record(_record(run_id=run_id, db=str(ledger.db_path))).phase == "preflight"


def test_fleet_store_keeps_phase_per_push(tmp_path) -> None:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    store.put_fleet_run(_record(phase="preflight"))
    assert store.list_fleet_runs()[0]["phase"] == "preflight"
    store.put_fleet_run(_record(phase="stories"))  # the next heartbeat
    assert store.list_fleet_runs()[0]["phase"] == "stories"


def test_fleet_store_upgrades_a_table_written_before_phase(tmp_path) -> None:
    db = tmp_path / "queue.db"
    store = QueueStore(db)
    store.init()
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE fleet_runs DROP COLUMN phase")
    conn.execute("DELETE FROM _migrations WHERE name = 'fleet_run_phase'")
    conn.commit()
    conn.close()
    QueueStore(db).init()
    QueueStore(db).put_fleet_run(_record(phase="preflight"))
    assert QueueStore(db).list_fleet_runs()[0]["phase"] == "preflight"


def test_fleet_service_accepts_and_serves_phase(tmp_path) -> None:
    from sdlc.queue_server import AccessPolicy, make_server

    store = QueueStore(tmp_path / "service.db")
    store.init()
    server = make_server(store, AccessPolicy(token="t", networks=("127.0.0.0/8",)), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        client = qc.QueueClient(f"http://127.0.0.1:{server.server_address[1]}", token="t")
        client.put_fleet_run(_record(phase="preflight"))
        assert [r["phase"] for r in client.list_fleet_runs()] == ["preflight"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


# ---------------------------------------------------------------------------
# Dashboard + status surfaces
# ---------------------------------------------------------------------------


def test_api_runs_carries_phase_for_local_and_remote_runs(tmp_path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    run_id = ledger.run_create("epic-1", "serial")
    ledger.event_log(run_id, "", "info", "preflight", "started: make test")
    registry = Registry(tmp_path / "registry.json")
    registry.register(_record(run_id=run_id, db=str(ledger.db_path), pid=__import__("os").getpid(), worker=None))
    remote = {**_record(run_id="remote-1", phase="preflight").to_dict(), "worker_online": True}

    rows = {r["id"]: r for r in _registry_runs_view(registry, None, [remote])}

    assert rows[run_id]["phase"] == "preflight"
    assert rows["remote-1"]["phase"] == "preflight" and rows["remote-1"]["worker"] == "xps"


def test_api_runs_takes_phase_from_the_memoized_list_runs_read(tmp_path, monkeypatch) -> None:
    # `list_runs` already derives a live run's phase; the view must not pay a
    # second ledger pass (three connections) per run on a list polled every tick.
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    run_ids = [ledger.run_create("epic-1", "serial") for _ in range(2)]
    ledger.event_log(run_ids[0], "", "info", "preflight", "started: make test")
    registry = Registry(tmp_path / "registry.json")
    for run_id in run_ids:
        registry.register(_record(run_id=run_id, db=str(ledger.db_path), pid=__import__("os").getpid(), worker=None))
    reads: list[str] = []
    real_list_runs = Ledger.list_runs

    def _list_runs(self, *args, **kwargs):
        reads.append(str(self.db_path))
        rows = real_list_runs(self, *args, **kwargs)
        monkeypatch.setattr(Ledger, "run_phase", _no_second_pass)
        return rows

    def _no_second_pass(self, run_id):
        raise AssertionError("phase re-derived outside the memoized list_runs read")

    monkeypatch.setattr(Ledger, "list_runs", _list_runs)
    rows = {r["id"]: r for r in _registry_runs_view(registry, None, [])}
    assert rows[run_ids[0]]["phase"] == "preflight"
    assert rows[run_ids[1]]["phase"] == "stories"
    assert reads == [str(ledger.db_path)]  # one read for both runs of one ledger


def test_remote_run_finished_has_no_phase(tmp_path) -> None:
    done = {
        **_record(run_id="remote-2", status="FAILED", finished_at="2026-10-02T11:00:00+00:00", phase="preflight").to_dict(),
        "worker_online": True,
    }
    rows = _registry_runs_view(Registry(tmp_path / "r.json"), None, [done])
    assert rows[0]["phase"] is None


def test_dashboard_header_states_preflight_running_command_and_elapsed() -> None:
    page = _PAGE
    assert "preflight: running (" in page
    assert "preflight-elapsed" in page  # the live-ticking elapsed
    assert "preflight: passed (" in page
    assert "preflight: failed" in page
    # ...and the sidebar names a run's phase beside its worker.
    assert "r.phase" in page


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the page's script")
def test_dashboard_preflight_line_renders_the_header_contract() -> None:
    # The header strings themselves, from the page's own `preflightLine` under
    # node: the contract, a remote run's bare phase, and a hostile command inert.
    from test_remote_run_detail import _js_function

    script = "\n".join(_js_function(_PAGE, f) for f in ("esc", "humanDuration", "preflightLine"))
    script += """
    const cfg = {preflight: "pending"};
    console.log(JSON.stringify([
      preflightLine({preflight: {state: "running", command: "make test", duration_seconds: 125}}, cfg),
      preflightLine({preflight: {state: "passed", duration_seconds: 42}}, cfg),
      preflightLine({preflight: {state: "failed", reason: "PRE_FLIGHT_RED: 'make test' exited 1"}}, cfg),
      preflightLine({phase: "preflight"}, {}),
      preflightLine({preflight: {state: "running", command: "<img src=x>", duration_seconds: 5}}, cfg),
      preflightLine({}, {preflight: "skipped"}),
      preflightLine({}, {}),
    ]));
    """
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60, check=True
    )
    assert json.loads(out.stdout) == [
        "preflight: running (make test, <span id='preflight-elapsed'>2m 05s</span>)",
        "preflight: passed (42s)",
        "preflight: failed &mdash; PRE_FLIGHT_RED: &#39;make test&#39; exited 1",
        "preflight: running",
        "preflight: running (&lt;img src=x&gt;, <span id='preflight-elapsed'>5s</span>)",
        "preflight: skipped",
        "",
    ]


def test_status_snapshot_exposes_phase_and_preflight_for_the_header(tmp_path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    run_id = ledger.run_create("epic-1", "serial")
    ledger.event_log(run_id, "", "info", "preflight", "started: make test")
    snap = status_snapshot(ledger, run_id)
    assert snap["run"]["phase"] == "preflight"
    assert snap["run"]["preflight"]["command"] == "make test"
    assert isinstance(snap["run"]["preflight"]["duration_seconds"], int)


def test_status_snapshot_reads_the_preflight_events_once(tmp_path, monkeypatch) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    run_id = ledger.run_create("epic-1", "serial")
    ledger.event_log(run_id, "", "info", "preflight", "started: make test")
    calls: list[str] = []
    real = Ledger.preflight_state

    def _counting(self, rid, **kwargs):
        calls.append(rid)
        return real(self, rid, **kwargs)

    monkeypatch.setattr(Ledger, "preflight_state", _counting)
    snap = status_snapshot(ledger, run_id)
    assert snap["run"]["phase"] == "preflight" and snap["run"]["preflight"]["state"] == "running"
    assert calls == [run_id]


# ---------------------------------------------------------------------------
# sdlc fix / sdlc fix all
# ---------------------------------------------------------------------------


def test_fix_run_row_exists_before_preflight(tmp_path, fleet_pushes) -> None:
    db = tmp_path / ".sdlc-state.db"
    registry = Registry(tmp_path / "registry.json")
    seen: dict = {}

    def preflight() -> bool:
        snap = status_snapshot(Ledger(db))
        seen["phase"] = snap["run"]["phase"]
        seen["status"] = snap["run"]["status"]
        seen["fleet"] = [r.phase for r in fleet_pushes]
        return True

    result = run_fix(
        FixOptions(issue=1), ledger=Ledger(db), dispatcher=RecordingDispatcher(),
        preflight=preflight, runner=FakeGh(_issue_json()), root=tmp_path, registry=registry,
    )
    assert result.status == "DONE"
    assert (seen["phase"], seen["status"]) == ("preflight", "IN_PROGRESS")
    assert "preflight" in seen["fleet"]  # the fleet heard it before the gate finished


def test_fix_preflight_failure_leaves_a_failed_run_with_the_reason(tmp_path, fleet_pushes, notices) -> None:
    db = tmp_path / ".sdlc-state.db"
    registry = Registry(tmp_path / "registry.json")
    dispatch = RecordingDispatcher()
    result = run_fix(
        FixOptions(issue=1), ledger=Ledger(db), dispatcher=dispatch,
        preflight=lambda: False, runner=FakeGh(_issue_json()), root=tmp_path, registry=registry,
    )
    assert result.preflight_failed is True and result.status == "FAILED"
    assert dispatch.calls == []
    [run] = _runs(db)
    assert result.run_id == run["id"] and run["status"] == "FAILED"
    assert _events(db, "preflight")[-1][1].startswith("PRE_FLIGHT_RED")
    assert registry.records()[0].status == "FAILED"
    assert fleet_pushes[-1].status == "FAILED"
    assert any(e == "run_finished" and f["terminal"] == "FAILED" and "PRE_FLIGHT_RED" in f["reason"] for e, f in notices)


def test_batch_run_row_exists_before_preflight_and_failure_stamps_it(tmp_path, fleet_pushes) -> None:
    db = tmp_path / ".sdlc-state.db"
    registry = Registry(tmp_path / "registry.json")
    seen: dict = {}

    def preflight() -> bool:
        snap = status_snapshot(Ledger(db))
        seen["phase"] = snap["run"]["phase"]
        seen["total"] = snap["counts"]["total"]
        return False

    dispatch = BatchProbeDispatcher()
    result = run_fix_batch(
        FixBatchOptions(target="all"), ledger=Ledger(db), dispatcher=dispatch,
        preflight=preflight, runner=FakeBatchGh([_batch_issue(1), _batch_issue(2)]),
        root=tmp_path, registry=registry,
    )
    assert seen == {"phase": "preflight", "total": 2}
    assert result.preflight_failed is True and result.status == "FAILED"
    assert dispatch.calls == []
    [run] = _runs(db)
    assert run["status"] == "FAILED" and result.run_id == run["id"]
    assert registry.records()[0].status == "FAILED" and fleet_pushes[-1].status == "FAILED"
    assert _events(db, "preflight")[-1][0] == "error"


# ---------------------------------------------------------------------------
# resume re-runs an interrupted preflight
# ---------------------------------------------------------------------------


def _interrupted_build_in_preflight(tmp_path: Path) -> tuple[Ledger, str]:
    db = tmp_path / "ledger.db"

    def killed() -> bool:
        raise KeyboardInterrupt  # the process died mid-suite: no outcome event

    with pytest.raises(KeyboardInterrupt):
        run_build(
            BuildOptions(scope="epic-99", sequential=True),
            queue=_sample_queue(), ledger=Ledger(db), dispatcher=FakeDispatcher(),
            preflight=killed,
        )
    ledger = Ledger(db)
    [run] = _runs(db)
    assert run["status"] == "IN_PROGRESS"
    assert ledger.preflight_state(run["id"])["state"] == "running"
    return ledger, run["id"]


def test_resume_of_a_run_interrupted_in_preflight_reruns_preflight(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sdlc.resume.discover_queue", lambda scope, root: _sample_queue())
    ledger, run_id = _interrupted_build_in_preflight(tmp_path)
    calls = {"n": 0}

    def preflight() -> bool:
        calls["n"] += 1
        return True

    result = run_resume(
        "epic-99", ledger=ledger, dispatcher=FakeDispatcher(), run_id=run_id, preflight=preflight,
    )
    assert calls["n"] == 1  # re-run, not skipped
    assert result.preflight_failed is False and result.completed == 3
    assert ledger.preflight_state(run_id)["state"] == "passed"
    assert len(_runs(ledger.db_path)) == 1  # same run


def test_resume_with_a_red_preflight_stamps_the_run_failed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sdlc.resume.discover_queue", lambda scope, root: _sample_queue())
    ledger, run_id = _interrupted_build_in_preflight(tmp_path)
    dispatcher = FakeDispatcher()
    result = run_resume(
        "epic-99", ledger=ledger, dispatcher=dispatcher, run_id=run_id, preflight=lambda: False,
    )
    assert result.preflight_failed is True
    assert dispatcher.calls == []
    assert ledger.run_row(run_id)["status"] == "FAILED"
    assert _events(ledger.db_path, "preflight")[-1][1].startswith("PRE_FLIGHT_RED")


def test_resume_of_a_run_that_passed_preflight_does_not_rerun_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sdlc.resume.discover_queue", lambda scope, root: _sample_queue())
    db = tmp_path / "ledger.db"
    run_build(
        BuildOptions(scope="epic-99", sequential=True),
        queue=_sample_queue(), ledger=Ledger(db), dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    ledger = Ledger(db)
    run_id = _runs(db)[0]["id"]
    ledger.run_update_status(run_id, "IN_PROGRESS")
    ledger.set_story_status(run_id, "s1-003", "IN_PROGRESS")

    def boom() -> bool:
        raise AssertionError("a run past preflight must not re-run it")

    run_resume("epic-99", ledger=ledger, dispatcher=FakeDispatcher(), run_id=run_id, preflight=boom)


def test_fix_resume_of_a_run_interrupted_in_preflight_reruns_it(tmp_path) -> None:
    db = tmp_path / ".sdlc-state.db"
    gh = FakeGh(_issue_json())

    def killed() -> bool:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_fix(
            FixOptions(issue=1), ledger=Ledger(db), dispatcher=RecordingDispatcher(),
            preflight=killed, runner=gh, root=tmp_path,
        )
    run_id = _runs(db)[0]["id"]
    calls = {"n": 0}

    def preflight() -> bool:
        calls["n"] += 1
        return True

    result = resume_fix(
        run_id, ledger=Ledger(db), dispatcher=RecordingDispatcher(), runner=gh,
        root=tmp_path, preflight=preflight,
    )
    assert calls["n"] == 1
    assert result.status == "DONE" and result.preflight_failed is False
    assert Ledger(db).preflight_state(run_id)["state"] == "passed"


def test_fix_resume_with_a_red_preflight_fails_the_run(tmp_path) -> None:
    db = tmp_path / ".sdlc-state.db"
    gh = FakeGh(_issue_json())

    def killed() -> bool:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_fix(
            FixOptions(issue=1), ledger=Ledger(db), dispatcher=RecordingDispatcher(),
            preflight=killed, runner=gh, root=tmp_path,
        )
    run_id = _runs(db)[0]["id"]
    dispatch = RecordingDispatcher()
    result = resume_fix(
        run_id, ledger=Ledger(db), dispatcher=dispatch, runner=gh, root=tmp_path,
        preflight=lambda: False,
    )
    assert result.preflight_failed is True and result.status == "FAILED"
    assert dispatch.calls == []
    assert Ledger(db).run_row(run_id)["status"] == "FAILED"


def _build_that_failed_preflight(tmp_path: Path) -> tuple[Ledger, str]:
    db = tmp_path / "ledger.db"
    run_build(
        BuildOptions(scope="epic-99", sequential=True),
        queue=_sample_queue(), ledger=Ledger(db), dispatcher=FakeDispatcher(),
        preflight=lambda: False,
    )
    ledger = Ledger(db)
    [run] = _runs(db)
    assert run["status"] == "FAILED"
    assert ledger.preflight_state(run["id"])["state"] == "failed"
    return ledger, run["id"]


def test_a_run_that_failed_preflight_is_offered_for_resume(tmp_path) -> None:
    # Why a red gate must not be skippable on resume: its FAILED run keeps every
    # story TODO, so the CLI names it ("failed with resumable stories — try:
    # sdlc resume --run <id>") and a fleet reclaim of a lapsed job resumes it.
    ledger, run_id = _build_that_failed_preflight(tmp_path)
    assert ledger.latest_failed_run("epic-99") == run_id
    assert has_resumable_work(ledger, run_id)
    assert ledger_run_terminal(str(ledger.db_path), run_id) is None


def test_resume_of_a_run_that_failed_preflight_reruns_the_gate(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sdlc.resume.discover_queue", lambda scope, root: _sample_queue())
    ledger, run_id = _build_that_failed_preflight(tmp_path)
    calls = {"n": 0}

    def preflight() -> bool:
        calls["n"] += 1
        return True

    result = run_resume(
        "epic-99", ledger=ledger, dispatcher=FakeDispatcher(), run_id=run_id, preflight=preflight,
    )
    assert calls["n"] == 1  # the gate never passed, so it is re-run — not skipped
    assert result.preflight_failed is False and result.completed == 3
    assert ledger.preflight_state(run_id)["state"] == "passed"
    assert len(_runs(ledger.db_path)) == 1  # same run


def test_resume_of_a_run_that_failed_preflight_dispatches_nothing_while_red(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sdlc.resume.discover_queue", lambda scope, root: _sample_queue())
    ledger, run_id = _build_that_failed_preflight(tmp_path)
    dispatcher = FakeDispatcher()
    result = run_resume(
        "epic-99", ledger=ledger, dispatcher=dispatcher, run_id=run_id, preflight=lambda: False,
    )
    assert result.preflight_failed is True
    assert dispatcher.calls == []
    assert ledger.run_row(run_id)["status"] == "FAILED"
    assert [lvl for lvl, _m in _events(ledger.db_path, "preflight")] == ["info", "error", "info", "error"]


def test_fix_resume_of_a_run_that_failed_preflight_reruns_the_gate(tmp_path) -> None:
    # Not refused as a run that "predates the Issue #547 freeze": it has no plan
    # only because its gate never passed, so nothing was investigated.
    db = tmp_path / ".sdlc-state.db"
    gh = FakeGh(_issue_json())
    run_fix(
        FixOptions(issue=1), ledger=Ledger(db), dispatcher=RecordingDispatcher(),
        preflight=lambda: False, runner=gh, root=tmp_path,
    )
    run_id = _runs(db)[0]["id"]
    calls = {"n": 0}

    def preflight() -> bool:
        calls["n"] += 1
        return True

    result = resume_fix(
        run_id, ledger=Ledger(db), dispatcher=RecordingDispatcher(), runner=gh,
        root=tmp_path, preflight=preflight,
    )
    assert calls["n"] == 1
    assert result.status == "DONE" and result.aborted is False
    assert Ledger(db).preflight_state(run_id)["state"] == "passed"


def test_resume_hands_a_fix_run_its_preflight_and_reports_a_red_gate(tmp_path, monkeypatch) -> None:
    # `run_resume` delegates a fix run to `resume_fix`: the injected gate must
    # reach it (never the real suite), and a red re-run must read as the same
    # PRE_FLIGHT_FAILURE a build resume gives — not "0 done, 1 failed".
    import sdlc.fix_issue as fix_mod

    def _real_suite(**_kw) -> bool:
        raise AssertionError("run_resume did not forward its preflight to resume_fix")

    monkeypatch.setattr(fix_mod, "default_preflight", _real_suite)
    db = tmp_path / ".sdlc-state.db"
    gh = FakeGh(_issue_json())

    def killed() -> bool:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_fix(
            FixOptions(issue=1), ledger=Ledger(db), dispatcher=RecordingDispatcher(),
            preflight=killed, runner=gh, root=tmp_path,
        )
    run_id = _runs(db)[0]["id"]
    calls = {"n": 0}

    def red() -> bool:
        calls["n"] += 1
        return False

    dispatch = RecordingDispatcher()
    result = run_resume(
        "issue-1", ledger=Ledger(db), dispatcher=dispatch, run_id=run_id,
        runner=gh, root=tmp_path, preflight=red,
    )
    assert calls["n"] == 1
    assert result.preflight_failed is True
    assert (result.resumed, result.failed) == (0, 0)
    assert dispatch.calls == []
    assert Ledger(db).run_row(run_id)["status"] == "FAILED"


# ---------------------------------------------------------------------------
# a batch whose gate never passed is not resumed — it starts afresh
# ---------------------------------------------------------------------------


def _batch_with_preflight(root: Path, preflight) -> tuple[Ledger, str]:
    """A `sdlc fix all` batch over issues 1 and 2 whose gate is ``preflight``."""
    db = root / ".sdlc-state.db"
    run_fix_batch(
        FixBatchOptions(target="all"), ledger=Ledger(db), dispatcher=BatchProbeDispatcher(),
        preflight=preflight, runner=FakeBatchGh([_batch_issue(1), _batch_issue(2)]), root=root,
    )
    ledger = Ledger(db)
    [run] = _runs(db)
    return ledger, run["id"]


def _batch_killed_in_preflight(root: Path) -> tuple[Ledger, str]:
    def killed() -> bool:
        raise KeyboardInterrupt  # the worker died mid-suite: no outcome event

    with pytest.raises(KeyboardInterrupt):
        _batch_with_preflight(root, killed)
    [run] = _runs(root / ".sdlc-state.db")
    return Ledger(root / ".sdlc-state.db"), run["id"]


def _story_statuses(ledger: Ledger, run_id: str) -> list[str]:
    return sorted(str(s["status"]) for s in ledger.story_rows(run_id))


def test_a_batch_interrupted_in_preflight_is_refused_not_closed_done(tmp_path) -> None:
    # The epic resume cannot rebuild issue stories: it used to re-run the gate,
    # dispatch nothing and stamp the run DONE with every issue still TODO.
    ledger, run_id = _batch_killed_in_preflight(tmp_path)

    def must_not_run() -> bool:
        raise AssertionError("a batch that cannot be resumed must not re-run its gate")

    dispatch = BatchProbeDispatcher()
    result = run_resume(
        "all", ledger=ledger, dispatcher=dispatch, run_id=run_id, root=tmp_path,
        preflight=must_not_run,
    )
    assert result.refused is True
    assert "batch" in result.refusal_reason and "sdlc fix all" in result.refusal_reason
    assert dispatch.calls == []
    assert ledger.run_row(run_id)["status"] == "IN_PROGRESS"  # not DONE
    assert _story_statuses(ledger, run_id) == ["TODO", "TODO"]
    # Neither the CLI hint nor a fleet reclaim treats it as resumable work.
    assert has_resumable_work(ledger, run_id) is False
    assert ledger_run_terminal(str(ledger.db_path), run_id) == "fix batch never passed preflight"


def test_a_batch_that_failed_preflight_is_not_offered_for_resume(tmp_path) -> None:
    ledger, run_id = _batch_with_preflight(tmp_path, lambda: False)
    assert ledger.run_row(run_id)["status"] == "FAILED"
    assert has_resumable_work(ledger, run_id) is False
    assert ledger_run_terminal(str(ledger.db_path), run_id) == "fix batch never passed preflight"
    result = run_resume(
        "all", ledger=ledger, dispatcher=BatchProbeDispatcher(), run_id=run_id,
        root=tmp_path, preflight=lambda: True,
    )
    assert result.refused is True
    assert ledger.run_row(run_id)["status"] == "FAILED"
    assert _story_statuses(ledger, run_id) == ["TODO", "TODO"]


def test_cli_resume_never_names_a_red_batch(tmp_path) -> None:
    from typer.testing import CliRunner

    from sdlc.cli import app

    ledger, run_id = _batch_with_preflight(tmp_path, lambda: False)
    db = str(ledger.db_path)
    bare = CliRunner().invoke(app, ["resume", "--db", db])
    assert bare.exit_code == 0, bare.output
    assert "nothing to resume: no incomplete run for scope 'all'." in bare.output
    assert run_id[:8] not in bare.output  # no "failed with resumable stories" hint

    explicit = CliRunner().invoke(app, ["resume", "--db", db, "--run", run_id])
    assert explicit.exit_code == 1
    assert "never passed preflight" in explicit.output
    assert ledger.run_row(run_id)["status"] == "FAILED"


def test_a_batch_past_its_gate_keeps_the_existing_resume_rule(tmp_path) -> None:
    # Only a batch whose gate never passed is refused; one that got past it (or
    # skipped it) is judged exactly as before this story.
    ledger, run_id = _batch_with_preflight(tmp_path, lambda: True)
    ledger.run_update_status(run_id, "IN_PROGRESS")  # interrupted after its gate
    ledger.set_story_status(run_id, "issue-1", "IN_PROGRESS")
    assert ledger.preflight_owed(run_id) is False
    assert ledger_run_terminal(str(ledger.db_path), run_id) != "fix batch never passed preflight"


# ---------------------------------------------------------------------------
# fleet job: finishes failed with the preflight reason
# ---------------------------------------------------------------------------


def test_ledger_preflight_failure_is_none_unless_the_gate_failed(tmp_path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    run_id = ledger.run_create("epic-1", "serial")
    assert ledger_preflight_failure(str(ledger.db_path), run_id) is None
    ledger.event_log(run_id, "", "info", "preflight", "started: make test")
    assert ledger_preflight_failure(str(ledger.db_path), run_id) is None
    ledger.event_log(run_id, "", "error", "preflight", TIMEOUT)
    assert ledger_preflight_failure(str(ledger.db_path), run_id) == TIMEOUT
    assert ledger_preflight_failure(str(tmp_path / "missing.db"), run_id) is None


def _scheduler_world(tmp_path: Path):
    from test_scheduler import Clock, FakeLauncher, _repo, _run, _store

    return Clock, FakeLauncher, _repo, _run, _store


def test_a_job_whose_run_died_in_preflight_finishes_failed_with_that_reason(tmp_path) -> None:
    Clock, FakeLauncher, _repo, _run, _store = _scheduler_world(tmp_path)
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    registry = Registry(tmp_path / "registry.json")
    launcher = FakeLauncher(alive_polls=2, code=1)
    clock = Clock()
    ledger = Ledger(tmp_path / "alpha.db")
    ledger.init()
    run_id = ledger.run_create("epic-3", "serial")
    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:  # the child opens its run and starts its gate...
            ledger.event_log(run_id, "", "info", "preflight", "started: make test")
            registry.register(
                RunRecord(run_id=run_id, repo=repo, db=str(ledger.db_path), scope="epic-3",
                          pid=launcher.procs[0].pid, status="IN_PROGRESS", started_at="")
            )
        elif calls["n"] == 2:  # ...which comes back red
            ledger.event_log(run_id, "", "error", "preflight", RED)
            ledger.run_update_status(run_id, "FAILED")
            registry.mark_finished(run_id, "FAILED")
        clock.advance(seconds)

    _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock, sleeper=sleeper, registry=registry)

    job = store.get_job(job_id)
    assert job.state == "failed"
    assert job.reason == RED


def test_a_lapsed_batch_job_whose_run_died_in_preflight_restarts_fresh(tmp_path) -> None:
    # The worker died mid-gate: reclaiming must re-run `sdlc fix all` — as it did
    # when no run existed yet — never `sdlc resume --run`, which closed it DONE.
    from datetime import datetime, timezone

    from test_scheduler import _dead_pid

    Clock, FakeLauncher, _repo, _run, _store = _scheduler_world(tmp_path)
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    ledger, run_id = _batch_killed_in_preflight(Path(repo))
    job_id = store.add_job(repo=repo, kind="fix", scope="all")
    store.claim_job(job_id, claimed_by="dead:1", lease_seconds=0,
                    now=datetime(2026, 9, 7, 11, 0, tzinfo=timezone.utc))
    store.attach_run(job_id, run_id)
    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord(run_id=run_id, repo=repo, db=str(ledger.db_path), scope="issues-all",
                  pid=_dead_pid(), status="IN_PROGRESS", started_at="")
    )
    launcher = FakeLauncher(alive_polls=1)
    lines: list[str] = []

    result = _run(store, tmp_path=tmp_path, launcher=launcher, registry=registry,
                  echo=lines.append)

    assert result.resumed == 0
    assert launcher.calls[0][0][-2:] == ["fix", "all"]
    assert "resume" not in launcher.calls[0][0]
    assert any("fix batch never passed preflight" in line for line in lines)
    assert ledger.run_row(run_id)["status"] == "IN_PROGRESS"  # never closed DONE


def test_a_workers_heartbeat_push_carries_the_runs_phase(tmp_path) -> None:
    from sdlc.queue_worker import WorkerProfile
    from sdlc.scheduler import SchedulerConfig

    Clock, FakeLauncher, _repo, _run, _store = _scheduler_world(tmp_path)
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    store.add_job(repo=repo, kind="build", scope="epic-3")
    registry = Registry(tmp_path / "registry.json")
    launcher = FakeLauncher(alive_polls=4, code=0)
    clock = Clock()
    ledger = Ledger(tmp_path / "alpha.db")
    ledger.init()
    run_id = ledger.run_create("epic-3", "serial")
    ledger.event_log(run_id, "", "info", "preflight", "started: make test")
    seen: list[str | None] = []
    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            registry.register(
                RunRecord(run_id=run_id, repo=repo, db=str(ledger.db_path), scope="epic-3",
                          pid=launcher.procs[0].pid, status="IN_PROGRESS", started_at="")
            )
        elif calls["n"] == 3:
            seen.extend(r["phase"] for r in store.list_fleet_runs())
        clock.advance(seconds)

    profile = WorkerProfile(
        name="xps", host="xps-host", pools=["claude-m3"], harnesses=["claude"],
        sandbox=None, repos=[], dashboard_url=None,
    )
    _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, sleeper=sleeper,
        registry=registry, config=SchedulerConfig(slots=2, poll_seconds=1.0, worker=profile),
    )
    assert seen == ["preflight"]


# ---------------------------------------------------------------------------
# Gap coverage: best-effort edges, CLI surfaces, status renderings
# ---------------------------------------------------------------------------


def _fresh_run(tmp_path: Path) -> tuple[Ledger, str]:
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    return ledger, ledger.run_create("epic-1", "serial")


def test_preflight_phase_survives_a_failing_notifier_and_view(tmp_path, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(build_mod, "notify", boom)
    ledger, run_id = _fresh_run(tmp_path)
    reason = build_mod.run_preflight_phase(
        ledger, run_id, lambda: False, [], command="make test", render_view=boom,
    )
    assert reason.startswith("PRE_FLIGHT_RED")
    assert ledger.run_row(run_id)["status"] == "FAILED"


def test_preflight_phase_renders_the_view_on_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(build_mod, "notify", lambda *a, **k: None)
    ledger, run_id = _fresh_run(tmp_path)
    rendered: list[str] = []
    build_mod.run_preflight_phase(
        ledger, run_id, lambda: False, [], command="make test", render_view=rendered.append,
    )
    assert rendered == [run_id]


def test_registry_phase_tolerates_an_unwritable_registry() -> None:
    class Broken:
        def set_phase(self, *_a):
            raise OSError("read-only")

    build_mod._registry_phase(Broken(), "run-1", "preflight")  # must not raise
    build_mod._registry_phase(None, "run-1", "preflight")


def test_format_preflight_edge_cases() -> None:
    assert format_preflight({"state": "running"}) == "preflight: running (?)"
    assert format_preflight({"state": "failed"}) == "preflight: failed — no reason recorded"
    assert format_preflight({"state": "mystery"}) is None


def test_live_phase_falls_back_to_the_cached_one_when_ledger_unreadable(tmp_path) -> None:
    bad = tmp_path / "not-a-db"
    bad.write_text("garbage")
    record = _record(db=str(bad), phase="preflight")
    assert live_record(record).phase == "preflight"


def test_ledger_preflight_failure_degrades_to_none_on_unreadable_ledger(tmp_path) -> None:
    bad = tmp_path / "not-a-db"
    bad.write_text("garbage")
    assert ledger_preflight_failure(str(bad), "run-1") is None


def test_ledger_preflight_failure_without_a_reason_is_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        Ledger, "preflight_state", lambda self, run_id: {"state": "failed", "reason": ""}
    )
    assert ledger_preflight_failure(str(tmp_path / "l.db"), "run-1") is None


def test_cli_resume_reports_a_preflight_failure(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    import sdlc.resume as resume_mod
    from sdlc.cli import app
    from sdlc.resume import ResumeResult

    monkeypatch.setattr(
        resume_mod, "run_resume",
        lambda *a, **k: ResumeResult(run_id="r1", preflight_failed=True),
    )
    result = CliRunner().invoke(app, ["resume", "--db", str(tmp_path / ".sdlc-state.db")])
    assert result.exit_code == 1
    assert "PRE_FLIGHT_FAILURE" in result.output
    # The gate's PRE_FLIGHT_* line suggests --preflight-timeout / --skip-preflight,
    # which `sdlc resume` does not take: say where they do apply.
    assert "start a fresh run" in result.output and "--skip-preflight" in result.output


def test_cli_status_prints_the_preflight_header(tmp_path) -> None:
    from typer.testing import CliRunner

    from sdlc.cli import app

    ledger, run_id = _fresh_run(tmp_path)
    ledger.event_log(run_id, "", "info", "preflight", "started: make test")
    result = CliRunner().invoke(app, ["status", "--db", str(ledger.db_path)])
    assert result.exit_code == 0, result.output
    assert "preflight: running (make test," in result.output


def test_status_markdown_carries_the_preflight_line(tmp_path) -> None:
    from sdlc.status import format_markdown

    ledger, run_id = _fresh_run(tmp_path)
    ledger.event_log(run_id, "", "info", "preflight", "started: make test")
    snap = status_snapshot(ledger)
    doctor = {"summary": {"ok": 0, "warn": 0, "fail": 0}, "checks": [], "install": None}
    md = format_markdown(snap, doctor)
    assert "preflight: running (make test," in md
