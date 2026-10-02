# ABOUTME: Tests for `queue run --worker` inside the scheduler loop (Story 35.2-001).
# ABOUTME: Registration at start, 30 s heartbeats with live slot counts, capability filter.

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from sdlc.doctor import Finding
from sdlc.queue import HEARTBEAT_SECONDS, QueueStore
from sdlc.queue_worker import WorkerProfile
from sdlc.registry import Registry, RunRecord
from sdlc.scheduler import SchedulerConfig, run_queue


class FakeProc:
    def __init__(self, pid: int, alive_polls: int = 1) -> None:
        self.pid = pid
        self._alive = alive_polls

    def poll(self) -> int | None:
        if self._alive > 0:
            self._alive -= 1
            return None
        return 0

    def stop(self) -> None:
        self._alive = 0


class FakeLauncher:
    def __init__(self, *, alive_polls: int = 1) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self._alive = alive_polls

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), str(cwd)))
        return FakeProc(90000 + len(self.calls), self._alive)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _clean(_root) -> Finding:
    return Finding("install", "Installed controller vs checkout", "CLEAN", "matches")


def _store(tmp_path) -> QueueStore:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    return store


def _repo(tmp_path, name: str) -> str:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _profile(**overrides) -> WorkerProfile:
    fields = {
        "name": "m3max",
        "host": "macbook-pro-m3-max",
        "pools": ["claude-m3"],
        "harnesses": ["claude"],
        "sandbox": None,
        "repos": ["agentic-coding-monitor"],
    }
    fields.update(overrides)
    return WorkerProfile(**fields)


def _drain(store, tmp_path, *, profile, launcher=None, clock=None, slots=2, follow=False):
    clock = clock or Clock()
    launcher = launcher or FakeLauncher(alive_polls=1)
    run_queue(
        store,
        config=SchedulerConfig(slots=slots, poll_seconds=1.0, follow=follow, worker=profile),
        registry=Registry(tmp_path / "registry.json"),
        launcher=launcher,
        clock=clock,
        sleeper=clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
        identity=profile.name,
    )
    return launcher, clock


def test_the_worker_registers_when_the_drain_starts(tmp_path) -> None:
    store = _store(tmp_path)

    _drain(store, tmp_path, profile=_profile(), slots=3)

    worker = store.get_worker("m3max")
    assert worker is not None
    assert (worker.host, worker.pools, worker.repos) == (
        "macbook-pro-m3-max", ["claude-m3"], ["agentic-coding-monitor"],
    )
    assert worker.slots == 3


def test_a_drain_without_a_worker_registers_nothing(tmp_path) -> None:
    store = _store(tmp_path)
    store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")

    clock = Clock()
    run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=1.0),
        registry=Registry(tmp_path / "registry.json"),
        launcher=FakeLauncher(),
        clock=clock,
        sleeper=clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
    )

    assert store.list_workers() == []


def test_the_worker_heartbeats_every_thirty_seconds_while_following(tmp_path) -> None:
    store = _store(tmp_path)
    clock = Clock()
    beats: list[str] = []
    original = store.register_worker

    def spy(name, **kwargs):
        beats.append(clock().isoformat())
        return original(name, **kwargs)

    store.register_worker = spy  # type: ignore[method-assign]

    class StopAfter:
        """Interrupts the follow loop once enough fake time has passed."""

        def __call__(self, seconds: float) -> None:
            clock.advance(seconds)
            if (clock() - start).total_seconds() >= 95:
                raise KeyboardInterrupt

    start = clock()
    run_queue(
        store,
        config=SchedulerConfig(slots=1, poll_seconds=1.0, follow=True, worker=_profile()),
        registry=Registry(tmp_path / "registry.json"),
        launcher=FakeLauncher(),
        clock=clock,
        sleeper=StopAfter(),
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
        identity="m3max",
    )

    seconds = [
        (datetime.fromisoformat(b) - start).total_seconds() for b in beats
    ]
    assert seconds[0] == 0
    assert all(b - a >= HEARTBEAT_SECONDS for a, b in zip(seconds, seconds[1:]))
    assert len(seconds) == 4  # t=0, 30, 60, 90
    assert store.get_worker("m3max").is_online(clock())


