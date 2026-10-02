# ABOUTME: Tests for worker registration and capability-matched claims (Story 35.2-001).
# ABOUTME: Registration, heartbeat expiry, eligibility, least-loaded tie-break, reasons.

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sdlc.queue import (
    HEARTBEAT_SECONDS,
    OFFLINE_AFTER_SECONDS,
    UNSATISFIABLE_REASON_PREFIX,
    QueueError,
    QueueStore,
)

T0 = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)


def _later(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def store(tmp_path) -> QueueStore:
    s = QueueStore(tmp_path / "queue.db")
    s.init()
    return s


def _register(store: QueueStore, name: str, **overrides):
    caps = {
        "host": name,
        "pools": ["claude-m3"],
        "harnesses": ["claude"],
        "sandbox": None,
        "repos": ["agentic-coding-monitor"],
        "slots": 2,
        "slots_free": 2,
        "now": T0,
    }
    caps.update(overrides)
    return store.register_worker(name, **caps)


def _job(store: QueueStore, **requirements_and_pins) -> int:
    pins = {k: requirements_and_pins.pop(k) for k in ("host", "pool") if k in requirements_and_pins}
    return store.add_job(
        repo="/r/agentic-coding-monitor",
        kind="build",
        scope="epic-1",
        requirements_json=json.dumps(requirements_and_pins) if requirements_and_pins else None,
        **pins,
    )


# --- registration + heartbeat --------------------------------------------------


def test_register_records_the_advertised_capabilities(store) -> None:
    record = _register(
        store,
        "m3max",
        host="macbook-pro-m3-max",
        pools=["claude-m3", "codex-shared"],
        harnesses=["claude", "codex"],
        sandbox="podman",
        repos=["a", "b"],
    )

    assert record.name == "m3max"
    assert record.host == "macbook-pro-m3-max"
    assert record.pools == ["claude-m3", "codex-shared"]
    assert record.harnesses == ["claude", "codex"]
    assert record.sandbox == "podman"
    assert record.repos == ["a", "b"]
    assert (record.slots, record.slots_free) == (2, 2)
    assert store.get_worker("m3max") == record


def test_a_heartbeat_refreshes_in_place_and_keeps_the_first_registration_time(store) -> None:
    _register(store, "m3max", now=T0)
    beat = _register(store, "m3max", slots_free=0, repos=["a", "new-clone"], now=_later(30))

    assert [w.name for w in store.list_workers()] == ["m3max"]
    assert beat.registered_at == T0.isoformat()
    assert beat.last_heartbeat == _later(30).isoformat()
    assert beat.slots_free == 0
    assert beat.repos == ["a", "new-clone"]


def test_a_worker_is_online_until_it_misses_three_heartbeats(store) -> None:
    worker = _register(store, "m3max", now=T0)

    assert OFFLINE_AFTER_SECONDS == 3 * HEARTBEAT_SECONDS == 90
    assert worker.is_online(_later(HEARTBEAT_SECONDS))
    assert worker.is_online(_later(OFFLINE_AFTER_SECONDS))
    assert not worker.is_online(_later(OFFLINE_AFTER_SECONDS + 1))


def test_slots_free_is_clamped_to_the_cap_and_defaults_to_it(store) -> None:
    assert _register(store, "a", slots=3, slots_free=None).slots_free == 3
    assert _register(store, "b", slots=2, slots_free=9).slots_free == 2


@pytest.mark.parametrize(
    "overrides",
    [{"slots": 0}, {"slots": -1}, {"host": ""}],
    ids=["zero-slots", "negative-slots", "blank-host"],
)
def test_register_refuses_unusable_input(store, overrides) -> None:
    with pytest.raises(QueueError):
        _register(store, "m3max", **overrides)


def test_register_refuses_a_blank_name(store) -> None:
    with pytest.raises(QueueError):
        _register(store, "  ")


def test_a_queue_db_from_before_workers_lists_none_without_a_table(tmp_path) -> None:
    path = tmp_path / "old.db"
    sqlite3.connect(path).close()
    assert QueueStore(path).list_workers() == []


# --- silence: offline + reclaimable leases --------------------------------------


def test_an_offline_worker_leases_become_reclaimable(store) -> None:
    _register(store, "m3max", now=T0)
    job_id = _job(store)
    claimed = store.claim_next(claimed_by="m3max", lease_seconds=3600, now=T0)
    assert claimed is not None and claimed.id == job_id

    # Another worker's claim is the moment the service notices the silence.
    _register(store, "home-lab", now=_later(OFFLINE_AFTER_SECONDS + 30))
    store.claim_next(claimed_by="home-lab", lease_seconds=90, now=_later(OFFLINE_AFTER_SECONDS + 30))

    expired = store.expired_running_jobs(now=_later(OFFLINE_AFTER_SECONDS + 31))
    assert [j.id for j in expired] == [job_id]


def test_a_live_workers_lease_is_left_alone(store) -> None:
    _register(store, "m3max", now=T0)
    job_id = _job(store)
    store.claim_next(claimed_by="m3max", lease_seconds=3600, now=T0)

    _register(store, "m3max", now=_later(60))  # heartbeat
    _register(store, "home-lab", now=_later(100))
    store.claim_next(claimed_by="home-lab", lease_seconds=90, now=_later(100))

    assert store.expired_running_jobs(now=_later(101)) == []
    assert store.get_job(job_id).state == "running"


def test_registering_also_releases_leases_of_silent_peers(store) -> None:
    _register(store, "m3max", now=T0)
    job_id = _job(store)
    store.claim_next(claimed_by="m3max", lease_seconds=3600, now=T0)

    _register(store, "home-lab", now=_later(200))

    assert [j.id for j in store.expired_running_jobs(now=_later(201))] == [job_id]


# --- eligibility ----------------------------------------------------------------


def test_a_job_goes_only_to_a_worker_that_has_the_repo(store) -> None:
    _register(store, "no-clone", repos=["something-else"])
    job_id = _job(store, repo="agentic-coding-monitor", harness="claude")

    assert store.claim_next(claimed_by="no-clone", lease_seconds=90, now=T0) is None

    _register(store, "has-clone", repos=["agentic-coding-monitor"])
    claimed = store.claim_next(claimed_by="has-clone", lease_seconds=90, now=T0)
    assert claimed is not None and claimed.id == job_id
    assert claimed.worker == "has-clone"


def test_a_job_goes_only_to_a_worker_with_the_harness(store) -> None:
    _register(store, "claude-only", harnesses=["claude"])
    _job(store, harness="codex")
    assert store.claim_next(claimed_by="claude-only", lease_seconds=90, now=T0) is None


def test_a_codex_job_needs_the_codex_shared_pool_as_well(store) -> None:
    _register(store, "codex-no-pool", harnesses=["claude", "codex"], pools=["claude-m3"])
    job_id = _job(store, harness="codex")
    assert store.claim_next(claimed_by="codex-no-pool", lease_seconds=90, now=T0) is None

    _register(store, "codex-pooled", harnesses=["claude", "codex"], pools=["codex-shared"])
    claimed = store.claim_next(claimed_by="codex-pooled", lease_seconds=90, now=T0)
    assert claimed is not None and claimed.id == job_id


def test_a_harness_map_naming_codex_for_one_stage_needs_the_pool(store) -> None:
    """`claude,codex` — a map that routes one stage to codex — still needs both."""
    _register(store, "pooled", harnesses=["claude", "codex"], pools=["claude-m3", "codex-shared"])
    _register(store, "claude-box", harnesses=["claude"], pools=["claude-m3"])
    job_id = _job(store, harness="claude,codex")

    assert store.claim_next(claimed_by="claude-box", lease_seconds=90, now=T0) is None
    claimed = store.claim_next(claimed_by="pooled", lease_seconds=90, now=T0)
    assert claimed is not None and claimed.id == job_id


@pytest.mark.parametrize("wanted", ["true", "container", "podman"])
def test_a_sandbox_job_needs_a_worker_with_a_container_runtime(store, wanted) -> None:
    _register(store, "bare", sandbox=None)
    _job(store, sandbox=wanted)
    assert store.claim_next(claimed_by="bare", lease_seconds=90, now=T0) is None

    _register(store, "boxed", sandbox="podman")
    assert store.claim_next(claimed_by="boxed", lease_seconds=90, now=T0) is not None


def test_a_named_sandbox_runtime_must_match(store) -> None:
    _register(store, "docker-box", sandbox="docker")
    _job(store, sandbox="podman")
    assert store.claim_next(claimed_by="docker-box", lease_seconds=90, now=T0) is None


def test_sandbox_false_asks_for_nothing(store) -> None:
    _register(store, "bare", sandbox=None)
    _job(store, sandbox="false")
    assert store.claim_next(claimed_by="bare", lease_seconds=90, now=T0) is not None


def test_a_host_pin_is_claimed_only_by_that_workers_host(store) -> None:
    _register(store, "m3max", host="macbook-pro-m3-max")
    _register(store, "lab", host="home-lab")
    job_id = _job(store, host="home-lab")

    assert store.claim_next(claimed_by="m3max", lease_seconds=90, now=T0) is None
    claimed = store.claim_next(claimed_by="lab", lease_seconds=90, now=T0)
    assert claimed is not None and claimed.id == job_id


def test_a_pool_pin_needs_a_worker_that_declared_the_pool(store) -> None:
    _register(store, "m3max", pools=["claude-m3"])
    _register(store, "shared", pools=["claude-shared"])
    job_id = _job(store, pool="claude-shared")

    assert store.claim_next(claimed_by="m3max", lease_seconds=90, now=T0) is None
    claimed = store.claim_next(claimed_by="shared", lease_seconds=90, now=T0)
    assert claimed is not None and claimed.id == job_id


def test_a_worker_with_no_free_slots_claims_nothing(store) -> None:
    _register(store, "full", slots=2, slots_free=0)
    _job(store)
    assert store.claim_next(claimed_by="full", lease_seconds=90, now=T0) is None


def test_an_unregistered_claimer_keeps_the_pre_registry_rules(store) -> None:
    """Story 35.1-001 behaviour: host/pool only, requirements recorded not matched."""
    job_id = _job(store, repo="never-heard-of-it", harness="codex")
    claimed = store.claim_next(claimed_by="legacy", lease_seconds=90, now=T0)
    assert claimed is not None and claimed.id == job_id


def test_a_claim_still_prefers_the_higher_priority_among_eligible_jobs(store) -> None:
    _register(store, "m3max")
    low = store.add_job(repo="/r/a", kind="build", scope="1", priority="low")
    urgent = store.add_job(repo="/r/b", kind="build", scope="2", priority="urgent")

    first = store.claim_next(claimed_by="m3max", lease_seconds=90, now=T0)
    second = store.claim_next(claimed_by="m3max", lease_seconds=90, now=T0)

    assert (first.id, second.id) == (urgent, low)


# --- least loaded -----------------------------------------------------------------


def test_the_claim_goes_to_the_worker_with_more_free_slots(store) -> None:
    _register(store, "busy", slots=4, slots_free=1)
    _register(store, "idle", slots=4, slots_free=3)
    job_id = _job(store, repo="agentic-coding-monitor", harness="claude")

    assert store.claim_next(claimed_by="busy", lease_seconds=90, now=T0) is None
    claimed = store.claim_next(claimed_by="idle", lease_seconds=90, now=T0)
    assert claimed is not None and claimed.id == job_id


def test_equal_load_does_not_defer_so_either_worker_may_claim(store) -> None:
    _register(store, "a", slots_free=2)
    _register(store, "b", slots_free=2)
    _job(store)

    assert store.claim_next(claimed_by="a", lease_seconds=90, now=T0) is not None


def test_a_worker_is_not_deferred_to_one_that_is_ineligible_or_offline(store) -> None:
    _register(store, "busy", slots=4, slots_free=1)
    _register(store, "idle-wrong-repo", slots=4, slots_free=4, repos=["other"])
    _register(store, "idle-silent", slots=4, slots_free=4, now=T0 - timedelta(hours=1))
    job_id = _job(store, repo="agentic-coding-monitor")

    claimed = store.claim_next(claimed_by="busy", lease_seconds=90, now=T0)
    assert claimed is not None and claimed.id == job_id


# --- unsatisfiable jobs ------------------------------------------------------------


def test_an_unsatisfiable_job_stays_queued_with_the_missing_needs_named(store) -> None:
    _register(store, "m3max", repos=["other"], sandbox=None)
    job_id = _job(store, repo="agentic-coding-monitor", harness="claude", sandbox="true")

    assert store.claim_next(claimed_by="m3max", lease_seconds=90, now=T0) is None

    job = store.get_job(job_id)
    assert job.state == "queued"
    assert job.reason == "no eligible worker (needs repo agentic-coding-monitor, sandbox)"
    assert job.reason.startswith(UNSATISFIABLE_REASON_PREFIX)


def test_the_reason_names_the_host_pin_and_pool_too(store) -> None:
    _register(store, "m3max", host="m3", pools=["claude-m3"])
    job_id = _job(store, host="home-lab", pool="claude-shared")

    store.stamp_unsatisfiable(now=T0)

    assert store.get_job(job_id).reason == (
        "no eligible worker (needs host home-lab, pool claude-shared)"
    )


def test_when_each_need_is_met_somewhere_but_not_together_all_are_listed(store) -> None:
    _register(store, "has-repo", repos=["agentic-coding-monitor"], sandbox=None)
    _register(store, "has-sandbox", repos=["other"], sandbox="podman")
    job_id = _job(store, repo="agentic-coding-monitor", sandbox="true")

    store.stamp_unsatisfiable(now=T0)

    assert store.get_job(job_id).reason == (
        "no eligible worker (needs repo agentic-coding-monitor, sandbox)"
    )


def test_a_job_with_no_online_worker_is_unsatisfiable_too(store) -> None:
    _register(store, "asleep", now=T0 - timedelta(hours=1))
    job_id = _job(store, repo="agentic-coding-monitor")

    store.stamp_unsatisfiable(now=T0)

    assert store.get_job(job_id).reason.startswith(UNSATISFIABLE_REASON_PREFIX)


def test_the_reason_clears_once_a_capable_worker_registers(store) -> None:
    _register(store, "m3max", repos=["other"])
    job_id = _job(store, repo="agentic-coding-monitor")
    store.stamp_unsatisfiable(now=T0)
    assert store.get_job(job_id).reason is not None

    _register(store, "lab", repos=["agentic-coding-monitor"], now=_later(5))

    assert store.get_job(job_id).reason is None


def test_a_job_without_needs_is_never_stamped(store) -> None:
    _register(store, "asleep", now=T0 - timedelta(hours=1))
    job_id = _job(store)

    store.stamp_unsatisfiable(now=T0)

    assert store.get_job(job_id).reason is None


def test_no_workers_at_all_means_local_mode_and_no_reason(store) -> None:
    job_id = _job(store, repo="agentic-coding-monitor")

    store.stamp_unsatisfiable(now=T0)

    assert store.get_job(job_id).reason is None


def test_an_unrelated_reason_survives_a_satisfiable_stamp(store) -> None:
    _register(store, "m3max")
    job_id = _job(store, repo="agentic-coding-monitor")
    store.set_reason(job_id, "repo busy")

    store.stamp_unsatisfiable(now=T0)

    assert store.get_job(job_id).reason == "repo busy"


# --- the scheduler's view --------------------------------------------------------------


def test_claimable_for_worker_filters_candidates_and_uses_the_live_slot_count(store) -> None:
    _register(store, "m3max", slots=2, slots_free=0)  # last heartbeat said full
    mine = _job(store, repo="agentic-coding-monitor")
    _job(store, repo="not-cloned-here")
    candidates = store.peek_claimable(now=T0)

    assert store.claimable_for_worker("m3max", candidates, now=T0) == []
    kept = store.claimable_for_worker("m3max", candidates, slots_free=1, now=T0)
    assert [j.id for j in kept] == [mine]


# --- helper edge cases (coverage gate) --------------------------------------


def test_is_online_false_on_garbled_heartbeat() -> None:
    from sdlc.queue import WorkerRecord

    rec = WorkerRecord(name="w", host="h", registered_at="x", last_heartbeat="not-a-date")
    assert rec.is_online(T0) is False


def test_is_online_treats_naive_heartbeat_as_utc() -> None:
    from sdlc.queue import WorkerRecord

    rec = WorkerRecord(name="w", host="h", registered_at="x", last_heartbeat="2026-10-02T12:00:00")
    assert rec.is_online(_later(1)) is True


@pytest.mark.parametrize("raw", ["not json", "[1, 2]"])
def test_job_requirements_ignores_malformed_payload(raw: str) -> None:
    from sdlc.queue import JobRecord, _job_requirements

    job = JobRecord(**{**_minimal_job_kwargs(), "requirements": raw})
    assert _job_requirements(job) == {}


def test_job_requirements_drops_null_values() -> None:
    from sdlc.queue import JobRecord, _job_requirements

    job = JobRecord(
        **{**_minimal_job_kwargs(), "requirements": json.dumps({"repo": "r", "x": None})}
    )
    assert _job_requirements(job) == {"repo": "r"}


def test_pause_clears_empty_without_db(tmp_path) -> None:
    assert QueueStore(tmp_path / "absent.db").pause_clears() == []


def _minimal_job_kwargs() -> dict:
    import dataclasses

    from sdlc.queue import JobRecord

    kwargs = {}
    for f in dataclasses.fields(JobRecord):
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            kwargs[f.name] = 0 if f.type in ("int", int) else "x"
    return kwargs
