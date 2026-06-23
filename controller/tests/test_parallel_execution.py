# ABOUTME: Tests for bounded concurrent cohort execution (Story 17.1-001).
# ABOUTME: A probe dispatcher measures real overlap; the ledger is a real temp DB.

from __future__ import annotations

import threading
import time

import pytest

from sdlc.build import (
    BuildOptions,
    Ledger,
    effective_concurrency,
    parse_build_args,
    run_build,
)
from sdlc.cohort import Story


# ---------------------------------------------------------------------------
# Probe dispatcher: records the maximum number of stories dispatched at once
# ---------------------------------------------------------------------------

def _payload(agent_type: str, story) -> dict:
    sid = getattr(story, "id", "x")
    return {
        "build": {
            "branch_name": f"feature/{sid}",
            "build_status": "SUCCESS",
            "commit_sha": "deadbeef",
        },
        "coverage": {
            "pr_number": 100,
            "pr_url": "https://example/pull/100",
            "coverage_pct": 95.0,
            "tests_added": 3,
            "coverage_status": "PASS",
            "security_status": "PASS",
        },
        "review": {
            "pr_number": 100,
            "approval_status": "APPROVED",
            "change_count": 0,
            "final_status": "APPROVED",
        },
        "merge": {
            "pr_number": 100,
            "merge_status": "MERGED",
            "merge_sha": "cafef00d",
            "merged_at": "2026-06-12T00:00:00Z",
        },
        "bugfix": {
            "failure_category": "TEST_BUG",
            "fix_status": "FIXED",
            "tests_passing": True,
            "bugs_fixed": 0,
            "tests_fixed": 1,
        },
    }[agent_type]


class ConcurrencyProbeDispatcher:
    """Stands in for the real agent dispatch and tracks concurrent stories.

    Because a single story's four stages run sequentially, the peak number of
    *simultaneous* dispatch calls equals the peak number of stories in flight —
    the exact thing the cohort executor is supposed to drive above 1.
    """

    def __init__(self, hold: float = 0.05, overrides=None) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.hold = hold
        self.overrides = overrides or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        from sdlc.dispatch import AgentResult

        sid = getattr(story, "id", "")
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append((agent_type, sid))
        try:
            time.sleep(self.hold)
            key = (agent_type, sid)
            if key in self.overrides:
                payload = self.overrides[key]
                if callable(payload):
                    payload = payload()
            else:
                payload = _payload(agent_type, story)
            return AgentResult(agent_type=agent_type, data=payload, raw="")
        finally:
            with self._lock:
                self.active -= 1


