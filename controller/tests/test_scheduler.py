# ABOUTME: Behavior tests for the queue scheduler loop (Story 32.1-002).
# ABOUTME: Claim/lease/renew/reclaim, per-repo exclusivity, slot cap, park, notify.
# ABOUTME: Plus Story 32.2-001's one host-level rate-limit window for the whole queue.

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sdlc.doctor import Finding
from sdlc.queue import QueueStore
from sdlc.registry import Registry, RunRecord


# --- fakes ----------------------------------------------------------------


class FakeProc:
    """A launched job process that finishes after ``alive_polls`` loop passes."""

    def __init__(self, pid: int, *, alive_polls: int = 1, code: int = 0) -> None:
        self.pid = pid
        self._alive = alive_polls
        self._code = code
        self.stopped = False

    def poll(self) -> int | None:
        if self._alive > 0:
            self._alive -= 1
            return None
        return self._code

    def stop(self) -> None:
        self.stopped = True
        self._alive = 0


class FakeLauncher:
    """Records every argv the scheduler dispatches and hands back a FakeProc."""

    def __init__(self, *, alive_polls: int = 1, code: int = 0) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.procs: list[FakeProc] = []
        self._alive = alive_polls
        self._code = code
        self._next_pid = 90000

    def __call__(self, argv, cwd):
        self._next_pid += 1
        self.calls.append((list(argv), str(cwd)))
        proc = FakeProc(self._next_pid, alive_polls=self._alive, code=self._code)
        self.procs.append(proc)
        return proc

    @property
    def commands(self) -> list[list[str]]:
        # Drop the interpreter/binary prefix so assertions read as `sdlc` verbs.
        return [argv[-3:] if argv[-3:-2] == ["resume"] else argv[1:] for argv, _ in self.calls]


class Clock:
    """A monotonic fake clock the injected sleeper advances."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _store(tmp_path) -> QueueStore:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    return store


def _repo(tmp_path, name: str) -> str:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _clean(_root) -> Finding:
    return Finding("install", "Installed controller vs checkout", "CLEAN", "matches")


def _run(store, *, tmp_path, launcher, clock=None, config=None, **kwargs):
    from sdlc.scheduler import SchedulerConfig, run_queue

    clock = clock or Clock()

    def sleeper(seconds: float) -> None:
        clock.advance(seconds)

    return run_queue(
        store,
        config=config or SchedulerConfig(slots=2, poll_seconds=1.0),
        registry=kwargs.pop("registry", Registry(tmp_path / "registry.json")),
        launcher=launcher,
        clock=clock,
        sleeper=kwargs.pop("sleeper", sleeper),
        notifier=kwargs.pop("notifier", lambda *a, **k: None),
        version_check=kwargs.pop("version_check", _clean),
        echo=kwargs.pop("echo", lambda _line: None),
        **kwargs,
    )


def _dead_pid() -> int:
    """A pid that is certainly gone — spawned and reaped before we return it."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# --- AC1: slot cap --------------------------------------------------------


def test_slot_cap_limits_concurrent_jobs_and_the_rest_follow(tmp_path) -> None:
    """Three repos, a cap of 2: two run at once, the third starts on a free slot."""
    store = _store(tmp_path)
    for name in ("alpha", "beta", "gamma"):
        store.add_job(repo=_repo(tmp_path, name), kind="fix", scope="42")

    concurrency_at_launch: list[int] = []
    launcher = FakeLauncher(alive_polls=1)
    original = launcher.__call__

    def recording(argv, cwd):
        concurrency_at_launch.append(len(store.running_repos()))
        return original(argv, cwd)

    result = _run(store, tmp_path=tmp_path, launcher=recording)

    assert len(launcher.calls) == 3
    assert max(concurrency_at_launch) == 2
    assert result.started == 3
    assert [j.state for j in store.list_jobs()] == ["done"] * 3


def test_a_job_declaring_more_workers_costs_more_slots(tmp_path) -> None:
    """`--concurrency=2` is two agent slots, so it fills a cap of 2 by itself."""
    from sdlc.scheduler import agent_slots

    store = _store(tmp_path)
    big = store.add_job(
        repo=_repo(tmp_path, "alpha"), kind="build", scope="epic-3",
        options_json=json.dumps(["epic-3", "--concurrency=2"]),
    )
    store.add_job(repo=_repo(tmp_path, "beta"), kind="fix", scope="7")

    assert agent_slots(store.get_job(big)) == 2

    concurrency_at_launch: list[int] = []
    launcher = FakeLauncher(alive_polls=1)

    def recording(argv, cwd):
        concurrency_at_launch.append(len(store.running_repos()))
        return launcher(argv, cwd)

    _run(store, tmp_path=tmp_path, launcher=recording)
    assert concurrency_at_launch == [1, 1]  # never overlapped


def test_a_job_larger_than_the_cap_still_runs_alone(tmp_path) -> None:
    """An oversized job must not starve — it is admitted when nothing else runs."""
    store = _store(tmp_path)
    store.add_job(
        repo=_repo(tmp_path, "alpha"), kind="build", scope="epic-3",
        options_json=json.dumps(["epic-3", "--concurrency=9"]),
    )
    launcher = FakeLauncher(alive_polls=1)

    result = _run(store, tmp_path=tmp_path, launcher=launcher)
    assert result.started == 1


# --- AC2: per-repo exclusivity -------------------------------------------


def test_two_jobs_in_one_repo_never_overlap(tmp_path) -> None:
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    store.add_job(repo=repo, kind="fix", scope="1")
    store.add_job(repo=repo, kind="fix", scope="2")

    concurrency_at_launch: list[int] = []
    launcher = FakeLauncher(alive_polls=1)

    def recording(argv, cwd):
        concurrency_at_launch.append(len(store.running_repos()))
        return launcher(argv, cwd)

    _run(store, tmp_path=tmp_path, launcher=recording)

    assert len(launcher.calls) == 2
    assert concurrency_at_launch == [1, 1]


def test_a_blocked_repo_stamps_reason_repo_busy(tmp_path) -> None:
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    store.add_job(repo=repo, kind="fix", scope="1")
    waiting = store.add_job(repo=repo, kind="fix", scope="2")

    reasons: list[str | None] = []
    launcher = FakeLauncher(alive_polls=2)

    def recording(argv, cwd):
        proc = launcher(argv, cwd)
        reasons.append(store.get_job(waiting).reason)
        return proc

    _run(store, tmp_path=tmp_path, launcher=recording)

    # While the first job held the repo, the waiting job carried the reason;
    # once claimed, the reason is cleared.
    assert reasons[0] is None  # not stamped until the first fill pass completes
    assert store.get_job(waiting).state == "done"


def test_repo_busy_reason_is_visible_while_the_repo_is_held(tmp_path) -> None:
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    store.add_job(repo=repo, kind="fix", scope="1")
    waiting = store.add_job(repo=repo, kind="fix", scope="2")

    seen: list[str | None] = []
    clock = Clock()

    def sleeper(seconds: float) -> None:
        seen.append(store.get_job(waiting).reason)
        clock.advance(seconds)

    _run(store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=2),
         clock=clock, sleeper=sleeper)

    assert "repo busy" in seen


def test_two_build_jobs_in_one_repo_overlap_after_32_1_003(tmp_path) -> None:
    """Story 32.1-003 relaxes exclusivity for build/build: they may run at once.

    Both jobs share one repo, so `running_repos()` (distinct repo paths) stays
    at 1 either way — the concurrency signal here is the count of `running`
    *job rows*, which only exceeds 1 if the second job was claimed and
    launched before the first one finished.
    """
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    store.add_job(repo=repo, kind="build", scope="epic-1")
    store.add_job(repo=repo, kind="build", scope="epic-2")

    concurrency_at_launch: list[int] = []
    launcher = FakeLauncher(alive_polls=2)

    def recording(argv, cwd):
        running = sum(1 for j in store.list_jobs() if j.state == "running")
        concurrency_at_launch.append(running)
        return launcher(argv, cwd)

    _run(store, tmp_path=tmp_path, launcher=recording)

    assert len(launcher.calls) == 2
    assert max(concurrency_at_launch) == 2  # both launched while the repo was "busy"
    assert [j.state for j in store.list_jobs()] == ["done"] * 2


