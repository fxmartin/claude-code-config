# ABOUTME: Tests for the minimal run-logging API + CLI (Story 11.2-013).
# ABOUTME: open/stage/close write the ledger + registry so fix-issue surfaces in the dashboard.

from __future__ import annotations

import json
import os

from typer.testing import CliRunner

from sdlc.build import Ledger, status_snapshot
from sdlc.cli import app
from sdlc.registry import Registry, derive_state
from sdlc.runlog import run_close, run_open, run_stage

runner = CliRunner()

DEAD_PID = 2**31 - 1


# --- core API -------------------------------------------------------------


def test_run_open_creates_ledger_run_and_registers(tmp_path, monkeypatch):
    """run_open seeds a ledger run + sole story and a registry record."""
    db = tmp_path / ".sdlc-state.db"
    reg_path = tmp_path / "registry.json"
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(reg_path))

    handle = run_open(scope="issue-42", db=db, repo=tmp_path)

    assert handle is not None
    assert handle.run_id
    assert handle.story_id == "issue-42"

    # Ledger has the run and a single IN_PROGRESS story.
    ledger = Ledger(db)
    run_row = ledger.run_row(handle.run_id)
    assert run_row is not None
    assert run_row["scope"] == "issue-42"
    assert run_row["mode"] == "fix-issue"
    assert run_row["status"] == "IN_PROGRESS"
    stories = ledger.story_rows(handle.run_id)
    assert [s["story_id"] for s in stories] == ["issue-42"]

    # Registry discovered the run pointing at this ledger.
    rec = {r.run_id: r for r in Registry(reg_path).records()}[handle.run_id]
    assert rec.repo == str(tmp_path.resolve())
    assert rec.db == str(db.resolve())
    assert rec.scope == "issue-42"
    assert rec.pid == os.getpid()
    assert rec.status == "IN_PROGRESS"


def test_run_open_records_orchestrator_pid_not_own(tmp_path, monkeypatch):
    """A skill-supplied pid is registered verbatim so a live run is not DEAD.

    Regression: the markdown skill shells out to a short-lived `sdlc run-open`
    subprocess; recording *its* pid (os.getpid) would derive a still-running fix
    as DEAD the instant the subprocess exits. The orchestrator passes its own
    long-lived pid via --pid, which must be stored as-is.
    """
    reg_path = tmp_path / "registry.json"
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(reg_path))
    db = tmp_path / ".sdlc-state.db"
    # A live process distinct from this one stands in for the orchestrator.
    orchestrator_pid = os.getppid()

    handle = run_open(scope="issue-88", db=db, repo=tmp_path, pid=orchestrator_pid)

    rec = {r.run_id: r for r in Registry(reg_path).records()}[handle.run_id]
    assert rec.pid == orchestrator_pid
    # The orchestrator is alive and the run is unfinished → IN_PROGRESS, not DEAD.
    assert derive_state(rec) == "IN_PROGRESS"


def test_run_open_dead_pid_derives_dead(tmp_path, monkeypatch):
    """A run whose orchestrator pid is gone derives DEAD, as a crash should."""
    reg_path = tmp_path / "registry.json"
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(reg_path))
    db = tmp_path / ".sdlc-state.db"

    handle = run_open(scope="issue-89", db=db, repo=tmp_path, pid=DEAD_PID)

    rec = {r.run_id: r for r in Registry(reg_path).records()}[handle.run_id]
    assert derive_state(rec) == "DEAD"


def test_run_stage_start_then_finish(tmp_path, monkeypatch):
    """A phase logs an IN_PROGRESS stage that transitions to DONE."""
    db = tmp_path / ".sdlc-state.db"
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    handle = run_open(scope="issue-7", db=db, repo=tmp_path)

    assert run_stage(action="start", run_id=handle.run_id, stage="investigate", db=db)
    snap = status_snapshot(Ledger(db), handle.run_id)
    inv = next(st for st in snap["stories"][0]["stages"] if st["name"] == "build")
    # `build` is a pipeline stage; the investigate phase lives in the raw rows.
    assert inv["status"] == "PENDING"

    assert run_stage(
        action="finish", run_id=handle.run_id, stage="investigate", db=db, status="DONE"
    )
    breakdown = Ledger(db).stage_breakdown(handle.run_id)["issue-7"]
    inv_rows = [r for r in breakdown if r["name"] == "investigate"]
    assert len(inv_rows) == 1
    assert inv_rows[0]["status"] == "DONE"


