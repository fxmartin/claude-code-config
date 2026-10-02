# ABOUTME: `sdlc queue run --worker` against a live `sdlc queue serve` through QueueClient (Story 35.2-005).
# ABOUTME: Claim -> run -> finish, parks, lease lapse + reclaim, outages — plus local mode unchanged.

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sdlc.approval import ApprovalVerdict
from sdlc.doctor import Finding
from sdlc.ledger_view import Ledger
from sdlc.queue import QueueStore
from sdlc.queue_client import QueueClient, QueueUnavailable
from sdlc.queue_server import AccessPolicy, make_server
from sdlc.queue_worker import PreparedRepo, WorkerProfile
from sdlc.registry import Registry, RunRecord
from sdlc.scheduler import SchedulerConfig, run_queue

TOKEN = "s3cret-token"
T0 = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    """One clock shared by the service and the workers, so a lease lapses on demand."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeProc:
    def __init__(self, pid: int, *, alive_polls: int = 1, code: int = 0, on_exit=None) -> None:
        self.pid = pid
        self._alive = alive_polls
        self._code = code
        self._on_exit = on_exit
        self.stopped = False

    def poll(self) -> int | None:
        if self._alive > 0:
            self._alive -= 1
            return None
        if self._on_exit is not None:
            self._on_exit()
            self._on_exit = None
        return self._code

    def stop(self) -> None:
        self.stopped = True
        self._alive = 0


class FakeLauncher:
    """Launches fake `sdlc build`s that register a run and finish it ``DONE``."""

    def __init__(self, registry: Registry, *, alive_polls: int = 2, finish: bool = True) -> None:
        self.registry = registry
        self.calls: list[tuple[list[str], str]] = []
        self.procs: list[FakeProc] = []
        self._alive = alive_polls
        self._finish = finish
        self._pid = 90000

    def __call__(self, argv, cwd):
        self._pid += 1
        pid = self._pid
        self.calls.append((list(argv), str(cwd)))
        run_id = f"run-{len(self.calls)}"
        self.registry.register(
            RunRecord(
                run_id=run_id, repo=str(cwd), db=str(Path(cwd) / ".sdlc-state.db"),
                scope="1.1-001", pid=pid, status="IN_PROGRESS", started_at=T0.isoformat(),
            )
        )
        proc = FakeProc(
            pid,
            alive_polls=self._alive,
            on_exit=(lambda: self.registry.mark_finished(run_id, "DONE", completed=1))
            if self._finish
            else None,
        )
        self.procs.append(proc)
        return proc


class LoggingClient(QueueClient):
    """A QueueClient that remembers every route it called."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.routes: list[tuple[str, str]] = []

    def _request(self, method, path, body=None):
        self.routes.append((method, path.split("?")[0]))
        return super()._request(method, path, body)


class Flaky(LoggingClient):
    """A client whose network can be cut: every request raises while ``down``."""

    down = False

    def _request(self, method, path, body=None):
        if self.down:
            raise QueueUnavailable(f"fleet queue {self.url} unreachable: network is down")
        return super()._request(method, path, body)


class Live:
    def __init__(self, store: QueueStore, clock: Clock) -> None:
        policy = AccessPolicy(token=TOKEN, networks=("127.0.0.0/8",))
        self.server = make_server(store, policy, "127.0.0.1", 0, clock=clock)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=10)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(tmp_path: Path, clock: Clock) -> QueueStore:
    store = QueueStore(tmp_path / "service" / "queue.db")
    (tmp_path / "service").mkdir()
    store.init()
    live = Live(store, clock)
    store.url = live.url  # type: ignore[attr-defined]
    yield store
    live.stop()


def _client(service: QueueStore, cls=LoggingClient):
    return cls(service.url, token=TOKEN)  # type: ignore[attr-defined]


def _clean(_root) -> Finding:
    return Finding("install", "Installed controller vs checkout", "CLEAN", "matches")