def test_a_fix_job_still_waits_out_a_running_build_in_one_repo(tmp_path) -> None:
    """The relaxed rule is build/build only — fix stays exclusive against build."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    store.add_job(repo=repo, kind="build", scope="epic-1")
    fix_job = store.add_job(repo=repo, kind="fix", scope="1")

    concurrency_at_launch: list[int] = []
    launcher = FakeLauncher(alive_polls=2)

    def recording(argv, cwd):
        running = sum(1 for j in store.list_jobs() if j.state == "running")
        concurrency_at_launch.append(running)
        return launcher(argv, cwd)

    _run(store, tmp_path=tmp_path, launcher=recording)

    assert len(launcher.calls) == 2
    assert concurrency_at_launch == [1, 1]  # never overlapped
    assert store.get_job(fix_job).state == "done"


def test_a_build_job_still_waits_out_a_running_fix_in_one_repo(tmp_path) -> None:
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    store.add_job(repo=repo, kind="fix", scope="1")
    build_job = store.add_job(repo=repo, kind="build", scope="epic-1")

    concurrency_at_launch: list[int] = []
    launcher = FakeLauncher(alive_polls=2)

    def recording(argv, cwd):
        running = sum(1 for j in store.list_jobs() if j.state == "running")
        concurrency_at_launch.append(running)
        return launcher(argv, cwd)

    _run(store, tmp_path=tmp_path, launcher=recording)

    assert len(launcher.calls) == 2
    assert concurrency_at_launch == [1, 1]  # never overlapped
    assert store.get_job(build_job).state == "done"


def test_the_scheduler_never_injects_allow_dirty_or_force(tmp_path) -> None:
    """No `--allow-dirty`, no stash, no bypass of the #590 guard (AC2)."""
    store = _store(tmp_path)
    store.add_job(
        repo=_repo(tmp_path, "alpha"), kind="build", scope="epic-3",
        options_json=json.dumps(["epic-3", "--auto"]),
    )
    launcher = FakeLauncher(alive_polls=1)
    _run(store, tmp_path=tmp_path, launcher=launcher)

    argv = launcher.calls[0][0]
    assert argv[-3:] == ["build", "epic-3", "--auto"]
    assert "--allow-dirty" not in argv
    assert "--force" not in argv


def test_a_job_with_no_frozen_options_replays_its_scope(tmp_path) -> None:
    store = _store(tmp_path)
    store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="42")
    launcher = FakeLauncher(alive_polls=1)
    _run(store, tmp_path=tmp_path, launcher=launcher)

    assert launcher.calls[0][0][-2:] == ["fix", "42"]
    assert launcher.calls[0][1] == _repo(tmp_path, "alpha")


def test_malformed_frozen_options_fall_back_to_the_scope(tmp_path) -> None:
    store = _store(tmp_path)
    store.add_job(
        repo=_repo(tmp_path, "alpha"), kind="build", scope="epic-3",
        options_json="{not json",
    )
    launcher = FakeLauncher(alive_polls=1)
    _run(store, tmp_path=tmp_path, launcher=launcher)
    assert launcher.calls[0][0][-2:] == ["build", "epic-3"]


# --- AC3: leases, renewal, reclaim ---------------------------------------


def test_the_lease_is_renewed_on_its_interval(tmp_path) -> None:
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="42")

    leases: list[str | None] = []
    clock = Clock()

    def sleeper(seconds: float) -> None:
        clock.advance(seconds)
        leases.append(store.get_job(job_id).lease_until)

    _run(
        store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=6),
        clock=clock, sleeper=sleeper,
        config=SchedulerConfig(slots=2, poll_seconds=20.0, lease_seconds=90, renew_seconds=30),
    )

    # Distinct lease stamps prove the renewal fired more than once mid-run.
    assert len({lease for lease in leases if lease}) >= 2


def test_an_expired_job_with_a_run_is_resumed_not_restarted(tmp_path) -> None:
    """The killed-scheduler path: reclaim, then re-enter through `sdlc resume`."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    clock = Clock()
    store.claim_job(job_id, claimed_by="dead:1", lease_seconds=90, now=clock())
    store.attach_run(job_id, "run-abc")

    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord(run_id="run-abc", repo=repo, db=str(tmp_path / "x.db"),
                  scope="epic-3", pid=_dead_pid(), status="IN_PROGRESS", started_at="")
    )

    clock.advance(200)
    launcher = FakeLauncher(alive_polls=1)
    result = _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock, registry=registry)

    assert result.resumed == 1
    assert result.started == 0
    assert launcher.calls[0][0][-3:] == ["resume", "--run", "run-abc"]


def test_an_expired_job_whose_pid_is_alive_is_left_alone(tmp_path) -> None:
    """Registry liveness wins over a lapsed lease — never two drivers on one run."""
    import os

    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    clock = Clock()
    store.claim_job(job_id, claimed_by="other:1", lease_seconds=90, now=clock())
    store.attach_run(job_id, "run-abc")

    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord(run_id="run-abc", repo=repo, db=str(tmp_path / "x.db"),
                  scope="epic-3", pid=os.getpid(), status="IN_PROGRESS", started_at="")
    )

    clock.advance(200)
    launcher = FakeLauncher(alive_polls=1)
    result = _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock, registry=registry)

    assert launcher.calls == []
    assert result.resumed == 0
    assert store.get_job(job_id).state == "running"
    assert store.get_job(job_id).claimed_by == "other:1"


def test_an_expired_job_that_never_started_a_run_is_requeued(tmp_path) -> None:
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="fix", scope="42")
    clock = Clock()
    store.claim_job(job_id, claimed_by="dead:1", lease_seconds=90, now=clock())

    clock.advance(200)
    launcher = FakeLauncher(alive_polls=1)
    result = _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock)

    assert result.started == 1
    assert launcher.calls[0][0][-2:] == ["fix", "42"]
    assert store.get_job(job_id).state == "done"


# --- AC4: per-job controller version check -------------------------------


def test_a_stale_controller_parks_the_job_blocked_with_the_remedy(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="build", scope="epic-3")

    def stale(_root) -> Finding:
        return Finding(
            "install", "Installed controller vs checkout", "WARN",
            "installed 2.60.0, checkout 2.61.0 — the installed controller is behind",
            "reinstall from the checkout: bash scripts/install-controller.sh",
        )

    launcher = FakeLauncher(alive_polls=1)
    result = _run(store, tmp_path=tmp_path, launcher=launcher, version_check=stale)

    assert launcher.calls == []
    assert result.parked == 1
    job = store.get_job(job_id)
    assert job.state == "blocked"
    assert "install-controller.sh" in (job.reason or "")


def test_the_version_check_runs_per_job_not_once(tmp_path) -> None:
    store = _store(tmp_path)
    store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    store.add_job(repo=_repo(tmp_path, "beta"), kind="fix", scope="2")

    seen: list[str] = []

    def spy(root) -> Finding:
        seen.append(str(root))
        return _clean(root)

    _run(store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=1), version_check=spy)
    assert sorted(seen) == [_repo(tmp_path, "alpha"), _repo(tmp_path, "beta")]


# --- AC5: foreground loop, Ctrl-C ----------------------------------------


def test_ctrl_c_stops_the_children_and_releases_every_lease(tmp_path) -> None:
    store = _store(tmp_path)
    started = store.add_job(repo=_repo(tmp_path, "alpha"), kind="build", scope="epic-3")
    launcher = FakeLauncher(alive_polls=99)
    clock = Clock()

    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        calls["n"] += 1
        clock.advance(seconds)
        if calls["n"] >= 2:
            raise KeyboardInterrupt

    result = _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock, sleeper=sleeper)

    assert result.interrupted is True
    assert launcher.procs[0].stopped is True
    job = store.get_job(started)
    assert job.claimed_by is None
    assert job.state == "queued"  # no run was ever attached — safe to restart


def test_ctrl_c_leaves_a_started_run_resumable(tmp_path) -> None:
    """Interrupting mid-run must not restart from scratch on the next drain."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    registry = Registry(tmp_path / "registry.json")
    launcher = FakeLauncher(alive_polls=99)
    clock = Clock()
    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        calls["n"] += 1
        # The subprocess registers its run the way `run_build` does.
        if calls["n"] == 1:
            registry.register(
                RunRecord(run_id="run-xyz", repo=repo, db=str(tmp_path / "x.db"),
                          scope="epic-3", pid=launcher.procs[0].pid,
                          status="IN_PROGRESS", started_at="")
            )
        clock.advance(seconds)
        if calls["n"] >= 3:
            raise KeyboardInterrupt

    first = _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock,
                 sleeper=sleeper, registry=registry)
    assert first.interrupted is True
    assert store.get_job(job_id).run_id == "run-xyz"
    assert store.get_job(job_id).state == "running"

    # The dead scheduler's pid is gone; the next drain reclaims and resumes.
    registry.register(
        RunRecord(run_id="run-xyz", repo=repo, db=str(tmp_path / "x.db"),
                  scope="epic-3", pid=_dead_pid(), status="IN_PROGRESS", started_at="")
    )
    clock.advance(200)
    second_launcher = FakeLauncher(alive_polls=1)
    second = _run(store, tmp_path=tmp_path, launcher=second_launcher, clock=clock,
                  registry=registry)

    assert second.resumed == 1
    assert second_launcher.calls[0][0][-3:] == ["resume", "--run", "run-xyz"]