def test_run_stage_maps_pipeline_stage_into_detail(tmp_path, monkeypatch):
    """A `build` phase shows up progressing in the reused status snapshot."""
    db = tmp_path / ".sdlc-state.db"
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    handle = run_open(scope="issue-9", db=db, repo=tmp_path)

    run_stage(action="start", run_id=handle.run_id, stage="build", db=db)
    snap = status_snapshot(Ledger(db), handle.run_id)
    build_cell = next(s for s in snap["stories"][0]["stages"] if s["name"] == "build")
    assert build_cell["status"] == "IN_PROGRESS"

    run_stage(
        action="finish", run_id=handle.run_id, stage="build", db=db, status="DONE"
    )
    snap = status_snapshot(Ledger(db), handle.run_id)
    build_cell = next(s for s in snap["stories"][0]["stages"] if s["name"] == "build")
    assert build_cell["status"] == "DONE"


def test_run_close_finalizes_run_and_registry(tmp_path, monkeypatch):
    """Closing a run stamps it terminal in both the ledger and the registry."""
    reg_path = tmp_path / "registry.json"
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(reg_path))
    db = tmp_path / ".sdlc-state.db"
    handle = run_open(scope="issue-3", db=db, repo=tmp_path)

    assert run_close(run_id=handle.run_id, db=db, status="DONE", completed=1)

    run_row = Ledger(db).run_row(handle.run_id)
    assert run_row["status"] == "DONE"
    assert run_row["finished_at"] is not None
    # The sole story is marked terminal too, so the dashboard counts reconcile.
    story = Ledger(db).story_rows(handle.run_id)[0]
    assert story["status"] == "DONE"

    rec = {r.run_id: r for r in Registry(reg_path).records()}[handle.run_id]
    assert rec.status == "DONE"
    assert rec.finished_at is not None
    assert rec.completed == 1
    assert derive_state(rec) == "DONE"


def test_run_close_failed_status(tmp_path, monkeypatch):
    """A failed fix-issue finalizes FAILED in ledger and registry."""
    reg_path = tmp_path / "registry.json"
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(reg_path))
    db = tmp_path / ".sdlc-state.db"
    handle = run_open(scope="issue-5", db=db, repo=tmp_path)

    run_close(run_id=handle.run_id, db=db, status="FAILED", completed=0)

    assert Ledger(db).run_row(handle.run_id)["status"] == "FAILED"
    rec = {r.run_id: r for r in Registry(reg_path).records()}[handle.run_id]
    assert rec.status == "FAILED"


def test_run_close_aborted_clears_in_progress(tmp_path, monkeypatch):
    """A deliberate early stop closes the run ABORTED, not stuck IN_PROGRESS.

    Regression: a Phase 2 stop condition (issue closed / assigned elsewhere /
    wontfix) exits after run-open but before Phase 11. With the orchestrator pid
    still alive, an unclosed run would derive IN_PROGRESS forever — so the skill
    must finalize it ABORTED on the way out.
    """
    reg_path = tmp_path / "registry.json"
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(reg_path))
    db = tmp_path / ".sdlc-state.db"
    # A *live* orchestrator pid — so only an explicit close can clear IN_PROGRESS.
    handle = run_open(scope="issue-4", db=db, repo=tmp_path, pid=os.getppid())

    run_close(run_id=handle.run_id, db=db, status="ABORTED", completed=0)

    run_row = Ledger(db).run_row(handle.run_id)
    assert run_row["status"] == "ABORTED"
    assert run_row["finished_at"] is not None  # terminal → not perpetually open
    rec = {r.run_id: r for r in Registry(reg_path).records()}[handle.run_id]
    assert rec.status == "ABORTED"
    assert rec.finished_at is not None
    assert derive_state(rec) == "ABORTED"


# --- best-effort: a logging failure never raises ---------------------------


def test_run_stage_unwritable_db_returns_false(tmp_path, monkeypatch):
    """A stage against a never-opened run is a no-op, not an exception."""
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    db = tmp_path / "missing" / ".sdlc-state.db"  # parent dir does not exist
    # No run-open, no schema — must degrade quietly to False.
    assert run_stage(action="start", run_id="nope", stage="build", db=db) is False


def test_run_close_unknown_run_is_best_effort(tmp_path, monkeypatch):
    """Closing an unknown run never raises; registry stays clean."""
    reg_path = tmp_path / "registry.json"
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(reg_path))
    db = tmp_path / ".sdlc-state.db"
    Ledger(db).init()
    # Unknown run id: ledger UPDATE matches nothing, registry mark_finished no-ops.
    assert run_close(run_id="ghost", db=db, status="DONE") is True


