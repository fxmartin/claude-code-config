# ABOUTME: An approved parked job resumes on the worker that holds its run (Story 35.5-005).
# ABOUTME: Two schedulers on one store: owner-only take-back, pins, offline owner, unknown run id.

from __future__ import annotations

from pathlib import Path

from test_scheduler_approval import (
    Clock,
    FakeLauncher,
    Probe,
    _approved,
    _clean,
    _pending,
)

from sdlc.approval import ApprovalVerdict
from sdlc.queue import (
    PARKED_OWNER_OFFLINE_SECONDS,
    PARKED_OWNER_REASON_PREFIX,
    QueueStore,
)
from sdlc.queue_worker import WorkerProfile
from sdlc.registry import Registry, RunRecord
from sdlc.scheduler import SchedulerConfig, _Scheduler

M3 = "claude-m3"
SHARED = "claude-shared"


def _merged() -> ApprovalVerdict:
    return ApprovalVerdict(state="merged", approved=False, signal="")


class Worker:
    """One fleet worker: its own scheduler, registry (its disk) and approval probe."""

    def __init__(self, store, tmp_path, clock, *, name, host, pool, verdict) -> None:
        self.name = name
        self.registry = Registry(tmp_path / f"{name}-registry.json")
        self.launcher = FakeLauncher(alive_polls=5)
        self.probe = Probe(verdict)
        self.lines: list[str] = []
        profile = WorkerProfile(name=name, host=host, pools=[pool], harnesses=["claude"], repos=[])
        self.scheduler = _Scheduler(
            store, config=SchedulerConfig(slots=2, worker=profile),
            registry=self.registry, launcher=self.launcher, clock=clock,
            sleeper=lambda _s: None, notifier=lambda *a, **k: None, version_check=_clean,
            probe=None, approval_probe=self.probe,
            fix_rounds=lambda _db, _run: 0, plan_files=lambda _db, _run: [],
            echo=self.lines.append, identity=f"{name}:1",
        )

    def beat(self) -> None:
        self.scheduler._heartbeat(force=True)

    def poll(self) -> bool:
        return self.scheduler._poll_parked()

    def holds(self, run_id: str, repo: str) -> None:
        """The run lives on this worker's disk."""
        self.registry.register(RunRecord(
            run_id=run_id, repo=repo, db=str(Path(repo) / ".sdlc-state.db"), scope="epic-3",
            pid=1, status="AWAITING_APPROVAL", started_at="", finished_at="2026-09-07",
        ))

    @property
    def verbs(self) -> list[str]:
        """The queue verb of each launch (a worker's argv starts with its keep-awake prefix)."""
        return [next(a for a in argv if a in ("resume", "reconcile", "build")) for argv, _ in self.launcher.calls]


def _fleet(tmp_path, *, verdict):
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    clock = Clock()
    m3 = Worker(store, tmp_path, clock, name="m3max", host="m3max", pool=M3, verdict=verdict)
    lab = Worker(store, tmp_path, clock, name="home-lab", host="home-lab", pool=SHARED, verdict=verdict)
    m3.beat()
    lab.beat()
    return store, clock, m3, lab


def _park(store, tmp_path, clock, *, worker: str | None, run_id: str = "d72324cb-run", **pins) -> int:
    """A job ``worker`` ran that parked awaiting approval on #891 (``None``: a legacy row)."""
    repo = tmp_path / "claude-code-config"
    repo.mkdir(exist_ok=True)
    job_id = store.add_job(repo=str(repo), kind="build", scope="35.2-011", **pins)
    store.claim_job(job_id, claimed_by=f"{worker}:1", lease_seconds=90, now=clock(), worker=worker)
    store.attach_run(job_id, run_id)
    store.park_job(job_id, pr_number=891, reason="awaiting approval on #891", poll_after=None)
    return job_id


# --- AC1: only the worker that ran it takes it back --------------------------