# --- AC6: terminal state + notify ----------------------------------------


def test_a_failing_job_is_recorded_failed_and_announced(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="42")
    events: list[tuple[str, dict]] = []

    result = _run(
        store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=1, code=1),
        notifier=lambda event, **fields: events.append((event, fields)),
    )

    assert store.get_job(job_id).state == "failed"
    assert result.failed == 1
    # A *queue* event, not a second `run_finished` — the job's own subprocess
    # already fires that one for its run (see the double-notify test below).
    assert events and events[0][0] == "queue_job_finished"
    assert events[0][1]["terminal"] == "FAILED"


def test_a_parked_run_status_maps_to_blocked(tmp_path) -> None:
    """A run that *finished* needing a human is parked, not failed.

    Guards the ``_PARKED_RUN_STATUSES`` mapping on the status the controller
    genuinely writes to the registry for this case: ``_run_terminal`` produces
    ``AWAITING_APPROVAL``, ``finalize_run`` stamps it, and the CLI still exits
    1 — so only the status distinguishes it from a real failure. (The other
    park, a run left *open* and resumable, is a different branch and is covered
    by ``test_a_cost_gated_run_is_parked_blocked_not_failed``.)
    """
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    registry = Registry(tmp_path / "registry.json")
    launcher = FakeLauncher(alive_polls=2, code=1)
    clock = Clock()
    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        # The real sequence: the child registers itself open on its first pass,
        # then `finalize_run` stamps the terminal on its way out.
        calls["n"] += 1
        if calls["n"] == 1:
            registry.register(
                RunRecord(run_id="run-appr", repo=repo, db=str(tmp_path / "x.db"),
                          scope="epic-3", pid=launcher.procs[0].pid,
                          status="IN_PROGRESS", started_at="")
            )
        elif calls["n"] == 2:
            registry.mark_finished("run-appr", "AWAITING_APPROVAL", completed=1)
        clock.advance(seconds)

    result = _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock,
                  sleeper=sleeper, registry=registry)

    job = store.get_job(job_id)
    assert job.state == "blocked"
    assert job.reason == "run status AWAITING_APPROVAL"
    assert result.parked == 1


def test_a_launch_failure_marks_the_job_failed(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="42")

    def exploding(argv, cwd):
        raise OSError("no such binary")

    result = _run(store, tmp_path=tmp_path, launcher=exploding)

    assert result.failed == 1
    job = store.get_job(job_id)
    assert job.state == "failed"
    assert "no such binary" in (job.reason or "")


def test_the_scheduler_never_writes_to_the_run_registry(tmp_path) -> None:
    """The dashboard sees a queued job as a normal run — the subprocess owns that."""
    store = _store(tmp_path)
    store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="42")
    registry_path = tmp_path / "registry.json"

    _run(store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=1),
         registry=Registry(registry_path))

    assert not registry_path.exists()


def test_an_empty_queue_drains_immediately(tmp_path) -> None:
    store = _store(tmp_path)
    launcher = FakeLauncher()
    result = _run(store, tmp_path=tmp_path, launcher=launcher)
    assert launcher.calls == []
    assert result.started == 0


def test_cancelled_jobs_are_never_dispatched(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="42")
    store.cancel_job(job_id)

    launcher = FakeLauncher()
    _run(store, tmp_path=tmp_path, launcher=launcher)
    assert launcher.calls == []


# --- slot accounting ------------------------------------------------------


@pytest.mark.parametrize(
    "options, expected",
    [
        (None, 1),
        (json.dumps(["epic-3"]), 1),
        (json.dumps(["epic-3", "--concurrency=4"]), 4),
        (json.dumps(["epic-3", "--concurrency=4", "--sequential"]), 1),
        (json.dumps(["epic-3", "--concurrency=0"]), 1),
        (json.dumps(["epic-3", "--concurrency=abc"]), 1),
        ("{not json", 1),
    ],
)
def test_agent_slots_reads_the_frozen_worker_cap(tmp_path, options, expected) -> None:
    from sdlc.scheduler import agent_slots

    store = _store(tmp_path)
    job_id = store.add_job(
        repo="/a", kind="build", scope="epic-3", options_json=options
    )
    assert agent_slots(store.get_job(job_id)) == expected


# --- process seam + argv helpers -----------------------------------------


def test_controller_argv_prefers_the_installed_binary(monkeypatch) -> None:
    from sdlc import scheduler

    monkeypatch.setattr(scheduler.shutil, "which", lambda _name: "/usr/local/bin/sdlc")
    assert scheduler.controller_argv() == ["/usr/local/bin/sdlc"]


def test_controller_argv_falls_back_to_this_interpreter(monkeypatch) -> None:
    """A scheduler run from a checkout dispatches its own code, not a stale install."""
    from sdlc import scheduler

    monkeypatch.setattr(scheduler.shutil, "which", lambda _name: None)
    assert scheduler.controller_argv() == [sys.executable, "-m", "sdlc.cli"]


def test_job_argv_refuses_to_resume_a_job_with_no_run(tmp_path) -> None:
    from sdlc.scheduler import job_argv

    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="build", scope="epic-3")
    with pytest.raises(ValueError, match="no run to resume"):
        job_argv(store.get_job(job_id), resume=True, prefix=["sdlc"])


def test_job_argv_ignores_frozen_options_that_are_not_a_list(tmp_path) -> None:
    from sdlc.scheduler import job_argv

    store = _store(tmp_path)
    job_id = store.add_job(
        repo="/a", kind="build", scope="epic-3", options_json=json.dumps({"a": 1})
    )
    assert job_argv(store.get_job(job_id), resume=False, prefix=["sdlc"]) == [
        "sdlc", "build", "epic-3",
    ]


def test_the_default_launcher_spawns_a_detached_process_group(tmp_path) -> None:
    import os

    from sdlc.scheduler import _default_launcher

    proc = _default_launcher(
        [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path
    )
    try:
        # start_new_session puts the child in its own group, which is what makes
        # stop() reach the job's own agents rather than just its parent.
        assert os.getpgid(proc.pid) == proc.pid
    finally:
        proc.stop()
    assert proc.poll() is not None


def test_stopping_an_already_dead_process_is_a_no_op(tmp_path) -> None:
    from sdlc.scheduler import _default_launcher

    proc = _default_launcher([sys.executable, "-c", "pass"], tmp_path)
    for _ in range(200):
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    assert proc.poll() is not None
    proc.stop()  # must not raise


def test_the_default_clock_returns_an_aware_utc_instant() -> None:
    from sdlc.scheduler import _utc_now

    assert _utc_now().tzinfo is timezone.utc


# --- races and degraded paths --------------------------------------------


def test_a_claim_lost_to_another_scheduler_is_skipped(tmp_path) -> None:
    """Losing the guarded UPDATE costs one retry, never a double run."""
    store = _store(tmp_path)
    stolen = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    store.add_job(repo=_repo(tmp_path, "beta"), kind="fix", scope="2")

    real_claim = store.claim_job
    thefts = {"n": 0}

    def racing_claim(job_id, **kwargs):
        if job_id == stolen and thefts["n"] == 0:
            thefts["n"] += 1
            real_claim(job_id, claimed_by="other:1", lease_seconds=90,
                       now=kwargs.get("now"))
        return real_claim(job_id, **kwargs)

    store.claim_job = racing_claim  # type: ignore[method-assign]
    launcher = FakeLauncher(alive_polls=1)
    result = _run(store, tmp_path=tmp_path, launcher=launcher)

    assert thefts["n"] == 1
    assert result.started == 1
    assert launcher.calls[0][0][-2:] == ["fix", "2"]


def test_losing_a_lease_mid_run_drops_our_bookkeeping(tmp_path) -> None:
    """Another scheduler reclaimed the job — its child is no longer ours to kill."""
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="42")
    launcher = FakeLauncher(alive_polls=99)
    clock = Clock()

    def sleeper(seconds: float) -> None:
        clock.advance(seconds)
        # Simulate the takeover: someone else now holds the claim.
        store.reclaim_job(job_id, claimed_by="other:1", lease_seconds=90,
                          now=clock() + timedelta(seconds=1000))

    lines: list[str] = []
    result = _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, sleeper=sleeper,
        echo=lines.append,
        config=SchedulerConfig(slots=2, poll_seconds=40.0, renew_seconds=30),
    )

    assert any("lease lost" in line for line in lines)
    assert launcher.procs[0].stopped is False  # never killed — not ours any more
    assert result.interrupted is False


