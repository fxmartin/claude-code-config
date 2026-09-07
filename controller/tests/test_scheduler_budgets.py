# ABOUTME: Behavior tests for the scheduler's per-job budget breaker (Story 32.3-001).
# ABOUTME: Fix-round and wall-clock caps park a job `needs_attention` with notify.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sdlc.doctor import Finding
from sdlc.queue import QueueStore
from sdlc.registry import Registry, RunRecord


class FakeProc:
    """A job process that never finishes on its own — only a stop ends it."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.stopped = False

    def poll(self) -> int | None:
        return 0 if self.stopped else None

    def stop(self) -> None:
        self.stopped = True


class FakeLauncher:
    def __init__(self) -> None:
        self.procs: list[FakeProc] = []

    def __call__(self, argv, cwd):
        proc = FakeProc(90000 + len(self.procs))
        self.procs.append(proc)
        return proc


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


def _run(store, *, tmp_path, launcher, clock, **kwargs):
    from sdlc.scheduler import SchedulerConfig, run_queue

    def sleeper(seconds: float) -> None:
        clock.advance(seconds)

    return run_queue(
        store,
        config=kwargs.pop("config", SchedulerConfig(slots=2, poll_seconds=600.0)),
        registry=kwargs.pop("registry", Registry(tmp_path / "registry.json")),
        launcher=launcher,
        clock=clock,
        sleeper=kwargs.pop("sleeper", sleeper),
        notifier=kwargs.pop("notifier", lambda *a, **k: None),
        version_check=kwargs.pop("version_check", _clean),
        echo=kwargs.pop("echo", lambda _line: None),
        **kwargs,
    )


def test_wall_clock_cap_parks_the_job_needs_attention(tmp_path) -> None:
    """AC3: a job that outlives its class's wall-clock cap is parked, not left running."""
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    budget = store.get_job(job_id).job_budget()

    clock = Clock()
    launcher = FakeLauncher()
    # One poll of 600s never reaches the cap; a poll longer than it always does.
    result = _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock,
        config=_config(poll_seconds=budget.wall_clock_seconds + 1),
    )

    job = store.get_job(job_id)
    assert job.state == "needs_attention"
    assert "wall-clock" in (job.reason or "")
    assert launcher.procs[0].stopped is True
    assert result.parked == 1


