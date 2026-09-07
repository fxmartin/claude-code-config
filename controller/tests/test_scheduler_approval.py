# ABOUTME: Behavior tests for the approval-aware queue (Story 32.2-002).
# ABOUTME: Park on AWAITING_APPROVAL, bounded re-poll, resume / reconcile / fail branches.

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sdlc.approval import ApprovalVerdict
from sdlc.doctor import Finding
from sdlc.ledger_view import Ledger
from sdlc.queue import JobRecord, QueueStore
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
    def verbs(self) -> list[list[str]]:
        """Each dispatched argv with the interpreter/binary prefix dropped."""
        return [argv[argv.index("sdlc") + 1:] if "sdlc" in argv else argv[1:]
                for argv, _ in self.calls]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Probe:
    """A recording approval probe returning a canned verdict per call."""

    def __init__(self, *verdicts: ApprovalVerdict | None) -> None:
        self._verdicts = list(verdicts)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, root: Path, pr_number: int) -> ApprovalVerdict | None:
        self.calls.append((str(root), pr_number))
        if not self._verdicts:
            return _pending()
        return self._verdicts.pop(0) if len(self._verdicts) > 1 else self._verdicts[0]


def _pending() -> ApprovalVerdict:
    return ApprovalVerdict(state="open", approved=False, signal="")


def _approved(signal: str = "risk-approved label") -> ApprovalVerdict:
    return ApprovalVerdict(state="open", approved=True, signal=signal)


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


def _ledger_with_parked_story(repo: str, *, pr_number: int | None = 12) -> tuple[str, str]:
    """Seed ``repo``'s ledger with a run holding one AWAITING_APPROVAL story."""
    db = str(Path(repo) / ".sdlc-state.db")
    ledger = Ledger(Path(db))
    ledger.init()
    run_id = ledger.run_create("epic-3", "serial")
    ledger.story_upsert(
        run_id, "3.1-001", "epic-3", "a story", "Must", 3,
        "backend-typescript-architect", "feature/3.1-001", pr_number,
        "AWAITING_APPROVAL",
    )
    return run_id, db


def _parked_job(store: QueueStore, repo: str, *, pr: int = 12, run_id: str = "run-a") -> int:
    """A job already sitting in the `parked` state, poll due immediately."""
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    store.claim_job(job_id, claimed_by="seed", lease_seconds=90, now=Clock()())
    store.attach_run(job_id, run_id)
    store.park_job(job_id, pr_number=pr, reason="awaiting approval", poll_after=None)
    return job_id


# --- AC1: a run that parks AWAITING_APPROVAL is parked with its PR ----------


def test_an_awaiting_approval_run_parks_the_job_with_its_pr_number(tmp_path) -> None:
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    run_id, db = _ledger_with_parked_story(repo)
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    registry = Registry(tmp_path / "registry.json")
    launcher = FakeLauncher(alive_polls=2, code=1)
    clock = Clock()
    events: list[tuple[str, dict]] = []
    passes = {"n": 0}

    def sleeper(seconds: float) -> None:
        passes["n"] += 1
        if passes["n"] == 1:
            registry.register(RunRecord(
                run_id=run_id, repo=repo, db=db, scope="epic-3",
                pid=launcher.procs[0].pid, status="IN_PROGRESS", started_at="",
            ))
        elif passes["n"] == 2:
            registry.mark_finished(run_id, "AWAITING_APPROVAL", completed=0)
        clock.advance(seconds)

    result = _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, sleeper=sleeper,
        registry=registry, notifier=lambda ev, **f: events.append((ev, f)),
        approval_probe=Probe(_pending()),
    )

    job = store.get_job(job_id)
    assert job.state == "parked"
    assert job.pr_number == 12
    assert job.run_id == run_id
    assert result.parked == 1
    assert [ev for ev, _ in events] == ["queue_job_parked"]
    assert events[0][1]["pr"] == 12