def test_a_heartbeat_reports_the_slots_a_running_job_holds(tmp_path) -> None:
    store = _store(tmp_path)
    store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    clock = Clock()
    free_at_beat: list[int] = []
    original = store.register_worker

    def spy(name, **kwargs):
        free_at_beat.append(kwargs["slots_free"])
        return original(name, **kwargs)

    store.register_worker = spy  # type: ignore[method-assign]

    class Slow:
        def __init__(self) -> None:
            self.polls = 0

        pid = 424242

        def poll(self):
            self.polls += 1
            return None if self.polls < 45 else 0

        def stop(self) -> None: ...

    run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=1.0, worker=_profile()),
        registry=Registry(tmp_path / "registry.json"),
        launcher=lambda argv, cwd: Slow(),
        clock=clock,
        sleeper=clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
        identity="m3max",
    )

    assert free_at_beat[0] == 2  # registered idle
    assert 1 in free_at_beat  # a later beat saw the running job's slot taken


def test_only_jobs_the_worker_is_eligible_for_are_started(tmp_path) -> None:
    store = _store(tmp_path)
    mine = store.add_job(
        repo=_repo(tmp_path, "alpha"), kind="fix", scope="1",
        requirements_json=json.dumps({"repo": "agentic-coding-monitor", "harness": "claude"}),
    )
    other_box = store.add_job(
        repo=_repo(tmp_path, "beta"), kind="fix", scope="2",
        requirements_json=json.dumps({"repo": "not-cloned-here"}),
    )
    codex = store.add_job(
        repo=_repo(tmp_path, "gamma"), kind="fix", scope="3",
        requirements_json=json.dumps({"harness": "codex"}),
    )

    holders: list[str | None] = []

    class Recording(FakeLauncher):
        def __call__(self, argv, cwd):
            holders.append(store.get_job(mine).worker)  # the claim, as the job starts
            return super().__call__(argv, cwd)

    launcher, _ = _drain(store, tmp_path, profile=_profile(), launcher=Recording())

    assert len(launcher.calls) == 1
    assert holders == ["m3max"]
    assert store.get_job(mine).state == "done"
    assert store.get_job(other_box).state == "queued"
    assert store.get_job(codex).state == "queued"
    assert store.get_job(other_box).reason == "no eligible worker (needs repo not-cloned-here)"
    assert store.get_job(codex).reason == "no eligible worker (needs harness codex, pool codex-shared)"


def test_a_less_loaded_peer_gets_the_job_instead(tmp_path) -> None:
    store = _store(tmp_path)
    store.register_worker(
        "idle-peer", host="elsewhere", harnesses=["claude"], slots=8, slots_free=8,
        now=Clock()(),
    )
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")

    launcher, _ = _drain(store, tmp_path, profile=_profile(), slots=2)

    assert launcher.calls == []
    assert store.get_job(job_id).state == "queued"


# --- Story 35.2-004: a caffeinate assertion for the lifetime of each worker job ---------


def test_keep_awake_prefix_is_caffeinate_on_macos_only() -> None:
    from sdlc.scheduler import keep_awake_prefix

    assert keep_awake_prefix(system="Darwin", which=lambda _: "/usr/bin/caffeinate") == [
        "/usr/bin/caffeinate", "-i",
    ]
    assert keep_awake_prefix(system="Linux", which=lambda _: "/usr/bin/caffeinate") == []
    assert keep_awake_prefix(system="Darwin", which=lambda _: None) == []


def test_a_worker_job_runs_under_the_keep_awake_prefix(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sdlc.scheduler.keep_awake_prefix", lambda: ["caffeinate", "-i"])
    store = _store(tmp_path)
    store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")

    launcher, _ = _drain(store, tmp_path, profile=_profile(repos=["alpha"]))

    argv, _cwd = launcher.calls[0]
    assert argv[:2] == ["caffeinate", "-i"]
    assert "fix" in argv[2:]


def test_a_plain_drain_is_not_wrapped_in_caffeinate(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sdlc.scheduler.keep_awake_prefix", lambda: ["caffeinate", "-i"])
    store = _store(tmp_path)
    store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    launcher = FakeLauncher()
    clock = Clock()

    run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=1.0),
        registry=Registry(tmp_path / "registry.json"),
        launcher=launcher,
        clock=clock,
        sleeper=clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
    )

    assert launcher.calls[0][0][0] != "caffeinate"


# --- Story 35.2-004: a worker the controller was reinstalled under exits for a restart ---


