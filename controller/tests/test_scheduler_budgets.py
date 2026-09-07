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


def test_ledger_plan_files_reads_the_runs_investigation_plan(tmp_path) -> None:
    """AC2's write side: the files come from the run's own frozen `fix-plan` event."""
    from sdlc.build import Ledger
    from sdlc.fix_issue import _record_fix_plan
    from sdlc.scheduler import ledger_plan_files

    db = tmp_path / "state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("issue-42", "fix")
    _record_fix_plan(
        ledger, run_id, {"files_to_modify": ["src/b.py", "src/a.py"], "complexity": "LOW"}
    )

    assert ledger_plan_files(str(db), run_id) == ["src/a.py", "src/b.py"]


def test_ledger_plan_files_unions_every_plan_the_run_recorded(tmp_path) -> None:
    """A resumed run can record more than one plan; the job's footprint is their union."""
    from sdlc.build import Ledger
    from sdlc.fix_issue import _record_fix_plan
    from sdlc.scheduler import ledger_plan_files

    db = tmp_path / "state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("issue-42", "fix")
    _record_fix_plan(ledger, run_id, {"files_to_modify": ["src/a.py"]})
    _record_fix_plan(ledger, run_id, {"files_to_modify": ["src/c.py", "src/a.py"]})

    assert ledger_plan_files(str(db), run_id) == ["src/a.py", "src/c.py"]


def test_ledger_plan_files_degrades_to_empty_on_an_unreadable_ledger(tmp_path) -> None:
    """A missing or corrupt ledger must leave the graph empty, never crash the drain."""
    from sdlc.scheduler import ledger_plan_files

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    assert ledger_plan_files(str(tmp_path / "nope.db"), "run-1") == []
    assert ledger_plan_files(str(corrupt), "run-1") == []


def test_ledger_plan_files_ignores_a_plan_it_cannot_parse(tmp_path) -> None:
    """A non-JSON or file-less plan event contributes nothing rather than raising."""
    from sdlc.build import Ledger
    from sdlc.scheduler import ledger_plan_files

    db = tmp_path / "state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("issue-42", "fix")
    ledger.event_log(run_id, "", "info", "fix-plan", "not json at all")
    ledger.event_log(run_id, "", "info", "fix-plan", '["a list, not a plan"]')
    ledger.event_log(run_id, "", "info", "fix-plan", '{"root_cause": "no files field"}')

    assert ledger_plan_files(str(db), run_id) == []


def test_the_scheduler_records_a_running_jobs_investigated_files(tmp_path) -> None:
    """AC2: the queue row learns the job's file footprint while the run is live."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="fix", scope="1")

    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    launcher = FakeLauncher()
    calls: list[tuple[str, str]] = []

    def plan_files(db: str, run_id: str) -> list[str]:
        calls.append((db, run_id))
        return ["src/a.py", "src/b.py"]

    class OneShot(FakeLauncher):
        """Registers a run, then exits on the *next* pass so the record lands first."""

        def __call__(self, argv, cwd):
            proc = launcher(argv, cwd)
            registry.register(
                RunRecord(run_id="run-plan", repo=repo, db=str(tmp_path / "x.db"),
                          scope="1", pid=proc.pid, status="IN_PROGRESS", started_at="")
            )
            return proc

    def sleeper(seconds: float) -> None:
        launcher.procs[0].stopped = True  # let the second pass reap it
        clock.advance(seconds)

    _run(
        store, tmp_path=tmp_path, launcher=OneShot(), clock=clock, sleeper=sleeper,
        registry=registry, plan_files=plan_files,
    )

    assert store.get_job(job_id).files_to_modify() == {"src/a.py", "src/b.py"}
    assert calls == [(str(tmp_path / "x.db"), "run-plan")]


def test_recorded_files_hold_back_an_overlapping_queued_peer(tmp_path) -> None:
    """AC2 end to end: what the scheduler records is what holds the next job back."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    running = store.add_job(repo=repo, kind="fix", scope="1")
    waiting = store.add_job(repo=repo, kind="fix", scope="2")
    store.record_files(waiting, ["src/a.py"])  # a previous run's footprint

    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    launcher = FakeLauncher()

    class Launching(FakeLauncher):
        def __call__(self, argv, cwd):
            proc = launcher(argv, cwd)
            registry.register(
                RunRecord(run_id=f"run-{len(launcher.procs)}", repo=repo,
                          db=str(tmp_path / "x.db"), scope="1", pid=proc.pid,
                          status="IN_PROGRESS", started_at="")
            )
            return proc

    seen: list[tuple[int, str, str | None, frozenset[str]]] = []

    def sleeper(seconds: float) -> None:
        seen.append((
            len(launcher.procs),
            store.get_job(waiting).state,
            store.get_job(waiting).reason,
            frozenset(store.get_job(running).files_to_modify()),
        ))
        if len(seen) >= 2:
            launcher.procs[0].stopped = True  # let the first job finish cleanly
        clock.advance(seconds)

    _run(
        store, tmp_path=tmp_path, launcher=Launching(), clock=clock, registry=registry,
        sleeper=sleeper, plan_files=lambda _db, _run: ["src/a.py"],
    )

    # While the first job is pending with a recorded footprint, the overlapping
    # second job is neither launched nor left unexplained.
    assert (1, "queued", f"waiting on job {running} (overlapping files)",
            frozenset({"src/a.py"})) in seen
    # And the hold releases itself: once the first job is terminal the second runs.
    assert len(launcher.procs) == 2