def test_an_awaiting_approval_run_with_no_pr_stays_blocked(tmp_path) -> None:
    """Nothing to poll means no park — the pre-32.2 `blocked` terminal stands."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    run_id, db = _ledger_with_parked_story(repo, pr_number=None)
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    registry = Registry(tmp_path / "registry.json")
    launcher = FakeLauncher(alive_polls=2, code=1)
    clock = Clock()
    passes = {"n": 0}

    def sleeper(seconds: float) -> None:
        passes["n"] += 1
        if passes["n"] == 1:
            registry.register(RunRecord(
                run_id=run_id, repo=repo, db=db, scope="epic-3",
                pid=launcher.procs[0].pid, status="IN_PROGRESS", started_at="",
            ))
        elif passes["n"] == 2:
            registry.mark_finished(run_id, "AWAITING_APPROVAL", completed=0)
        clock.advance(seconds)

    _run(store, tmp_path=tmp_path, launcher=launcher, clock=clock, sleeper=sleeper,
         registry=registry)

    job = store.get_job(job_id)
    assert job.state == "blocked"
    assert job.reason == "run status AWAITING_APPROVAL"


# --- AC4: parked jobs hold no slot -----------------------------------------


def test_a_parked_job_frees_its_repo_for_the_next_job(tmp_path) -> None:
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    _parked_job(store, repo)
    next_id = store.add_job(repo=repo, kind="fix", scope="7")
    launcher = FakeLauncher(alive_polls=1)

    _run(store, tmp_path=tmp_path, launcher=launcher, approval_probe=Probe(_pending()))

    assert store.get_job(next_id).state == "done"
    assert len(launcher.calls) == 1


# --- AC2: the label arrives → resume ---------------------------------------


def test_the_label_appearing_resumes_the_run_through_resume_py(tmp_path) -> None:
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    run_id, db = _ledger_with_parked_story(repo)
    job_id = _parked_job(store, repo, run_id=run_id)
    registry = Registry(tmp_path / "registry.json")
    registry.register(RunRecord(
        run_id=run_id, repo=repo, db=db, scope="epic-3", pid=1,
        status="AWAITING_APPROVAL", started_at="", finished_at="2026-09-07T11:00:00Z",
    ))
    launcher = FakeLauncher(alive_polls=1, code=0)
    events: list[tuple[str, dict]] = []
    clock = Clock()

    def sleeper(seconds: float) -> None:
        # The resumed run reaches DONE the way the controller records it.
        registry.mark_finished(run_id, "DONE", completed=1)
        clock.advance(seconds)

    result = _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, sleeper=sleeper,
        registry=registry, notifier=lambda ev, **f: events.append((ev, f)),
        approval_probe=Probe(_approved()),
    )

    assert launcher.verbs == [["resume", "--run", run_id]]
    assert result.resumed == 1
    assert store.get_job(job_id).state == "done"
    assert [ev for ev, _ in events] == ["queue_job_resumed", "queue_job_finished"]
    assert events[0][1]["signal"] == "risk-approved label"


def test_an_approving_review_also_resumes(tmp_path) -> None:
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = _parked_job(store, repo, run_id="run-a")
    launcher = FakeLauncher(alive_polls=1, code=0)

    _run(store, tmp_path=tmp_path, launcher=launcher,
         approval_probe=Probe(_approved("approving review")))

    assert launcher.verbs == [["resume", "--run", "run-a"]]
    assert store.get_job(job_id).run_id == "run-a"


def test_a_stale_controller_never_resumes_a_parked_job(tmp_path) -> None:
    """The per-job version check guards the resume launch too (Story 15.1-004)."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = _parked_job(store, repo)
    launcher = FakeLauncher()

    def stale(_root) -> Finding:
        return Finding("install", "Installed controller vs checkout", "WARN",
                       "installed 2.1.0, checkout 2.2.0", "reinstall the controller")

    _run(store, tmp_path=tmp_path, launcher=launcher, version_check=stale,
         approval_probe=Probe(_approved()))

    assert launcher.calls == []
    assert store.get_job(job_id).state == "blocked"


# --- AC3: closed and hand-merged PRs ---------------------------------------


def test_a_pr_closed_without_merging_fails_the_job_and_notifies(tmp_path) -> None:
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = _parked_job(store, repo)
    launcher = FakeLauncher()
    events: list[tuple[str, dict]] = []

    result = _run(
        store, tmp_path=tmp_path, launcher=launcher,
        notifier=lambda ev, **f: events.append((ev, f)),
        approval_probe=Probe(ApprovalVerdict(state="closed", approved=False, signal="")),
    )

    job = store.get_job(job_id)
    assert job.state == "failed"
    assert job.reason == "pr closed"
    assert result.failed == 1
    assert launcher.calls == []
    assert [ev for ev, _ in events] == ["queue_job_finished"]
    assert events[0][1]["terminal"] == "FAILED"


def test_a_hand_merged_pr_reconciles_rather_than_resumes(tmp_path) -> None:
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    run_id, db = _ledger_with_parked_story(repo)
    job_id = _parked_job(store, repo, run_id=run_id)
    registry = Registry(tmp_path / "registry.json")
    registry.register(RunRecord(
        run_id=run_id, repo=repo, db=db, scope="epic-3", pid=1,
        status="AWAITING_APPROVAL", started_at="", finished_at="2026-09-07T11:00:00Z",
    ))
    launcher = FakeLauncher(alive_polls=1, code=0)
    clock = Clock()

    def sleeper(seconds: float) -> None:
        registry.mark_finished(run_id, "DONE", completed=1)
        clock.advance(seconds)

    result = _run(
        store, tmp_path=tmp_path, launcher=launcher, clock=clock, sleeper=sleeper,
        registry=registry,
        approval_probe=Probe(ApprovalVerdict(state="merged", approved=True, signal="")),
    )

    assert launcher.verbs == [["reconcile", run_id]]
    assert result.reconciled == 1
    assert result.resumed == 0
    assert store.get_job(job_id).state == "done"


