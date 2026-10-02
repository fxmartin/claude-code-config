# ABOUTME: Scheduler tests for per-pool rate-limit pauses (Story 35.2-003).
# ABOUTME: A park pauses only its pool, a probe clears it pool-wide, local mode is unchanged.

from __future__ import annotations

import sqlite3
from datetime import timedelta

from sdlc.doctor import Finding
from sdlc.queue import QueueStore
from sdlc.queue_worker import WorkerProfile
from sdlc.registry import Registry
from sdlc.scheduler import SchedulerConfig, run_queue

from test_scheduler import (
    Clock,
    FakeLauncher,
    RecordingNotifier,
    _parked_job,
    _repo,
    _stop_after,
)

SHARED = "claude-shared"
M3 = "claude-m3"


def _clean(_root) -> Finding:
    return Finding("install", "Installed controller vs checkout", "CLEAN", "matches")


def _store(tmp_path) -> QueueStore:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    return store


def _profile(name: str, pool: str) -> WorkerProfile:
    return WorkerProfile(
        name=name, host=name, pools=[pool], harnesses=["claude"], repos=["alpha", "beta"]
    )


def _drain(store, tmp_path, *, profile, clock, registry, passes=3, probe=None, notifier=None):
    launcher = FakeLauncher(alive_polls=1)
    run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=1.0, worker=profile),
        registry=registry,
        launcher=launcher,
        clock=clock,
        sleeper=_stop_after(clock, passes),
        notifier=notifier or (lambda *a, **k: None),
        version_check=_clean,
        echo=lambda _line: None,
        identity=profile.name,
        probe=probe,
    )
    return launcher


def _park_in_shared_pool(store, registry, tmp_path, clock, **ledger):
    """A job a ``claude-shared`` worker left parked on a closed window."""
    store.register_worker(
        "xps", host="xps", pools=[SHARED], harnesses=["claude"], repos=["alpha"],
        slots=2, slots_free=2, now=clock(),
    )
    job_id, run_id, db = _parked_job(store, registry, tmp_path, "alpha", **ledger)
    # The previous holder's worker name stays on the row, which is how the pool is known.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE jobs SET worker = 'xps' WHERE id = ?", (job_id,))
    return job_id, run_id, db


def test_a_park_pauses_only_the_pool_of_the_worker_that_held_it(tmp_path) -> None:
    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    reset_at = clock.now.timestamp() + 600
    _, run_id, _ = _park_in_shared_pool(store, registry, tmp_path, clock, reset_at=reset_at)
    m3_job = store.add_job(repo=_repo(tmp_path, "beta"), kind="fix", scope="42", pool=M3)
    shared_job = store.add_job(repo=_repo(tmp_path, "gamma"), kind="fix", scope="43", pool=SHARED)
    notifier = RecordingNotifier()

    launcher = _drain(
        store, tmp_path, profile=_profile("m3max", M3), clock=clock, registry=registry,
        notifier=notifier,
    )

    pause = store.dispatch_pause(SHARED)
    assert pause is not None and pause.run_id == run_id and pause.pool == SHARED
    assert store.dispatch_pause() is None and store.dispatch_pause(M3) is None
    assert store.get_job(shared_job).state == "queued"  # the paused pool waits
    assert store.get_job(m3_job).state != "queued"  # the M3 pool kept building
    assert len(launcher.calls) >= 1
    (event,) = notifier.names("queue_paused")
    assert event["subject"] == f"development queue ({SHARED})"


def test_a_probe_success_on_a_worker_of_the_pool_reopens_it_for_everyone(tmp_path) -> None:
    from sdlc.capability import ProbeStatus

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    # The pause was discovered elsewhere: no ledger of ours records this run.
    store.pause_dispatch(
        until=clock.now + timedelta(hours=5),
        reason="rate limited", run_id="remote-run", repo="/elsewhere", pool=SHARED, now=clock(),
    )
    shared_job = store.add_job(repo=_repo(tmp_path, "alpha"), kind="fix", scope="1", pool=SHARED)
    notifier = RecordingNotifier()

    _drain(
        store, tmp_path, profile=_profile("xps", SHARED), clock=clock, registry=registry,
        probe=lambda: ProbeStatus.AVAILABLE, notifier=notifier, passes=400,
    )

    assert store.dispatch_pause(SHARED) is None  # cleared in the shared store: every worker
    assert len(notifier.names("queue_resumed")) == 1
    assert store.get_job(shared_job).state != "queued"


def test_a_probe_from_a_worker_outside_the_pool_does_not_reopen_it(tmp_path) -> None:
    from sdlc.capability import ProbeStatus

    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    store.pause_dispatch(
        until=clock.now + timedelta(hours=5), reason="rate limited",
        run_id="remote-run", pool=SHARED, now=clock(),
    )
    probes = {"n": 0}

    def probe() -> ProbeStatus:
        probes["n"] += 1
        return ProbeStatus.AVAILABLE

    _drain(
        store, tmp_path, profile=_profile("m3max", M3), clock=clock, registry=registry,
        probe=probe, passes=400,
    )

    assert probes["n"] == 0  # the M3's credentials say nothing about claude-shared
    assert store.dispatch_pause(SHARED) is not None


def test_a_local_queue_still_pauses_everything_with_one_poolless_window(tmp_path) -> None:
    store = _store(tmp_path)
    registry = Registry(tmp_path / "registry.json")
    clock = Clock()
    reset_at = clock.now.timestamp() + 600
    _parked_job(store, registry, tmp_path, "alpha", reset_at=reset_at)
    other = store.add_job(repo=_repo(tmp_path, "beta"), kind="fix", scope="42", pool=M3)
    launcher = FakeLauncher(alive_polls=1)

    run_queue(
        store,
        config=SchedulerConfig(slots=2, poll_seconds=1.0),
        registry=registry,
        launcher=launcher,
        clock=clock,
        sleeper=_stop_after(clock, 3),
        notifier=lambda *a, **k: None,
        version_check=_clean,
        echo=lambda _line: None,
    )

    pause = store.dispatch_pause()
    assert pause is not None and pause.pool is None
    assert [p.pool for p in store.dispatch_pauses()] == [None]
    assert launcher.calls == []
    assert store.get_job(other).state == "queued"
