# ABOUTME: Tests for rate-limit pauses scoped to one subscription pool (Story 35.2-003).
# ABOUTME: Isolation by pool, pool-wide clear, unpause by pool, and the local single-pause mode.

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sdlc.queue import CODEX_POOL, QueueStore

T0 = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
SHARED = "claude-shared"
M3 = "claude-m3"


def _later(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def store(tmp_path) -> QueueStore:
    s = QueueStore(tmp_path / "queue.db")
    s.init()
    return s


def _register(store: QueueStore, name: str, pools: list[str], harnesses=("claude",)):
    return store.register_worker(
        name,
        host=name,
        pools=pools,
        harnesses=list(harnesses),
        repos=["r"],
        slots=2,
        slots_free=2,
        now=T0,
    )


def _job(store: QueueStore, *, pool: str | None = None, harness: str | None = None) -> int:
    return store.add_job(
        repo="/r/r",
        kind="build",
        scope="epic-1",
        pool=pool,
        requirements_json=json.dumps({"harness": harness}) if harness else None,
    )


def _pause(store: QueueStore, pool: str | None, *, seconds: float = 3600, **kwargs) -> bool:
    return store.pause_dispatch(
        until=_later(seconds), reason="rate limited", pool=pool, now=T0, **kwargs
    )


# --- AC1: a pool's pause holds only that pool ---------------------------------


def test_the_pause_carries_its_pool(store) -> None:
    assert _pause(store, SHARED) is True
    pause = store.dispatch_pause(SHARED)
    assert pause is not None and pause.pool == SHARED
    assert pause.to_dict()["pool"] == SHARED


def test_a_paused_pool_yields_nothing_while_another_pool_keeps_claiming(store) -> None:
    _register(store, "xps", [SHARED])
    _register(store, "m3max", [M3])
    shared_job = _job(store, pool=SHARED)
    m3_job = _job(store, pool=M3)
    _pause(store, SHARED)

    assert store.claim_next(claimed_by="xps", lease_seconds=60, now=_later(60)) is None
    claimed = store.claim_next(claimed_by="m3max", lease_seconds=60, now=_later(60))
    assert claimed is not None and claimed.id == m3_job
    assert store.get_job(shared_job).state == "queued"


def test_an_unpooled_job_is_held_by_the_pool_of_the_worker_that_would_run_it(store) -> None:
    _register(store, "xps", [SHARED])
    _register(store, "m3max", [M3])
    job_id = _job(store)  # no pool pin: the worker's declared Claude pool applies
    _pause(store, SHARED)

    assert store.claim_next(claimed_by="xps", lease_seconds=60, now=_later(60)) is None
    claimed = store.claim_next(claimed_by="m3max", lease_seconds=60, now=_later(60))
    assert claimed is not None and claimed.id == job_id


def test_a_codex_stage_is_held_by_the_codex_pool_not_the_claude_pool(store) -> None:
    _register(store, "m3max", [M3, CODEX_POOL], harnesses=("claude", "codex"))
    codex_job = _job(store, harness="codex")
    _pause(store, CODEX_POOL)

    # The claude pool is fine, but this job runs on Codex: held.
    assert store.claim_next(claimed_by="m3max", lease_seconds=60, now=_later(60)) is None
    # A pure-claude job on the same worker still flows.
    claude_job = _job(store, harness="claude")
    claimed = store.claim_next(claimed_by="m3max", lease_seconds=60, now=_later(60))
    assert claimed is not None and claimed.id == claude_job
    assert store.get_job(codex_job).state == "queued"


def test_a_pool_claims_again_once_paused_until_passes(store) -> None:
    _register(store, "xps", [SHARED])
    job_id = _job(store, pool=SHARED)
    _pause(store, SHARED, seconds=100)

    assert store.claim_next(claimed_by="xps", lease_seconds=60, now=_later(99)) is None
    claimed = store.claim_next(claimed_by="xps", lease_seconds=60, now=_later(101))
    assert claimed is not None and claimed.id == job_id


def test_an_unregistered_claimer_is_held_by_the_pools_it_declares(store) -> None:
    job_id = _job(store, pool=SHARED)
    _pause(store, SHARED)
    assert (
        store.claim_next(claimed_by="adhoc", lease_seconds=60, pools=[SHARED], now=_later(1))
        is None
    )
    _pause(store, M3)
    store.clear_pause(SHARED)
    claimed = store.claim_next(
        claimed_by="adhoc", lease_seconds=60, pools=[SHARED], now=_later(1)
    )
    assert claimed is not None and claimed.id == job_id


def test_pauses_are_independent_rows_that_extend_but_never_shorten(store) -> None:
    assert _pause(store, SHARED, seconds=100) is True
    assert _pause(store, M3, seconds=100) is True  # another pool: its own window
    assert _pause(store, SHARED, seconds=500) is False  # same pool: extended
    assert _pause(store, SHARED, seconds=50) is False  # never shortened
    pauses = {p.pool: p for p in store.dispatch_pauses()}
    assert set(pauses) == {SHARED, M3}
    assert pauses[SHARED].paused_until == _later(500).isoformat()
    assert pauses[M3].paused_until == _later(100).isoformat()


# --- AC2: one probe clears the pool for every worker ---------------------------


def test_clearing_a_pool_releases_every_worker_of_that_pool(store) -> None:
    _register(store, "xps", [SHARED])
    _register(store, "nuc", [SHARED])
    _job(store, pool=SHARED)
    _pause(store, SHARED)
    assert store.claim_next(claimed_by="xps", lease_seconds=60, now=_later(60)) is None
    assert store.claim_next(claimed_by="nuc", lease_seconds=60, now=_later(60)) is None

    store.clear_pause(SHARED)  # what the probe-success path does

    assert store.dispatch_pause(SHARED) is None
    assert store.claim_next(claimed_by="nuc", lease_seconds=60, now=_later(60)) is not None


def test_probe_stamp_is_per_pool(store) -> None:
    _pause(store, SHARED)
    _pause(store, M3)
    store.mark_pause_probed(SHARED, now=_later(30))
    assert store.dispatch_pause(SHARED).probed_at == _later(30).isoformat()
    assert store.dispatch_pause(M3).probed_at is None


# --- AC3: unpause by pool ------------------------------------------------------


def test_clearing_one_pool_leaves_the_others(store) -> None:
    _pause(store, SHARED)
    _pause(store, M3)
    store.clear_pause(SHARED)
    assert [p.pool for p in store.dispatch_pauses()] == [M3]


def test_clearing_without_a_pool_clears_every_pause(store) -> None:
    _pause(store, SHARED)
    _pause(store, M3)
    _pause(store, None)
    store.clear_pause()
    assert store.dispatch_pauses() == []


# --- local mode: the degenerate single pause is unchanged ----------------------


def test_a_poolless_pause_holds_every_claim_as_before(store) -> None:
    _register(store, "m3max", [M3])
    _job(store, pool=M3)
    assert _pause(store, None) is True
    pause = store.dispatch_pause()
    assert pause is not None and pause.pool is None
    assert store.claim_next(claimed_by="m3max", lease_seconds=60, now=_later(60)) is None
    store.clear_pause()
    assert store.dispatch_pause() is None
    assert store.claim_next(claimed_by="m3max", lease_seconds=60, now=_later(60)) is not None


def test_dispatch_pause_without_a_pool_reads_only_the_poolless_pause(store) -> None:
    _pause(store, SHARED)
    assert store.dispatch_pause() is None
    assert store.dispatch_pause(SHARED) is not None


# --- migration: a queue.db written before pools keeps its pause ----------------


def test_a_legacy_single_pause_row_survives_the_migration(tmp_path) -> None:
    path = tmp_path / "queue.db"
    legacy = QueueStore(path)
    legacy.init()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE queue_pauses")
        conn.execute("DELETE FROM _migrations WHERE version >= 8")
        conn.execute(
            "INSERT INTO queue_state(id, paused_until, reason, run_id, repo, source, "
            "paused_at) VALUES (1, ?, 'old', 'run-1', '/r', 'reset-epoch', ?)",
            (_later(900).isoformat(), T0.isoformat()),
        )
    migrated = QueueStore(path)
    migrated.ensure_migrated()
    pause = migrated.dispatch_pause()
    assert pause is not None and pause.pool is None
    assert pause.reason == "old" and pause.run_id == "run-1"
    assert pause.paused_until == _later(900).isoformat()
