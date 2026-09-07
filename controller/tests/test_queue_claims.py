# ABOUTME: Unit tests for the queue store's claim/lease/reclaim verbs (Story 32.1-002).
# ABOUTME: Atomic claim, lease renewal, expiry, per-repo exclusion, and terminal states.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _store(tmp_path):
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    return store


def _now() -> datetime:
    return datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)


def test_claim_job_moves_queued_to_running_with_a_lease(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="fix", scope="42")

    claimed = store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())

    assert claimed is not None
    assert claimed.state == "running"
    assert claimed.claimed_by == "host:1"
    assert claimed.lease_until == (_now() + timedelta(seconds=90)).isoformat()


def test_claim_job_is_atomic_second_claimer_gets_nothing(tmp_path) -> None:
    """The state guard in the UPDATE is what makes a double claim impossible."""
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="fix", scope="42")

    first = store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())
    second = store.claim_job(job_id, claimed_by="host:2", lease_seconds=90, now=_now())

    assert first is not None
    assert second is None
    assert store.get_job(job_id).claimed_by == "host:1"


def test_peek_claimable_orders_by_priority_then_fifo(tmp_path) -> None:
    store = _store(tmp_path)
    low = store.add_job(repo="/a", kind="fix", scope="1", priority="low")
    urgent = store.add_job(repo="/b", kind="fix", scope="2", priority="urgent")
    normal = store.add_job(repo="/c", kind="fix", scope="3")

    order = [j.id for j in store.peek_claimable(now=_now())]
    assert order == [urgent, normal, low]


def test_peek_claimable_excludes_busy_repos(tmp_path) -> None:
    store = _store(tmp_path)
    store.add_job(repo="/a", kind="fix", scope="1")
    other = store.add_job(repo="/b", kind="fix", scope="2")

    rows = store.peek_claimable(busy_repos={"/a"}, now=_now())
    assert [j.id for j in rows] == [other]


def test_peek_claimable_skips_cancelled_and_running(tmp_path) -> None:
    store = _store(tmp_path)
    cancelled = store.add_job(repo="/a", kind="fix", scope="1")
    store.cancel_job(cancelled)
    running = store.add_job(repo="/b", kind="fix", scope="2")
    store.claim_job(running, claimed_by="host:1", lease_seconds=90, now=_now())

    assert store.peek_claimable(now=_now()) == []


def test_running_repos_reports_repos_with_a_live_job(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="fix", scope="1")
    store.add_job(repo="/b", kind="fix", scope="2")

    assert store.running_repos() == set()
    store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())
    assert store.running_repos() == {"/a"}


def test_renew_lease_extends_only_the_holder(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="fix", scope="42")
    store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())

    later = _now() + timedelta(seconds=30)
    assert store.renew_lease(job_id, claimed_by="host:1", lease_seconds=90, now=later)
    assert store.get_job(job_id).lease_until == (later + timedelta(seconds=90)).isoformat()

    assert not store.renew_lease(job_id, claimed_by="host:2", lease_seconds=90, now=later)


def test_expired_running_jobs_only_reports_lapsed_leases(tmp_path) -> None:
    store = _store(tmp_path)
    fresh = store.add_job(repo="/a", kind="fix", scope="1")
    lapsed = store.add_job(repo="/b", kind="fix", scope="2")
    store.claim_job(fresh, claimed_by="host:1", lease_seconds=90, now=_now())
    store.claim_job(lapsed, claimed_by="host:1", lease_seconds=90, now=_now())

    after = _now() + timedelta(seconds=60)
    assert store.expired_running_jobs(now=after) == []

    store.renew_lease(fresh, claimed_by="host:1", lease_seconds=90, now=after)
    much_later = _now() + timedelta(seconds=120)
    assert [j.id for j in store.expired_running_jobs(now=much_later)] == [lapsed]


def test_reclaim_job_takes_over_an_expired_running_job(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="build", scope="epic-3")
    store.claim_job(job_id, claimed_by="dead:1", lease_seconds=90, now=_now())
    store.attach_run(job_id, "run-abc")

    later = _now() + timedelta(seconds=200)
    taken = store.reclaim_job(job_id, claimed_by="host:2", lease_seconds=90, now=later)

    assert taken is not None
    assert taken.claimed_by == "host:2"
    assert taken.run_id == "run-abc"
    assert taken.state == "running"

    # A second scheduler racing for the same job now finds a live lease.
    assert store.reclaim_job(job_id, claimed_by="host:3", lease_seconds=90, now=later) is None


def test_release_claim_requeues_a_job_that_never_started_a_run(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="fix", scope="42")
    store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())

    store.release_claim(job_id, claimed_by="host:1", reason="scheduler interrupted", now=_now())

    job = store.get_job(job_id)
    assert job.state == "queued"
    assert job.claimed_by is None
    assert job.lease_until is None
    assert job.reason == "scheduler interrupted"


def test_release_claim_expires_the_lease_of_a_job_with_a_run(tmp_path) -> None:
    """A started run must stay `running` so the next scheduler resumes it."""
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="build", scope="epic-3")
    store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())
    store.attach_run(job_id, "run-abc")

    store.release_claim(job_id, claimed_by="host:1", reason="interrupted", now=_now())

    job = store.get_job(job_id)
    assert job.state == "running"
    assert job.claimed_by is None
    assert job.lease_until == _now().isoformat()
    assert [j.id for j in store.expired_running_jobs(now=_now() + timedelta(seconds=1))] == [job_id]


def test_finish_job_records_a_terminal_state_and_clears_the_lease(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="fix", scope="42")
    store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())

    store.finish_job(job_id, "done")

    job = store.get_job(job_id)
    assert job.state == "done"
    assert job.lease_until is None
    assert job.claimed_by is None


def test_finish_job_accepts_blocked_for_a_parked_job(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="build", scope="epic-3")

    store.finish_job(job_id, "blocked", reason="stale controller")

    job = store.get_job(job_id)
    assert job.state == "blocked"
    assert job.reason == "stale controller"


def test_finish_job_rejects_an_unknown_state(tmp_path) -> None:
    from sdlc.queue import QueueError

    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="fix", scope="42")
    with pytest.raises(QueueError):
        store.finish_job(job_id, "frobnicated")


def test_set_reason_stamps_a_queued_job(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="fix", scope="42")

    store.set_reason(job_id, "repo busy")
    assert store.get_job(job_id).reason == "repo busy"


def test_reads_on_a_missing_store_never_conjure_one(tmp_path) -> None:
    """A host that has never enqueued anything must be readable, not created."""
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "nothing-here.db")

    assert store.peek_claimable(now=_now()) == []
    assert store.expired_running_jobs(now=_now()) == []
    assert store.running_repos() == set()
    assert not (tmp_path / "nothing-here.db").exists()


def test_release_claim_ignores_a_job_we_do_not_hold(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="fix", scope="42")
    store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())

    store.release_claim(job_id, claimed_by="host:2", reason="not mine", now=_now())
    store.release_claim(9999, claimed_by="host:1", reason="unknown", now=_now())

    job = store.get_job(job_id)
    assert job.state == "running"
    assert job.claimed_by == "host:1"
    assert job.reason is None
