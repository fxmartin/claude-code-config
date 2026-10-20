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


# --- Story 35.5-005: the worker that parked a job takes it back ------------


def _worker(store: QueueStore, name: str, *, host: str | None = None, pools=(), now=None):
    return store.register_worker(
        name, host=host or name, pools=list(pools), harnesses=["claude"],
        slots=2, slots_free=2, now=now or _now(),
    )


def _parked_by(store: QueueStore, worker: str | None, *, repo: str = "/a", **pins) -> int:
    """A job ``worker`` ran (``None``: a local drain) that parked awaiting approval."""
    job_id = store.add_job(repo=repo, kind="build", scope="epic-3", **pins)
    store.claim_job(job_id, claimed_by=f"{worker}:1", lease_seconds=90, now=_now(), worker=worker)
    store.attach_run(job_id, "run-abc")
    store.park_job(job_id, pr_number=12, reason="awaiting approval", poll_after=None)
    return job_id


def test_park_job_records_the_worker_that_held_the_run(tmp_path) -> None:
    store = _store(tmp_path)
    _worker(store, "m3max")
    job_id = _parked_by(store, "m3max")

    parked = store.get_job(job_id)
    assert parked.parked_by == "m3max"
    assert parked.worker is None and parked.claimed_by is None  # it holds no claim
    assert parked.to_dict()["parked_by"] == "m3max"