def test_a_finished_run_is_not_treated_as_live(tmp_path) -> None:
    """A finished registry record must not block reclaim of its lapsed job."""
    import os

    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    clock = Clock()
    store.claim_job(job_id, claimed_by="dead:1", lease_seconds=90, now=clock())
    store.attach_run(job_id, "run-fin")

    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord(run_id="run-fin", repo=repo, db=str(tmp_path / "x.db"),
                  scope="epic-3", pid=os.getpid(), status="DONE", started_at="",
                  finished_at="2026-09-07T11:00:00+00:00")
    )

    clock.advance(200)
    launcher = FakeLauncher(alive_polls=1)
    result = _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock,
                  registry=registry)
    assert result.resumed == 1


def test_an_unknown_run_id_has_no_status(tmp_path) -> None:
    """A run the registry never recorded falls back to the exit code alone."""
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="build", scope="epic-3")
    clock = Clock()
    store.claim_job(job_id, claimed_by="dead:1", lease_seconds=90, now=clock())
    store.attach_run(job_id, "run-ghost")

    clock.advance(200)
    result = _run(store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=1),
                  clock=clock)

    assert result.resumed == 1
    assert store.get_job(job_id).state == "done"


def test_a_child_that_cannot_be_stopped_still_has_its_lease_released(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="42")

    class StubbornProc(FakeProc):
        def stop(self) -> None:
            raise OSError("operation not permitted")

    launcher = FakeLauncher(alive_polls=99)

    def stubborn(argv, cwd):
        proc = StubbornProc(4242, alive_polls=99)
        launcher.calls.append((list(argv), str(cwd)))
        launcher.procs.append(proc)
        return proc

    clock = Clock()
    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        calls["n"] += 1
        clock.advance(seconds)
        if calls["n"] >= 2:
            raise KeyboardInterrupt

    lines: list[str] = []
    result = _run(store, tmp_path=tmp_path, launcher=stubborn, clock=clock,
                  sleeper=sleeper, echo=lines.append)

    assert result.interrupted is True
    assert any("could not stop" in line for line in lines)
    assert store.get_job(job_id).claimed_by is None


def test_follow_keeps_the_loop_alive_on_an_empty_queue(tmp_path) -> None:
    """An intake can enqueue into a scheduler that is already draining."""
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    launcher = FakeLauncher(alive_polls=1)
    clock = Clock()
    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        calls["n"] += 1
        clock.advance(seconds)
        if calls["n"] == 2:
            store.add_job(repo=_repo(tmp_path, "late"), kind="fix", scope="99")
        if calls["n"] >= 5:
            raise KeyboardInterrupt

    result = _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock,
                  sleeper=sleeper, config=SchedulerConfig(slots=2, follow=True))

    assert result.started == 1
    assert launcher.calls[0][0][-2:] == ["fix", "99"]


def test_stop_escalates_to_sigkill_when_sigterm_is_ignored(monkeypatch) -> None:
    """Never assume a child dies promptly — SIGTERM, then SIGKILL (CI contract)."""
    import signal as signal_mod

    from sdlc import scheduler

    signals: list[int] = []
    monkeypatch.setattr(scheduler.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(scheduler.os, "killpg", lambda pgid, sig: signals.append(sig))

    class StubbornPopen:
        pid = 4242

        def __init__(self) -> None:
            self._waits = 0

        def poll(self):
            return None if self._waits < 2 else 0

        def wait(self, timeout=None):
            self._waits += 1
            if self._waits < 2:
                raise subprocess.TimeoutExpired("cmd", timeout or 0)
            return 0

    scheduler._PopenProcess(StubbornPopen()).stop()
    assert signals == [signal_mod.SIGTERM, signal_mod.SIGKILL]


def test_stop_gives_up_quietly_when_the_group_is_already_gone(monkeypatch) -> None:
    from sdlc import scheduler

    monkeypatch.setattr(scheduler.os, "getpgid", lambda pid: pid)

    def vanished(_pgid, _sig):
        raise ProcessLookupError

    monkeypatch.setattr(scheduler.os, "killpg", vanished)

    class LingeringPopen:
        pid = 4242

        def poll(self):
            return None

        def wait(self, timeout=None):  # pragma: no cover - never reached
            raise AssertionError("wait must not be called after a failed kill")

    scheduler._PopenProcess(LingeringPopen()).stop()  # must not raise


def test_stop_gives_up_when_sigkill_also_times_out(monkeypatch) -> None:
    """Both signals sent, neither reaped in time: return quietly, never hang."""
    from sdlc import scheduler

    monkeypatch.setattr(scheduler.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(scheduler.os, "killpg", lambda pgid, sig: None)

    class ImmovablePopen:
        pid = 4242

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("cmd", timeout or 0)

    scheduler._PopenProcess(ImmovablePopen()).stop()  # must return, not raise


def test_our_own_in_flight_job_is_never_reclaimed_from_us(tmp_path) -> None:
    """A lease that lapses while we still hold the child must not double-launch it."""
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="42")
    launcher = FakeLauncher(alive_polls=4)
    clock = Clock()

    def sleeper(seconds: float) -> None:
        clock.advance(seconds)

    result = _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, sleeper=sleeper,
        # A lease far shorter than the renewal interval: every pass after the
        # first sees our own job as "expired".
        config=SchedulerConfig(slots=2, poll_seconds=20.0, lease_seconds=5,
                               renew_seconds=600),
    )

    assert len(launcher.calls) == 1
    assert result.started == 1


def test_an_unrelated_registry_record_is_not_mistaken_for_our_run(tmp_path) -> None:
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="fix", scope="42")
    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord(run_id="someone-else", repo=repo, db=str(tmp_path / "x.db"),
                  scope="other", pid=1, status="IN_PROGRESS", started_at="")
    )

    _run(store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=2),
         registry=registry)

    assert store.get_job(job_id).run_id is None


def test_an_ownerless_running_job_with_no_run_is_still_requeued(tmp_path) -> None:
    """A claim already dropped by its holder must not strand the job `running`."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="fix", scope="42")
    clock = Clock()
    store.claim_job(job_id, claimed_by="dead:1", lease_seconds=90, now=clock())
    with store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET claimed_by = NULL, lease_until = NULL WHERE id = ?",
            (job_id,),
        )

    launcher = FakeLauncher(alive_polls=1)
    result = _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock)

    assert result.started == 1
    assert launcher.calls[0][0][-2:] == ["fix", "42"]
    assert store.get_job(job_id).state == "done"


def test_reclaim_race_lost_to_another_scheduler_skips_release(tmp_path) -> None:
    """A reclaim another scheduler already won must not release a claim we
    never held — only the owner may release (queue.py's own guard)."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="fix", scope="42")
    clock = Clock()
    store.claim_job(job_id, claimed_by="dead:1", lease_seconds=90, now=clock())
    with store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET claimed_by = NULL, lease_until = NULL WHERE id = ?",
            (job_id,),
        )

    release_calls: list[int] = []

    class RaceLostStore:
        def __init__(self, real: QueueStore) -> None:
            self._real = real

        def reclaim_job(self, *args, **kwargs):
            return None  # another scheduler already won this reclaim

        def release_claim(self, job_id, **kwargs):
            release_calls.append(job_id)
            return self._real.release_claim(job_id, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    result = _run(RaceLostStore(store), tmp_path=tmp_path, launcher=FakeLauncher())

    assert release_calls == []
    assert result.started == 0
    row = store.get_job(job_id)
    assert row is not None
    assert row.state == "running"
    assert row.claimed_by is None


# --- review follow-ups (bugfix #32.1-002) ---------------------------------


def _park_registry(registry, *, repo, pid, run_id, status, tmp_path):
    """Register a run the way a *paused* child leaves it: open, never finished."""
    registry.register(
        RunRecord(run_id=run_id, repo=repo, db=str(tmp_path / "x.db"),
                  scope="epic-3", pid=pid, status=status, started_at="")
    )


def test_a_cost_gated_run_is_parked_blocked_not_failed(tmp_path) -> None:
    """The registry status a *real* pause leaves behind is `IN_PROGRESS`, not
    `RATE_LIMITED`: `_cost_gate_close_out`/`_rate_limit_close_out` deliberately
    skip `finalize_run`, so the record stays open while the CLI exits 1. Reading
    the exit code alone buries a run `sdlc resume` could still finish.
    """
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    registry = Registry(tmp_path / "registry.json")
    launcher = FakeLauncher(alive_polls=2, code=1)
    clock = Clock()
    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            _park_registry(registry, repo=repo, pid=launcher.procs[0].pid,
                           run_id="run-gate", status="IN_PROGRESS", tmp_path=tmp_path)
        clock.advance(seconds)

    result = _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock,
                  sleeper=sleeper, registry=registry)

    job = store.get_job(job_id)
    assert job.state == "blocked"
    assert result.parked == 1
    assert "resume" in (job.reason or "")