def _independent(n: int) -> list[Story]:
    """A single cohort of ``n`` dependency-free stories."""
    return [
        Story(f"p{i}-001", f"Story {i}", "99", "sample", "epic-99.md", "P1", 2, "py", [])
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Argument parsing + effective-concurrency resolution
# ---------------------------------------------------------------------------

def test_parse_concurrency_flag() -> None:
    opts = parse_build_args(["epic-99", "--concurrency=3"])
    assert opts.concurrency == 3


def test_parse_concurrency_default_is_five() -> None:
    assert parse_build_args([]).concurrency == 5


def test_parse_concurrency_rejects_below_one() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        parse_build_args(["--concurrency=0"])


def test_effective_concurrency_sequential_is_one() -> None:
    assert effective_concurrency(BuildOptions(sequential=True, concurrency=5)) == 1


def test_effective_concurrency_honours_flag() -> None:
    assert effective_concurrency(BuildOptions(concurrency=4)) == 4


def test_effective_concurrency_floors_at_one() -> None:
    assert effective_concurrency(BuildOptions(concurrency=0)) == 1


# ---------------------------------------------------------------------------
# AC1/AC2: a parallel cohort runs >1 story at once, bounded by the cap
# ---------------------------------------------------------------------------

def test_parallel_cohort_runs_at_least_two_concurrently(tmp_path) -> None:
    dispatcher = ConcurrencyProbeDispatcher()
    opts = BuildOptions(scope="epic-99", skip_preflight=True, concurrency=5)
    result = run_build(
        opts,
        queue=_independent(3),
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert result.completed == 3
    assert result.failed == 0
    assert dispatcher.max_active >= 2  # genuine overlap, not the serial path


def test_parallel_cohort_respects_concurrency_cap(tmp_path) -> None:
    dispatcher = ConcurrencyProbeDispatcher()
    opts = BuildOptions(scope="epic-99", skip_preflight=True, concurrency=2)
    run_build(
        opts,
        queue=_independent(4),
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert dispatcher.max_active <= 2  # never more than the cap at once
    assert dispatcher.max_active >= 2  # but it does reach the cap


# ---------------------------------------------------------------------------
# AC3: --sequential / --concurrency=1 reproduce the serial path (no overlap)
# ---------------------------------------------------------------------------

def test_sequential_runs_one_at_a_time(tmp_path) -> None:
    dispatcher = ConcurrencyProbeDispatcher()
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    run_build(
        opts,
        queue=_independent(3),
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert dispatcher.max_active == 1  # strictly serial — byte-for-byte today


def test_concurrency_one_runs_one_at_a_time(tmp_path) -> None:
    dispatcher = ConcurrencyProbeDispatcher()
    opts = BuildOptions(scope="epic-99", skip_preflight=True, concurrency=1)
    run_build(
        opts,
        queue=_independent(3),
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert dispatcher.max_active == 1


# ---------------------------------------------------------------------------
# AC4: dependency-blocking preserved + failure isolation under concurrency
# ---------------------------------------------------------------------------

def test_parallel_blocks_dependents_of_a_failed_story(tmp_path) -> None:
    def fail_build():
        return {
            "branch_name": "feature/p0-001",
            "build_status": "FAILED",
            "commit_sha": "0",
            "error_summary": "boom",
        }

    queue = [
        Story("p0-001", "Root", "99", "sample", "epic-99.md", "P1", 2, "py", []),
        Story("p0-002", "Dependent", "99", "sample", "epic-99.md", "P1", 2, "py", ["p0-001"]),
    ]
    dispatcher = ConcurrencyProbeDispatcher(
        overrides={("build", "p0-001"): fail_build}
    )
    opts = BuildOptions(scope="epic-99", skip_preflight=True, auto=True, concurrency=5)
    result = run_build(
        opts,
        queue=queue,
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert result.story_status["p0-001"] == "FAILED"
    assert result.story_status["p0-002"] == "BLOCKED"


def test_parallel_failure_isolation_other_workers_finish(tmp_path) -> None:
    """A worker that raises mid-flight is recorded FAILED; peers still finish."""
    boom = ConcurrencyProbeDispatcher()

    def explode(agent_type, prompt, story=None, **kwargs):
        if getattr(story, "id", "") == "p1-001" and agent_type == "build":
            raise RuntimeError("worker exploded")
        return ConcurrencyProbeDispatcher.__call__(boom, agent_type, prompt, story, **kwargs)

    queue = _independent(3)
    opts = BuildOptions(scope="epic-99", skip_preflight=True, concurrency=5)
    result = run_build(
        opts,
        queue=queue,
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=explode,
        preflight=lambda: True,
    )
    assert result.story_status["p1-001"] == "FAILED"
    # The other two are unaffected and reach DONE.
    assert result.story_status["p0-001"] == "DONE"
    assert result.story_status["p2-001"] == "DONE"


# ---------------------------------------------------------------------------
# Outcome aggregation: a fully-green parallel run marks every story DONE
# ---------------------------------------------------------------------------

def test_parallel_aggregates_all_done(tmp_path) -> None:
    dispatcher = ConcurrencyProbeDispatcher()
    opts = BuildOptions(scope="epic-99", skip_preflight=True, concurrency=5)
    result = run_build(
        opts,
        queue=_independent(4),
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert result.completed == 4
    assert all(v == "DONE" for v in result.story_status.values())


# ---------------------------------------------------------------------------
# AC5: resume honours the same concurrency semantics as build
# ---------------------------------------------------------------------------

_PARALLEL_EPIC = """# Epic 88

##### Story 88.1-001: One
**Priority**: P1
**Points**: 1
**Dependencies**: None.

##### Story 88.1-002: Two
**Priority**: P1
**Points**: 1
**Dependencies**: None.
"""


def _make_parallel_project(tmp_path):
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-88-sample.md").write_text(_PARALLEL_EPIC, encoding="utf-8")
    return tmp_path


def _seed_parallel_interrupted(db_path, concurrency: int = 5) -> str:
    """A parallel run crashed before either independent story built."""
    import json

    ledger = Ledger(db_path)
    ledger.init()
    rid = ledger.run_create("epic-88", "parallel")
    ledger.set_total(rid, 2)
    ledger.event_log(rid, "", "info", "controller", "run started: scope=epic-88 mode=parallel")
    ledger.event_log(
        rid, "", "info", "config",
        json.dumps({"skip_coverage": True, "coverage_threshold": 90, "concurrency": concurrency}),
    )
    ledger.story_upsert(rid, "88.1-001", "88", "One", "P1", 1, "general-purpose", "", None, "TODO")
    ledger.story_upsert(rid, "88.1-002", "88", "Two", "P1", 1, "general-purpose", "", None, "TODO")
    return rid


def test_resume_options_carry_concurrency(tmp_path) -> None:
    from sdlc.resume import _options_from_config

    opts = _options_from_config(
        "epic-88", {"mode": "parallel"}, {"concurrency": 3},
    )
    assert opts.concurrency == 3


def test_resume_runs_cohort_concurrently(tmp_path) -> None:
    from sdlc.resume import run_resume

    _make_parallel_project(tmp_path)
    db = tmp_path / "ledger.db"
    _seed_parallel_interrupted(db, concurrency=5)
    dispatcher = ConcurrencyProbeDispatcher()
    result = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
    )
    assert result.completed == 2
    assert dispatcher.max_active >= 2  # resume fans out like build


def test_resume_sequential_runs_one_at_a_time(tmp_path) -> None:
    from sdlc.resume import run_resume

    _make_parallel_project(tmp_path)
    db = tmp_path / "ledger.db"
    # mode=serial seed → resume must stay strictly one-at-a-time.
    ledger = Ledger(db)
    ledger.init()
    rid = ledger.run_create("epic-88", "serial")
    ledger.set_total(rid, 2)
    import json
    ledger.event_log(
        rid, "", "info", "config", json.dumps({"skip_coverage": True}),
    )
    ledger.story_upsert(rid, "88.1-001", "88", "One", "P1", 1, "general-purpose", "", None, "TODO")
    ledger.story_upsert(rid, "88.1-002", "88", "Two", "P1", 1, "general-purpose", "", None, "TODO")
    dispatcher = ConcurrencyProbeDispatcher()
    run_resume("epic-88", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path)
    assert dispatcher.max_active == 1


# ---------------------------------------------------------------------------
# Cohort-boundary pause signals under concurrency (build path)
# ---------------------------------------------------------------------------

def test_parallel_cost_gate_pauses_run_resumably(tmp_path) -> None:
    """An interactive over-threshold estimate gates every ready story *before*
    dispatch; the cohort executor captures the pause and the run halts resumably
    (NEEDS_ATTENTION stories, no agent ever runs)."""
    dispatcher = ConcurrencyProbeDispatcher()
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, concurrency=5,
        cost_estimate_threshold=1, auto=False,
    )
    result = run_build(
        opts,
        queue=_independent(2),
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert result.cost_gated is True
    assert dispatcher.calls == []  # the gate halts before any agent dispatch
    assert result.story_status["p0-001"] == "NEEDS_ATTENTION"


def test_parallel_rate_limit_park_hands_off_resumable(tmp_path) -> None:
    """A story whose window reset is beyond the auto-wait cap parks the run
    RATE_LIMITED after the cohort barrier — while its peer still runs to DONE in
    the pool (cohort-barrier failure-free isolation)."""
    from sdlc.rate_limit import RateLimitSignal
    from test_build import _sample_queue
    from test_rate_limit_gate import RateLimitingDispatcher, _Sleeps

    db = tmp_path / "ledger.db"
    dispatcher = RateLimitingDispatcher(
        trip_on=("build", "s1-001"),
        signal=RateLimitSignal(source="usage-limit", reset_at=999_999.0),
        times=99,  # keep throttling s1-001 — it must park, not spin
    )
    result = run_build(
        BuildOptions(
            scope="epic-99", skip_preflight=True, concurrency=5,
            rate_limit_max_wait_s=300, window_s=18000,
        ),
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
        sleep_fn=_Sleeps(),
        clock=lambda: 0.0,
    )
    assert result.rate_limited is True
    ledger = Ledger(db)
    statuses = {r["story_id"]: r["status"] for r in ledger.story_rows(result.run_id)}
    assert statuses["s1-001"] == "RATE_LIMITED"  # parked, resumable
    assert statuses["s1-002"] == "DONE"  # peer finished in the pool


def _boom_story_failed(event, **kwargs):
    """A notifier that fails only on the terminal story_failed event."""
    if event == "story_failed":
        raise RuntimeError("notifier down")


def test_parallel_worker_error_notify_failure_is_swallowed(tmp_path, monkeypatch) -> None:
    """Failure isolation records the raising worker FAILED and fires story_failed;
    a notifier that itself raises is swallowed so peers still complete."""
    import sdlc.build as build_mod

    monkeypatch.setattr(build_mod, "notify", _boom_story_failed)

    probe = ConcurrencyProbeDispatcher()

    def explode(agent_type, prompt, story=None, **kwargs):
        if getattr(story, "id", "") == "p1-001" and agent_type == "build":
            raise RuntimeError("worker exploded")
        return ConcurrencyProbeDispatcher.__call__(probe, agent_type, prompt, story, **kwargs)

    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, concurrency=5),
        queue=_independent(3),
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=explode,
        preflight=lambda: True,
    )
    assert result.story_status["p1-001"] == "FAILED"  # captured + notify swallowed
    assert result.story_status["p0-001"] == "DONE"
    assert result.story_status["p2-001"] == "DONE"


def test_serial_failed_story_notify_failure_is_swallowed(tmp_path, monkeypatch) -> None:
    """In the serial path a terminal FAILED story fires story_failed; a raising
    notifier is swallowed so the run still closes out cleanly."""
    import sdlc.build as build_mod

    monkeypatch.setattr(build_mod, "notify", _boom_story_failed)

    def fail_build():
        return {
            "branch_name": "feature/p0-001",
            "build_status": "FAILED",
            "commit_sha": "0",
            "error_summary": "boom",
        }

    dispatcher = ConcurrencyProbeDispatcher(
        overrides={("build", "p0-001"): fail_build}
    )
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True, auto=True),
        queue=_independent(1),
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert result.story_status["p0-001"] == "FAILED"


# ---------------------------------------------------------------------------
# Resume mirrors the same cohort-boundary pause + isolation semantics
# ---------------------------------------------------------------------------

def _seed_parallel_cost_gated(db_path) -> str:
    """A parallel run interrupted before build, with the interactive cost gate on."""
    import json

    ledger = Ledger(db_path)
    ledger.init()
    rid = ledger.run_create("epic-88", "parallel")
    ledger.set_total(rid, 2)
    ledger.event_log(rid, "", "info", "controller", "run started: scope=epic-88 mode=parallel")
    ledger.event_log(
        rid, "", "info", "config",
        json.dumps({
            "skip_coverage": True,
            "coverage_threshold": 90,
            "concurrency": 5,
            "cost_estimate_threshold": 1,
            "auto": False,
        }),
    )
    ledger.story_upsert(rid, "88.1-001", "88", "One", "P1", 1, "general-purpose", "", None, "TODO")
    ledger.story_upsert(rid, "88.1-002", "88", "Two", "P1", 1, "general-purpose", "", None, "TODO")
    return rid


def test_resume_concurrency_override_fans_out(tmp_path) -> None:
    """`sdlc resume --concurrency=N` overrides the persisted worker cap."""
    from sdlc.resume import run_resume

    _make_parallel_project(tmp_path)
    db = tmp_path / "ledger.db"
    _seed_parallel_interrupted(db, concurrency=1)  # persisted serial cap…
    dispatcher = ConcurrencyProbeDispatcher()
    result = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
        concurrency=3,  # …overridden wider for this resume
    )
    assert result.completed == 2
    assert dispatcher.max_active >= 2  # the override took effect (was serial)


def test_resume_parallel_cost_gate_pauses_run(tmp_path) -> None:
    """A resumed parallel cohort honours the persisted interactive cost gate."""
    from sdlc.resume import run_resume

    _make_parallel_project(tmp_path)
    db = tmp_path / "ledger.db"
    _seed_parallel_cost_gated(db)
    dispatcher = ConcurrencyProbeDispatcher()
    result = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
    )
    assert result.cost_gated is True
    assert dispatcher.calls == []  # gate halts pre-dispatch under concurrency too
    statuses = {r["story_id"]: r["status"] for r in Ledger(db).story_rows(result.run_id)}
    assert statuses["88.1-001"] == "NEEDS_ATTENTION"


def test_resume_parallel_failure_isolation(tmp_path) -> None:
    """A worker that raises mid-resume is recorded FAILED; its peer still finishes."""
    from sdlc.resume import run_resume

    _make_parallel_project(tmp_path)
    db = tmp_path / "ledger.db"
    _seed_parallel_interrupted(db, concurrency=5)
    probe = ConcurrencyProbeDispatcher()

    def explode(agent_type, prompt, story=None, **kwargs):
        if getattr(story, "id", "") == "88.1-001" and agent_type == "build":
            raise RuntimeError("worker exploded on resume")
        return ConcurrencyProbeDispatcher.__call__(probe, agent_type, prompt, story, **kwargs)

    result = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=explode, root=tmp_path,
    )
    statuses = {r["story_id"]: r["status"] for r in Ledger(db).story_rows(result.run_id)}
    assert statuses["88.1-001"] == "FAILED"
    assert statuses["88.1-002"] == "DONE"