class SurvivesOnePoll(FakeProc):
    """Alive for its first reap, gone by the second.

    The breaker only ever looks at a job that is still in flight
    (`_enforce_budgets` runs after `_reap`), so a proc that exits on the first
    reap is never offered to it. This one gives the breaker exactly one look
    before finishing cleanly, which is what makes "the breaker did *not* fire"
    an assertion rather than an accident of ordering.
    """

    def __init__(self, pid: int) -> None:
        super().__init__(pid)
        self._polls = 0

    def poll(self) -> int | None:
        if self.stopped:
            return 0
        self._polls += 1
        return 0 if self._polls > 1 else None


class SurvivingLauncher(FakeLauncher):
    def __call__(self, argv, cwd):
        proc = SurvivesOnePoll(90000 + len(self.procs))
        self.procs.append(proc)
        return proc


def _dead_pid() -> int:
    """A pid that is certainly gone — spawned and reaped before we return it."""
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _registering(launcher, registry, repo, tmp_path, run_id="run-thrash"):
    """A launcher whose job registers its run, the way `run_build` does."""

    def launching(argv, cwd):
        proc = launcher(argv, cwd)
        registry.register(
            RunRecord(run_id=run_id, repo=repo, db=str(tmp_path / "x.db"),
                      scope="1", pid=proc.pid, status="IN_PROGRESS", started_at="")
        )
        return proc

    return launching


def _hand_back(store, registry, job_id, repo, tmp_path, clock, run_id="run-thrash"):
    """Retire the parked run's process and requeue the job, as an operator would.

    The scheduler only resumes a job whose run is genuinely dead (two drivers on
    one run is what the registry guard exists to prevent), so the park's pid has
    to be reaped before the next drain, and the lease has to be strictly older
    than that drain's clock.
    """
    registry.register(
        RunRecord(run_id=run_id, repo=repo, db=str(tmp_path / "x.db"),
                  scope="1", pid=_dead_pid(), status="IN_PROGRESS", started_at="")
    )
    store.requeue_job(job_id, now=clock.now)
    clock.advance(200)


def test_requeue_recovers_a_fix_round_park_instead_of_re_parking_it(tmp_path) -> None:
    """`sdlc queue requeue` is the documented exit from a budget park, so it has
    to actually work.

    `ledger_fix_rounds` is *cumulative* over the run and a requeue re-enters the
    **same** run, so the count that fired the breaker is still at the cap on the
    resumed job's very first poll. Without a baseline the job is stopped again
    having made no progress at all, and the `_enforce_budgets` docstring, the
    README and the architecture doc that all advertise requeue as the exit are
    simply wrong.
    """
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="fix", scope="1")
    budget = store.get_job(job_id).job_budget()
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()

    # The ledger does not forget: rounds already burned stay burned.
    def fix_rounds(_db: str, _run_id: str) -> int:
        return budget.max_fix_rounds

    _run(
        store, tmp_path=tmp_path, clock=clock, registry=registry,
        launcher=_registering(FakeLauncher(), registry, repo, tmp_path),
        fix_rounds=fix_rounds,
    )
    assert store.get_job(job_id).state == "needs_attention"
    assert store.get_job(job_id).fix_rounds_baseline == budget.max_fix_rounds

    _hand_back(store, registry, job_id, repo, tmp_path, clock)
    second = SurvivingLauncher()
    result = _run(
        store, tmp_path=tmp_path, clock=clock, registry=registry,
        launcher=_registering(second, registry, repo, tmp_path),
        fix_rounds=fix_rounds,
    )

    assert result.resumed == 1
    assert result.parked == 0
    assert second.procs[0].stopped is False  # the breaker never touched it
    assert store.get_job(job_id).state == "done"


