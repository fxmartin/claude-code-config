# ABOUTME: Tests for Story 35.5-006 — `sdlc reconcile` writes a run's final status to the fleet
# ABOUTME: record and to the registry the worker reads, and doctor flags a split registry path.

from __future__ import annotations

import plistlib
import socket
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

import sdlc.doctor as doctor
from sdlc.cli import app
from sdlc.queue import QueueStore
from sdlc.queue_client import QueueClient, finish_fleet_run
from sdlc.queue_server import AccessPolicy
from sdlc.queue_server import make_server as make_queue_server
from sdlc.reconcile import reconcile_run
from sdlc.registry import Registry, RunRecord, registry_path_for

from test_cli_reconcile import _init_repo_with_origin, _land_story
from test_reconcile import _seed_run

TOKEN = "s3cret-token"
runner = CliRunner()


def _record(run_id: str = "run-1", **overrides) -> RunRecord:
    fields = {
        "run_id": run_id,
        "repo": "/Users/fx/Work/alpha",
        "db": "/Users/fx/Work/alpha/.sdlc-state.db",
        "scope": "epic-1",
        "pid": 4242,
        "status": "AWAITING_APPROVAL",
        "started_at": "2026-10-18T10:00:00+00:00",
        "total": 2,
        "completed": 1,
        "worker": "m3max",
        "dashboard_url": "http://m3max.tailnet:8787",
        "origin": "http://gitlab.test/root/alpha.git",
    }
    fields.update(overrides)
    return RunRecord(**fields)


@pytest.fixture
def store(tmp_path: Path) -> QueueStore:
    s = QueueStore(tmp_path / "service-queue.db")
    s.init()
    return s


@pytest.fixture
def live(store: QueueStore):
    policy = AccessPolicy(token=TOKEN, networks=("127.0.0.0/8",))
    server = make_queue_server(store, policy, "127.0.0.1", 0)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=10)


def _fleet_env(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)


def _dead_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _row(store: QueueStore, run_id: str = "run-1") -> dict:
    return next(r for r in store.list_fleet_runs() if r["run_id"] == run_id)


# --- finish_fleet_run: the push ---------------------------------------------------


