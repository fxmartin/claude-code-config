# ABOUTME: Behavior tests for the queue scheduler loop (Story 32.1-002).
# ABOUTME: Claim/lease/renew/reclaim, per-repo exclusivity, slot cap, park, notify.

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

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