def _repo(tmp_path: Path, name: str = "alpha") -> str:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _profile(name: str = "m3max", **overrides) -> WorkerProfile:
    fields = {
        "name": name, "host": f"{name}-host", "pools": ["claude-m3"],
        "harnesses": ["claude"], "sandbox": None, "repos": [],
        "dashboard_url": "http://m3max.tailnet:8787",
    }
    fields.update(overrides)
    return WorkerProfile(**fields)


def _pass_through_sync(job, *, busy_repos, keepalive) -> PreparedRepo:
    return PreparedRepo(path=Path(job.repo), sha="deadbeef")


def _drain(
    store, tmp_path, clock, *, profile, launcher, registry, follow=False, sleeper=None,
    echo=None, **kwargs,
):
    return run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=1.0, follow=follow, worker=profile),
        registry=registry,
        launcher=launcher,
        clock=clock,
        sleeper=sleeper or clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=echo or (lambda _line: None),
        identity=profile.name,
        prepare_repo=kwargs.pop("prepare_repo", _pass_through_sync),
        probe=None,
        **kwargs,
    )


# --- AC1: a worker drains the fleet queue through the client ----------------------


def test_a_worker_claims_runs_and_finishes_a_fleet_job_over_http(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    repo = _repo(tmp_path)
    job_id = service.add_job(repo=repo, kind="build", scope="1.1-001")
    registry = Registry(tmp_path / "registry.json")
    client = _client(service)

    result = _drain(
        client, tmp_path, clock, profile=_profile(), launcher=FakeLauncher(registry),
        registry=registry,
    )

    job = service.get_job(job_id)
    assert job is not None and job.state == "done"
    assert job.run_id == "run-1"  # attach_run went over HTTP
    assert job.synced_sha == "deadbeef"  # record_sync too
    assert (result.started, result.done) == (1, 1)
    worker = service.get_worker("m3max")
    assert worker is not None and worker.host == "m3max-host"
    # The run shows on the fleet with the worker that ran it — the MVP acceptance.
    (row,) = service.list_fleet_runs()
    assert (row["run_id"], row["worker"]) == ("run-1", "m3max")
    assert row["dashboard_url"] == "http://m3max.tailnet:8787"
    assert row["status"] == "DONE"


def test_every_scheduler_write_goes_through_a_service_route(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    service.add_job(repo=_repo(tmp_path), kind="build", scope="1.1-001")
    registry = Registry(tmp_path / "registry.json")
    client = _client(service)

    _drain(
        client, tmp_path, clock, profile=_profile(),
        launcher=FakeLauncher(registry, alive_polls=60), registry=registry,
    )

    routes = set(client.routes)
    assert ("POST", "/workers") in routes
    assert ("GET", "/jobs/claimable") in routes
    assert ("POST", "/jobs/eligible") in routes
    assert ("POST", "/jobs/1/claim") in routes
    assert ("POST", "/jobs/1/sync") in routes
    assert ("POST", "/jobs/1/run") in routes
    assert ("POST", "/jobs/1/renew") in routes  # a 60-pass job outlives its renew interval
    assert ("PUT", "/runs") in routes
    assert ("POST", "/jobs/1/finish") in routes
    assert ("GET", "/pause") in routes


def test_a_worker_does_not_take_a_job_it_is_not_eligible_for(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    job_id = service.add_job(repo=_repo(tmp_path), kind="build", scope="1.1-001", host="home-lab")
    registry = Registry(tmp_path / "registry.json")
    launcher = FakeLauncher(registry)

    _drain(_client(service), tmp_path, clock, profile=_profile(), launcher=launcher, registry=registry)

    assert launcher.calls == []
    assert service.get_job(job_id).state == "queued"  # type: ignore[union-attr]


def test_a_losing_claim_race_over_http_is_not_a_failure(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    """Another worker wins the guarded UPDATE between peek and claim: try the next job."""
    first = service.add_job(repo=_repo(tmp_path, "a"), kind="build", scope="1")
    second = service.add_job(repo=_repo(tmp_path, "b"), kind="build", scope="2")
    registry = Registry(tmp_path / "registry.json")
    client = _client(service)
    real_peek = client.peek_claimable

    def peek_then_lose(**kwargs):
        candidates = real_peek(**kwargs)
        service.claim_job(first, claimed_by="xps", lease_seconds=90, now=clock(), worker="xps")
        return candidates

    client.peek_claimable = peek_then_lose  # type: ignore[method-assign]

    _drain(client, tmp_path, clock, profile=_profile(), launcher=FakeLauncher(registry), registry=registry)

    assert service.get_job(first).claimed_by == "xps"  # type: ignore[union-attr]
    assert service.get_job(second).state == "done"  # type: ignore[union-attr]


# --- parks and pauses -------------------------------------------------------------


def _ledger_with_parked_story(repo: str, pr_number: int = 12) -> tuple[str, str]:
    db = str(Path(repo) / ".sdlc-state.db")
    ledger = Ledger(Path(db))
    ledger.init()
    run_id = ledger.run_create("epic-3", "serial")
    ledger.story_upsert(
        run_id, "3.1-001", "epic-3", "a story", "Must", 3,
        "backend-typescript-architect", "feature/3.1-001", pr_number, "AWAITING_APPROVAL",
    )
    return run_id, db


class _PlainLauncher:
    """Launches children that register nothing — the test registers their runs itself."""

    def __init__(self, *, alive_polls: int = 2) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.procs: list[FakeProc] = []
        self._alive = alive_polls

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), str(cwd)))
        self.procs.append(FakeProc(90100 + len(self.calls), alive_polls=self._alive, code=1))
        return self.procs[-1]


def test_an_approval_park_is_polled_and_resumed_over_http(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    repo = _repo(tmp_path)
    run_id, db = _ledger_with_parked_story(repo)
    job_id = service.add_job(repo=repo, kind="build", scope="epic-3")
    registry = Registry(tmp_path / "registry.json")
    launcher = _PlainLauncher()
    client = _client(service)
    passes = {"n": 0}

    def sleeper(seconds: float) -> None:
        passes["n"] += 1
        if passes["n"] == 1:  # the child registers its run ...
            registry.register(
                RunRecord(run_id=run_id, repo=repo, db=db, scope="epic-3",
                          pid=launcher.procs[0].pid, status="IN_PROGRESS", started_at="")
            )
        elif passes["n"] == 2:  # ... and stops on its change request
            registry.mark_finished(run_id, "AWAITING_APPROVAL", completed=0)
        clock.advance(seconds)

    verdicts = iter([ApprovalVerdict(state="open", approved=False, signal="")])

    def probe(_root, _pr):
        return next(verdicts, ApprovalVerdict(state="open", approved=True, signal="risk-approved label"))

    def stop_once_resumed(seconds: float) -> None:
        if len(launcher.calls) == 2:
            raise KeyboardInterrupt  # the resident worker has seen the resume through
        sleeper(seconds)
        clock.advance(30)  # the park's poll interval is not the test's to wait out

    result = _drain(
        client, tmp_path, clock, profile=_profile(), launcher=launcher, follow=True,
        registry=registry, sleeper=stop_once_resumed, approval_probe=probe,
    )

    routes = set(client.routes)
    assert {("POST", f"/jobs/{job_id}/park"), ("GET", "/jobs/due"),
            ("POST", f"/jobs/{job_id}/poll"), ("POST", f"/jobs/{job_id}/take")} <= routes
    parked = service.get_job(job_id)
    assert parked is not None and parked.pr_number == 12 and parked.run_id == run_id
    assert result.parked == 1 and result.resumed == 1
    assert launcher.calls[-1][0][-3:] == ["resume", "--run", run_id]


def test_an_elapsed_rate_limit_window_is_lifted_and_announced_over_http(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    """The window is on record but elapsed: only a *raw* read lets the worker announce the resume."""
    service.pause_dispatch(until=T0 + timedelta(seconds=5), reason="rate limited", pool="claude-m3", now=T0)
    # Work the shut window is holding back, so the one-shot drain stays to see it reopen.
    service.add_job(repo=_repo(tmp_path), kind="build", scope="1.1-001", pool="claude-m3")
    registry = Registry(tmp_path / "registry.json")
    lines: list[str] = []
    passes = {"n": 0}

    def sleeper(seconds: float) -> None:
        passes["n"] += 1
        clock.advance(seconds * 3)

    _drain(
        _client(service), tmp_path, clock, profile=_profile(), launcher=FakeLauncher(registry),
        registry=registry, follow=False, sleeper=sleeper, echo=lines.append,
    )

    assert service.dispatch_pauses() == []
    assert any("queue resumed" in line and "claude-m3" in line for line in lines)


# --- AC3: a lapsed lease over HTTP -----------------------------------------------


def _held_by(store: QueueStore, worker: str, repo: str, clock: Clock, *, run_id: str | None) -> int:
    job_id = store.add_job(repo=repo, kind="build", scope="1.1-001")
    store.register_worker(worker, host=f"{worker}-host", now=clock())
    assert store.claim_job(job_id, claimed_by=worker, lease_seconds=90, now=clock(), worker=worker)
    if run_id:
        store.attach_run(job_id, run_id)
    return job_id


def _dead_pid() -> int:
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_the_service_surfaces_a_lapsed_lease_but_a_peer_waits_for_the_holders_heartbeat(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    repo = _repo(tmp_path)
    job_id = _held_by(service, "m3max", repo, clock, run_id="run-on-m3")
    clock.advance(200)  # m3max's network dropped: no renewals, no heartbeats
    client = _client(service)
    assert [j.id for j in client.expired_running_jobs(now=clock())] == [job_id]

    registry = Registry(tmp_path / "xps-registry.json")
    launcher = FakeLauncher(registry)
    _drain(client, tmp_path, clock, profile=_profile("xps"), launcher=launcher, registry=registry)

    # The run's pid cannot be checked from here, so the peer leaves it alone.
    job = service.get_job(job_id)
    assert launcher.calls == []
    assert job is not None and job.state == "running" and job.worker == "m3max"


def test_the_holders_heartbeat_confirms_its_dead_run_and_hands_the_job_back(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    repo = _repo(tmp_path)
    job_id = _held_by(service, "m3max", repo, clock, run_id="run-on-m3")
    clock.advance(200)  # the network was out; the job's process died meanwhile
    m3_registry = Registry(tmp_path / "m3-registry.json")
    m3_registry.register(
        RunRecord(run_id="run-on-m3", repo=repo, db=str(tmp_path / "x.db"), scope="1.1-001",
                  pid=_dead_pid(), status="IN_PROGRESS", started_at="")
    )
    m3 = _client(service)
    released: list[dict] = []
    real_release = m3.release_claim

    def spy(job_id_, **kwargs):
        released.append(dict(job_id=job_id_, held=service.get_job(job_id_).worker, **kwargs))
        return real_release(job_id_, **kwargs)

    m3.release_claim = spy  # type: ignore[method-assign]
    launcher = FakeLauncher(m3_registry)

    def stop_once_resumed(seconds: float) -> None:
        if launcher.calls:
            raise KeyboardInterrupt
        clock.advance(seconds)  # the released lease lapses a moment after the release

    result = _drain(
        m3, tmp_path, clock, profile=_profile("m3max"), launcher=launcher, registry=m3_registry,
        follow=True, sleeper=stop_once_resumed,
    )

    # On its first beat the holder released the claim (worker -> NULL), so any
    # eligible worker could take it; being free, it resumed its own run itself.
    first = released[0]  # (the later one is the Ctrl-C that ends the test)
    assert (first["job_id"], first["held"], first["claimed_by"]) == (job_id, "m3max", "m3max")
    assert "run is gone" in first["reason"]
    assert ("POST", "/jobs/1/reclaim") in m3.routes
    assert result.resumed == 1
    assert launcher.calls[0][0][-3:] == ["resume", "--run", "run-on-m3"]


def test_an_eligible_peer_reclaims_a_released_job_with_a_fresh_run(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    job_id = _held_by(service, "m3max", _repo(tmp_path), clock, run_id="run-on-m3")
    service.release_claim(job_id, claimed_by="m3max", reason="worker back", now=clock())
    clock.advance(10)
    registry = Registry(tmp_path / "xps-registry.json")
    launcher = FakeLauncher(registry)

    result = _drain(_client(service), tmp_path, clock, profile=_profile("xps"), launcher=launcher,
                    registry=registry)

    # `run-on-m3` lives in m3max's ledger: there is nothing here to resume, so the
    # scope starts a new run rather than failing a `sdlc resume --run <unknown>`.
    final = service.get_job(job_id)
    assert result.started == 1 and result.resumed == 0
    assert launcher.calls[0][0][-2:] == ["build", "1.1-001"]
    assert final is not None and final.state == "done" and final.run_id == "run-1"


class _Refuses:
    """A launcher for a worker that must not start anything (no free slot)."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, argv, cwd):  # pragma: no cover - the test asserts it is never reached
        self.calls.append(argv)
        raise AssertionError("this worker must not launch")


def test_a_returning_holder_whose_run_is_still_alive_keeps_its_job(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    repo = _repo(tmp_path)
    job_id = _held_by(service, "m3max", repo, clock, run_id="run-on-m3")
    clock.advance(200)
    registry = Registry(tmp_path / "m3-registry.json")
    registry.register(
        RunRecord(run_id="run-on-m3", repo=repo, db=str(tmp_path / "x.db"), scope="1.1-001",
                  pid=os.getpid(), status="IN_PROGRESS", started_at="")
    )

    _drain(_client(service), tmp_path, clock, profile=_profile("m3max"), launcher=_Refuses(), registry=registry)

    job = service.get_job(job_id)
    assert job is not None and job.state == "running" and job.worker == "m3max"


def test_a_cancel_on_a_peers_lapsed_job_waits_for_the_holders_heartbeat(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    repo = _repo(tmp_path)
    job_id = _held_by(service, "m3max", repo, clock, run_id="run-on-m3")
    service.cancel_job(job_id)
    clock.advance(200)

    # A peer cannot tell the run is gone, so it does not retire the job under it ...
    xps_registry = Registry(tmp_path / "xps-registry.json")
    _drain(_client(service), tmp_path, clock, profile=_profile("xps"), launcher=_Refuses(), registry=xps_registry)
    job = service.get_job(job_id)
    assert job is not None and job.state == "running" and job.cancel_requested

    # ... the holder's beat, finding the run gone, does.
    m3_registry = Registry(tmp_path / "m3-registry.json")
    m3_registry.register(
        RunRecord(run_id="run-on-m3", repo=repo, db=str(tmp_path / "x.db"), scope="1.1-001",
                  pid=_dead_pid(), status="IN_PROGRESS", started_at="")
    )
    _drain(_client(service), tmp_path, clock, profile=_profile("m3max"), launcher=_Refuses(), registry=m3_registry)
    assert service.get_job(job_id).state == "cancelled"  # type: ignore[union-attr]


def test_a_peer_that_is_not_eligible_leaves_a_released_job_alone(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    repo = _repo(tmp_path)
    job_id = service.add_job(
        repo=repo, kind="build", scope="1", requirements_json='{"repo": "only-on-m3"}'
    )
    service.claim_job(job_id, claimed_by="m3max", lease_seconds=90, now=clock(), worker="m3max")
    service.attach_run(job_id, "run-on-m3")
    service.release_claim(job_id, claimed_by="m3max", reason="gone", now=clock())
    clock.advance(10)
    registry = Registry(tmp_path / "xps-registry.json")

    _drain(_client(service), tmp_path, clock, profile=_profile("xps", repos=["something-else"]),
           launcher=_Refuses(), registry=registry)

    job = service.get_job(job_id)
    assert job is not None and job.state == "running" and job.claimed_by is None


# --- the network drops mid-job -----------------------------------------------------


def test_a_drain_survives_an_outage_and_finishes_its_job_when_the_service_returns(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    job_id = service.add_job(repo=_repo(tmp_path), kind="build", scope="1.1-001")
    registry = Registry(tmp_path / "registry.json")
    client = _client(service, Flaky)
    lines: list[str] = []
    passes = {"n": 0}

    def sleeper(seconds: float) -> None:
        passes["n"] += 1
        # The job exits at pass ~3; cut the network around its exit, restore it later.
        client.down = 3 <= passes["n"] <= 8
        clock.advance(seconds)

    result = _drain(
        client, tmp_path, clock, profile=_profile(), launcher=FakeLauncher(registry),
        registry=registry, follow=False, sleeper=sleeper, echo=lines.append,
    )

    job = service.get_job(job_id)
    assert job is not None and job.state == "done"  # the finish waited for the service
    assert result.done == 1
    assert sum(line.startswith("fleet queue unreachable") for line in lines) == 1  # once, not every pass
    assert any("reachable again" in line for line in lines)


def test_a_one_shot_drain_fails_fast_when_the_service_is_down_at_start(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    client = _client(service, Flaky)
    client.down = True
    registry = Registry(tmp_path / "registry.json")

    with pytest.raises(QueueUnavailable, match="unreachable"):
        _drain(client, tmp_path, clock, profile=_profile(), launcher=FakeLauncher(registry), registry=registry)


def test_a_following_worker_waits_out_an_outage_at_start(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    job_id = service.add_job(repo=_repo(tmp_path), kind="build", scope="1.1-001")
    client = _client(service, Flaky)
    client.down = True
    registry = Registry(tmp_path / "registry.json")
    passes = {"n": 0}

    def sleeper(seconds: float) -> None:
        passes["n"] += 1
        client.down = passes["n"] < 4
        if passes["n"] > 40:
            raise KeyboardInterrupt  # a resident worker never exits on its own
        clock.advance(seconds)

    # `follow` would loop forever; the interrupt stands in for launchd stopping it.
    result = _drain(
        client, tmp_path, clock, profile=_profile(), launcher=FakeLauncher(registry),
        registry=registry, follow=True, sleeper=sleeper,
    )

    assert result.interrupted
    assert service.get_job(job_id).state == "done"  # type: ignore[union-attr]
    assert service.get_worker("m3max") is not None


def test_ctrl_c_during_an_outage_does_not_traceback_and_leaves_the_lease_to_lapse(
    tmp_path: Path, service: QueueStore, clock: Clock
) -> None:
    job_id = service.add_job(repo=_repo(tmp_path), kind="build", scope="1.1-001")
    client = _client(service, Flaky)
    registry = Registry(tmp_path / "registry.json")
    launcher = FakeLauncher(registry, alive_polls=100)
    lines: list[str] = []
    passes = {"n": 0}

    def sleeper(seconds: float) -> None:
        passes["n"] += 1
        if passes["n"] == 2:
            client.down = True
        if passes["n"] == 4:
            raise KeyboardInterrupt
        clock.advance(seconds)

    result = _drain(
        client, tmp_path, clock, profile=_profile(), launcher=launcher, registry=registry,
        follow=True, sleeper=sleeper, echo=lines.append,
    )

    assert result.interrupted and launcher.procs[0].stopped
    assert any("could not release" in line for line in lines)
    job = service.get_job(job_id)
    assert job is not None and job.state == "running"  # its lease lapses; the reclaim path recovers it


# --- the local queue is untouched ---------------------------------------------------


def test_a_local_store_drain_registers_no_worker_and_needs_no_service(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    job_id = store.add_job(repo=_repo(tmp_path), kind="build", scope="1.1-001")
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()

    run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=1.0),
        registry=registry,
        launcher=FakeLauncher(registry),
        clock=clock,
        sleeper=clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
        probe=None,
    )

    assert store.get_job(job_id).state == "done"  # type: ignore[union-attr]
    assert store.list_workers() == []