class _BoundedClock(Clock):
    """A sleeper that fails the test instead of spinning a `--follow` drain forever."""

    def __init__(self, passes: int = 50) -> None:
        super().__init__()
        self._passes = passes

    def advance(self, seconds: float) -> None:
        self._passes -= 1
        assert self._passes > 0, "the drain never exited"
        super().advance(seconds)


def _resident(store, tmp_path, *, probe, launcher=None, slots=2):
    clock = _BoundedClock()
    launcher = launcher or FakeLauncher(alive_polls=1)
    result = run_queue(
        store,
        config=SchedulerConfig(
            slots=slots, poll_seconds=1.0, follow=True, worker=_profile(repos=["alpha", "beta"]),
        ),
        registry=Registry(tmp_path / "registry.json"),
        launcher=launcher,
        clock=clock,
        sleeper=clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
        identity="m3max",
        installed_version="2.84.0",
        installed_probe=probe,
    )
    return result, launcher


def test_a_worker_exits_for_a_restart_once_the_controller_is_reinstalled(tmp_path) -> None:
    # `--follow` would otherwise park every job on the guard and never exit,
    # so KeepAlive would never get the chance to restart it on the new code.
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")

    result, launcher = _resident(store, tmp_path, probe=lambda: "2.85.0")

    assert result.restart is True
    assert launcher.calls == []  # never runs a job on the stale code
    job = store.get_job(job_id)
    assert job.state == "queued"  # untouched — the restarted worker takes it
    assert result.parked == 0


def test_a_worker_lets_its_running_job_finish_before_it_exits(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    second = store.add_job(repo=_repo(tmp_path, "beta"), kind="fix", scope="2")
    probes = iter(["2.84.0"])

    result, launcher = _resident(
        store, tmp_path, probe=lambda: next(probes, "2.85.0"),
        launcher=FakeLauncher(alive_polls=3), slots=1,
    )

    assert result.restart is True
    assert len(launcher.calls) == 1
    assert store.get_job(first).state == "done"  # reaped, not abandoned
    assert store.get_job(second).state == "queued"


def test_a_draining_worker_advertises_no_free_slots(tmp_path) -> None:
    # Peers defer a job to a less-loaded eligible worker; one that will claim
    # nothing more must not look less loaded while its last job finishes.
    store = _store(tmp_path)
    store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    probes = iter(["2.84.0"])

    result, _ = _resident(
        store, tmp_path, probe=lambda: next(probes, "2.85.0"),
        launcher=FakeLauncher(alive_polls=5), slots=2,
    )

    assert result.restart is True
    assert store.get_worker("m3max").slots_free == 0


def test_an_unreadable_install_is_no_reason_to_restart(tmp_path) -> None:
    # A probe mid-reinstall can meet a half-written environment.
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    clock = Clock()
    launcher = FakeLauncher(alive_polls=1)

    result = run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=1.0, worker=_profile(repos=["alpha"])),
        registry=Registry(tmp_path / "registry.json"),
        launcher=launcher,
        clock=clock,
        sleeper=clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
        identity="m3max",
        installed_version="2.84.0",
        installed_probe=lambda: None,
    )

    assert result.restart is False
    assert len(launcher.calls) == 1
    assert store.get_job(job_id).state == "done"


def test_a_plain_drain_never_probes_the_install(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    clock = Clock()
    probed: list[int] = []

    result = run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=1.0),
        registry=Registry(tmp_path / "registry.json"),
        launcher=FakeLauncher(alive_polls=1),
        clock=clock,
        sleeper=clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
        installed_version="2.84.0",
        installed_probe=lambda: probed.append(1) or "2.85.0",
    )

    assert probed == []
    assert result.restart is False
    assert store.get_job(job_id).state == "done"


def test_installed_controller_version_rereads_the_install(monkeypatch) -> None:
    import sdlc
    from sdlc.scheduler import installed_controller_version

    monkeypatch.setattr(sdlc, "_resolve_version", lambda: "2.85.0")
    assert installed_controller_version() == "2.85.0"

    def half_written() -> str:
        raise FileNotFoundError("pyproject.toml")

    monkeypatch.setattr(sdlc, "_resolve_version", half_written)
    assert installed_controller_version() is None


# --- Story 35.4-003: cancel of a running job reaches the worker's kill path ---


