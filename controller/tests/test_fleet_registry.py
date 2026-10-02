# ABOUTME: Tests for the fleet registry (Story 35.4-001) — workers push run records to the
# ABOUTME: service's /runs, the XPS dashboard merges them, and degrades when it is unreachable.

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import sdlc.queue_client as qc
from sdlc.dashboard import _PAGE, _change_token, _registry_runs_view, make_server
from sdlc.doctor import Finding
from sdlc.queue import QueueError, QueueStore
from sdlc.queue_client import QueueClient, QueueUnavailable, push_fleet_run
from sdlc.queue_server import AccessPolicy
from sdlc.queue_server import make_server as make_queue_server
from sdlc.registry import WORKER_ENV, Registry, RunRecord, derive_state

TOKEN = "s3cret-token"


def _record(run_id: str = "run-1", **overrides) -> RunRecord:
    fields = {
        "run_id": run_id,
        "repo": "/Users/fx/Work/alpha",
        "db": "/Users/fx/Work/alpha/.sdlc-state.db",
        "scope": "epic-1",
        "pid": 4242,
        "status": "IN_PROGRESS",
        "started_at": "2026-10-02T10:00:00+00:00",
        "total": 5,
        "completed": 1,
        "worker": "m3max",
    }
    fields.update(overrides)
    return RunRecord(**fields)


@pytest.fixture
def store(tmp_path: Path) -> QueueStore:
    s = QueueStore(tmp_path / "service-queue.db")
    s.init()
    return s


class _Live:
    """A real queue service on an ephemeral loopback port (token-gated)."""

    def __init__(self, store: QueueStore) -> None:
        policy = AccessPolicy(token=TOKEN, networks=("127.0.0.0/8",))
        self.server = make_queue_server(store, policy, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=10)


@pytest.fixture
def live(store: QueueStore):
    running = _Live(store)
    yield running
    running.stop()


def _fleet_env(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)