def test_a_local_park_records_no_owner(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = _parked_by(store, None)

    assert store.get_job(job_id).parked_by is None


def test_only_the_owner_sees_a_job_it_parked_as_due(tmp_path) -> None:
    store = _store(tmp_path)
    _worker(store, "m3max")
    _worker(store, "home-lab")
    job_id = _parked_by(store, "m3max")

    assert [j.id for j in store.due_parked_jobs(now=_now(), worker="m3max")] == [job_id]
    assert store.due_parked_jobs(now=_now(), worker="home-lab") == []
    assert store.due_parked_jobs(now=_now()) == []  # a plain drain is not its owner either


def test_a_job_parked_by_an_unregistered_holder_is_anyones(tmp_path) -> None:
    """Nobody can come back for it — the pre-fleet rule, as a lapsed lease keeps it."""
    store = _store(tmp_path)
    _worker(store, "home-lab")
    job_id = _parked_by(store, "gone-worker")

    assert [j.id for j in store.due_parked_jobs(now=_now(), worker="home-lab")] == [job_id]
    assert [j.id for j in store.due_parked_jobs(now=_now())] == [job_id]


def test_a_peer_cannot_take_back_a_job_another_worker_parked(tmp_path) -> None:
    store = _store(tmp_path)
    _worker(store, "m3max")
    _worker(store, "home-lab")
    job_id = _parked_by(store, "m3max")

    assert store.take_parked_job(
        job_id, claimed_by="home-lab:1", lease_seconds=90, now=_now(), worker="home-lab"
    ) is None
    assert store.take_parked_job(job_id, claimed_by="plain:1", lease_seconds=90, now=_now()) is None
    taken = store.take_parked_job(
        job_id, claimed_by="m3max:1", lease_seconds=90, now=_now(), worker="m3max"
    )
    assert taken is not None and taken.worker == "m3max"
    assert taken.parked_by == "m3max"  # kept, not cleared, by the take-back


def test_host_and_pool_pins_apply_to_a_take_back(tmp_path) -> None:
    """A legacy row (no owner) still only goes where a claim of it could."""
    store = _store(tmp_path)
    _worker(store, "m3max", host="m3max", pools=["claude-m3"])
    _worker(store, "home-lab", host="home-lab", pools=["claude-shared"])
    _worker(store, "m3-other-pool", host="m3max", pools=["claude-shared"])
    job_id = _parked_by(store, None, host="m3max", pool="claude-m3")

    assert store.due_parked_jobs(now=_now(), worker="home-lab") == []
    assert store.due_parked_jobs(now=_now(), worker="m3-other-pool") == []
    assert [j.id for j in store.due_parked_jobs(now=_now(), worker="m3max")] == [job_id]
    for peer in ("home-lab", "m3-other-pool"):
        assert store.take_parked_job(
            job_id, claimed_by=f"{peer}:1", lease_seconds=90, now=_now(), worker=peer
        ) is None
    assert store.take_parked_job(
        job_id, claimed_by="m3max:1", lease_seconds=90, now=_now(), worker="m3max"
    ) is not None


def test_repark_job_hands_a_take_back_straight_back_to_its_park(tmp_path) -> None:
    store = _store(tmp_path)
    _worker(store, "m3max")
    job_id = _parked_by(store, "m3max")
    store.take_parked_job(job_id, claimed_by="m3max:1", lease_seconds=90, now=_now(), worker="m3max")
    later = _now() + timedelta(seconds=300)

    assert store.repark_job(
        job_id, claimed_by="m3max:1", reason="unknown run id: run-abc", poll_after=later
    ) is True

    job = store.get_job(job_id)
    assert job.state == "parked"
    assert job.reason == "unknown run id: run-abc"
    assert job.poll_after == later.isoformat()
    assert job.claimed_by is None and job.worker is None and job.lease_until is None
    assert job.parked_by == "m3max" and job.pr_number == 12 and job.run_id == "run-abc"
    assert store.running_repos() == set()


def test_repark_job_keeps_a_legacy_row_ownerless(tmp_path) -> None:
    store = _store(tmp_path)
    _worker(store, "home-lab")
    job_id = _parked_by(store, None)
    store.take_parked_job(
        job_id, claimed_by="home-lab:1", lease_seconds=90, now=_now(), worker="home-lab"
    )

    store.repark_job(job_id, claimed_by="home-lab:1", reason="unknown run id", poll_after=None)

    assert store.get_job(job_id).parked_by is None  # still anyone's, so the right worker can find it


def test_repark_job_needs_the_holder(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = _parked_by(store, None)
    store.take_parked_job(job_id, claimed_by="a:1", lease_seconds=90, now=_now())

    assert store.repark_job(job_id, claimed_by="b:1", reason="x", poll_after=None) is False
    assert store.get_job(job_id).state == "running"


def test_repark_job_honours_a_cancel_that_landed_meanwhile(tmp_path) -> None:
    store = _store(tmp_path)
    job_id = _parked_by(store, None)
    store.take_parked_job(job_id, claimed_by="a:1", lease_seconds=90, now=_now())
    store.cancel_job(job_id)

    assert store.repark_job(job_id, claimed_by="a:1", reason="x", poll_after=None) is False
    assert store.get_job(job_id).state == "cancelled"


def test_a_parked_job_names_its_owner_once_it_has_been_offline_a_day(tmp_path) -> None:
    from sdlc.queue import PARKED_OWNER_OFFLINE_SECONDS, PARKED_OWNER_REASON_PREFIX

    store = _store(tmp_path)
    _worker(store, "m3max")
    job_id = _parked_by(store, "m3max")

    _worker(store, "home-lab", now=_now() + timedelta(seconds=PARKED_OWNER_OFFLINE_SECONDS - 60))
    assert store.get_job(job_id).reason == "awaiting approval"  # a reboot is not a day

    _worker(store, "home-lab", now=_now() + timedelta(seconds=PARKED_OWNER_OFFLINE_SECONDS + 60))
    job = store.get_job(job_id)
    assert job.state == "parked"
    assert job.reason.startswith(PARKED_OWNER_REASON_PREFIX + "m3max")
    assert "#12" in job.reason and "sdlc queue requeue" in job.reason and "cancel" in job.reason
    # Still the owner's alone: nobody else is offered it.
    later = _now() + timedelta(seconds=PARKED_OWNER_OFFLINE_SECONDS + 60)
    assert store.due_parked_jobs(now=later, worker="home-lab") == []


def test_ensure_migrated_adds_the_parked_by_column(tmp_path) -> None:
    store = _pre_32_2_store(tmp_path)

    store.ensure_migrated()

    with sqlite3.connect(store.db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "parked_by" in cols
    assert store.list_jobs()[0].parked_by is None