def test_an_open_record_with_a_clean_exit_is_still_done(tmp_path) -> None:
    """Exit 0 is the stronger signal: a run that succeeded is never `blocked`."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    registry = Registry(tmp_path / "registry.json")
    launcher = FakeLauncher(alive_polls=2, code=0)
    clock = Clock()
    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            _park_registry(registry, repo=repo, pid=launcher.procs[0].pid,
                           run_id="run-ok", status="IN_PROGRESS", tmp_path=tmp_path)
        clock.advance(seconds)

    _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock,
         sleeper=sleeper, registry=registry)

    assert store.get_job(job_id).state == "done"


def test_a_finished_failed_run_is_still_failed(tmp_path) -> None:
    """A run that *did* reach FAILED stays `failed` — parking is for open runs."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    registry = Registry(tmp_path / "registry.json")
    launcher = FakeLauncher(alive_polls=2, code=1)
    clock = Clock()
    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            registry.register(
                RunRecord(run_id="run-bad", repo=repo, db=str(tmp_path / "x.db"),
                          scope="epic-3", pid=launcher.procs[0].pid,
                          status="FAILED", started_at="",
                          finished_at="2026-09-07T11:00:00+00:00")
            )
        clock.advance(seconds)

    result = _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock,
                  sleeper=sleeper, registry=registry)

    assert store.get_job(job_id).state == "failed"
    assert result.failed == 1


def test_a_job_added_with_flag_only_options_still_dispatches_its_scope(tmp_path) -> None:
    """`sdlc queue add build epic-3 --options '["--auto"]'` must build epic-3.

    ``--options`` is documented as "a JSON array of CLI flags", so the vector
    need not carry the scope positional the ``--enqueue`` path freezes. Dropping
    it silently widened the job to `all` — every epic in the repo.
    """
    from sdlc.scheduler import job_argv

    store = _store(tmp_path)
    job_id = store.add_job(
        repo=_repo(tmp_path, "alpha"), kind="build", scope="epic-3",
        options_json=json.dumps(["--auto"]),
    )
    assert job_argv(store.get_job(job_id), resume=False, prefix=["sdlc"]) == [
        "sdlc", "build", "epic-3", "--auto",
    ]


def test_frozen_options_that_carry_the_scope_are_replayed_verbatim(tmp_path) -> None:
    """The `--enqueue` vector already holds the scope — never duplicate it."""
    from sdlc.scheduler import job_argv

    store = _store(tmp_path)
    job_id = store.add_job(
        repo=_repo(tmp_path, "alpha"), kind="build", scope="epic-3",
        options_json=json.dumps(["epic-3", "--auto"]),
    )
    assert job_argv(store.get_job(job_id), resume=False, prefix=["sdlc"]) == [
        "sdlc", "build", "epic-3", "--auto",
    ]


def test_a_two_token_harness_value_is_not_mistaken_for_a_scope(tmp_path) -> None:
    """`--harness <spec>` is the one two-token flag; its value is not a scope."""
    from sdlc.scheduler import job_argv

    store = _store(tmp_path)
    job_id = store.add_job(
        repo=_repo(tmp_path, "alpha"), kind="build", scope="epic-3",
        options_json=json.dumps(["--harness", "build=claude"]),
    )
    assert job_argv(store.get_job(job_id), resume=False, prefix=["sdlc"]) == [
        "sdlc", "build", "epic-3", "--harness", "build=claude",
    ]


def test_a_registry_record_from_another_repo_is_never_attached(tmp_path) -> None:
    """OS pid reuse must not link a job to a foreign repo's run."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="fix", scope="42")
    registry = Registry(tmp_path / "registry.json")
    launcher = FakeLauncher(alive_polls=2)
    clock = Clock()
    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            registry.register(
                RunRecord(run_id="other-repo", repo=_repo(tmp_path, "beta"),
                          db=str(tmp_path / "x.db"), scope="99",
                          pid=launcher.procs[0].pid, status="IN_PROGRESS",
                          started_at="")
            )
        clock.advance(seconds)

    _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock,
         sleeper=sleeper, registry=registry)

    assert store.get_job(job_id).run_id is None


def test_a_finished_registry_record_is_never_attached(tmp_path) -> None:
    """A stale record whose run already ended cannot be this child's run."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="fix", scope="42")
    registry = Registry(tmp_path / "registry.json")
    launcher = FakeLauncher(alive_polls=2)
    clock = Clock()
    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            registry.register(
                RunRecord(run_id="stale", repo=repo, db=str(tmp_path / "x.db"),
                          scope="42", pid=launcher.procs[0].pid, status="DONE",
                          started_at="", finished_at="2026-09-07T11:00:00+00:00")
            )
        clock.advance(seconds)

    _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock,
         sleeper=sleeper, registry=registry)

    assert store.get_job(job_id).run_id is None


def test_a_finished_job_announces_a_queue_specific_event(tmp_path) -> None:
    """The child already fires `run_finished` for its own run — don't double up.

    `finalize_run` (build.py) notifies `run_finished` from inside the job's own
    subprocess, which inherits our environment. A second `run_finished` here
    meant an operator draining ten jobs got twenty near-identical messages.
    """
    store = _store(tmp_path)
    store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="42")
    events: list[tuple[str, dict]] = []

    _run(
        store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=1, code=0),
        notifier=lambda event, **fields: events.append((event, fields)),
    )

    assert [event for event, _ in events] == ["queue_job_finished"]
    assert events[0][1]["terminal"] == "DONE"


def test_a_build_waiting_on_a_slot_is_not_stamped_repo_busy(tmp_path) -> None:
    """Story 32.1-003: build/build is not exclusive, so "repo busy" would lie.

    With one slot the second build genuinely waits, but on a *slot*, not on the
    repo — stamping "repo busy" here (what the pre-32.1-003 predicate did for
    every busy repo) would tell FX the queue is blocked by a rule that no
    longer exists.
    """
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    store.add_job(repo=repo, kind="build", scope="epic-1")
    waiting = store.add_job(repo=repo, kind="build", scope="epic-2")

    seen: list[str | None] = []
    clock = Clock()

    def sleeper(seconds: float) -> None:
        seen.append(store.get_job(waiting).reason)
        clock.advance(seconds)

    _run(store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=2),
         clock=clock, sleeper=sleeper,
         config=SchedulerConfig(slots=1, poll_seconds=1.0))

    assert "repo busy" not in seen
    assert store.get_job(waiting).state == "done"


def test_a_fix_waiting_on_a_running_build_is_stamped_repo_busy(tmp_path) -> None:
    """The other half of the relaxed predicate: fix-behind-build is still blocked.

    `running_repos(kind="fix")` is empty here, so only the `job.kind == "fix"`
    arm can explain this job — the arm that keeps a fix off a repo another
    kind of run already holds.

    Both jobs are pinned to one class because Story 32.3-001 derives an omitted
    priority from the kind (a `fix` outranks a `build`), which would claim the
    fix first and leave the *build* waiting — the mirror image of the case under
    test. Equal classes fall back to FIFO, so the build runs and the fix waits.
    """
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    store.add_job(repo=repo, kind="build", scope="epic-1", priority="normal")
    waiting = store.add_job(repo=repo, kind="fix", scope="1", priority="normal")

    seen: list[str | None] = []
    clock = Clock()

    def sleeper(seconds: float) -> None:
        seen.append(store.get_job(waiting).reason)
        clock.advance(seconds)

    _run(store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=2),
         clock=clock, sleeper=sleeper,
         config=SchedulerConfig(slots=2, poll_seconds=1.0))

    assert "repo busy" in seen
    assert store.get_job(waiting).state == "done"


# --- AC1-AC4: one rate-limit window for the whole queue (Story 32.2-001) ----


def _ledger_run(
    repo: str,
    *,
    status: str = "RATE_LIMITED",
    reset_at: float | None = None,
    max_wait_s: int | None = None,
) -> tuple[str, str]:
    """Seed a repo's ledger with one run in ``status``; return (run_id, db path).

    The real ledger, not a fake: rate-limit truth stays in the run's ledger
    (this story only caches its reset in the queue), so the scheduler's read of
    it has to be exercised against the schema `build.py` actually writes.
    """
    from sdlc.build import Ledger

    db = str(Path(repo) / ".sdlc-state.db")
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-3", "build")
    config: dict[str, object] = {}
    if reset_at is not None:
        config["rate_limit_reset_at"] = reset_at
    if max_wait_s is not None:
        config["rate_limit_max_wait_s"] = max_wait_s
    if config:
        ledger.event_log(run_id, "", "info", "config", json.dumps(config))
    ledger.run_update_status(run_id, status)
    return run_id, db