def _dead_url() -> str:
    """A loopback URL nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _beat(store: QueueStore, name: str, *, at: datetime | None = None) -> None:
    store.register_worker(name, host=name, now=at)


# --- RunRecord + derive_state -----------------------------------------------------


def test_run_record_carries_worker_and_defaults_to_none() -> None:
    bare = RunRecord(
        run_id="r", repo="/r", db="/r/db", scope="s", pid=1, status="IN_PROGRESS",
        started_at="2026-10-02T10:00:00+00:00",
    )
    assert bare.worker is None
    assert RunRecord.from_dict(_record().to_dict()).worker == "m3max"
    # A registry.json written before this story has no `worker` key at all.
    legacy = {k: v for k, v in _record().to_dict().items() if k != "worker"}
    assert RunRecord.from_dict(legacy).worker is None


def test_a_remote_run_is_judged_by_its_workers_heartbeat_not_a_local_pid() -> None:
    # pid 1 is alive here; the record's pid names a process on another machine.
    record = _record(pid=1)
    assert derive_state(record, remote=True, worker_online=True) == "IN_PROGRESS"
    assert derive_state(record, remote=True, worker_online=False) == "DEAD"


def test_a_remote_run_with_an_unknown_worker_keeps_its_recorded_status() -> None:
    # No heartbeat to judge by (a host that never ran `queue run --worker`): say
    # what the record says rather than guess DEAD.
    assert derive_state(_record(), remote=True, worker_online=None) == "IN_PROGRESS"


def test_a_finished_remote_run_keeps_its_terminal_status_even_offline() -> None:
    done = _record(status="DONE", finished_at="2026-10-02T11:00:00+00:00")
    assert derive_state(done, remote=True, worker_online=False) == "DONE"


def test_a_local_record_still_uses_the_pid() -> None:
    assert derive_state(_record(pid=2**22 + 12345)) == "DEAD"


# --- the service store ------------------------------------------------------------


def test_the_store_upserts_a_run_by_id_and_lists_it(store: QueueStore) -> None:
    store.put_fleet_run(_record(completed=1))
    store.put_fleet_run(_record(completed=3))
    rows = store.list_fleet_runs()
    assert [(r["run_id"], r["completed"], r["worker"]) for r in rows] == [("run-1", 3, "m3max")]
    assert rows[0]["updated_at"]


def test_the_store_requires_a_worker(store: QueueStore) -> None:
    with pytest.raises(QueueError, match="worker"):
        store.put_fleet_run(_record(worker=None))
    with pytest.raises(QueueError, match="worker"):
        store.put_fleet_run(_record(worker="  "))


def test_a_late_in_progress_push_cannot_reopen_a_finished_run(store: QueueStore) -> None:
    store.put_fleet_run(
        _record(status="DONE", finished_at="2026-10-02T11:00:00+00:00", completed=5)
    )
    store.put_fleet_run(_record(status="IN_PROGRESS", completed=4))  # a stale heartbeat push
    (row,) = store.list_fleet_runs()
    assert (row["status"], row["completed"]) == ("DONE", 5)
    assert row["finished_at"]


def test_a_queue_db_written_before_the_fleet_registry_upgrades_in_place(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    old = QueueStore(path)
    old.init()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE fleet_runs")
        conn.execute("DELETE FROM _migrations WHERE name = 'fleet_runs'")
    assert old.list_fleet_runs() == []  # no table yet: nothing has pushed
    old.ensure_migrated()
    old.put_fleet_run(_record())
    assert len(old.list_fleet_runs()) == 1


def test_a_missing_queue_db_lists_no_fleet_runs(tmp_path: Path) -> None:
    assert QueueStore(tmp_path / "never.db").list_fleet_runs() == []


# --- the /runs routes + client -----------------------------------------------------


def test_put_runs_records_the_run_and_get_runs_returns_it(live: _Live, store: QueueStore) -> None:
    client = QueueClient(live.url, token=TOKEN)
    client.put_fleet_run(_record())
    rows = client.list_fleet_runs()
    assert [(r["run_id"], r["worker"], r["total"]) for r in rows] == [("run-1", "m3max", 5)]
    assert [r["run_id"] for r in store.list_fleet_runs()] == ["run-1"]


def test_get_runs_flags_whether_each_runs_worker_is_online(live: _Live, store: QueueStore) -> None:
    now = datetime.now(timezone.utc)
    _beat(store, "m3max", at=now)
    _beat(store, "xps", at=now - timedelta(minutes=10))
    client = QueueClient(live.url, token=TOKEN)
    client.put_fleet_run(_record("a", worker="m3max"))
    client.put_fleet_run(_record("b", worker="xps"))
    client.put_fleet_run(_record("c", worker="laptop"))  # never registered as a worker
    online = {r["run_id"]: r["worker_online"] for r in client.list_fleet_runs()}
    assert online == {"a": True, "b": False, "c": None}


def test_put_runs_rejects_bad_input_with_400(live: _Live) -> None:
    def put(body: dict) -> int:
        req = urllib.request.Request(
            live.url + "/runs", data=json.dumps(body).encode(), method="PUT",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    good = _record().to_dict()
    assert put(good) == 200
    assert put({k: v for k, v in good.items() if k != "worker"}) == 400
    assert put({**good, "run_id": ""}) == 400
    assert put({**good, "total": "many"}) == 400
    assert put({**good, "pid": True}) == 400


def test_a_malformed_run_list_is_reported_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = QueueClient("http://fleet.invalid", token=TOKEN)
    monkeypatch.setattr(client, "_call", lambda *a, **k: {"runs": "not-a-list"})
    with pytest.raises(QueueUnavailable, match="malformed run list"):
        client.list_fleet_runs()
    monkeypatch.setattr(client, "_call", lambda *a, **k: {"runs": [{"run_id": "a"}, 7]})
    assert client.list_fleet_runs() == [{"run_id": "a"}]


def test_put_runs_turns_a_store_rejection_into_400(
    live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(record: RunRecord) -> None:
        raise QueueError("refused")

    monkeypatch.setattr(store, "put_fleet_run", refuse)
    req = urllib.request.Request(
        live.url + "/runs", data=json.dumps(_record().to_dict()).encode(), method="PUT",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 400


def test_the_client_replays_a_put_once_because_it_is_an_idempotent_upsert(live: _Live) -> None:
    # PUT is an idempotent upsert, so the one retry is safe for it.
    attempts = []

    def flaky(req, timeout):
        attempts.append(req.get_method())
        if len(attempts) == 1:
            raise ConnectionResetError("reset")
        raise AssertionError("second attempt is checked below")

    client = QueueClient(live.url, token=TOKEN, opener=flaky)
    with pytest.raises(AssertionError):
        client.put_fleet_run(_record())
    assert attempts == ["PUT", "PUT"]


# --- push on register / finish ----------------------------------------------------


def test_push_is_a_noop_without_a_fleet_url(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []
    monkeypatch.setattr(QueueClient, "put_fleet_run", lambda self, record: called.append(record))
    push_fleet_run(_record())
    assert called == []


def test_push_names_the_worker_from_the_environment(
    live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fleet_env(monkeypatch, live.url)
    monkeypatch.setenv(WORKER_ENV, "xps")
    push_fleet_run(_record(worker=None))
    assert [r["worker"] for r in store.list_fleet_runs()] == ["xps"]


def test_push_falls_back_to_the_short_hostname(
    live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fleet_env(monkeypatch, live.url)
    monkeypatch.delenv(WORKER_ENV, raising=False)
    monkeypatch.setattr(qc.socket, "gethostname", lambda: "omarchy-xps13.tailnet.ts.net")
    push_fleet_run(_record(worker=None))
    assert [r["worker"] for r in store.list_fleet_runs()] == ["omarchy-xps13"]


def test_push_never_raises_when_the_service_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    _fleet_env(monkeypatch, _dead_url())
    push_fleet_run(_record())  # must not raise: the local file stays authoritative


def test_push_never_raises_on_a_malformed_fleet_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", "not-a-url")
    push_fleet_run(_record())


def test_register_writes_locally_and_pushes_with_the_worker(
    tmp_path: Path, live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sdlc.build import _registry_register

    _fleet_env(monkeypatch, live.url)
    monkeypatch.setenv(WORKER_ENV, "m3max")
    registry = Registry(tmp_path / "registry.json")
    _registry_register(registry, "run-9", "epic-9", tmp_path / "l.db", 4, repo=tmp_path)

    (local,) = registry.records()
    assert local.worker == "m3max"
    (remote,) = store.list_fleet_runs()
    assert (remote["run_id"], remote["worker"], remote["total"]) == ("run-9", "m3max", 4)


def test_finish_pushes_the_terminal_record(
    tmp_path: Path, live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sdlc.build import _registry_finish, _registry_register

    _fleet_env(monkeypatch, live.url)
    monkeypatch.setenv(WORKER_ENV, "m3max")
    registry = Registry(tmp_path / "registry.json")
    _registry_register(registry, "run-9", "epic-9", tmp_path / "l.db", 4, repo=tmp_path)
    _registry_finish(registry, "run-9", "DONE", 4)

    (remote,) = store.list_fleet_runs()
    assert (remote["status"], remote["completed"]) == ("DONE", 4)
    assert remote["finished_at"]


def test_a_down_service_does_not_fail_register_or_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sdlc.build import _registry_finish, _registry_register

    _fleet_env(monkeypatch, _dead_url())
    registry = Registry(tmp_path / "registry.json")
    _registry_register(registry, "run-9", "epic-9", tmp_path / "l.db", 4, repo=tmp_path)
    _registry_finish(registry, "run-9", "DONE", 4)
    (local,) = registry.records()
    assert (local.status, local.completed) == ("DONE", 4)


def test_without_a_fleet_the_local_record_has_no_worker(tmp_path: Path) -> None:
    from sdlc.build import _registry_register

    registry = Registry(tmp_path / "registry.json")
    _registry_register(registry, "run-9", "epic-9", tmp_path / "l.db", 4, repo=tmp_path)
    assert registry.records()[0].worker is None


# --- the worker's heartbeat refreshes the counts ----------------------------------


def test_the_workers_heartbeat_pushes_each_in_flight_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sdlc.queue_worker import WorkerProfile
    from sdlc.scheduler import SchedulerConfig, run_queue

    pushed: list[RunRecord] = []
    monkeypatch.setattr("sdlc.scheduler.push_fleet_run", pushed.append)
    local = QueueStore(tmp_path / "queue.db")
    local.init()
    repo = tmp_path / "alpha"
    repo.mkdir()
    local.add_job(repo=str(repo.resolve()), kind="fix", scope="1")
    registry = Registry(tmp_path / "registry.json")
    # What the job subprocess registers: pid 90001 is FakeLauncher's first child.
    registry.register(
        _record("run-live", repo=str(repo.resolve()), pid=90001, worker=None, completed=2)
    )

    class Slow:
        pid = 90001
        polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls < 70 else 0

        def stop(self) -> None: ...

    now = [datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)]

    def advance(seconds: float) -> None:
        now[0] += timedelta(seconds=seconds)

    def clean(_root) -> Finding:
        return Finding("install", "Installed controller vs checkout", "CLEAN", "matches")

    run_queue(
        local,
        config=SchedulerConfig(
            slots=1, poll_seconds=1.0,
            worker=WorkerProfile(name="m3max", host="h", pools=["p"], harnesses=["claude"]),
        ),
        registry=registry,
        launcher=lambda argv, cwd: Slow(),
        clock=lambda: now[0],
        sleeper=advance,
        notifier=lambda *a, **k: None,
        version_check=clean,
        echo=lambda _line: None,
        identity="m3max",
    )

    assert pushed, "a heartbeat while the run was in flight should have pushed it"
    assert {(r.run_id, r.worker) for r in pushed} == {("run-live", "m3max")}
    assert pushed[0].completed == 2


def test_the_default_launcher_hands_a_job_its_workers_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sdlc.queue_worker import WorkerProfile
    from sdlc.scheduler import SchedulerConfig, _launcher_for

    seen: dict = {}

    def fake_popen(argv, cwd, start_new_session, env):
        seen["env"] = env

        class _P:
            pid = 1

        return _P()

    monkeypatch.setattr("sdlc.scheduler.subprocess.Popen", fake_popen)
    cfg = SchedulerConfig(worker=WorkerProfile(name="m3max", host="h"))
    _launcher_for(cfg)(["true"], tmp_path)
    assert seen["env"][WORKER_ENV] == "m3max"

    _launcher_for(SchedulerConfig())(["true"], tmp_path)
    assert seen["env"] is None  # a plain drain leaves the environment alone


# --- the dashboard: merge, dedupe, remote state ----------------------------------


def _local_registry(tmp_path: Path, *records: RunRecord) -> Registry:
    registry = Registry(tmp_path / "registry.json")
    for record in records:
        registry.register(record)
    return registry


def _remote_row(run_id: str = "remote-1", **overrides) -> dict:
    row = {**_record(run_id).to_dict(), "updated_at": "2026-10-02T10:05:00+00:00",
           "worker_online": True}
    row.update(overrides)
    return row


def test_the_view_merges_remote_rows_and_every_row_carries_its_worker(tmp_path: Path) -> None:
    local = _record("local-1", worker=None, pid=1, repo="/xps/beta")
    registry = _local_registry(tmp_path, local)
    rows = _registry_runs_view(registry, fleet_rows=[_remote_row()])
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"local-1", "remote-1"}
    assert by_id["local-1"]["worker"] is None
    assert by_id["remote-1"]["worker"] == "m3max"
    assert by_id["remote-1"]["repo"] == "/Users/fx/Work/alpha"
    assert (by_id["remote-1"]["done"], by_id["remote-1"]["total"]) == (1, 5)
    assert by_id["remote-1"]["status"] == "IN_PROGRESS"


def test_a_run_in_both_registries_appears_once_and_the_local_record_wins(tmp_path: Path) -> None:
    local = _record("shared", worker="xps", pid=1, completed=3)
    registry = _local_registry(tmp_path, local)
    rows = _registry_runs_view(
        registry, fleet_rows=[_remote_row("shared", completed=1, worker_online=False)]
    )
    assert [r["id"] for r in rows] == ["shared"]
    assert rows[0]["worker"] == "xps"
    assert rows[0]["status"] == "IN_PROGRESS"  # local pid 1 is alive; heartbeat is not consulted


def test_a_remote_runs_state_comes_from_its_workers_heartbeat(tmp_path: Path) -> None:
    registry = _local_registry(tmp_path)
    rows = _registry_runs_view(
        registry,
        fleet_rows=[
            _remote_row("up", worker_online=True),
            _remote_row("down", worker_online=False),
            _remote_row("done", status="DONE", finished_at="2026-10-02T11:00:00+00:00",
                        worker_online=False),
        ],
    )
    assert {r["id"]: r["status"] for r in rows} == {
        "up": "IN_PROGRESS", "down": "DEAD", "done": "DONE",
    }


def test_a_garbled_remote_row_is_skipped_not_fatal(tmp_path: Path) -> None:
    registry = _local_registry(tmp_path)
    rows = _registry_runs_view(registry, fleet_rows=[{"run_id": "x"}, _remote_row()])
    assert [r["id"] for r in rows] == ["remote-1"]


def test_remote_rows_sort_with_local_ones_newest_first(tmp_path: Path) -> None:
    registry = _local_registry(
        tmp_path, _record("old", started_at="2026-10-01T10:00:00+00:00", pid=1, worker=None)
    )
    rows = _registry_runs_view(
        registry, fleet_rows=[_remote_row("new", started_at="2026-10-02T10:00:00+00:00")]
    )
    assert [r["id"] for r in rows] == ["new", "old"]


@contextmanager
def _dashboard(registry: Registry):
    server = make_server(db_path=None, host="127.0.0.1", port=0, registry=registry)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - localhost test
        return json.loads(resp.read())


def test_api_runs_serves_the_merged_fleet_view(
    tmp_path: Path, live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fleet_env(monkeypatch, live.url)
    _beat(store, "m3max")
    store.put_fleet_run(_record("remote-1"))
    registry = _local_registry(tmp_path, _record("local-1", worker=None, pid=1, repo="/xps/beta"))
    with _dashboard(registry) as base:
        rows = _get_json(base + "/api/runs")
        fleet = _get_json(base + "/api/fleet")
    assert {r["id"]: r["worker"] for r in rows} == {"local-1": None, "remote-1": "m3max"}
    assert fleet["configured"] is True and fleet["available"] is True and fleet["error"] is None


def test_api_runs_without_a_fleet_is_the_local_view_and_fleet_is_unconfigured(
    tmp_path: Path,
) -> None:
    registry = _local_registry(tmp_path, _record("local-1", worker=None, pid=1))
    with _dashboard(registry) as base:
        rows = _get_json(base + "/api/runs")
        fleet = _get_json(base + "/api/fleet")
    assert [r["id"] for r in rows] == ["local-1"]
    assert fleet == {"configured": False, "available": True, "error": None}


def test_an_unreachable_service_still_renders_local_runs_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fleet_env(monkeypatch, _dead_url())
    registry = _local_registry(tmp_path, _record("local-1", worker=None, pid=1))
    with _dashboard(registry) as base:
        rows = _get_json(base + "/api/runs")
        fleet = _get_json(base + "/api/fleet")
    assert [r["id"] for r in rows] == ["local-1"]
    assert fleet["configured"] is True and fleet["available"] is False
    assert "unreachable" in fleet["error"]


def test_a_malformed_fleet_url_degrades_instead_of_failing_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", "not-a-url")
    registry = _local_registry(tmp_path, _record("local-1", worker=None, pid=1))
    with _dashboard(registry) as base:
        assert [r["id"] for r in _get_json(base + "/api/runs")] == ["local-1"]
        assert _get_json(base + "/api/fleet")["available"] is False


def test_the_fleet_is_fetched_once_per_tick_not_once_per_endpoint(
    tmp_path: Path, live: _Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fleet_env(monkeypatch, live.url)
    calls = []
    original = QueueClient.list_fleet_runs
    monkeypatch.setattr(
        QueueClient, "list_fleet_runs", lambda self: calls.append(1) or original(self)
    )
    with _dashboard(_local_registry(tmp_path)) as base:
        _get_json(base + "/api/runs")
        _get_json(base + "/api/fleet")
    assert len(calls) == 1


def test_selecting_a_remote_run_returns_a_snapshot_built_from_the_pushed_record(
    tmp_path: Path, live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fleet_env(monkeypatch, live.url)
    _beat(store, "m3max")
    store.put_fleet_run(_record("remote-1", completed=2, total=5))
    with _dashboard(_local_registry(tmp_path)) as base:
        snap = _get_json(base + "/api/status?run=remote-1")
    assert snap["run"]["id"] == "remote-1"
    assert snap["run"]["worker"] == "m3max"
    assert snap["run"]["status"] == "IN_PROGRESS"
    assert (snap["counts"]["done"], snap["counts"]["total"]) == (2, 5)
    assert snap["stories"] == []  # the worker's ledger is not reachable from here


def test_the_change_token_moves_when_a_remote_run_advances(
    tmp_path: Path, live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fleet_env(monkeypatch, live.url)
    _beat(store, "m3max")
    server = make_server(db_path=None, host="127.0.0.1", port=0, registry=_local_registry(tmp_path))
    try:
        store.put_fleet_run(_record("remote-1", completed=1))
        before = _change_token(server)
        server.fleet.invalidate()
        store.put_fleet_run(_record("remote-1", completed=2))
        assert _change_token(server) != before
    finally:
        server.server_close()


# --- the sidebar string contract --------------------------------------------------


def test_the_sidebar_shows_the_worker_beside_the_repo() -> None:
    start = _PAGE.index("function renderRuns(")
    body = _PAGE[start:_PAGE.index("\n}", start)]
    # `📁 repo @ worker`, worker escaped, and only when the row carries one.
    assert "r.worker" in body
    assert "\" @ \" + esc(r.worker)" in body


def test_the_page_polls_the_fleet_and_shows_a_muted_unavailable_line() -> None:
    tick = _PAGE[_PAGE.index("async function tick()"):]
    assert 'fetch("/api/fleet"' in tick[: tick.index("\n}")]
    fn = _PAGE[_PAGE.index("function renderFleet("):]
    fn = fn[: fn.index("\n}")]
    assert "fleet unavailable" in fn
    assert "id='fleetNote'" in _PAGE or 'id="fleetNote"' in _PAGE
    assert ".fleetnote" in _PAGE  # muted styling, the GitHub-panel precedent
