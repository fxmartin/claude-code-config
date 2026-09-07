# ABOUTME: Unit tests for the queue store's approval-park verbs (Story 32.2-002).
# ABOUTME: park/poll scheduling/take-back, no-slot-held, cancel+requeue, migration.

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sdlc.queue import QueueError, QueueStore


def _store(tmp_path) -> QueueStore:
    store = QueueStore(tmp_path / "queue.db")
    store.init()
    return store


def _now() -> datetime:
    return datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)


def _parked(store: QueueStore, *, repo: str = "/a", pr: int = 12) -> int:
    """A job that ran, opened PR ``pr``, and parked awaiting approval."""
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3")
    store.claim_job(job_id, claimed_by="host:1", lease_seconds=90, now=_now())
    store.attach_run(job_id, "run-abc")
    store.park_job(
        job_id,
        pr_number=pr,
        reason="awaiting approval on PR #%d" % pr,
        poll_after=_now() + timedelta(seconds=300),
    )
    return job_id


# --- AC1: park records the PR and the next poll ---------------------------


def test_park_job_records_the_pr_number_and_drops_the_lease(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = _parked(store)

    job = store.get_job(job_id)
    assert job is not None
    assert job.state == "parked"
    assert job.pr_number == 12
    assert job.run_id == "run-abc"
    assert job.claimed_by is None
    assert job.lease_until is None
    assert job.poll_after == (_now() + timedelta(seconds=300)).isoformat()
    assert "awaiting approval" in (job.reason or "")


def test_park_job_refuses_a_job_that_never_opened_a_run(tmp_path) -> None:
    """Nothing to resume later means nothing worth polling for — refuse it."""
    store = _store(tmp_path)
    job_id = store.add_job(repo="/a", kind="build", scope="epic-3")

    with pytest.raises(QueueError):
        store.park_job(job_id, pr_number=12, reason="x", poll_after=_now())


def test_park_job_refuses_an_unknown_job(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(QueueError):
        store.park_job(999, pr_number=12, reason="x", poll_after=_now())


# --- AC4: a parked job holds no slot --------------------------------------


def test_a_parked_job_frees_its_repo_and_holds_no_slot(tmp_path) -> None:
    """`running_repos` is the slot/exclusivity truth — a park must leave it."""
    store = _store(tmp_path)
    _parked(store, repo="/a")

    assert store.running_repos() == set()
    # And a fresh job in the same repo is claimable again.
    other = store.add_job(repo="/a", kind="fix", scope="7")
    assert [j.id for j in store.peek_claimable(now=_now())] == [other]


# --- AC1/AC4: bounded polling --------------------------------------------


def test_due_parked_jobs_only_returns_jobs_whose_poll_is_due(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = _parked(store)

    early = _now() + timedelta(seconds=299)
    assert store.due_parked_jobs(now=early) == []

    due = _now() + timedelta(seconds=300)
    assert [j.id for j in store.due_parked_jobs(now=due)] == [job_id]


def test_a_park_with_no_poll_after_is_due_immediately(tmp_path) -> None:
    """A store written before this column existed must not stall forever."""
    store = _store(tmp_path)
    job_id = _parked(store)
    store.schedule_poll(job_id, None)

    assert [j.id for j in store.due_parked_jobs(now=_now())] == [job_id]


def test_schedule_poll_pushes_the_next_poll_out(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = _parked(store)
    later = _now() + timedelta(seconds=900)

    store.schedule_poll(job_id, later)

    assert store.get_job(job_id).poll_after == later.isoformat()
    assert store.due_parked_jobs(now=_now() + timedelta(seconds=600)) == []


def test_due_parked_jobs_ignores_non_parked_states(tmp_path) -> None:
    """Polling stops the moment the job leaves `parked` (AC4)."""
    store = _store(tmp_path)
    job_id = _parked(store)
    store.cancel_job(job_id)

    assert store.due_parked_jobs(now=_now() + timedelta(days=1)) == []


# --- AC2: taking a parked job back under a lease --------------------------


def test_take_parked_job_moves_it_to_running_under_a_lease(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = _parked(store)

    taken = store.take_parked_job(
        job_id, claimed_by="host:1", lease_seconds=90, now=_now()
    )

    assert taken is not None
    assert taken.state == "running"
    assert taken.claimed_by == "host:1"
    assert taken.pr_number == 12  # the PR stays on the record (DoD: PR recorded)
    assert taken.poll_after is None
    assert store.running_repos() == {"/a"}


def test_take_parked_job_is_atomic_second_taker_gets_nothing(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = _parked(store)

    first = store.take_parked_job(
        job_id, claimed_by="host:1", lease_seconds=90, now=_now()
    )
    second = store.take_parked_job(
        job_id, claimed_by="host:2", lease_seconds=90, now=_now()
    )

    assert first is not None
    assert second is None
    assert store.get_job(job_id).claimed_by == "host:1"


def test_take_parked_job_respects_per_repo_exclusivity(tmp_path) -> None:
    """A repo already running a job must not also get its parked one back."""
    store = _store(tmp_path)
    parked_id = _parked(store, repo="/a")
    live = store.add_job(repo="/a", kind="fix", scope="7")
    store.claim_job(live, claimed_by="host:1", lease_seconds=90, now=_now())

    taken = store.take_parked_job(
        parked_id, claimed_by="host:1", lease_seconds=90, now=_now()
    )

    assert taken is None
    assert store.get_job(parked_id).state == "parked"


# --- operator exits from a park -------------------------------------------


def test_a_parked_job_can_be_cancelled(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = _parked(store)

    store.cancel_job(job_id)

    assert store.get_job(job_id).state == "cancelled"


def test_a_parked_job_can_be_requeued_back_onto_its_run(tmp_path) -> None:
    """`requeue` on a park returns it to `running` with a lapsed lease."""
    store = _store(tmp_path)
    job_id = _parked(store)

    store.requeue_job(job_id, now=_now())

    job = store.get_job(job_id)
    assert job.state == "running"
    assert job.lease_until == _now().isoformat()
    assert [j.id for j in store.expired_running_jobs(now=_now() + timedelta(seconds=1))] == [job_id]


def test_a_parked_job_can_be_finished_failed(tmp_path) -> None:
    """The closed-PR branch (AC3) is an ordinary terminal from `parked`."""
    store = _store(tmp_path)
    job_id = _parked(store)

    store.finish_job(job_id, "failed", reason="pr closed")

    job = store.get_job(job_id)
    assert job.state == "failed"
    assert job.reason == "pr closed"


# --- schema migration ------------------------------------------------------


def _pre_32_2_store(tmp_path) -> QueueStore:
    """A queue.db written before this story's columns existed."""
    path = tmp_path / "old-queue.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL, kind TEXT NOT NULL, scope TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'normal',
            state TEXT NOT NULL DEFAULT 'queued',
            claimed_by TEXT, lease_until TIMESTAMP, run_id TEXT, options TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reason TEXT
        );
        INSERT INTO jobs(repo, kind, scope) VALUES ('/a', 'fix', '42');
        """
    )
    conn.commit()
    conn.close()
    return QueueStore(path)


def test_an_unmigrated_store_still_lists_without_the_new_columns(tmp_path) -> None:
    """`sdlc queue list` never migrates — it must not crash on an old DB."""
    store = _pre_32_2_store(tmp_path)

    rows = store.list_jobs()

    assert [r.scope for r in rows] == ["42"]
    assert rows[0].pr_number is None
    assert rows[0].poll_after is None


def test_ensure_migrated_adds_the_approval_columns_in_place(tmp_path) -> None:
    store = _pre_32_2_store(tmp_path)

    store.ensure_migrated()

    with sqlite3.connect(store.db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert {"pr_number", "poll_after"} <= cols
    # And the pre-existing row survived untouched.
    assert [r.scope for r in store.list_jobs()] == ["42"]