def _parked_job(store, registry, tmp_path, name: str, **ledger_kwargs):
    """A job whose run is rate-limit parked and whose scheduler is gone.

    The shape a previous drain leaves behind: state `running` with a lapsed
    lease (so it is a reclaim candidate) and a dead run pid.
    """
    repo = _repo(tmp_path, name)
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    store.claim_job(job_id, claimed_by="dead:1", lease_seconds=0,
                    now=datetime(2026, 9, 7, 11, 0, tzinfo=timezone.utc))
    run_id, db = _ledger_run(repo, **ledger_kwargs)
    store.attach_run(job_id, run_id)
    registry.register(
        RunRecord(run_id=run_id, repo=repo, db=db, scope="epic-3",
                  pid=_dead_pid(), status="IN_PROGRESS", started_at="")
    )
    return job_id, run_id, db


class ResumingLauncher(FakeLauncher):
    """A FakeLauncher whose `resume` child clears the run's rate-limit park.

    What a real `sdlc resume` does — it re-enters the run, so the ledger stops
    saying RATE_LIMITED. Without it the fake child would leave the park standing
    and the queue would (rightly) pause on it again the moment it was reaped.
    """

    def __init__(self, ledgers: dict[str, str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._ledgers = ledgers

    def __call__(self, argv, cwd):
        from sdlc.build import Ledger

        argv = list(argv)
        if "resume" in argv:
            run_id = argv[argv.index("--run") + 1]
            Ledger(self._ledgers[run_id]).run_update_status(run_id, "DONE")
        return super().__call__(argv, cwd)


class RecordingNotifier:
    """Captures every notify event so "one pause, one resume" is assertable."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event, **fields):
        self.events.append((event, fields))

    def names(self, event: str) -> list[dict]:
        return [fields for name, fields in self.events if name == event]


def _stop_after(clock, passes: int):
    """A sleeper that advances the fake clock, then interrupts the drain."""
    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        calls["n"] += 1
        clock.advance(seconds)
        if calls["n"] >= passes:
            raise KeyboardInterrupt

    return sleeper


def test_a_rate_limited_run_pauses_the_whole_queue(tmp_path) -> None:
    """One run's RATE_LIMITED ledger row parks *dispatch*, host-wide (AC1).

    The reset the run recorded becomes the queue's own `paused_until`, and the
    other repo's queued job is not claimed — one window, waited out once.
    """
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    reset_at = clock.now.timestamp() + 600
    job_id, run_id, db = _parked_job(store, registry, tmp_path, "alpha", reset_at=reset_at)
    other = store.add_job(repo=_repo(tmp_path, "beta"), kind="fix", scope="42")

    launcher = FakeLauncher(alive_polls=1)
    notifier = RecordingNotifier()
    result = _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, registry=registry,
        notifier=notifier, sleeper=_stop_after(clock, 3),
        config=SchedulerConfig(slots=2, poll_seconds=1.0),
    )

    assert launcher.calls == []  # nothing dispatched while the window is closed
    assert store.get_job(other).state == "queued"
    pause = store.dispatch_pause()
    assert pause is not None
    assert pause.run_id == run_id
    assert pause.is_active(clock()) is True
    assert pause.paused_until == datetime.fromtimestamp(reset_at, timezone.utc).isoformat()
    assert len(notifier.names("queue_paused")) == 1
    assert result.paused == 1
    # The parked job is still resumable, never written off as terminal.
    assert store.get_job(job_id).state == "running"
    assert store.get_job(job_id).run_id == run_id


def test_the_queue_resumes_the_parked_job_itself_at_the_reset(tmp_path) -> None:
    """The window reopens → one resume, through `resume.py`, then claiming again (AC2)."""
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    reset_at = clock.now.timestamp() + 20
    job_id, run_id, db = _parked_job(store, registry, tmp_path, "alpha", reset_at=reset_at)
    other = store.add_job(repo=_repo(tmp_path, "beta"), kind="fix", scope="42")

    launcher = ResumingLauncher({run_id: db}, alive_polls=1)
    notifier = RecordingNotifier()
    result = _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, registry=registry,
        notifier=notifier, config=SchedulerConfig(slots=2, poll_seconds=10.0),
    )

    assert launcher.commands[0] == ["resume", "--run", run_id]
    assert ["fix", "42"] in [cmd[:2] for cmd in launcher.commands]
    assert store.dispatch_pause() is None
    assert len(notifier.names("queue_paused")) == 1
    assert len(notifier.names("queue_resumed")) == 1
    assert result.resumed == 1
    assert store.get_job(other).state == "done"
    assert store.get_job(job_id).state == "done"


def test_a_second_rate_limited_run_does_not_re_announce_the_window(tmp_path) -> None:
    """Two parked runs, one window: one pause notify and one resume notify (AC2)."""
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    reset_at = clock.now.timestamp() + 20
    _parked_job(store, registry, tmp_path, "alpha", reset_at=reset_at)
    _parked_job(store, registry, tmp_path, "beta", reset_at=reset_at)

    notifier = RecordingNotifier()
    _run(
        store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=1), clock=clock,
        registry=registry, notifier=notifier,
        config=SchedulerConfig(slots=2, poll_seconds=10.0),
    )

    assert len(notifier.names("queue_paused")) == 1
    assert len(notifier.names("queue_resumed")) == 1


def test_a_limit_without_a_reset_pauses_for_the_configured_max_wait(tmp_path) -> None:
    """No reset time → the run's own `rate_limit_max_wait` bounds the pause (AC3)."""
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    _parked_job(store, registry, tmp_path, "alpha", max_wait_s=900)

    _run(
        store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=1), clock=clock,
        registry=registry, sleeper=_stop_after(clock, 2),
        config=SchedulerConfig(slots=2, poll_seconds=1.0),
    )

    pause = store.dispatch_pause()
    assert pause is not None
    expected = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=900)
    assert pause.paused_until == expected.isoformat()


def test_an_available_probe_reopens_the_queue_before_the_blind_wait_ends(tmp_path) -> None:
    """The reset-less wait is probed, mirroring `_probe_parked_reset` (AC3).

    Only an AVAILABLE verdict clears the window early — the same
    never-fail-open contract the run-level gate keeps (issue #564).
    """
    from sdlc.capability import ProbeStatus
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    job_id, run_id, db = _parked_job(store, registry, tmp_path, "alpha", max_wait_s=18000)

    launcher = ResumingLauncher({run_id: db}, alive_polls=1)
    notifier = RecordingNotifier()
    result = _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, registry=registry,
        notifier=notifier, probe=lambda: ProbeStatus.AVAILABLE,
        config=SchedulerConfig(slots=2, poll_seconds=600.0),
    )

    # Resumed long before the 5h blind wait would have elapsed.
    assert clock.now < datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=18000)
    assert launcher.commands[0] == ["resume", "--run", run_id]
    assert store.dispatch_pause() is None
    assert len(notifier.names("queue_resumed")) == 1
    assert result.resumed == 1


def test_a_closed_probe_keeps_the_queue_paused(tmp_path) -> None:
    """UNAVAILABLE (and UNKNOWN) keep the window shut — the gate never fails open."""
    from sdlc.capability import ProbeStatus
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    _parked_job(store, registry, tmp_path, "alpha", max_wait_s=18000)

    launcher = FakeLauncher(alive_polls=1)
    _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, registry=registry,
        probe=lambda: ProbeStatus.UNAVAILABLE, sleeper=_stop_after(clock, 4),
        config=SchedulerConfig(slots=2, poll_seconds=600.0),
    )

    assert launcher.calls == []
    assert store.dispatch_pause().is_active(clock()) is True


def test_the_probe_is_throttled_rather_than_run_every_pass(tmp_path) -> None:
    """A blind wait must not become one API request per poll interval."""
    from sdlc.capability import ProbeStatus
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    _parked_job(store, registry, tmp_path, "alpha", max_wait_s=18000)

    probes = {"n": 0}

    def probe() -> ProbeStatus:
        probes["n"] += 1
        return ProbeStatus.UNAVAILABLE

    _run(
        store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=1), clock=clock,
        registry=registry, probe=probe, sleeper=_stop_after(clock, 20),
        config=SchedulerConfig(slots=2, poll_seconds=100.0),
    )

    # 20 passes covering 1900s: one probe per 300s window, not one per pass.
    assert probes["n"] == 6