def test_fix_round_cap_parks_the_job_needs_attention(tmp_path) -> None:
    """AC3: the ledger's `bugfix` rounds are the queue's own thrash breaker."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="fix", scope="1")
    budget = store.get_job(job_id).job_budget()

    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    launcher = FakeLauncher()

    def fix_rounds(_db: str, _run_id: str) -> int:
        return budget.max_fix_rounds

    # The job's subprocess registers a run; the scheduler joins on its pid.
    def launching(argv, cwd):
        proc = launcher(argv, cwd)
        registry.register(
            RunRecord(run_id="run-thrash", repo=repo, db=str(tmp_path / "x.db"),
                      scope="1", pid=proc.pid, status="IN_PROGRESS", started_at="")
        )
        return proc

    result = _run(
        store, tmp_path=tmp_path, launcher=launching, clock=clock,
        registry=registry, fix_rounds=fix_rounds,
    )

    job = store.get_job(job_id)
    assert job.state == "needs_attention"
    assert "fix round" in (job.reason or "")
    assert launcher.procs[0].stopped is True
    assert result.parked == 1


def test_a_job_inside_its_budget_is_never_parked(tmp_path) -> None:
    """The breaker must not fire on a healthy job — no cap, no park."""
    store = _store(tmp_path)
    store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")

    clock = Clock()

    class OneShot(FakeLauncher):
        def __call__(self, argv, cwd):
            proc = super().__call__(argv, cwd)
            proc.stopped = True  # exits cleanly on the first reap
            return proc

    launcher = OneShot()
    result = _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock)

    assert result.parked == 0
    assert store.get_job(1).state == "done"


def test_a_budget_park_notifies_down_the_queue_event(tmp_path) -> None:
    """AC3: parked with notify — the same `queue_job_finished` path as any terminal."""
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    budget = store.get_job(job_id).job_budget()

    seen: list[tuple[str, dict]] = []

    def notifier(event, **fields):
        seen.append((event, fields))

    _run(
        store, tmp_path=tmp_path, launcher=FakeLauncher(), clock=Clock(),
        notifier=notifier,
        config=_config(poll_seconds=budget.wall_clock_seconds + 1),
    )

    assert seen == [
        (
            "queue_job_finished",
            {
                "repo": "alpha",
                "subject": f"queue job {job_id} (fix 1)",
                "terminal": "NEEDS_ATTENTION",
                "run": "",
            },
        )
    ]


def test_a_parked_job_can_be_requeued_and_resumed(tmp_path) -> None:
    """The park is not a dead end — `sdlc queue requeue` re-arms it."""
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    budget = store.get_job(job_id).job_budget()

    _run(
        store, tmp_path=tmp_path, launcher=FakeLauncher(), clock=Clock(),
        config=_config(poll_seconds=budget.wall_clock_seconds + 1),
    )
    assert store.get_job(job_id).state == "needs_attention"

    store.requeue_job(job_id)
    assert store.get_job(job_id).state == "queued"


def test_ledger_fix_rounds_counts_bugfix_stage_attempts(tmp_path) -> None:
    """The default counter reads the run's own ledger, not the pipeline's counter."""
    from sdlc.build import Ledger
    from sdlc.scheduler import ledger_fix_rounds

    db = tmp_path / "state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-3", "auto")
    ledger.story_upsert(
        run_id, "1.1-001", "epic-3", "Title", "High", 3, "backend", "br", None, "TODO"
    )
    for attempt in (1, 2):
        ledger.stage_start(run_id, "1.1-001", "bugfix", attempt)
        ledger.stage_finish(run_id, "1.1-001", "bugfix", attempt, "DONE")
    ledger.stage_start(run_id, "1.1-001", "build", 1)
    ledger.stage_finish(run_id, "1.1-001", "build", 1, "DONE")

    assert ledger_fix_rounds(str(db), run_id) == 2


def test_ledger_fix_rounds_degrades_to_zero_on_an_unreadable_ledger(tmp_path) -> None:
    """A missing/corrupt ledger must not fire the breaker or crash the drain."""
    from sdlc.scheduler import ledger_fix_rounds

    assert ledger_fix_rounds(str(tmp_path / "nope.db"), "run-1") == 0


def test_ledger_fix_rounds_degrades_to_zero_on_a_corrupt_ledger_file(tmp_path) -> None:
    """A ledger file that exists but is not a valid sqlite database raises inside
    `stage_breakdown` — the outer `except Exception` must still land on zero."""
    from sdlc.scheduler import ledger_fix_rounds

    db = tmp_path / "corrupt.db"
    db.write_bytes(b"not a sqlite database")
    assert ledger_fix_rounds(str(db), "run-1") == 0


def test_an_overlap_held_job_is_stamped_with_the_holder_reason(tmp_path) -> None:
    """32.3-001 AC2: `_stamp_repo_busy` explains a file overlap, not just a busy repo."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    first = store.add_job(repo=repo, kind="fix", scope="1")
    second = store.add_job(repo=repo, kind="fix", scope="2")
    budget = store.get_job(first).job_budget()
    store.record_files(first, ["src/a.py"])
    store.record_files(second, ["src/a.py"])

    seen: list[str | None] = []
    clock = Clock()

    def sleeper(seconds: float) -> None:
        seen.append(store.get_job(second).reason)
        clock.advance(seconds)

    _run(
        store, tmp_path=tmp_path, launcher=FakeLauncher(), clock=clock,
        sleeper=sleeper, config=_config(poll_seconds=budget.wall_clock_seconds + 1),
    )

    assert any(reason and "overlapping files" in reason for reason in seen)


def test_a_budget_breach_still_parks_the_job_when_stopping_it_fails(tmp_path) -> None:
    """AC3: a process that won't answer to `stop()` must not block the park."""
    store = _store(tmp_path)
    job_id = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1")
    budget = store.get_job(job_id).job_budget()

    class UnstoppableProc(FakeProc):
        def stop(self) -> None:
            raise OSError("no such process")

    class UnstoppableLauncher(FakeLauncher):
        def __call__(self, argv, cwd):
            proc = UnstoppableProc(90000 + len(self.procs))
            self.procs.append(proc)
            return proc

    echoed: list[str] = []
    _run(
        store, tmp_path=tmp_path, launcher=UnstoppableLauncher(), clock=Clock(),
        config=_config(poll_seconds=budget.wall_clock_seconds + 1),
        echo=echoed.append,
    )

    assert store.get_job(job_id).state == "needs_attention"
    assert any("could not stop pid" in line for line in echoed)


def _config(*, poll_seconds: float):
    from sdlc.scheduler import SchedulerConfig

    return SchedulerConfig(slots=2, poll_seconds=poll_seconds)