# --- AC1/AC4: bounded, rate-limit-respecting polling -----------------------


def test_a_parked_job_is_not_polled_again_before_the_interval_elapses(tmp_path) -> None:
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    _parked_job(store, repo)
    probe = Probe(_pending())
    clock = Clock()
    passes = {"n": 0}

    def sleeper(seconds: float) -> None:
        passes["n"] += 1
        if passes["n"] >= 4:
            raise KeyboardInterrupt
        clock.advance(seconds)

    _run(
        store, tmp_path=tmp_path, launcher=FakeLauncher(), clock=clock, sleeper=sleeper,
        config=SchedulerConfig(slots=2, poll_seconds=1.0, follow=True,
                               approval_poll_seconds=300.0),
        approval_probe=probe,
    )

    # Four passes one second apart, a 300s poll interval: exactly one API read.
    assert len(probe.calls) == 1
    assert probe.calls[0] == (repo, 12)


def test_the_poll_fires_again_once_the_interval_has_elapsed(tmp_path) -> None:
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    _parked_job(store, repo)
    probe = Probe(_pending())
    clock = Clock()
    passes = {"n": 0}

    def sleeper(seconds: float) -> None:
        passes["n"] += 1
        if passes["n"] >= 4:
            raise KeyboardInterrupt
        clock.advance(200.0)

    _run(
        store, tmp_path=tmp_path, launcher=FakeLauncher(), clock=clock, sleeper=sleeper,
        config=SchedulerConfig(slots=2, poll_seconds=1.0, follow=True,
                               approval_poll_seconds=300.0),
        approval_probe=probe,
    )

    # Passes at t=0, 200, 400, 600 → polls at 0 and 400 (300s apart minimum).
    assert len(probe.calls) == 2


@pytest.mark.parametrize(
    "requested,expected",
    [(0.0, 30.0), (5.0, 30.0), (300.0, 300.0), (99999.0, 3600.0)],
)
def test_the_poll_interval_is_clamped_to_its_bounds(requested, expected) -> None:
    from sdlc.scheduler import approval_poll_interval

    assert approval_poll_interval(requested) == expected


def test_a_busy_repo_is_not_polled_at_all(tmp_path) -> None:
    """No slot to act on the answer means no reason to spend an API read (AC4)."""
    from sdlc.scheduler import SchedulerConfig

    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    _parked_job(store, repo)
    live = store.add_job(repo=repo, kind="fix", scope="7")
    store.claim_job(live, claimed_by="other-host", lease_seconds=90, now=Clock()())
    probe = Probe(_approved())

    _run(store, tmp_path=tmp_path, launcher=FakeLauncher(), approval_probe=probe,
         config=SchedulerConfig(slots=2, poll_seconds=1.0))

    assert probe.calls == []


def test_a_host_hiccup_leaves_the_job_parked_and_retries_later(tmp_path) -> None:
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = _parked_job(store, repo)
    clock = Clock()

    _run(store, tmp_path=tmp_path, launcher=FakeLauncher(), clock=clock,
         approval_probe=Probe(None))

    job = store.get_job(job_id)
    assert job.state == "parked"
    assert job.poll_after is not None
    assert job.poll_after > clock().isoformat()


def test_polling_stops_once_the_job_leaves_the_park(tmp_path) -> None:
    """A cancelled park is terminal for the poller (AC4)."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = _parked_job(store, repo)
    store.cancel_job(job_id)
    probe = Probe(_approved())

    _run(store, tmp_path=tmp_path, launcher=FakeLauncher(), approval_probe=probe)

    assert probe.calls == []
    assert store.get_job(job_id).state == "cancelled"


def test_a_parked_job_alone_does_not_hold_a_plain_drain_open(tmp_path) -> None:
    """Without --follow the drain polls once and exits; the park outlives it."""
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = _parked_job(store, repo)
    probe = Probe(_pending())

    result = _run(store, tmp_path=tmp_path, launcher=FakeLauncher(),
                  approval_probe=probe)

    assert len(probe.calls) == 1
    assert result.interrupted is False
    assert store.get_job(job_id).state == "parked"


# --- edge cases -------------------------------------------------------------


def test_the_pr_lookup_ignores_stories_that_are_not_awaiting_approval(tmp_path) -> None:
    """A mixed run must not park on a *done* story's PR."""
    from sdlc.scheduler import _awaiting_approval_pr

    repo = _repo(tmp_path, "alpha")
    db = str(Path(repo) / ".sdlc-state.db")
    ledger = Ledger(Path(db))
    ledger.init()
    run_id = ledger.run_create("epic-3", "serial")
    ledger.story_upsert(run_id, "3.1-001", "epic-3", "done one", "Must", 3, "backend",
                        "feature/3.1-001", 11, "DONE")
    ledger.story_upsert(run_id, "3.1-002", "epic-3", "parked one", "Must", 3, "backend",
                        "feature/3.1-002", 12, "AWAITING_APPROVAL")

    record = RunRecord(run_id=run_id, repo=repo, db=db, scope="epic-3", pid=1,
                       status="AWAITING_APPROVAL", started_at="")

    assert _awaiting_approval_pr(record) == 12


