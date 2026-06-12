# ABOUTME: Tests for the build-stories state machine port (Story 7.3-001).
# ABOUTME: Agent dispatch is mocked; the ledger is a real temp SQLite DB.

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sdlc.build import (
    BuildOptions,
    BuildResult,
    Ledger,
    parse_build_args,
    run_build,
)
from sdlc.cohort import Story


# ---------------------------------------------------------------------------
# Argument parsing — same surface the skill accepts today
# ---------------------------------------------------------------------------

def test_parse_default_scope_is_all() -> None:
    opts = parse_build_args([])
    assert opts.scope == "all"
    assert opts.sequential is False
    assert opts.dry_run is False
    assert opts.coverage_threshold == 90


def test_parse_scope_and_flags() -> None:
    opts = parse_build_args(
        [
            "epic-07",
            "--dry-run",
            "--auto",
            "--skip-coverage",
            "--limit=3",
            "--sequential",
            "--coverage-threshold=80",
            "--skip-preflight",
        ]
    )
    assert opts.scope == "epic-07"
    assert opts.dry_run is True
    assert opts.auto is True
    assert opts.skip_coverage is True
    assert opts.limit == 3
    assert opts.sequential is True
    assert opts.coverage_threshold == 80
    assert opts.skip_preflight is True


def test_parse_rejects_unknown_flag() -> None:
    with pytest.raises(ValueError, match="unknown"):
        parse_build_args(["--frobnicate"])


# ---------------------------------------------------------------------------
# Ledger — thin wrapper over the Epic-04 SQLite schema
# ---------------------------------------------------------------------------

