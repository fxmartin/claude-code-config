# ABOUTME: Every scheduler-facing queue verb over HTTP: route + QueueClient method (Story 35.2-005).
# ABOUTME: Each verb runs against a live in-process `serve` and is compared with the local QueueStore.

from __future__ import annotations

import inspect
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sdlc.queue import JobRecord, QueueBackend, QueueError, QueueStore
from sdlc.queue_client import QueueClient, QueueRequestError, QueueUnavailable
from sdlc.queue_server import AccessPolicy, make_server
from sdlc.registry import RunRecord

TOKEN = "s3cret-token"
T0 = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)

# The scheduler's store surface, as the story lists it (plus what it already used).
SCHEDULER_SURFACE = (
    "peek_claimable", "claim_job", "claimable_for_worker", "reclaim_job",
    "expired_running_jobs", "running_repos", "overlap_holds", "park_job",
    "take_parked_job", "due_parked_jobs", "schedule_poll", "set_reason",
    "attach_run", "record_sync", "record_files", "record_fix_rounds_baseline",
    "restart_fresh", "requeue_job", "dispatch_pause", "dispatch_pauses",
    "mark_pause_probed", "get_worker", "register_worker", "put_fleet_run",
    "renew_lease", "release_claim", "finish_job", "pause_dispatch", "clear_pause",
    "get_job", "list_jobs",
)


