# ABOUTME: Tests for the fleet registry (Story 35.4-001) — workers push run records to the
# ABOUTME: service's /runs, the XPS dashboard merges them, and degrades when it is unreachable.

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import replace
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
# `shutdown()` blocks until `serve_forever` next polls its stop flag; the
# stdlib's 0.5s default idled every live-server test half a second.
_FAST_SHUTDOWN = {"poll_interval": 0.01}


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
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs=_FAST_SHUTDOWN, daemon=True)
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
    # A stale heartbeat push: it read the record before the finish, so it carries
    # the finished run's own pid.
    store.put_fleet_run(_record(status="IN_PROGRESS", completed=4))
    (row,) = store.list_fleet_runs()
    assert (row["status"], row["completed"]) == ("DONE", 5)
    assert row["finished_at"]


def test_a_resume_reopens_a_finished_run_and_its_later_pushes_land(store: QueueStore) -> None:
    # `sdlc resume` re-registers the same run id from a new process (resume.py), so
    # a different pid is a resume, not a stale heartbeat — the row must reopen.
    store.put_fleet_run(_record(completed=1))
    store.put_fleet_run(
        _record(status="FAILED", finished_at="2026-10-02T11:00:00+00:00", completed=2)
    )
    store.put_fleet_run(_record(pid=5151, completed=3))
    (row,) = store.list_fleet_runs()
    assert (row["status"], row["completed"], row["pid"]) == ("IN_PROGRESS", 3, 5151)
    assert row["finished_at"] is None
    store.put_fleet_run(_record(pid=5151, completed=4))  # the resumed run's heartbeat
    (row,) = store.list_fleet_runs()
    assert (row["status"], row["completed"]) == ("IN_PROGRESS", 4)


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