def _open(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(db)


def test_ledger_init_creates_schema(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    conn = _open(db)
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"runs", "stories", "stages", "events"}.issubset(tables)


def test_ledger_run_create_and_status(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-07", "parallel")
    assert run_id
    ledger.run_update_status(run_id, "DONE")
    conn = _open(tmp_path / "ledger.db")
    row = conn.execute(
        "SELECT status, finished_at FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row[0] == "DONE"
    assert row[1] is not None  # terminal status stamps finished_at


def test_ledger_stage_lifecycle(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-07", "parallel")
    ledger.story_upsert(run_id, "7.3-001", "07", "Port", "P2", 8, "py", "", None, "TODO")
    ledger.stage_start(run_id, "7.3-001", "build", 1)
    ledger.stage_finish(run_id, "7.3-001", "build", 1, "DONE", "", "")
    conn = _open(tmp_path / "ledger.db")
    row = conn.execute(
        "SELECT status, finished_at FROM stages "
        "WHERE run_id=? AND story_id=? AND stage_name='build'",
        (run_id, "7.3-001"),
    ).fetchone()
    assert row[0] == "DONE"
    assert row[1] is not None


def test_ledger_event_log_appends(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-07", "parallel")
    ledger.event_log(run_id, "", "info", "controller", "run started")
    conn = _open(tmp_path / "ledger.db")
    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# State-machine fixtures: a fake dispatcher returning schema-valid responses
# ---------------------------------------------------------------------------

def _sample_queue() -> list[Story]:
    """A 3-story, single-epic project (the regression sample from the AC)."""
    return [
        Story("s1-001", "Story one", "99", "sample", "epic-99.md", "P1", 2, "py", []),
        Story("s1-002", "Story two", "99", "sample", "epic-99.md", "P1", 2, "py", []),
        Story(
            "s1-003",
            "Story three",
            "99",
            "sample",
            "epic-99.md",
            "P2",
            3,
            "py",
            ["s1-001"],
        ),
    ]


class FakeDispatcher:
    """Records dispatches and returns canned schema-valid responses.

    This stands in for the real subprocess-backed agent dispatch. ``script``
    maps an agent_type to either a dict payload or a callable producing one,
    so a test can inject a build failure for one story.
    """

    def __init__(self, overrides=None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.overrides = overrides or {}

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        from sdlc.dispatch import AgentResult

        self.calls.append((agent_type, getattr(story, "id", "")))
        key = (agent_type, getattr(story, "id", None))
        if key in self.overrides:
            payload = self.overrides[key]
            if callable(payload):
                payload = payload()
        else:
            payload = _default_payload(agent_type, story)
        return AgentResult(agent_type=agent_type, data=payload, raw="")


def _default_payload(agent_type, story):
    sid = getattr(story, "id", "x")
    return {
        "build": {
            "branch_name": f"feature/{sid}",
            "build_status": "SUCCESS",
            "commit_sha": "deadbeef",
        },
        "coverage": {
            "pr_number": 100,
            "pr_url": f"https://example/pull/100",
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


# ---------------------------------------------------------------------------
# run_build: happy path
# ---------------------------------------------------------------------------

def test_run_build_happy_path_marks_all_done(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert isinstance(result, BuildResult)
    assert result.completed == 3
    assert result.failed == 0
    # Every story reached merge.
    agent_types = {a for a, _ in dispatcher.calls}
    assert {"build", "coverage", "review", "merge"}.issubset(agent_types)


def test_run_build_writes_ledger_after_every_stage(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    conn = _open(db)
    # Every story has a DONE merge stage row.
    merged = conn.execute(
        "SELECT COUNT(*) FROM stages WHERE stage_name='merge' AND status='DONE'"
    ).fetchone()[0]
    assert merged == 3
    # The run is closed as DONE.
    run_status = conn.execute("SELECT status FROM runs").fetchone()[0]
    assert run_status == "DONE"


def test_run_build_skip_coverage_omits_coverage_stage(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True, skip_coverage=True
    )
    run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    agent_types = {a for a, _ in dispatcher.calls}
    assert "coverage" not in agent_types
    assert {"build", "review", "merge"}.issubset(agent_types)


# ---------------------------------------------------------------------------
# Failure routing: malformed/failed agent output → bugfix loop
# ---------------------------------------------------------------------------

def test_build_failure_routes_to_bugfix(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    # s1-002's first build fails; the bugfix agent FIXES it, then it retries.
    state = {"attempts": 0}

    def flaky_build():
        state["attempts"] += 1
        if state["attempts"] == 1:
            return {
                "branch_name": "feature/s1-002",
                "build_status": "FAILED",
                "commit_sha": "0000",
                "error_summary": "boom",
            }
        return {
            "branch_name": "feature/s1-002",
            "build_status": "SUCCESS",
            "commit_sha": "1111",
        }

    dispatcher = FakeDispatcher(overrides={("build", "s1-002"): flaky_build})
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True, auto=True)
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    # The bugfix agent was dispatched for s1-002.
    assert ("bugfix", "s1-002") in dispatcher.calls
    # And s1-002 ultimately completed after the fix.
    assert result.completed == 3


def test_malformed_response_treated_as_build_failure(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    from sdlc.contracts import SchemaValidationError

    def raise_schema_error():
        raise SchemaValidationError("build-agent response is missing 'branch_name'")

    # Build raises a contract error (malformed output) the first time, then
    # the bugfix agent reports it cannot fix it → story FAILED.
    dispatcher = FakeDispatcher(
        overrides={
            ("build", "s1-002"): raise_schema_error,
            ("bugfix", "s1-002"): {
                "failure_category": "SCHEMA_ERROR",
                "fix_status": "UNFIXED",
                "tests_passing": False,
                "bugs_fixed": 0,
                "tests_fixed": 0,
            },
        }
    )
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True, auto=True)
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    # s1-002 failed; the schema error was caught by the controller, not the
    # next stage. A bugfix attempt was made.
    assert ("bugfix", "s1-002") in dispatcher.calls
    assert result.failed == 1
    conn = _open(db)
    s2_status = conn.execute(
        "SELECT status FROM stories WHERE story_id='s1-002'"
    ).fetchone()[0]
    assert s2_status == "FAILED"


def test_failed_build_blocks_dependents(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    # s1-001 fails permanently; s1-003 depends on it → s1-003 is BLOCKED.
    dispatcher = FakeDispatcher(
        overrides={
            ("build", "s1-001"): {
                "branch_name": "feature/s1-001",
                "build_status": "FAILED",
                "commit_sha": "0",
                "error_summary": "nope",
            },
            ("bugfix", "s1-001"): {
                "failure_category": "CODE_BUG",
                "fix_status": "UNFIXED",
                "tests_passing": False,
                "bugs_fixed": 0,
                "tests_fixed": 0,
            },
        }
    )
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True, auto=True)
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    conn = _open(db)
    s3_status = conn.execute(
        "SELECT status FROM stories WHERE story_id='s1-003'"
    ).fetchone()[0]
    assert s3_status == "BLOCKED"
    # s1-001 failed; s1-002 still completed.
    assert result.failed >= 1


# ---------------------------------------------------------------------------
# Preflight gate
# ---------------------------------------------------------------------------

def test_preflight_failure_aborts_before_dispatch(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    opts = BuildOptions(scope="epic-99", sequential=True)  # preflight NOT skipped
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: False,  # red suite
    )
    assert result.preflight_failed is True
    assert dispatcher.calls == []  # nothing dispatched


def test_skip_preflight_does_not_call_preflight(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    called = {"n": 0}

    def preflight():
        called["n"] += 1
        return True

    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=preflight,
    )
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def test_dry_run_dispatches_nothing(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True, dry_run=True
    )
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert dispatcher.calls == []
    assert result.dry_run is True
    assert result.planned == 3


# ---------------------------------------------------------------------------
# Limit truncation
# ---------------------------------------------------------------------------

def test_limit_truncates_queue(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True, limit=1, dry_run=True
    )
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    # s1-001 is the first; it has no deps so no pull-in.
    assert result.planned == 1


# ---------------------------------------------------------------------------
# Regression: CLI state machine produces the same end state as the skill would
# ---------------------------------------------------------------------------

def test_regression_end_state_matches_expected(tmp_path) -> None:
    """3-story 1-epic sample: every story DONE, one run row, DONE status.

    This mirrors the legacy skill's end state: all stories merged, the run
    closed DONE, and a merge stage recorded per story.
    """
    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    conn = _open(db)
    statuses = {
        r[0]: r[1]
        for r in conn.execute("SELECT story_id, status FROM stories").fetchall()
    }
    assert statuses == {"s1-001": "DONE", "s1-002": "DONE", "s1-003": "DONE"}
    assert result.completed == 3
    runs = conn.execute("SELECT COUNT(*), MAX(status) FROM runs").fetchone()
    assert runs[0] == 1
    assert runs[1] == "DONE"