class _CancelProc:
    """A job that runs until stopped, recording the stop (the process-group kill)."""

    pid = 424243

    def __init__(self) -> None:
        self.stopped = 0
        self._dead = False

    def poll(self):
        return 0 if self._dead else None

    def stop(self) -> None:
        self.stopped += 1
        self._dead = True


def _cancel_after(store, job_id, clock, *, passes: int):
    """A sleeper that lets ``passes`` pass, then cancels the job from 'the XPS'."""
    seen = {"n": 0}

    def sleeper(seconds: float) -> None:
        clock.advance(seconds)
        seen["n"] += 1
        if seen["n"] == passes:
            store.cancel_job(job_id)
        if seen["n"] > passes + 20:  # a stuck cancel must fail the test, not hang it
            raise KeyboardInterrupt

    return sleeper


def test_cancelling_a_running_job_kills_its_run_and_releases_the_lease(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    clock = Clock()
    proc = _CancelProc()
    lines: list[str] = []

    result = run_queue(
        store,
        config=SchedulerConfig(slots=1, poll_seconds=1.0, worker=_profile()),
        registry=Registry(tmp_path / "registry.json"),
        launcher=lambda argv, cwd: proc,
        clock=clock,
        sleeper=_cancel_after(store, job_id, clock, passes=3),
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lines.append,
        identity="m3max",
    )

    job = store.get_job(job_id)
    assert proc.stopped == 1
    assert (job.state, job.claimed_by, job.worker, job.lease_until) == ("cancelled", None, None, None)
    assert job.cancel_requested is False
    assert not result.interrupted and result.failed == 0
    assert any("job" in line and "cancelled" in line for line in lines)


class _UnstoppableProc(_CancelProc):
    """A job whose process-group kill fails (e.g. the group is already gone)."""

    def stop(self) -> None:
        self.stopped += 1
        raise OSError("no such process group")


def test_a_failed_kill_still_finishes_the_cancel_and_releases_the_lease(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    clock = Clock()
    proc = _UnstoppableProc()
    lines: list[str] = []

    run_queue(
        store,
        config=SchedulerConfig(slots=1, poll_seconds=1.0, worker=_profile()),
        registry=Registry(tmp_path / "registry.json"),
        launcher=lambda argv, cwd: proc,
        clock=clock,
        sleeper=_cancel_after(store, job_id, clock, passes=3),
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lines.append,
        identity="m3max",
    )

    job = store.get_job(job_id)
    assert proc.stopped == 1
    assert (job.state, job.claimed_by, job.lease_until) == ("cancelled", None, None)
    assert any("could not stop pid" in line for line in lines)


def test_a_cancel_flagged_job_is_not_resumed_after_its_worker_died(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    clock = Clock()
    store.claim_next(claimed_by="dead-worker", lease_seconds=10, now=clock())
    store.attach_run(job_id, "run-1")
    store.cancel_job(job_id)
    clock.advance(60)  # the lease lapsed: a reclaim candidate
    launcher = FakeLauncher()

    run_queue(
        store,
        config=SchedulerConfig(slots=1, poll_seconds=1.0, worker=_profile()),
        registry=Registry(tmp_path / "registry.json"),
        launcher=launcher,
        clock=clock,
        sleeper=clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
        identity="m3max",
    )

    assert launcher.calls == []
    assert store.get_job(job_id).state == "cancelled"


def _peer_drain(store, registry, clock) -> None:
    """One `queue run` by a second worker on the same queue file."""
    run_queue(
        store,
        config=SchedulerConfig(slots=1, poll_seconds=1.0, worker=_profile(name="peer")),
        registry=registry,
        launcher=FakeLauncher(),
        clock=clock,
        sleeper=clock.advance,
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
        identity="peer",
    )


def _stall_then_cancel(store, job_id, clock, *, on_pass: int, during_stall):
    """The holder's sleeper: on pass ``on_pass`` it stalls past its 90 s lease,
    FX cancels from 'the XPS', and ``during_stall`` runs (a peer's drain)."""
    seen = {"n": 0}

    def sleeper(seconds: float) -> None:
        seen["n"] += 1
        if seen["n"] == on_pass:
            clock.advance(120)
            store.cancel_job(job_id)
            during_stall()
        else:
            clock.advance(seconds)
        if seen["n"] > on_pass + 20:  # a lost cancel must fail the test, not hang it
            raise KeyboardInterrupt

    return sleeper


def test_a_stalled_holder_stops_its_run_after_a_peer_retired_the_job(tmp_path) -> None:
    """The holder stalls past its lease before its run has attached, FX cancels,
    and a peer drain retires the job. Only the holder has the run's process, so
    a row already reading `cancelled` is its stop order too — walking away from
    it as a lost lease would leave the run going, unwatched."""
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    clock = Clock()
    registry = Registry(tmp_path / "registry.json")
    proc = _CancelProc()
    lines: list[str] = []

    run_queue(
        store,
        config=SchedulerConfig(slots=1, poll_seconds=1.0, worker=_profile()),
        registry=registry,
        launcher=lambda argv, cwd: proc,
        clock=clock,
        sleeper=_stall_then_cancel(
            store, job_id, clock, on_pass=1,
            during_stall=lambda: _peer_drain(store, registry, clock),
        ),
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lines.append,
        identity="m3max",
    )

    job = store.get_job(job_id)
    assert proc.stopped == 1
    assert (job.state, job.claimed_by, job.cancel_requested) == ("cancelled", None, False)
    assert job.reason == "cancelled by operator"
    assert not any("lease lost" in line for line in lines)


def test_a_peer_leaves_a_cancelled_job_whose_run_is_alive_to_its_holder(tmp_path) -> None:
    """Once the run has attached and its pid answers, a lapsed lease says nothing
    about the work: the peer leaves the flag where it is, and the holder stops
    the run itself when it wakes."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="fix", scope="1")
    clock = Clock()
    registry = Registry(tmp_path / "registry.json")
    proc = _CancelProc()
    proc.pid = os.getpid()  # a pid that answers: the run is alive
    registry.register(RunRecord(
        run_id="run-1", repo=repo, db=str(tmp_path / "x.db"), scope="1",
        pid=proc.pid, status="IN_PROGRESS", started_at="",
    ))
    after_peer = []

    def peer() -> None:
        _peer_drain(store, registry, clock)
        after_peer.append(store.get_job(job_id))

    run_queue(
        store,
        config=SchedulerConfig(slots=1, poll_seconds=1.0, worker=_profile()),
        registry=registry,
        launcher=lambda argv, cwd: proc,
        clock=clock,
        # Pass 2: `_attach_runs` has linked run-1 by then.
        sleeper=_stall_then_cancel(store, job_id, clock, on_pass=2, during_stall=peer),
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
        identity="m3max",
    )

    seen = after_peer[0]
    assert (seen.state, seen.claimed_by, seen.cancel_requested) == ("running", "m3max", True)
    job = store.get_job(job_id)
    assert proc.stopped == 1
    assert (job.state, job.claimed_by, job.cancel_requested) == ("cancelled", None, False)


def test_a_cancel_waits_for_a_run_that_outlived_its_worker(tmp_path) -> None:
    """The worker died but its run — in its own session — did not. Retiring the
    job now would leave that run going under a `cancelled` row, and resuming it
    would undo FX's cancel: it stays flagged until the run ends, then is retired."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="fix", scope="1")
    clock = Clock()
    store.claim_next(claimed_by="dead-worker", lease_seconds=10, now=clock())
    store.attach_run(job_id, "run-1")
    registry = Registry(tmp_path / "registry.json")
    registry.register(RunRecord(
        run_id="run-1", repo=repo, db=str(tmp_path / "x.db"), scope="1",
        pid=os.getpid(), status="IN_PROGRESS", started_at="",
    ))
    store.cancel_job(job_id)
    clock.advance(60)  # the lease lapsed: a reclaim candidate
    launcher = FakeLauncher()

    def drain() -> None:
        run_queue(
            store,
            config=SchedulerConfig(slots=1, poll_seconds=1.0, worker=_profile()),
            registry=registry,
            launcher=launcher,
            clock=clock,
            sleeper=clock.advance,
            notifier=lambda *a, **k: None,
            version_check=_clean,
            echo=lambda _line: None,
            identity="m3max",
        )

    drain()
    job = store.get_job(job_id)
    assert (job.state, job.cancel_requested, job.reason) == ("running", True, "cancel requested")

    registry.mark_finished("run-1", "FAILED", completed=0)  # the run ends on its own
    drain()

    job = store.get_job(job_id)
    assert launcher.calls == []
    assert (job.state, job.cancel_requested) == ("cancelled", False)