@pytest.mark.parametrize("url", ["not-a-url", "http://[::1"])
def test_push_never_raises_on_a_malformed_fleet_url(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", url)
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


def test_resuming_a_finished_run_reopens_it_on_the_fleet(
    tmp_path: Path, live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sdlc.build import _registry_finish, _registry_register

    _fleet_env(monkeypatch, live.url)
    monkeypatch.setenv(WORKER_ENV, "m3max")
    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setattr("sdlc.build.os.getpid", lambda: 4242)
    _registry_register(registry, "run-9", "epic-9", tmp_path / "l.db", 4, repo=tmp_path)
    _registry_finish(registry, "run-9", "AWAITING_APPROVAL", 2)
    # `sdlc resume` is a new process re-registering the same run id (resume.py).
    monkeypatch.setattr("sdlc.build.os.getpid", lambda: 5151)
    _registry_register(
        registry, "run-9", "epic-9", tmp_path / "l.db", 4, repo=tmp_path, completed=2
    )

    (remote,) = store.list_fleet_runs()
    assert (remote["status"], remote["pid"], remote["finished_at"]) == ("IN_PROGRESS", 5151, None)


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


def test_a_malformed_fleet_url_never_costs_a_build_its_local_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sdlc.build import _registry_register

    # urlsplit raises a bare ValueError on an unclosed IPv6 bracket; a plain build
    # only reads the fleet config to name itself, so it must shrug that off.
    monkeypatch.setenv("SDLC_QUEUE_URL", "http://[::1")
    registry = Registry(tmp_path / "registry.json")
    _registry_register(registry, "run-9", "epic-9", tmp_path / "l.db", 4, repo=tmp_path)
    (local,) = registry.records()
    assert (local.run_id, local.worker) == ("run-9", None)


def test_an_unreadable_cwd_never_costs_a_build_its_local_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sdlc.build import _registry_register

    def deleted(cls) -> Path:
        raise FileNotFoundError("the working directory was deleted")

    registry = Registry(tmp_path / "registry.json")
    monkeypatch.setattr(Path, "cwd", classmethod(deleted))
    # With `repo` given the cwd only feeds the fleet lookup: the run still registers.
    _registry_register(registry, "run-9", "epic-9", tmp_path / "l.db", 4, repo=tmp_path)
    # Without it the record cannot be placed — skipped, never a failed build.
    _registry_register(registry, "run-8", "epic-8", tmp_path / "l.db", 4)
    assert [r.run_id for r in registry.records()] == ["run-9"]


def test_without_a_fleet_the_local_record_has_no_worker(tmp_path: Path) -> None:
    from sdlc.build import _registry_register

    registry = Registry(tmp_path / "registry.json")
    _registry_register(registry, "run-9", "epic-9", tmp_path / "l.db", 4, repo=tmp_path)
    assert registry.records()[0].worker is None


# --- a worker writes its runs into the fleet table it serves ---------------------
# `queue run --worker` refuses a fleet URL, so a worker drains the queue its own
# host owns — the store `sdlc queue serve` publishes — and its jobs inherit no URL
# to push to. These drive a real `run_queue` and read that store, never a stub.

# Above both Linux's and macOS's pid_max, so never a live process: a job that has
# exited, as `derive_state` sees it.
_GONE_PID = 2**22 + 12345


class _Job:
    """A worker-launched job's process: in flight for ``polls`` passes, then exits ``code``."""

    def __init__(self, *, polls: int, code: int = 0, during=None) -> None:
        self.pid = _GONE_PID
        self._polls = polls
        self._code = code
        self._during = during  # run on each in-flight pass, to look at the store mid-run
        self.stopped = False

    def poll(self) -> int | None:
        if self.stopped:
            return -9
        if self._polls <= 0:
            return self._code
        self._polls -= 1
        if self._during is not None:
            self._during()
        return None

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def worker_host(tmp_path: Path):
    """The serving host's queue with one job, and the run record its job registers."""
    local = QueueStore(tmp_path / "queue.db")
    local.init()
    repo = str((tmp_path / "alpha").resolve())
    Path(repo).mkdir()
    local.add_job(repo=repo, kind="fix", scope="1")
    registry = Registry(tmp_path / "registry.json")
    # What the job's own `_registry_register` writes: its pid is the child's.
    registry.register(
        _record("run-w", repo=repo, db=str(tmp_path / "ledger.db"), pid=_GONE_PID,
                worker=None, completed=2)
    )
    return local, registry


def _drain_as_worker(
    local: QueueStore, registry: Registry, job: _Job, *,
    worker: bool = True, poll_seconds: float = 1.0, sleeper=None, echo=None,
    dashboard_url: str | None = None,
) -> None:
    from sdlc.queue_worker import WorkerProfile
    from sdlc.scheduler import SchedulerConfig, run_queue

    now = [datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)]

    def advance(seconds: float) -> None:
        now[0] += timedelta(seconds=seconds)

    def clean(_root) -> Finding:
        return Finding("install", "Installed controller vs checkout", "CLEAN", "matches")

    profile = WorkerProfile(
        name="m3max", host="h", pools=["p"], harnesses=["claude"], dashboard_url=dashboard_url
    )
    run_queue(
        local,
        config=SchedulerConfig(
            slots=1, poll_seconds=poll_seconds, worker=profile if worker else None
        ),
        registry=registry,
        launcher=lambda argv, cwd: job,
        clock=lambda: now[0],
        sleeper=sleeper or advance,
        notifier=lambda *a, **k: None,
        version_check=clean,
        echo=echo or (lambda _line: None),
        identity="m3max",
    )


def _fleet(local: QueueStore) -> list[tuple]:
    return [
        (r["run_id"], r["worker"], r["status"], r["completed"], r["finished_at"])
        for r in local.list_fleet_runs()
    ]


def test_a_workers_run_is_on_the_fleet_from_its_start_and_each_beat_refreshes_it(
    worker_host,
) -> None:
    local, registry = worker_host
    seen: list[list[tuple]] = []

    def look() -> None:
        seen.append(_fleet(local))
        if len(seen) == 10:
            # The run advances; with no ledger here the registry counts stand in.
            registry.register(replace(registry.records()[0], completed=3))

    _drain_as_worker(local, registry, _Job(polls=40, during=look))

    # Pushed when the scheduler linked the run — long before the first 30 s beat.
    assert seen[0] == [("run-w", "m3max", "IN_PROGRESS", 2, None)]
    assert seen[25] == seen[0]  # no beat yet: nothing new to say
    # The 30 s heartbeat re-pushed it with the counts read then.
    assert seen[-1] == [("run-w", "m3max", "IN_PROGRESS", 3, None)]


def test_a_workers_run_row_carries_the_dashboard_url_it_advertises(worker_host) -> None:
    # Story 35.4-002: the XPS reads a remote run's transcripts from this origin.
    local, registry = worker_host
    url = "http://m3max.tail1234.ts.net:8787"
    _drain_as_worker(local, registry, _Job(polls=1), dashboard_url=url)
    assert [r["dashboard_url"] for r in local.list_fleet_runs()] == [url]


@pytest.mark.parametrize(
    ("finishes", "code", "status"),
    [(True, 0, "DONE"), (False, 137, "DEAD")],
    ids=["finished", "killed"],
)
def test_when_a_workers_job_exits_the_fleet_row_says_what_its_own_dashboard_says(
    worker_host, finishes: bool, code: int, status: str
) -> None:
    local, registry = worker_host

    def finish() -> None:
        if finishes:  # the job's own `_registry_finish`, before it exits
            registry.mark_finished("run-w", "DONE", completed=5)

    _drain_as_worker(local, registry, _Job(polls=3, code=code, during=finish))

    (record,) = registry.records()
    assert derive_state(record) == status  # what the worker's own dashboard shows
    ((run_id, worker, pushed, completed, finished_at),) = _fleet(local)
    assert (run_id, worker, pushed) == ("run-w", "m3max", status)
    assert (completed, bool(finished_at)) == ((5, True) if finishes else (2, False))


def test_a_job_the_budget_stops_reads_dead_on_the_fleet(worker_host) -> None:
    local, registry = worker_host
    (queued,) = local.list_jobs()
    job = _Job(polls=10**6)

    # One pass longer than the wall-clock cap trips the breaker on the next one.
    _drain_as_worker(
        local, registry, job, poll_seconds=queued.job_budget().wall_clock_seconds + 1
    )

    assert job.stopped and local.get_job(queued.id).state == "needs_attention"
    assert [(r[0], r[2]) for r in _fleet(local)] == [("run-w", "DEAD")]


def test_interrupting_a_worker_marks_the_runs_it_stopped_dead_on_the_fleet(
    worker_host,
) -> None:
    local, registry = worker_host
    job = _Job(polls=10**6)
    passes = [0]

    def ctrl_c_on_the_third_pass(_seconds: float) -> None:
        passes[0] += 1
        if passes[0] == 3:
            raise KeyboardInterrupt

    _drain_as_worker(local, registry, job, sleeper=ctrl_c_on_the_third_pass)

    assert job.stopped
    assert [(r[0], r[2]) for r in _fleet(local)] == [("run-w", "DEAD")]


def test_a_job_the_operator_cancels_reads_dead_on_the_fleet(worker_host) -> None:
    local, registry = worker_host
    (queued,) = local.list_jobs()
    seen: list[list[tuple]] = []

    def cancel_from_the_xps() -> None:
        seen.append(_fleet(local))
        if len(seen) == 3:  # `sdlc queue cancel` on the running job (Story 35.4-003)
            local.cancel_job(queued.id)

    job = _Job(polls=10**6, during=cancel_from_the_xps)
    _drain_as_worker(local, registry, job)

    assert job.stopped and local.get_job(queued.id).state == "cancelled"
    assert seen[0] == [("run-w", "m3max", "IN_PROGRESS", 2, None)]
    # Its worker stays online, so a row left IN_PROGRESS would read live for good.
    assert [(r[0], r[2]) for r in _fleet(local)] == [("run-w", "DEAD")]


def test_a_resume_reaches_the_fleet_when_it_re_registers_not_as_the_stale_record(
    tmp_path: Path,
) -> None:
    local = QueueStore(tmp_path / "queue.db")
    local.init()
    repo = str((tmp_path / "alpha").resolve())
    Path(repo).mkdir()
    job_id = local.add_job(repo=repo, kind="build", scope="epic-1")
    # A killed scheduler's claim, long lapsed by the time this worker starts.
    local.claim_job(
        job_id, claimed_by="dead:1", lease_seconds=90,
        now=datetime(2026, 10, 2, 11, 55, 0, tzinfo=timezone.utc),
    )
    local.attach_run(job_id, "run-w")
    registry = Registry(tmp_path / "registry.json")
    # The previous launch's record: finished, its ledger unreadable, so it resumes.
    stale = _record("run-w", repo=repo, db=str(tmp_path / "ledger.db"), pid=_GONE_PID,
                    worker=None, status="FAILED", finished_at="2026-10-02T11:50:00+00:00")
    registry.register(stale)
    seen: list[list[tuple]] = []

    def look() -> None:
        seen.append(_fleet(local))
        if len(seen) == 40:  # `sdlc resume` re-registers under its own pid
            registry.register(
                replace(stale, pid=_GONE_PID + 1, status="IN_PROGRESS", finished_at=None,
                        completed=3)
            )

    _drain_as_worker(local, registry, _Job(polls=80, during=look))

    assert seen[35] == []  # the 30 s beat passed over the stale terminal record
    assert seen[-1] == [("run-w", "m3max", "IN_PROGRESS", 3, None)]  # the 60 s beat


def test_a_failed_fleet_write_is_logged_and_never_stalls_the_drain(
    worker_host, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    local, registry = worker_host

    def locked(record: RunRecord, *, now=None) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(local, "put_fleet_run", locked)
    lines: list[str] = []

    def finish() -> None:
        registry.mark_finished("run-w", "DONE", completed=5)

    _drain_as_worker(local, registry, _Job(polls=3, during=finish), echo=lines.append)

    # The fleet view is a mirror: the job itself still finished as usual.
    assert [job.state for job in local.list_jobs()] == ["done"]
    assert any(
        "fleet view: could not record run run-w: database is locked" in line for line in lines
    )


def test_a_plain_drain_is_no_fleet_worker_and_writes_no_fleet_rows(worker_host) -> None:
    local, registry = worker_host
    _drain_as_worker(local, registry, _Job(polls=3, code=137), worker=False)
    assert local.list_fleet_runs() == []


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
    thread = threading.Thread(target=server.serve_forever, kwargs=_FAST_SHUTDOWN, daemon=True)
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


@pytest.mark.parametrize("url", ["not-a-url", "http://[::1"])
def test_a_malformed_fleet_url_degrades_instead_of_failing_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", url)
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


def test_a_failed_fleet_fetch_backs_off_so_a_dead_peer_does_not_stall_every_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sdlc.dashboard import _FleetView

    # A blackholed tailnet peer costs a full client timeout per fetch, under the
    # view's lock: a failure must be cached longer than a success, not re-tried
    # on the very next tick.
    now = [0.0]
    fetches: list[dict] = []
    outcome = [{"configured": True, "available": False, "error": "down", "runs": []}]
    monkeypatch.setattr(
        _FleetView, "_fetch", staticmethod(lambda: fetches.append(outcome[0]) or outcome[0])
    )
    view = _FleetView(ttl=2.0, failure_ttl=30.0, clock=lambda: now[0])

    view.snapshot()
    now[0] = 5.0
    view.snapshot()
    assert len(fetches) == 1  # still inside the failure back-off
    now[0] = 31.0
    outcome[0] = {"configured": True, "available": True, "error": None, "runs": []}
    view.snapshot()
    assert len(fetches) == 2  # back-off over: the service is asked again
    now[0] = 34.0
    view.snapshot()
    assert len(fetches) == 3  # a healthy fleet keeps the short tick cache


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