def test_a_closed_run_is_pushed_with_its_final_status_and_worker_kept(
    live: str, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.put_fleet_run(_record())
    _fleet_env(monkeypatch, live)

    assert finish_fleet_run("run-1", "DONE", 2) is None

    row = _row(store)
    assert (row["status"], row["completed"]) == ("DONE", 2)
    assert row["finished_at"]
    assert row["phase"] is None
    # What the worker pushed stays as pushed.
    assert row["worker"] == "m3max"
    assert row["dashboard_url"] == "http://m3max.tailnet:8787"
    assert row["origin"] == "http://gitlab.test/root/alpha.git"
    assert row["pid"] == 4242


def test_a_failed_close_is_pushed_too(
    live: str, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.put_fleet_run(_record())
    _fleet_env(monkeypatch, live)
    assert finish_fleet_run("run-1", "FAILED", 1) is None
    assert _row(store)["status"] == "FAILED"


def test_nothing_is_pushed_without_a_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(QueueClient, "list_fleet_runs", lambda self: calls.append("list") or [])
    monkeypatch.setattr(QueueClient, "put_fleet_run", lambda self, record: calls.append("put"))
    assert finish_fleet_run("run-1", "DONE", 2) is None
    assert calls == []


def test_nothing_is_pushed_when_the_fleet_has_no_record_of_the_run(
    live: str, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.put_fleet_run(_record("another-run"))
    _fleet_env(monkeypatch, live)

    assert finish_fleet_run("run-1", "DONE", 2) is None

    assert [r["run_id"] for r in store.list_fleet_runs()] == ["another-run"]
    assert _row(store, "another-run")["status"] == "AWAITING_APPROVAL"


def test_a_record_already_final_in_that_status_is_left_alone(
    live: str, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.put_fleet_run(
        _record(status="DONE", finished_at="2026-10-18T11:00:00+00:00", completed=2)
    )
    _fleet_env(monkeypatch, live)
    assert finish_fleet_run("run-1", "DONE", 2) is None
    assert _row(store)["finished_at"] == "2026-10-18T11:00:00+00:00"


@pytest.mark.parametrize("sentinel", ["SDLC_IN_TEST", "SDLC_FLEET_DISABLED"])
def test_nothing_is_pushed_from_inside_a_dispatched_agent(
    live: str, store: QueueStore, monkeypatch: pytest.MonkeyPatch, sentinel: str
) -> None:
    store.put_fleet_run(_record())
    _fleet_env(monkeypatch, live)
    monkeypatch.setenv(sentinel, "1")

    assert finish_fleet_run("run-1", "DONE", 2) is None

    assert _row(store)["status"] == "AWAITING_APPROVAL"


def test_an_unreachable_service_yields_one_warning_naming_the_run_and_the_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fleet_env(monkeypatch, _dead_url())

    warning = finish_fleet_run("d72324cb-full-id", "DONE", 2)

    assert warning is not None and "\n" not in warning
    assert "d72324cb" in warning
    assert "sdlc queue runs prune d72324cb-full-id" in warning


def test_a_malformed_fleet_url_is_no_fleet_and_stays_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", "not-a-url")
    assert finish_fleet_run("run-1", "DONE", 2) is None


# --- reconcile_run: when the fleet is told ----------------------------------------


def _syncs():
    calls: list[tuple[str, str, int]] = []

    def sync(run_id: str, status: str, completed: int) -> str | None:
        calls.append((run_id, status, completed))
        return "fleet warning"

    return calls, sync


def test_reconcile_syncs_a_run_that_closes_final_and_reports_the_warning(
    tmp_path: Path,
) -> None:
    root = _init_repo_with_origin(tmp_path)
    _land_story(root, "99.1-001")
    run_id = _seed_run(tmp_path / "ledger.db", [("99.1-001", "FAILED", 100)])
    from sdlc.build import Ledger

    calls, sync = _syncs()
    result = reconcile_run(Ledger(tmp_path / "ledger.db"), run_id, root=root, fleet_sync=sync)

    assert calls == [(run_id, "DONE", 1)]
    assert result.fleet_warning == "fleet warning"


def test_reconcile_does_not_sync_a_run_that_is_not_final(tmp_path: Path) -> None:
    root = _init_repo_with_origin(tmp_path)
    run_id = _seed_run(
        tmp_path / "ledger.db",
        [("99.1-001", "NEEDS_ATTENTION", None), ("99.1-002", "NEEDS_ATTENTION", None)],
    )
    from sdlc.build import Ledger

    calls, sync = _syncs()
    result = reconcile_run(Ledger(tmp_path / "ledger.db"), run_id, root=root, fleet_sync=sync)

    assert result.run_status_after == "NEEDS_ATTENTION"
    assert calls == []
    assert result.fleet_warning is None


def test_reconcile_stamps_the_workers_registry_too(tmp_path: Path) -> None:
    root = _init_repo_with_origin(tmp_path)
    _land_story(root, "99.1-001")
    run_id = _seed_run(tmp_path / "ledger.db", [("99.1-001", "FAILED", 100)])
    from sdlc.build import Ledger

    shell = Registry(tmp_path / "shell" / "registry.json")
    worker = Registry(tmp_path / "worker" / "registry.json")
    worker.register(
        RunRecord(
            run_id=run_id, repo=str(root), db="db", scope="epic-99", pid=1,
            status="AWAITING_APPROVAL", started_at="2026-10-18T10:00:00+00:00",
        )
    )

    reconcile_run(
        Ledger(tmp_path / "ledger.db"), run_id, root=root, registry=shell,
        worker_registry=worker,
    )

    [record] = worker.records()
    assert (record.status, record.completed) == ("DONE", 1)
    assert record.finished_at


# --- registry path resolution -----------------------------------------------------


def test_registry_path_for_follows_the_given_environment(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert registry_path_for({}, home=home) == home / ".sdlc" / "registry.json"
    xdg = {"XDG_STATE_HOME": str(home / ".local/state")}
    assert registry_path_for(xdg, home=home) == home / ".local/state/sdlc/registry.json"
    explicit = {**xdg, "SDLC_REGISTRY_PATH": "/x/r.json"}
    assert registry_path_for(explicit, home=home) == Path("/x/r.json")


def _worker_plist(tmp_path: Path, env: dict[str, str]) -> Path:
    path = tmp_path / "LaunchAgents" / f"{doctor.WORKER_LABEL}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        plistlib.dumps({"Label": doctor.WORKER_LABEL, "EnvironmentVariables": env})
    )
    return path


def test_worker_registry_path_is_the_one_the_agent_environment_resolves(
    tmp_path: Path,
) -> None:
    plist = _worker_plist(tmp_path, {"XDG_STATE_HOME": str(tmp_path / "state")})
    assert doctor.worker_registry_path(
        agent_path=plist, unit_path=tmp_path / "none.service"
    ) == tmp_path / "state" / "sdlc" / "registry.json"


def test_worker_registry_path_is_none_where_no_worker_is_installed(tmp_path: Path) -> None:
    assert doctor.worker_registry_path(
        agent_path=tmp_path / "none.plist", unit_path=tmp_path / "none.service"
    ) is None


# --- doctor: a split registry path ------------------------------------------------


def test_doctor_warns_when_the_shell_and_the_worker_resolve_different_registries(
    tmp_path: Path,
) -> None:
    plist = _worker_plist(tmp_path, {"XDG_STATE_HOME": str(tmp_path / "state")})

    finding = doctor.check_registry_path(
        agent_path=plist, unit_path=tmp_path / "none.service",
        shell_path=tmp_path / "home" / ".sdlc" / "registry.json",
    )

    assert finding is not None and finding.status == "WARN"
    assert str(tmp_path / "state" / "sdlc" / "registry.json") in finding.detail
    assert str(tmp_path / "home" / ".sdlc" / "registry.json") in finding.detail
    assert "XDG_STATE_HOME" in (finding.remedy or "")


def test_doctor_is_clean_when_both_resolve_the_same_registry(tmp_path: Path) -> None:
    shared = tmp_path / "state" / "sdlc" / "registry.json"
    plist = _worker_plist(tmp_path, {"XDG_STATE_HOME": str(tmp_path / "state")})
    finding = doctor.check_registry_path(
        agent_path=plist, unit_path=tmp_path / "none.service", shell_path=shared
    )
    assert finding is not None and finding.status == "CLEAN"


def test_doctor_says_nothing_where_no_worker_is_installed(tmp_path: Path) -> None:
    assert doctor.check_registry_path(
        agent_path=tmp_path / "none.plist", unit_path=tmp_path / "none.service",
        shell_path=tmp_path / "r.json",
    ) is None


# --- the CLI, end to end ----------------------------------------------------------


def test_reconcile_in_a_shell_without_xdg_updates_the_worker_registry_and_fleet_record(
    tmp_path: Path,
    live: str,
    store: QueueStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _init_repo_with_origin(tmp_path)
    _land_story(root, "99.1-001")
    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-001", "AWAITING_APPROVAL", 100)])

    # The operator's SSH shell: no XDG_STATE_HOME, so its registry is ~/.sdlc/…
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("SDLC_REGISTRY_PATH", raising=False)
    # The worker's LaunchAgent pins XDG_STATE_HOME; that registry holds the run.
    state = tmp_path / "worker-state"
    plist = _worker_plist(tmp_path, {"XDG_STATE_HOME": str(state)})
    monkeypatch.setattr(doctor, "default_worker_plist", lambda: plist)
    worker_registry = Registry(state / "sdlc" / "registry.json")
    worker_registry.register(
        RunRecord(
            run_id=run_id, repo=str(root), db=str(db), scope="epic-99", pid=1,
            status="AWAITING_APPROVAL", started_at="2026-10-18T10:00:00+00:00",
            worker="m3max",
        )
    )
    store.put_fleet_run(_record(run_id, status="AWAITING_APPROVAL"))
    _fleet_env(monkeypatch, live)

    monkeypatch.chdir(root)
    result = runner.invoke(app, ["reconcile", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "warning" not in result.output.lower()
    [record] = worker_registry.records()
    assert record.status == "DONE" and record.finished_at
    assert _row(store, run_id)["status"] == "DONE"
    assert _row(store, run_id)["worker"] == "m3max"
    # The shell's own registry never held the run and still does not.
    assert Registry(home / ".sdlc" / "registry.json").records() == []


def test_reconcile_with_an_unreachable_fleet_still_closes_and_warns_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _init_repo_with_origin(tmp_path)
    _land_story(root, "99.1-001")
    db = tmp_path / "ledger.db"
    run_id = _seed_run(db, [("99.1-001", "AWAITING_APPROVAL", 100)])
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    _fleet_env(monkeypatch, _dead_url())

    monkeypatch.chdir(root)
    result = runner.invoke(app, ["reconcile", "--db", str(db)])

    assert result.exit_code == 0, result.output
    from sdlc.build import Ledger

    assert Ledger(db).run_row(run_id)["status"] == "DONE"
    warnings = [line for line in result.output.splitlines() if "queue runs prune" in line]
    assert len(warnings) == 1 and run_id in warnings[0]


def test_reconcile_without_a_fleet_prints_no_fleet_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _init_repo_with_origin(tmp_path)
    _land_story(root, "99.1-001")
    db = tmp_path / "ledger.db"
    _seed_run(db, [("99.1-001", "AWAITING_APPROVAL", 100)])
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))

    monkeypatch.chdir(root)
    result = runner.invoke(app, ["reconcile", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "fleet" not in result.output.lower()
