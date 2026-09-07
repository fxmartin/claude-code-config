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


# --- review follow-ups (bugfix #32.1-002) ---------------------------------


def test_claim_refuses_a_job_whose_repo_already_has_a_running_job(tmp_path) -> None:
    """Per-repo exclusivity is enforced *in the claim*, not only in the peek.

    `peek_claimable` filters busy repos in Python, so two schedulers that both
    peek before either claims would each see the repo idle and each claim a
    *different* queued job in it — two UPDATEs on distinct rows, both winners.
    The guard has to live in the same statement that takes the row.
    """
    store = _store(tmp_path)
    first = store.add_job(repo="/a", kind="fix", scope="42")
    second = store.add_job(repo="/a", kind="build", scope="epic-3")
    store.claim_job(first, claimed_by="host:1", lease_seconds=90, now=_now())

    assert store.claim_job(
        second, claimed_by="host:2", lease_seconds=90, now=_now()
    ) is None
    assert store.get_job(second).state == "queued"


def test_claim_allows_a_repo_whose_only_other_job_is_terminal(tmp_path) -> None:
    """A finished job never holds its repo hostage."""
    store = _store(tmp_path)
    first = store.add_job(repo="/a", kind="fix", scope="42")
    second = store.add_job(repo="/a", kind="build", scope="epic-3")
    store.claim_job(first, claimed_by="host:1", lease_seconds=90, now=_now())
    store.finish_job(first, "done")

    claimed = store.claim_job(
        second, claimed_by="host:2", lease_seconds=90, now=_now()
    )
    assert claimed is not None
    assert claimed.state == "running"


def test_claim_is_unaffected_by_a_running_job_in_another_repo(tmp_path) -> None:
    store = _store(tmp_path)
    other = store.add_job(repo="/b", kind="fix", scope="42")
    mine = store.add_job(repo="/a", kind="build", scope="epic-3")
    store.claim_job(other, claimed_by="host:1", lease_seconds=90, now=_now())

    assert store.claim_job(
        mine, claimed_by="host:2", lease_seconds=90, now=_now()
    ) is not None


def test_a_blocked_job_can_be_requeued(tmp_path) -> None:
    """`blocked` is a park, not a grave: the remedy is followed, then it runs.

    Without an exit, a job parked by the per-job controller-version check could
    only be replaced by hand-retyping its frozen options.
    """
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="build", scope="epic-3")
    store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())
    store.finish_job(job_id, "blocked", reason="reinstall the controller")

    store.requeue_job(job_id)

    job = store.get_job(job_id)
    assert job.state == "queued"
    assert job.claimed_by is None
    assert job.lease_until is None
    assert job.reason is None


def test_a_failed_job_can_be_requeued(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="fix", scope="42")
    store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())
    store.finish_job(job_id, "failed", reason="run status FAILED")

    store.requeue_job(job_id)
    assert store.get_job(job_id).state == "queued"


def test_requeue_refuses_a_running_job(tmp_path) -> None:
    from sdlc.queue import QueueError

    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="fix", scope="42")
    store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())

    with pytest.raises(QueueError):
        store.requeue_job(job_id)
    assert store.get_job(job_id).state == "running"


def test_requeue_unknown_id_raises(tmp_path) -> None:
    from sdlc.queue import QueueError

    store = _store(tmp_path)
    with pytest.raises(QueueError):
        store.requeue_job(999)


def test_a_requeued_job_keeps_its_frozen_options(tmp_path) -> None:
    """The whole point of a requeue over a hand-retyped `queue add`."""
    import json

    store = _store(tmp_path)
    job_id = store.add_job(
        repo="/a", kind="build", scope="epic-3",
        options_json=json.dumps(["epic-3", "--auto", "--concurrency=4"]),
    )
    store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())
    store.finish_job(job_id, "blocked", reason="stale controller")

    store.requeue_job(job_id)
    assert json.loads(store.get_job(job_id).options) == [
        "epic-3", "--auto", "--concurrency=4",
    ]


def test_a_blocked_job_can_be_cancelled(tmp_path) -> None:
    """The other exit from `blocked` — retire the job instead of retrying it."""
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="build", scope="epic-3")
    store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())
    store.finish_job(job_id, "blocked", reason="stale controller")

    store.cancel_job(job_id)
    assert store.get_job(job_id).state == "cancelled"


def test_requeueing_a_job_with_a_run_resumes_rather_than_restarts(tmp_path) -> None:
    """A job that already opened a run must never start over from scratch.

    Same two shapes `release_claim` draws: the job returns to `running` with an
    expired lease so the next drain reclaims it through `sdlc resume --run`.
    """
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="build", scope="epic-3")
    store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())
    store.attach_run(job_id, "run-halfdone")
    store.finish_job(job_id, "blocked", reason="parked")

    store.requeue_job(job_id, now=_now())

    job = store.get_job(job_id)
    assert job.state == "running"
    assert job.run_id == "run-halfdone"
    assert job.claimed_by is None
    # The lease is already expired, so the next pass sees it as reclaimable.
    assert [j.id for j in store.expired_running_jobs(now=_now() + timedelta(seconds=1))] == [
        job_id
    ]


def test_requeue_refuses_an_already_queued_job(tmp_path) -> None:
    from sdlc.queue import QueueError

    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="fix", scope="42")
    with pytest.raises(QueueError):
        store.requeue_job(job_id)