def test_only_the_worker_that_parked_a_job_resumes_it_on_approval(tmp_path) -> None:
    store, clock, m3, lab = _fleet(tmp_path, verdict=_approved())
    job_id = _park(store, tmp_path, clock, worker="m3max")
    m3.holds("d72324cb-run", store.get_job(job_id).repo)

    assert lab.poll() is False
    assert lab.probe.calls == []  # skipped without reading its CR
    assert store.get_job(job_id).state == "parked"

    assert m3.poll() is True
    job = store.get_job(job_id)
    assert job.state == "running" and job.worker == "m3max"
    assert m3.verbs == ["resume"]
    assert lab.launcher.calls == []


def test_a_hand_merged_cr_is_reconciled_on_the_worker_that_holds_the_run(tmp_path) -> None:
    store, clock, m3, lab = _fleet(tmp_path, verdict=_merged())
    job_id = _park(store, tmp_path, clock, worker="m3max")
    m3.holds("d72324cb-run", store.get_job(job_id).repo)

    lab.poll()
    m3.poll()

    assert lab.probe.calls == [] and lab.launcher.calls == []
    assert m3.verbs == ["reconcile"]
    assert store.get_job(job_id).worker == "m3max"


def test_host_and_pool_pins_apply_to_a_take_back(tmp_path) -> None:
    """Even a row with no recorded owner goes only where a claim of it could."""
    store, clock, m3, lab = _fleet(tmp_path, verdict=_approved())
    job_id = _park(store, tmp_path, clock, worker=None, host="m3max", pool=M3)
    m3.holds("d72324cb-run", store.get_job(job_id).repo)

    lab.poll()
    assert lab.probe.calls == [] and store.get_job(job_id).state == "parked"

    m3.poll()
    assert m3.verbs == ["resume"]


# --- AC3: an owner gone a long while leaves the job parked, and says so ------


def test_an_offline_owner_leaves_the_job_parked_with_a_reason_naming_it(tmp_path) -> None:
    store, clock, m3, lab = _fleet(tmp_path, verdict=_approved())
    job_id = _park(store, tmp_path, clock, worker="m3max")
    m3.holds("d72324cb-run", store.get_job(job_id).repo)

    clock.advance(PARKED_OWNER_OFFLINE_SECONDS + 60)
    lab.beat()
    lab.poll()

    job = store.get_job(job_id)
    assert job.state == "parked"  # not failed, not taken
    assert job.reason.startswith(PARKED_OWNER_REASON_PREFIX + "m3max")
    assert lab.probe.calls == [] and lab.launcher.calls == []

    # The owner comes back while the CR still waits: its own reason returns.
    m3.probe = Probe(_pending())
    m3.scheduler._approval_probe = m3.probe
    m3.beat()
    m3.poll()
    job = store.get_job(job_id)
    assert job.state == "parked"
    assert job.reason.startswith("awaiting approval on #891")


# --- AC4: an `unknown run id` take-back goes back to parked, not failed ------


def test_a_take_back_with_no_such_run_returns_the_job_to_parked(tmp_path) -> None:
    store, clock, m3, _lab = _fleet(tmp_path, verdict=_approved())
    job_id = _park(store, tmp_path, clock, worker="m3max")  # m3's registry lost the run

    assert m3.poll() is False

    job = store.get_job(job_id)
    assert job.state == "parked"
    assert "unknown run id: d72324cb-run" in job.reason
    assert job.parked_by == "m3max" and job.claimed_by is None
    assert job.poll_after is not None  # not re-read on the very next pass
    assert m3.launcher.calls == []
    assert any("unknown run id" in line for line in m3.lines)


def test_a_legacy_row_taken_by_the_wrong_worker_is_released_ownerless(tmp_path) -> None:
    """The incident: home-lab took job 12 back and failed it on `unknown run id`."""
    store, clock, _m3, lab = _fleet(tmp_path, verdict=_approved())
    job_id = _park(store, tmp_path, clock, worker=None)

    lab.poll()

    job = store.get_job(job_id)
    assert job.state == "parked"
    assert "unknown run id" in job.reason
    assert job.parked_by is None  # still anyone's, so the worker that has the run can take it
    assert lab.launcher.calls == []