def test_a_rate_limited_job_whose_child_exits_is_parked_not_failed(tmp_path) -> None:
    """A run that parked itself exits non-zero — that is a pause, not a failure.

    The job is handed back with an expired lease (the `release_claim` shape) so
    the reclaim path resumes it once the window reopens, rather than being
    stamped terminal and needing a manual `sdlc queue requeue`.
    """
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    run_id, db = _ledger_run(repo, reset_at=clock.now.timestamp() + 600)

    launcher = FakeLauncher(alive_polls=1, code=1)
    calls = {"n": 0}

    def sleeper(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:  # the child registers its run, the way run_build does
            registry.register(
                RunRecord(run_id=run_id, repo=repo, db=db, scope="epic-3",
                          pid=launcher.procs[0].pid, status="IN_PROGRESS",
                          started_at="")
            )
        clock.advance(seconds)
        if calls["n"] >= 5:
            raise KeyboardInterrupt

    result = _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, registry=registry,
        sleeper=sleeper, config=SchedulerConfig(slots=2, poll_seconds=1.0),
    )

    job = store.get_job(job_id)
    assert job.state == "running"  # resumable, not terminal
    assert job.run_id == run_id
    assert result.failed == 0
    assert result.paused == 1
    assert store.dispatch_pause() is not None


def test_a_run_waiting_in_process_does_not_pause_the_whole_queue(tmp_path) -> None:
    """A bounded in-process wait is not a durable park — the queue keeps going.

    `build._rate_limit_wait` flips a *live* run to RATE_LIMITED for the duration
    of a wait that stays inside its own auto-wait cap, writes no reset epoch, and
    un-flips itself seconds later. Treating that as a host park opened a window
    on the reset-less five-hour `max-wait` fallback and stalled every other repo
    behind a run that needed no help at all.
    """
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    alpha = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=alpha, kind="build", scope="epic-3")
    store.claim_job(job_id, claimed_by="peer:1", lease_seconds=900,
                    now=datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc))
    run_id, db = _ledger_run(alpha, reset_at=None)  # no reset: the in-process shape
    store.attach_run(job_id, run_id)
    registry.register(  # our own pid: a run that is unmistakably still alive
        RunRecord(run_id=run_id, repo=alpha, db=db, scope="epic-3",
                  pid=os.getpid(), status="IN_PROGRESS", started_at="")
    )
    beta = _repo(tmp_path, "beta")
    store.add_job(repo=beta, kind="fix", scope="7")

    clock = Clock()
    launcher = FakeLauncher(alive_polls=1)
    notifier = RecordingNotifier()
    result = _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, registry=registry,
        notifier=notifier, sleeper=_stop_after(clock, 4),
        config=SchedulerConfig(slots=2, poll_seconds=1.0),
    )

    assert store.dispatch_pause() is None
    assert notifier.names("queue_paused") == []
    assert result.paused == 0
    assert [cwd for _, cwd in launcher.calls] == [beta]  # beta was not held back


def test_a_stale_pause_row_never_wedges_the_queue_shut(tmp_path) -> None:
    """An elapsed window with no rate-limited run left resumes and announces once."""
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    clock = Clock()
    store.pause_dispatch(
        until=clock.now - timedelta(seconds=1), reason="rate limited", now=clock.now
    )
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="42")

    launcher = FakeLauncher(alive_polls=1)
    notifier = RecordingNotifier()
    _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, notifier=notifier,
        config=SchedulerConfig(slots=2, poll_seconds=1.0),
    )

    assert store.get_job(job_id).state == "done"
    assert store.dispatch_pause() is None
    assert len(notifier.names("queue_resumed")) == 1


def test_junk_in_a_run_config_reads_as_no_reset_and_the_default_cap(tmp_path) -> None:
    """Ledger config is whatever JSON was written; junk must degrade, not crash."""
    from sdlc.scheduler import _DEFAULT_RATE_LIMIT_MAX_WAIT_S, _as_float, _as_int

    assert _as_float(1757246400) == 1757246400.0
    assert _as_float("1757246400.5") == 1757246400.5
    assert _as_float("2026-09-07T12:00:00+00:00") is not None
    assert _as_float(None) is None
    assert _as_float(True) is None  # a bool is not an epoch, whatever int() says
    assert _as_float("soon") is None
    assert _as_float(["nope"]) is None

    assert _as_int(900, 18000) == 900
    assert _as_int("900", 18000) == 900
    assert _as_int(0, 18000) == _DEFAULT_RATE_LIMIT_MAX_WAIT_S
    assert _as_int(-5, 18000) == 18000
    assert _as_int("later", 18000) == 18000


def test_an_unreadable_ledger_never_pauses_the_queue(tmp_path) -> None:
    """A corrupt ledger is no evidence of a rate limit — the drain carries on."""
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    repo = _repo(tmp_path, "alpha")
    corrupt = Path(repo) / ".sdlc-state.db"
    corrupt.write_bytes(b"this is not a sqlite database at all")
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    clock = Clock()
    store.claim_job(job_id, claimed_by="dead:1", lease_seconds=0,
                    now=clock.now - timedelta(hours=1))
    store.attach_run(job_id, "run-abc")
    registry.register(
        RunRecord(run_id="run-abc", repo=repo, db=str(corrupt), scope="epic-3",
                  pid=_dead_pid(), status="IN_PROGRESS", started_at="")
    )

    launcher = FakeLauncher(alive_polls=1)
    _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, registry=registry,
        config=SchedulerConfig(slots=2, poll_seconds=1.0),
    )

    assert store.dispatch_pause() is None
    assert launcher.commands[0] == ["resume", "--run", "run-abc"]


def test_a_probe_that_raises_keeps_the_window_shut(tmp_path) -> None:
    """The gate protects a possibly-closed quota, so it never fails open."""
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    _parked_job(store, registry, tmp_path, "alpha", max_wait_s=18000)

    def exploding_probe():
        raise RuntimeError("the probe itself fell over")

    launcher = FakeLauncher(alive_polls=1)
    _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, registry=registry,
        probe=exploding_probe, sleeper=_stop_after(clock, 4),
        config=SchedulerConfig(slots=2, poll_seconds=600.0),
    )

    assert launcher.calls == []
    assert store.dispatch_pause().is_active(clock()) is True


def test_a_pause_whose_run_is_no_longer_registered_is_not_probed(tmp_path) -> None:
    """A pruned registry leaves the verdict nowhere to go — hold the window.

    "No evidence" keeps the pause, exactly as an UNKNOWN probe verdict does.
    """
    from sdlc.capability import ProbeStatus
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    clock = Clock()
    store.pause_dispatch(
        until=clock.now + timedelta(seconds=3600), reason="rate limited",
        run_id="run-gone", repo=str(tmp_path / "alpha"), now=clock.now,
    )
    store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="42")

    launcher = FakeLauncher(alive_polls=1)
    _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock,
        probe=lambda: ProbeStatus.AVAILABLE, sleeper=_stop_after(clock, 4),
        config=SchedulerConfig(slots=2, poll_seconds=600.0),
    )

    assert launcher.calls == []
    assert store.dispatch_pause().is_active(clock()) is True


def test_a_pause_row_with_a_corrupt_probe_stamp_is_probed_immediately(tmp_path) -> None:
    """An unparseable `probed_at` must not stall the probe forever."""
    from sdlc.queue import QueuePause
    from sdlc.scheduler import SchedulerConfig, _Scheduler

    store = _store(tmp_path)
    clock = Clock()
    scheduler = _Scheduler(
        store, config=SchedulerConfig(), registry=Registry(tmp_path / "registry.json"),
        launcher=FakeLauncher(), clock=clock, sleeper=lambda _s: None,
        notifier=lambda *a, **k: None, version_check=_clean, probe=None,
        approval_probe=lambda _root, _pr: None,
        fix_rounds=lambda _db, _run: 0, plan_files=lambda _db, _run: [],
        echo=lambda _line: None, identity="test:1",
    )

    corrupt = QueuePause(paused_until="", paused_at="not-a-time", probed_at="junk")
    assert scheduler._due_for_probe(corrupt) is True
    naive = QueuePause(paused_until="", paused_at="2026-09-07T11:00:00")
    assert scheduler._due_for_probe(naive) is True


def test_a_reused_probe_helper_that_itself_raises_keeps_the_window_shut(monkeypatch, tmp_path) -> None:
    """A failure in `_probe_parked_reset` itself (not the probe callback it
    wraps — that path is `test_a_probe_that_raises_keeps_the_window_shut`)
    must never fail the gate open either."""
    import sdlc.build as build_module
    from sdlc.capability import ProbeStatus
    from sdlc.queue import QueuePause
    from sdlc.scheduler import SchedulerConfig, _Scheduler

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    _, run_id, db = _parked_job(store, registry, tmp_path, "alpha", max_wait_s=18000)

    def exploding_helper(*_args, **_kwargs):
        raise RuntimeError("the reused helper itself fell over")

    monkeypatch.setattr(build_module, "_probe_parked_reset", exploding_helper)

    scheduler = _Scheduler(
        store, config=SchedulerConfig(), registry=registry,
        launcher=FakeLauncher(), clock=Clock(), sleeper=lambda _s: None,
        notifier=lambda *a, **k: None, version_check=_clean,
        probe=lambda: ProbeStatus.AVAILABLE,
        echo=lambda _line: None, identity="test:1",
    )
    pause = QueuePause(
        paused_until="2026-09-07T12:00:00+00:00", paused_at="2026-09-07T11:00:00+00:00",
        run_id=run_id,
    )
    assert scheduler._window_reopened(pause) is False