def test_resume_parallel_rate_limit_park(tmp_path) -> None:
    """A resumed cohort that re-hits a beyond-cap throttle re-parks RATE_LIMITED."""
    from sdlc.rate_limit import RateLimitSignal
    from sdlc.resume import run_resume
    from test_rate_limit_gate import RateLimitingDispatcher, _Sleeps

    _make_parallel_project(tmp_path)
    db = tmp_path / "ledger.db"
    _seed_parallel_interrupted(db, concurrency=5)
    dispatcher = RateLimitingDispatcher(
        trip_on=("build", "88.1-001"),
        signal=RateLimitSignal(source="usage-limit", reset_at=999_999.0),
        times=99,
    )
    result = run_resume(
        "epic-88", ledger=Ledger(db), dispatcher=dispatcher, root=tmp_path,
        clock=lambda: 0.0, sleep_fn=_Sleeps(),
    )
    statuses = {r["story_id"]: r["status"] for r in Ledger(db).story_rows(result.run_id)}
    assert statuses["88.1-001"] == "RATE_LIMITED"
    assert statuses["88.1-002"] == "DONE"
    assert Ledger(db).run_row(result.run_id)["status"] == "RATE_LIMITED"


# ---------------------------------------------------------------------------
# Story 17.3-001: truthful `mode` + concurrency observability
# ---------------------------------------------------------------------------