class FakeClock:
    """One clock for the service and (in the scheduler tests) the worker."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Live:
    def __init__(self, store: QueueStore, clock: FakeClock) -> None:
        policy = AccessPolicy(token=TOKEN, networks=("127.0.0.0/8",))
        self.server = make_server(store, policy, "127.0.0.1", 0, clock=clock)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=10)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path) -> QueueStore:
    s = QueueStore(tmp_path / "service.db")
    s.init()
    return s


@pytest.fixture
def client(store: QueueStore, clock: FakeClock):
    live = Live(store, clock)
    yield QueueClient(live.url, token=TOKEN)
    live.stop()


def _job(store: QueueStore, repo: str = "/r/a", kind: str = "build", **kw) -> int:
    return store.add_job(repo=repo, kind=kind, scope="1.1-001", **kw)


# --- the contract itself -------------------------------------------------------


@pytest.mark.parametrize("name", SCHEDULER_SURFACE)
def test_the_backend_protocol_declares_every_scheduler_verb(name: str) -> None:
    assert hasattr(QueueBackend, name), f"QueueBackend lacks {name}"


@pytest.mark.parametrize("name", SCHEDULER_SURFACE)
def test_the_client_implements_every_scheduler_verb(name: str) -> None:
    assert callable(getattr(QueueClient, name, None)), f"QueueClient lacks {name}"


@pytest.mark.parametrize("name", SCHEDULER_SURFACE)
def test_the_client_takes_the_same_keywords_as_the_store(name: str) -> None:
    store_params = set(inspect.signature(getattr(QueueStore, name)).parameters)
    client_params = set(inspect.signature(getattr(QueueClient, name)).parameters)
    assert store_params <= client_params, f"{name}: client lacks {store_params - client_params}"


def test_the_scheduler_is_typed_against_the_backend_not_the_store() -> None:
    import sdlc.scheduler as scheduler

    assert "QueueStore" not in inspect.signature(scheduler.run_queue).parameters["store"].annotation
    assert "QueueStore" not in str(inspect.signature(scheduler._Scheduler.__init__).parameters["store"])


# --- claims --------------------------------------------------------------------


def test_claim_job_claims_a_queued_job_and_reports_a_lost_race_as_none(
    client: QueueClient, store: QueueStore, clock: FakeClock
) -> None:
    job_id = _job(store)
    claimed = client.claim_job(job_id, claimed_by="m3max", lease_seconds=90, now=clock(), worker="m3max")
    assert claimed is not None and claimed.state == "running"
    assert claimed.claimed_by == "m3max" and claimed.worker == "m3max"
    assert claimed.lease_until == (T0 + timedelta(seconds=90)).isoformat()
    # The same job a second time: the guarded UPDATE says no -> 409 -> None.
    assert client.claim_job(job_id, claimed_by="xps", lease_seconds=90, worker="xps") is None


def test_claim_job_honours_repo_exclusivity(client: QueueClient, store: QueueStore) -> None:
    first = _job(store, kind="fix")
    second = _job(store, kind="build")
    assert client.claim_job(first, claimed_by="m3max", lease_seconds=90) is not None
    assert client.claim_job(second, claimed_by="xps", lease_seconds=90) is None


def test_claim_job_for_an_unknown_job_is_none(client: QueueClient) -> None:
    assert client.claim_job(999, claimed_by="m3max", lease_seconds=90) is None


def test_claim_job_rejects_a_bad_lease(client: QueueClient, store: QueueStore) -> None:
    job_id = _job(store)
    with pytest.raises(QueueRequestError) as err:
        client.claim_job(job_id, claimed_by="m3max", lease_seconds=0)
    assert err.value.status == 400


def test_reclaim_job_takes_over_only_a_lapsed_lease(
    client: QueueClient, store: QueueStore, clock: FakeClock
) -> None:
    job_id = _job(store)
    assert client.claim_job(job_id, claimed_by="m3max", lease_seconds=90, now=clock()) is not None
    assert client.reclaim_job(job_id, claimed_by="xps", lease_seconds=90, worker="xps") is None
    clock.advance(91)
    taken = client.reclaim_job(job_id, claimed_by="xps", lease_seconds=90, now=clock(), worker="xps")
    assert taken is not None and taken.claimed_by == "xps" and taken.worker == "xps"


def test_expired_running_jobs_lists_only_lapsed_leases(
    client: QueueClient, store: QueueStore, clock: FakeClock
) -> None:
    job_id = _job(store)
    client.claim_job(job_id, claimed_by="m3max", lease_seconds=90, now=clock())
    assert client.expired_running_jobs(now=clock()) == []
    clock.advance(120)
    assert [j.id for j in client.expired_running_jobs(now=clock())] == [job_id]


def test_peek_claimable_is_in_dispatch_order_and_filters_busy_repos(
    client: QueueClient, store: QueueStore
) -> None:
    low = _job(store, repo="/r/a", priority="low")
    urgent = _job(store, repo="/r/b", priority="urgent")
    fix = _job(store, repo="/r/c", kind="fix")
    assert [j.id for j in client.peek_claimable()] == [urgent, fix, low]
    assert [j.id for j in client.peek_claimable(busy_repos={"/r/c"})] == [
        j.id for j in store.peek_claimable(busy_repos={"/r/c"})
    ]
    assert fix not in [j.id for j in client.peek_claimable(busy_repos={"/r/c"})]
    assert [j.id for j in client.peek_claimable(fix_busy_repos={"/r/a", "/r/b"})] == [fix]


def test_peek_claimable_carries_repo_paths_with_awkward_characters(
    client: QueueClient, store: QueueStore
) -> None:
    odd = "/r/with space&amp=equals"
    job_id = _job(store, repo=odd)
    assert [j.id for j in client.peek_claimable(busy_repos={odd}, fix_busy_repos={odd})] == []
    assert [j.id for j in client.peek_claimable()] == [job_id]


def test_running_repos_filters_by_kind_and_exclusion(client: QueueClient, store: QueueStore) -> None:
    build = _job(store, repo="/r/a")
    fix = _job(store, repo="/r/b", kind="fix")
    client.claim_job(build, claimed_by="m3max", lease_seconds=90)
    client.claim_job(fix, claimed_by="m3max", lease_seconds=90)
    assert client.running_repos() == {"/r/a", "/r/b"}
    assert client.running_repos(kind="fix") == {"/r/b"}
    assert client.running_repos(excluding=fix) == {"/r/a"}
    assert client.running_repos(excluding=build, kind="build") == set()


def test_overlap_holds_reads_the_service_graph(client: QueueClient, store: QueueStore) -> None:
    a = _job(store, repo="/r/a")
    b = _job(store, repo="/r/a")
    assert client.overlap_holds() == {}
    client.record_files(a, ["x.py", "y.py"])
    client.record_files(b, ["y.py"])
    holds = client.overlap_holds()
    assert holds == store.overlap_holds() and holds  # int keys survive the JSON round trip
    assert all(isinstance(k, int) and isinstance(v, int) for k, v in holds.items())


def test_claimable_for_worker_applies_the_workers_eligibility(
    client: QueueClient, store: QueueStore, clock: FakeClock
) -> None:
    mine = _job(store, requirements_json=json.dumps({"repo": "agentic"}))
    pinned = _job(store, host="home-lab")
    store.register_worker("m3max", host="mac", repos=["agentic"], now=clock())
    candidates = client.peek_claimable()
    kept = client.claimable_for_worker("m3max", candidates, slots_free=1, now=clock())
    assert [j.id for j in kept] == [mine]
    assert pinned not in [j.id for j in kept]
    assert client.claimable_for_worker("m3max", candidates, slots_free=0, now=clock()) == []


def test_claimable_for_worker_passes_an_unregistered_worker_everything(
    client: QueueClient, store: QueueStore
) -> None:
    ids = [_job(store), _job(store, repo="/r/b")]
    assert [j.id for j in client.claimable_for_worker("ghost", client.peek_claimable())] == ids


def test_claimable_for_worker_ignores_jobs_the_service_no_longer_has(client: QueueClient) -> None:
    ghost = JobRecord(
        id=404, repo="/r", kind="build", scope="x", priority="normal", state="queued",
        claimed_by=None, lease_until=None, run_id=None, options=None,
        created_at=T0.isoformat(), updated_at=T0.isoformat(), reason=None,
    )
    assert client.claimable_for_worker("m3max", [ghost]) == []


def test_get_worker_returns_the_record_or_none(
    client: QueueClient, store: QueueStore, clock: FakeClock
) -> None:
    assert client.get_worker("m3max") is None
    store.register_worker("m3max", host="mac", pools=["claude-m3"], now=clock())
    worker = client.get_worker("m3max")
    assert worker is not None and worker.pools == ["claude-m3"]


def test_get_worker_name_with_awkward_characters_round_trips(
    client: QueueClient, store: QueueStore, clock: FakeClock
) -> None:
    store.register_worker("m3 max/√", host="mac", now=clock())
    worker = client.get_worker("m3 max/√")
    assert worker is not None and worker.name == "m3 max/√"


def test_register_worker_heartbeat_uses_the_service_clock(
    client: QueueClient, clock: FakeClock
) -> None:
    record = client.register_worker("m3max", host="mac", now=clock())
    assert record.last_heartbeat == T0.isoformat()


# --- parks ---------------------------------------------------------------------


def _running_with_run(client: QueueClient, store: QueueStore, run_id: str = "run-1") -> int:
    job_id = _job(store)
    client.claim_job(job_id, claimed_by="m3max", lease_seconds=90, worker="m3max")
    client.attach_run(job_id, run_id)
    return job_id


def test_park_take_and_due_round_trip(client: QueueClient, store: QueueStore, clock: FakeClock) -> None:
    job_id = _running_with_run(client, store)
    due_at = T0 + timedelta(minutes=5)
    assert client.park_job(job_id, pr_number=42, reason="awaiting approval", poll_after=due_at) is True
    parked = store.get_job(job_id)
    assert parked is not None and parked.state == "parked" and parked.pr_number == 42
    assert parked.poll_after == due_at.isoformat() and parked.claimed_by is None
    assert client.due_parked_jobs(now=clock()) == []
    clock.advance(301)
    assert [j.id for j in client.due_parked_jobs(now=clock())] == [job_id]
    taken = client.take_parked_job(job_id, claimed_by="m3max", lease_seconds=90, now=clock(), worker="m3max")
    assert taken is not None and taken.state == "running" and taken.worker == "m3max"
    assert client.take_parked_job(job_id, claimed_by="xps", lease_seconds=90) is None


def test_park_job_with_no_poll_time_is_due_at_once(
    client: QueueClient, store: QueueStore, clock: FakeClock
) -> None:
    job_id = _running_with_run(client, store)
    assert client.park_job(job_id, pr_number=7, reason="r", poll_after=None) is True
    assert [j.id for j in client.due_parked_jobs(now=clock())] == [job_id]


def test_park_job_of_a_cancel_flagged_job_returns_false(client: QueueClient, store: QueueStore) -> None:
    job_id = _running_with_run(client, store)
    client.cancel_job(job_id)
    assert client.park_job(job_id, pr_number=9, reason="r", poll_after=None) is False
    assert store.get_job(job_id).state == "cancelled"  # type: ignore[union-attr]


def test_park_job_without_a_run_is_a_queue_error(client: QueueClient, store: QueueStore) -> None:
    job_id = _job(store)
    client.claim_job(job_id, claimed_by="m3max", lease_seconds=90)
    with pytest.raises(QueueError, match="no run"):
        client.park_job(job_id, pr_number=1, reason="r", poll_after=None)


def test_park_job_for_an_unknown_job_is_a_queue_error(client: QueueClient) -> None:
    with pytest.raises(QueueError, match="unknown job"):
        client.park_job(404, pr_number=1, reason="r", poll_after=None)


def test_schedule_poll_moves_the_next_read(client: QueueClient, store: QueueStore, clock: FakeClock) -> None:
    job_id = _running_with_run(client, store)
    client.park_job(job_id, pr_number=1, reason="r", poll_after=None)
    later = T0 + timedelta(hours=1)
    client.schedule_poll(job_id, later)
    assert store.get_job(job_id).poll_after == later.isoformat()  # type: ignore[union-attr]
    assert client.due_parked_jobs(now=clock()) == []
    client.schedule_poll(job_id, None)
    assert store.get_job(job_id).poll_after is None  # type: ignore[union-attr]


# --- plain writers -------------------------------------------------------------


def test_set_reason_sets_and_clears(client: QueueClient, store: QueueStore) -> None:
    job_id = _job(store)
    client.set_reason(job_id, "repo busy")
    assert store.get_job(job_id).reason == "repo busy"  # type: ignore[union-attr]
    client.set_reason(job_id, None)
    assert store.get_job(job_id).reason is None  # type: ignore[union-attr]


def test_attach_run_links_the_run(client: QueueClient, store: QueueStore) -> None:
    job_id = _job(store)
    client.attach_run(job_id, "run-xyz")
    assert store.get_job(job_id).run_id == "run-xyz"  # type: ignore[union-attr]


def test_record_sync_replaces_the_repo_path_and_stamps_the_sha(
    client: QueueClient, store: QueueStore
) -> None:
    job_id = _job(store, repo="/xps/path")
    client.record_sync(job_id, repo="/Users/fx/Work/r", sha="abc123")
    job = store.get_job(job_id)
    assert job is not None and job.repo == "/Users/fx/Work/r" and job.synced_sha == "abc123"


def test_record_files_stores_a_sorted_footprint(client: QueueClient, store: QueueStore) -> None:
    job_id = _job(store)
    client.record_files(job_id, {"b.py", "a.py"})
    assert store.get_job(job_id).files_to_modify() == {"a.py", "b.py"}  # type: ignore[union-attr]


def test_record_fix_rounds_baseline(client: QueueClient, store: QueueStore) -> None:
    job_id = _job(store)
    client.record_fix_rounds_baseline(job_id, 4)
    assert store.get_job(job_id).fix_rounds_baseline == 4  # type: ignore[union-attr]


def test_restart_fresh_turns_a_running_job_back_into_a_clean_launch(
    client: QueueClient, store: QueueStore, clock: FakeClock
) -> None:
    job_id = _running_with_run(client, store)
    client.restart_fresh(job_id, reason="run is terminal (FAILED) — fresh run", now=clock())
    job = store.get_job(job_id)
    assert job is not None and job.state == "queued" and job.run_id is None
    assert job.reason == "run is terminal (FAILED) — fresh run"


def test_requeue_job_accepts_the_stores_now_keyword(client: QueueClient, store: QueueStore) -> None:
    job_id = _job(store)
    store.finish_job(job_id, "failed")
    client.requeue_job(job_id, now=T0)
    assert store.get_job(job_id).state == "queued"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "verb, args",
    [
        ("set_reason", ("x",)),
        ("attach_run", ("r",)),
        ("record_files", (["a"],)),
        ("record_fix_rounds_baseline", (1,)),
    ],
)
def test_job_writers_reject_an_unknown_job_with_a_request_error(
    client: QueueClient, verb: str, args: tuple
) -> None:
    with pytest.raises(QueueRequestError) as err:
        getattr(client, verb)(404, *args)
    assert err.value.status == 404


def test_bad_payloads_are_400s(client: QueueClient, store: QueueStore) -> None:
    job_id = _job(store)
    with pytest.raises(QueueRequestError) as err:
        client.record_fix_rounds_baseline(job_id, "many")  # type: ignore[arg-type]
    assert err.value.status == 400


# --- pauses --------------------------------------------------------------------


def test_dispatch_pauses_are_raw_so_a_resume_can_be_told_from_never_paused(
    client: QueueClient, clock: FakeClock
) -> None:
    until = T0 + timedelta(minutes=10)
    assert client.pause_dispatch(until=until, reason="rate limited", pool="claude-shared", now=clock())
    assert [p.pool for p in client.dispatch_pauses()] == ["claude-shared"]
    clock.advance(11 * 60)
    # Elapsed but still recorded: the scheduler announces the resume off this row.
    (pause,) = client.dispatch_pauses()
    assert pause.pool == "claude-shared" and not pause.is_active(clock())
    assert client.dispatch_pause("claude-shared") is not None
    assert client.dispatch_pause("claude-m3") is None
    client.clear_pause("claude-shared")
    assert client.dispatch_pauses() == []


def test_mark_pause_probed_stamps_the_pool(client: QueueClient, clock: FakeClock) -> None:
    client.pause_dispatch(until=T0 + timedelta(hours=1), pool="claude-shared", now=clock())
    client.pause_dispatch(until=T0 + timedelta(hours=1), now=clock())
    clock.advance(30)
    client.mark_pause_probed("claude-shared", now=clock())
    by_pool = {p.pool: p for p in client.dispatch_pauses()}
    assert by_pool["claude-shared"].probed_at == clock().isoformat()
    assert by_pool[None].probed_at is None


def test_mark_pause_probed_for_a_missing_pause_is_a_no_op(client: QueueClient) -> None:
    client.mark_pause_probed("nobody")
    assert client.dispatch_pauses() == []


# --- the run registry ----------------------------------------------------------


def test_put_fleet_run_accepts_the_stores_now_keyword(client: QueueClient, store: QueueStore) -> None:
    record = RunRecord(
        run_id="r1", repo="/r", db="/r/.db", scope="s", pid=1, status="IN_PROGRESS",
        started_at=T0.isoformat(), worker="m3max",
    )
    client.put_fleet_run(record, now=T0)
    assert [row["run_id"] for row in store.list_fleet_runs()] == ["r1"]


# --- transport failures surface as QueueUnavailable ------------------------------


def test_every_new_verb_names_the_url_when_the_service_is_gone(tmp_path: Path) -> None:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{sock.getsockname()[1]}"
    gone = QueueClient(dead, token=TOKEN)
    for call in (
        lambda: gone.peek_claimable(),
        lambda: gone.claim_job(1, claimed_by="w", lease_seconds=9),
        lambda: gone.expired_running_jobs(),
        lambda: gone.running_repos(),
        lambda: gone.get_worker("w"),
        lambda: gone.set_reason(1, None),
    ):
        with pytest.raises(QueueUnavailable, match="127.0.0.1"):
            call()
