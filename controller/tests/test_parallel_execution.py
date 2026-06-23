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