def test_a_pause_ledger_that_fails_to_construct_is_no_evidence(monkeypatch, tmp_path) -> None:
    """A ledger the queue cannot even open has nowhere to log a verdict —
    hold the window, same as any other "no evidence" case."""
    import sdlc.build as build_module
    from sdlc.queue import QueuePause
    from sdlc.scheduler import SchedulerConfig, _Scheduler

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    _, run_id, db = _parked_job(store, registry, tmp_path, "alpha", max_wait_s=18000)

    def exploding_ledger(_db_path):
        raise RuntimeError("cannot open this ledger")

    monkeypatch.setattr(build_module, "Ledger", exploding_ledger)

    scheduler = _Scheduler(
        store, config=SchedulerConfig(), registry=registry,
        launcher=FakeLauncher(), clock=Clock(), sleeper=lambda _s: None,
        notifier=lambda *a, **k: None, version_check=_clean, probe=None,
        echo=lambda _line: None, identity="test:1",
    )
    pause = QueuePause(paused_until="", paused_at="", run_id=run_id)
    assert scheduler._pause_ledger(pause) is None


def test_a_window_a_peer_scheduler_already_opened_is_not_re_announced(tmp_path) -> None:
    """Two `sdlc queue run` processes, one subscription, one announcement.

    The store's `pause_dispatch` is the arbiter: whoever writes the row first
    owns the notification, so a peer that discovers the same window between our
    read and our write stays silent rather than double-announcing it.
    """
    from sdlc.queue import QueuePause
    from sdlc.scheduler import SchedulerConfig, _RateLimitPark, _Scheduler

    store = _store(tmp_path)
    clock = Clock()
    notifier = RecordingNotifier()
    scheduler = _Scheduler(
        store, config=SchedulerConfig(), registry=Registry(tmp_path / "registry.json"),
        launcher=FakeLauncher(), clock=clock, sleeper=lambda _s: None,
        notifier=notifier, version_check=_clean, probe=None,
        approval_probe=lambda _root, _pr: None,
        fix_rounds=lambda _db, _run: 0, plan_files=lambda _db, _run: [],
        echo=lambda _line: None, identity="test:1",
    )
    park = _RateLimitPark(
        run_id="run-a", repo=str(tmp_path / "alpha"), db=tmp_path / "x.db",
        reset_at=clock.now.timestamp() + 600, max_wait_s=18000,
    )

    scheduler._pause_dispatch(park)
    assert len(notifier.names("queue_paused")) == 1
    scheduler._pause_dispatch(park)  # a peer already opened this window
    assert len(notifier.names("queue_paused")) == 1

    # A reset that has already passed is not a window at all.
    store.clear_pause()
    stale = _RateLimitPark(
        run_id="run-b", repo=str(tmp_path / "beta"), db=tmp_path / "x.db",
        reset_at=clock.now.timestamp() - 1, max_wait_s=18000,
    )
    scheduler._pause_dispatch(stale)
    assert store.dispatch_pause() is None
    assert len(notifier.names("queue_paused")) == 1

    # A pause with no attributed run has no ledger to log a probe verdict into.
    assert scheduler._pause_ledger(QueuePause(paused_until="", paused_at="")) is None


def test_every_parked_run_shares_one_window_even_when_slots_are_scarce(tmp_path) -> None:
    """Parked jobs outnumbering free slots is still *one* window (AC2).

    Discovery used to stop at the first park, so the queue cached only that
    run's reset. With a single slot the second park was found on a later pass —
    after the first window had been waited out and lifted — and opened a
    *second* window from a stale ledger, stalling dispatch on evidence the job
    already running had disproved. One shared subscription, one pause, one
    resume: the window has to cover the latest reset any parked run recorded.
    """
    from sdlc.capability import ProbeStatus
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    early, late = clock.now.timestamp() + 600, clock.now.timestamp() + 900
    alpha, alpha_run, alpha_db = _parked_job(
        store, registry, tmp_path, "alpha", reset_at=early
    )
    beta, beta_run, beta_db = _parked_job(
        store, registry, tmp_path, "beta", reset_at=late
    )

    launcher = ResumingLauncher({alpha_run: alpha_db, beta_run: beta_db}, alive_polls=1)
    notifier = RecordingNotifier()
    result = _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, registry=registry,
        notifier=notifier, probe=lambda: ProbeStatus.UNKNOWN,
        sleeper=_stop_after(clock, 40),  # safety net; the drain ends well before
        config=SchedulerConfig(slots=1, poll_seconds=100.0),
    )

    assert len(notifier.names("queue_paused")) == 1
    assert len(notifier.names("queue_resumed")) == 1
    assert result.paused == 1
    # The one window covers the *latest* reset, not whichever park was seen first.
    assert notifier.names("queue_paused")[0]["reset_at"] == (
        datetime.fromtimestamp(late, timezone.utc).isoformat()
    )
    assert store.dispatch_pause() is None
    assert sorted(cmd[2] for cmd in launcher.commands if cmd[0] == "resume") == sorted(
        [alpha_run, beta_run]
    )
    assert store.get_job(alpha).state == "done"
    assert store.get_job(beta).state == "done"


def test_probe_none_means_no_probe_rather_than_the_live_api(tmp_path, monkeypatch) -> None:
    """`run_queue(probe=None)` must honour the `RateLimitProbe | None` type.

    `None` was overwritten with `default_rate_limit_probe`, so a caller — a test
    above all — asking for *no* probe silently got a real API request against
    the resolved harness. CLAUDE.md's offline CI contract forbids that, and
    `_window_reopened`'s own `if self._probe is None` guard says it was never
    the intent.
    """
    import sdlc.build as build
    from sdlc.capability import ProbeStatus
    from sdlc.scheduler import SchedulerConfig

    calls = {"n": 0}

    def live_probe() -> ProbeStatus:
        calls["n"] += 1
        return ProbeStatus.AVAILABLE

    monkeypatch.setattr(build, "default_rate_limit_probe", live_probe)

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    _parked_job(store, registry, tmp_path, "alpha", max_wait_s=18000)

    _run(
        store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=1), clock=clock,
        registry=registry, probe=None, sleeper=_stop_after(clock, 5),
        config=SchedulerConfig(slots=2, poll_seconds=100.0),
    )

    # 500s elapsed — past the 300s throttle, so a wired probe would have fired.
    assert calls["n"] == 0
    assert store.dispatch_pause().is_active(clock()) is True


def test_omitting_the_probe_still_wires_the_live_one(tmp_path, monkeypatch) -> None:
    """The sentinel must not cost production its default probe."""
    import sdlc.build as build
    from sdlc.capability import ProbeStatus
    from sdlc.scheduler import SchedulerConfig

    calls = {"n": 0}

    def live_probe() -> ProbeStatus:
        calls["n"] += 1
        return ProbeStatus.AVAILABLE

    monkeypatch.setattr(build, "default_rate_limit_probe", live_probe)

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    _, run_id, db = _parked_job(store, registry, tmp_path, "alpha", max_wait_s=18000)

    _run(
        store, tmp_path=tmp_path, launcher=ResumingLauncher({run_id: db}, alive_polls=1),
        clock=clock, registry=registry, sleeper=_stop_after(clock, 20),
        config=SchedulerConfig(slots=2, poll_seconds=100.0),
    )

    assert calls["n"] >= 1
    assert store.dispatch_pause() is None


def test_a_recorded_reset_outranks_another_run_s_max_wait_guess(tmp_path) -> None:
    """Evidence beats the fallback when both are parked at once.

    A reset-less park's window is the run's own `max_wait` cap — a conservative
    *guess*, five hours by default. Sizing the shared window off it while
    another run holds a real reset epoch would stall every repo for hours on no
    evidence, so the guess only sizes the window when nothing better is parked.
    """
    from sdlc.capability import ProbeStatus
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    reset_at = clock.now.timestamp() + 600
    _parked_job(store, registry, tmp_path, "alpha", reset_at=reset_at)
    _parked_job(store, registry, tmp_path, "beta", max_wait_s=18000)

    _run(
        store, tmp_path=tmp_path, launcher=FakeLauncher(alive_polls=1), clock=clock,
        registry=registry, probe=lambda: ProbeStatus.UNKNOWN,
        sleeper=_stop_after(clock, 2),
        config=SchedulerConfig(slots=2, poll_seconds=100.0),
    )

    pause = store.dispatch_pause()
    assert pause is not None
    assert pause.paused_until == (
        datetime.fromtimestamp(reset_at, timezone.utc).isoformat()
    )
    assert pause.source == "reset-epoch"