def test_the_pr_lookup_is_none_without_a_registry_record(tmp_path) -> None:
    from sdlc.scheduler import _awaiting_approval_pr

    assert _awaiting_approval_pr(None) is None
    assert _awaiting_approval_pr(RunRecord(
        run_id="r", repo="/a", db="", scope="s", pid=1, status="X", started_at="",
    )) is None


def test_the_pr_lookup_survives_an_unreadable_ledger(tmp_path) -> None:
    """A corrupt ledger degrades to `blocked`, never to a crashed drain."""
    from sdlc.scheduler import _awaiting_approval_pr

    db = tmp_path / "corrupt.db"
    db.write_bytes(b"not a sqlite database")
    record = RunRecord(run_id="r", repo=str(tmp_path), db=str(db), scope="s", pid=1,
                       status="AWAITING_APPROVAL", started_at="")

    assert _awaiting_approval_pr(record) is None


def test_reconcile_argv_refuses_a_job_with_no_run() -> None:
    from sdlc.scheduler import reconcile_argv

    store_less = JobRecord(
        id=1, repo="/a", kind="build", scope="epic-3", priority="normal",
        state="parked", claimed_by=None, lease_until=None, run_id=None, options=None,
        created_at="", updated_at="", reason=None,
    )
    with pytest.raises(ValueError, match="no run to reconcile"):
        reconcile_argv(store_less)


def test_reconcile_argv_uses_the_reconcile_verb() -> None:
    from sdlc.scheduler import reconcile_argv

    job = JobRecord(
        id=1, repo="/a", kind="build", scope="epic-3", priority="normal",
        state="parked", claimed_by=None, lease_until=None, run_id="run-a", options=None,
        created_at="", updated_at="", reason=None,
    )
    assert reconcile_argv(job, prefix=["sdlc"]) == ["sdlc", "reconcile", "run-a"]


def test_a_park_with_no_change_request_is_not_polled_but_is_rescheduled(tmp_path) -> None:
    """An older park (or a hand-edited row) must not re-list on every pass."""
    import sqlite3

    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = _parked_job(store, repo)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE jobs SET pr_number = NULL WHERE id = ?", (job_id,))
    probe = Probe(_approved())

    _run(store, tmp_path=tmp_path, launcher=FakeLauncher(), approval_probe=probe)

    assert probe.calls == []
    job = store.get_job(job_id)
    assert job.state == "parked"
    assert job.poll_after is not None


def test_losing_the_take_race_leaves_the_job_to_the_other_scheduler(tmp_path) -> None:
    store = _store(tmp_path)
    repo = _repo(tmp_path, "alpha")
    job_id = _parked_job(store, repo)
    launcher = FakeLauncher()

    def refuse(*_args, **_kwargs):
        return None

    store.take_parked_job = refuse  # type: ignore[method-assign]

    result = _run(store, tmp_path=tmp_path, launcher=launcher,
                  approval_probe=Probe(_approved()))

    assert launcher.calls == []
    assert result.resumed == 0
    assert store.get_job(job_id).state == "parked"


def test_due_parked_jobs_on_a_store_that_was_never_created(tmp_path) -> None:
    """A read verb never conjures an empty queue.db (the store's contract)."""
    store = QueueStore(tmp_path / "absent.db")

    assert store.due_parked_jobs() == []
    assert not (tmp_path / "absent.db").exists()


def test_the_default_probe_delegates_to_the_read_only_poller(tmp_path, monkeypatch) -> None:
    from sdlc import scheduler as sched

    seen: dict = {}

    def fake_poll(root, pr_number):
        seen["args"] = (root, pr_number)
        return _approved()

    monkeypatch.setattr(sched, "poll_approval", fake_poll)

    verdict = sched._default_approval_probe(Path("/a"), 12)

    assert verdict.approved is True
    assert seen["args"] == (Path("/a"), 12)