def test_requeue_grants_one_more_budget_rather_than_disarming_the_breaker(
    tmp_path,
) -> None:
    """The rope a requeue grants is one more class budget, not infinity.

    A breaker an operator can permanently disarm by requeueing once is not a
    breaker, so the baseline has to move the cap up — never remove it.
    """
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="fix", scope="1")
    budget = store.get_job(job_id).job_budget()
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    burned = [budget.max_fix_rounds]

    def fix_rounds(_db: str, _run_id: str) -> int:
        return burned[0]

    _run(
        store, tmp_path=tmp_path, clock=clock, registry=registry,
        launcher=_registering(FakeLauncher(), registry, repo, tmp_path),
        fix_rounds=fix_rounds,
    )
    assert store.get_job(job_id).state == "needs_attention"

    # The resumed job thrashes through the whole second allowance too.
    _hand_back(store, registry, job_id, repo, tmp_path, clock)
    burned[0] = budget.max_fix_rounds * 2
    result = _run(
        store, tmp_path=tmp_path, clock=clock, registry=registry,
        launcher=_registering(FakeLauncher(), registry, repo, tmp_path),
        fix_rounds=fix_rounds,
    )

    assert result.parked == 1
    job = store.get_job(job_id)
    assert job.state == "needs_attention"
    # The reason counts rounds burned *since the last park* — the number the cap
    # is actually being compared against, not the run's lifetime total.
    assert f"{budget.max_fix_rounds} fix rounds" in (job.reason or "")
    assert job.fix_rounds_baseline == budget.max_fix_rounds * 2


def test_a_wall_clock_park_does_not_grant_fresh_fix_rounds(tmp_path) -> None:
    """Only the thrash arm banks a baseline.

    A job stopped by the clock has not spent its fix-round budget, so crediting
    the rounds it *had* burned would quietly hand it a second allowance and let
    it thrash past the cap on its next launch.
    """
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = store.add_job(repo=repo, kind="fix", scope="1")
    budget = store.get_job(job_id).job_budget()
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    burned = [budget.max_fix_rounds - 1]  # inside the cap, so the clock parks it

    def fix_rounds(_db: str, _run_id: str) -> int:
        return burned[0]

    _run(
        store, tmp_path=tmp_path, clock=clock, registry=registry,
        launcher=_registering(FakeLauncher(), registry, repo, tmp_path),
        fix_rounds=fix_rounds,
        config=_config(poll_seconds=budget.wall_clock_seconds + 1),
    )
    job = store.get_job(job_id)
    assert job.state == "needs_attention"
    assert "wall-clock" in (job.reason or "")
    assert job.fix_rounds_baseline == 0

    # One more round on the resumed job now reaches the class cap outright.
    _hand_back(store, registry, job_id, repo, tmp_path, clock)
    burned[0] = budget.max_fix_rounds
    result = _run(
        store, tmp_path=tmp_path, clock=clock, registry=registry,
        launcher=_registering(FakeLauncher(), registry, repo, tmp_path),
        fix_rounds=fix_rounds,
    )

    assert result.parked == 1
    assert "fix round" in (store.get_job(job_id).reason or "")


def test_stage_attempt_count_is_scoped_to_its_run_and_stage(tmp_path) -> None:
    """The cheap counter behind `ledger_fix_rounds` must not over-count.

    It replaces a full `stage_breakdown` materialisation on a 2s poll, so the
    thing worth pinning is its WHERE clause: other stages, and other runs
    sharing the ledger, are somebody else's rounds.
    """
    from sdlc.build import Ledger

    db = tmp_path / "state.db"
    ledger = Ledger(db)
    ledger.init()
    mine = ledger.run_create("epic-3", "auto")
    theirs = ledger.run_create("epic-4", "auto")
    for run_id, story in ((mine, "1.1-001"), (theirs, "1.2-001")):
        ledger.story_upsert(
            run_id, story, "epic-3", "Title", "High", 3, "backend", "br", None, "TODO"
        )
    for attempt in (1, 2, 3):
        ledger.stage_start(mine, "1.1-001", "bugfix", attempt)
        ledger.stage_finish(mine, "1.1-001", "bugfix", attempt, "DONE")
    ledger.stage_start(mine, "1.1-001", "review", 1)
    ledger.stage_finish(mine, "1.1-001", "review", 1, "DONE")
    ledger.stage_start(theirs, "1.2-001", "bugfix", 1)
    ledger.stage_finish(theirs, "1.2-001", "bugfix", 1, "DONE")

    assert ledger.stage_attempt_count(mine, "bugfix") == 3
    assert ledger.stage_attempt_count(theirs, "bugfix") == 1
    assert ledger.stage_attempt_count(mine, "coverage") == 0


def test_stage_attempt_count_on_a_missing_ledger_is_zero(tmp_path) -> None:
    """Read-never-creates: an absent ledger counts zero rather than raising."""
    from sdlc.build import Ledger

    assert Ledger(tmp_path / "nope.db").stage_attempt_count("run-1", "bugfix") == 0