def test_authoritative_mode_sequential_is_serial() -> None:
    from sdlc.build import authoritative_mode

    assert authoritative_mode(BuildOptions(sequential=True, concurrency=5)) == "serial"


def test_authoritative_mode_concurrency_one_is_serial() -> None:
    """`--concurrency=1` is byte-for-byte serial, so it must not wear `parallel`."""
    from sdlc.build import authoritative_mode

    assert authoritative_mode(BuildOptions(concurrency=1)) == "serial"


def test_authoritative_mode_multiworker_is_parallel() -> None:
    from sdlc.build import authoritative_mode

    assert authoritative_mode(BuildOptions(concurrency=3)) == "parallel"


def test_run_build_records_serial_mode_for_concurrency_one(tmp_path) -> None:
    """A `--concurrency=1` run persists mode `serial` in the run row and config."""
    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-99", skip_preflight=True, concurrency=1)
    result = run_build(
        opts,
        queue=_independent(2),
        ledger=Ledger(db),
        dispatcher=ConcurrencyProbeDispatcher(),
        preflight=lambda: True,
    )
    ledger = Ledger(db)
    assert ledger.run_row(result.run_id)["mode"] == "serial"
    assert ledger.run_config(result.run_id)["mode"] == "serial"


def test_run_build_records_parallel_mode_for_multiworker(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-99", skip_preflight=True, concurrency=5)
    result = run_build(
        opts,
        queue=_independent(2),
        ledger=Ledger(db),
        dispatcher=ConcurrencyProbeDispatcher(),
        preflight=lambda: True,
    )
    assert Ledger(db).run_row(result.run_id)["mode"] == "parallel"


def _seed_active_cohort(db, *, mode: str, concurrency: int, active: int, idle: int):
    """A run with ``active`` stories IN_PROGRESS and ``idle`` still TODO."""
    import json as _json

    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", mode)
    ledger.event_log(
        run_id, "", "info", "config", _json.dumps({"concurrency": concurrency, "mode": mode})
    )
    for i in range(active):
        ledger.story_upsert(run_id, f"a{i}", "99", "S", "P1", 2, "py", "", None, "IN_PROGRESS")
        ledger.stage_start(run_id, f"a{i}", "build", 1)
    for i in range(idle):
        ledger.story_upsert(run_id, f"t{i}", "99", "S", "P1", 2, "py", "", None, "TODO")
    return ledger, run_id


def test_status_snapshot_exposes_concurrency_figure(tmp_path) -> None:
    """A parallel run surfaces all active stories + an effective-concurrency figure."""
    from sdlc.build import status_snapshot

    ledger, run_id = _seed_active_cohort(
        tmp_path / "ledger.db", mode="parallel", concurrency=5, active=3, idle=2
    )
    snap = status_snapshot(ledger, run_id)

    conc = snap["run"]["concurrency"]
    assert conc["limit"] == 5
    assert conc["active"] == 3  # 3 of 5 workers busy — not just one
    # All three active stories are visible in the snapshot, not a single one.
    active_ids = {s["story_id"] for s in snap["stories"] if s["status"] == "IN_PROGRESS"}
    assert active_ids == {"a0", "a1", "a2"}


def test_status_snapshot_serial_run_concurrency_limit_is_one(tmp_path) -> None:
    """A serial run reports a worker cap of 1 regardless of any persisted figure."""
    from sdlc.build import status_snapshot

    ledger, run_id = _seed_active_cohort(
        tmp_path / "ledger.db", mode="serial", concurrency=5, active=1, idle=1
    )
    snap = status_snapshot(ledger, run_id)
    assert snap["run"]["concurrency"] == {"limit": 1, "active": 1}