def test_run_stage_rejects_unknown_action(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    db = tmp_path / ".sdlc-state.db"
    handle = run_open(scope="issue-1", db=db, repo=tmp_path)
    assert (
        run_stage(action="bogus", run_id=handle.run_id, stage="build", db=db) is False
    )


def test_run_open_unwritable_ledger_returns_none(tmp_path, monkeypatch):
    """A ledger that cannot be created yields None instead of raising."""
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    db = tmp_path / "missing" / ".sdlc-state.db"  # parent dir does not exist
    assert run_open(scope="issue-2", db=db, repo=tmp_path) is None


def test_run_open_swallows_registry_failure(tmp_path):
    """A registry write error leaves the ledger run but never raises."""

    class _BoomRegistry:
        def register(self, record):  # noqa: ARG002 - signature match
            raise OSError("registry unwritable")

    db = tmp_path / ".sdlc-state.db"
    handle = run_open(scope="issue-6", db=db, repo=tmp_path, registry=_BoomRegistry())
    # The ledger run still exists; only the discovery cache write was skipped.
    assert handle is not None
    assert Ledger(db).run_row(handle.run_id)["status"] == "IN_PROGRESS"


def test_run_stage_swallows_ledger_error(tmp_path, monkeypatch):
    """A stage write that raises mid-flight degrades to False, not an exception."""
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    db = tmp_path / "missing" / ".sdlc-state.db"  # parent dir does not exist
    # An explicit story id skips the lookup, so stage_start itself raises on connect.
    assert (
        run_stage(action="start", run_id="x", stage="build", db=db, story_id="s")
        is False
    )


def test_run_close_unwritable_ledger_returns_false(tmp_path):
    """A failed ledger finalize returns False; the registry mirror still runs."""

    class _BoomRegistry:
        def mark_finished(self, run_id, status, *, completed=None):  # noqa: ARG002
            raise OSError("registry unwritable")

    db = tmp_path / "missing" / ".sdlc-state.db"  # parent dir does not exist
    assert (
        run_close(run_id="x", db=db, status="DONE", registry=_BoomRegistry()) is False
    )


# --- CLI surface -----------------------------------------------------------


def test_cli_run_lifecycle(tmp_path, monkeypatch):
    """run-open → run-stage start/finish → run-close drives ledger + registry."""
    reg_path = tmp_path / "registry.json"
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(reg_path))
    db = tmp_path / ".sdlc-state.db"

    opened = runner.invoke(
        app,
        ["run-open", "--scope", "issue-11", "--db", str(db), "--repo", str(tmp_path)],
    )
    assert opened.exit_code == 0
    run_id = opened.stdout.strip()
    assert run_id

    started = runner.invoke(
        app,
        ["run-stage", "start", "--run", run_id, "--stage", "build", "--db", str(db)],
    )
    assert started.exit_code == 0
    finished = runner.invoke(
        app,
        [
            "run-stage",
            "finish",
            "--run",
            run_id,
            "--stage",
            "build",
            "--db",
            str(db),
            "--status",
            "DONE",
        ],
    )
    assert finished.exit_code == 0

    closed = runner.invoke(
        app,
        [
            "run-close",
            "--run",
            run_id,
            "--db",
            str(db),
            "--status",
            "DONE",
            "--completed",
            "1",
        ],
    )
    assert closed.exit_code == 0

    # The run is now discoverable via the same registry the dashboard reads.
    listed = runner.invoke(app, ["runs", "--json"])
    rows = {r["run_id"]: r for r in json.loads(listed.stdout)}
    assert rows[run_id]["scope"] == "issue-11"
    assert rows[run_id]["state"] == "DONE"


def test_cli_run_open_json(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(
        app, ["run-open", "--scope", "issue-12", "--db", str(db), "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run_id"]
    assert payload["db"] == str(db.resolve())
    assert payload["story_id"] == "issue-12"


def test_cli_run_stage_rejects_bad_action(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(
        app, ["run-stage", "bogus", "--run", "x", "--stage", "build", "--db", str(db)]
    )
    assert result.exit_code == 2


def test_cli_run_open_logging_unavailable_exits_nonzero(tmp_path, monkeypatch):
    """run-open exits 1 + warns when run_open degrades to None (ledger write failed).

    The markdown skill's best-effort guard (`|| true`) relies on the non-zero exit
    to carry on unlogged, and the stderr message tells the operator logging is off.
    """
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setattr("sdlc.runlog.run_open", lambda **_: None)
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(
        app, ["run-open", "--scope", "issue-13", "--db", str(db), "--repo", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "logging unavailable" in result.output


def test_cli_run_stage_finish_failure_exits_one(tmp_path, monkeypatch):
    """run-stage exits 1 when the ledger write fails (no run-open, no schema)."""
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(
        app, ["run-stage", "start", "--run", "missing", "--stage", "build", "--db", str(db)]
    )
    assert result.exit_code == 1


def test_cli_run_close_failure_exits_one(tmp_path, monkeypatch):
    """run-close exits 1 when the ledger write fails (unknown db/run)."""
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(
        app, ["run-close", "--run", "missing", "--db", str(db), "--status", "DONE"]
    )
    assert result.exit_code == 1
