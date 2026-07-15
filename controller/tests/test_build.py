# ABOUTME: Tests for the build-stories state machine port (Story 7.3-001).
# ABOUTME: Agent dispatch is mocked; the ledger is a real temp SQLite DB.

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

import pytest

from sdlc.contracts import AGENT_SCHEMAS

from sdlc.build import (
    IN_TEST_ENV_VAR,
    PER_TEST_TIMEOUT,
    BuildOptions,
    BuildResult,
    Ledger,
    _dispatch_overengineering_advisory,
    _filter_git_landed,
    _stamp_run_actor,
    default_preflight,
    detect_test_command,
    in_test_sentinel,
    parse_build_args,
    run_build,
)
from sdlc import issue_host as ih
from sdlc.cohort import Story
from sdlc.commitlint import lint_commit_message


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


def test_parse_harness_map_space_form() -> None:
    """Story 20.2-001: the space-separated `--harness ROLE=NAME,…` form parses."""
    opts = parse_build_args(
        ["epic-99", "--harness", "build=claude,review=codex,qa=codex"]
    )
    assert opts.harness_map == {
        "build": "claude",
        "review": "codex",
        "coverage": "codex",
    }
    assert opts.scope == "epic-99"


def test_parse_harness_map_equals_form() -> None:
    opts = parse_build_args(["--harness=review=codex"])
    assert opts.harness_map == {"review": "codex"}


def test_parse_harness_no_map_is_empty() -> None:
    assert parse_build_args(["epic-99"]).harness_map == {}


def test_parse_harness_missing_value_errors() -> None:
    with pytest.raises(ValueError, match="--harness needs a value"):
        parse_build_args(["--harness"])


def test_parse_harness_unknown_role_errors() -> None:
    with pytest.raises(ValueError, match="unknown pipeline role"):
        parse_build_args(["--harness", "deploy=codex"])


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
        # Attach a sample usage envelope so the orchestration exercises token
        # recording (mirrors the real --output-format json dispatch).
        return AgentResult(
            agent_type=agent_type, data=payload, raw="",
            usage=dict(_SAMPLE_USAGE), cost_usd=0.05,
            session_id=f"sess-{agent_type}",
        )


# A representative usage envelope (the four token counts the agent emits).
_SAMPLE_USAGE = {
    "input_tokens": 100,
    "output_tokens": 20,
    "cache_read_input_tokens": 4000,
    "cache_creation_input_tokens": 300,
}
# Sum of the four counts above → the per-stage token total the ledger stores.
_SAMPLE_STAGE_TOKENS = 100 + 20 + 4000 + 300


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


def test_run_build_logs_harness_capabilities(tmp_path, monkeypatch) -> None:
    """Story 20.5-001 AC1: preflight resolves and logs the dispatch harness capabilities."""
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
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
    rows = conn.execute(
        "SELECT message FROM events WHERE source='harness'"
    ).fetchall()
    assert rows, "expected at least one harness capability event"
    joined = "\n".join(r[0] for r in rows)
    assert "claude" in joined
    assert "worktree_isolation" in joined


def test_run_build_default_harness_labels_default_slot_and_skips_routing_line(
    tmp_path, monkeypatch
) -> None:
    """Issue #426: a plain run (no --harness) still resolves only the default
    slot, so its preflight lines are labeled ``(default slot)`` and no
    ``harness routing:`` line is emitted — nothing to route when every role
    already collapses to the built-in default."""
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
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
    rows = conn.execute(
        "SELECT message FROM events WHERE source='harness'"
    ).fetchall()
    joined = "\n".join(r[0] for r in rows)
    assert "(default slot)" in joined
    assert "harness routing:" not in joined


def test_run_build_per_role_harness_map_logs_routing_and_labels_default_slot(
    tmp_path, monkeypatch
) -> None:
    """Issue #426: a per-role ``--harness`` run (e.g. every role routed to Codex)
    must log the *effective* role->harness routing map so the run is auditable
    as Codex-routed, and the separate default-slot preflight line must be
    clearly labeled so it is never mistaken for the harness actually dispatching
    the work."""
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    opts.harness_map = {
        "build": "codex",
        "coverage": "codex",
        "review": "codex",
        "merge": "codex",
    }
    run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    conn = _open(db)
    rows = conn.execute(
        "SELECT message FROM events WHERE source='harness' ORDER BY id"
    ).fetchall()
    joined = "\n".join(r[0] for r in rows)
    assert (
        "harness routing: build=codex coverage=codex review=codex merge=codex "
        "docs=claude" in joined
    )
    # The default-slot capability lines still resolve `claude` (today's
    # behaviour, AC3 of Story 20.5-001) but must be labeled so a reader cannot
    # confuse them with the codex worker actually dispatching the stages.
    assert "harness 'claude' (default slot)" in joined


def test_run_build_records_no_degradation_for_builtin_claude(tmp_path, monkeypatch) -> None:
    """Story 20.5-002 AC3: the fully-capable Claude harness records no degradation."""
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
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
    rows = conn.execute(
        "SELECT message FROM events WHERE source='degradation'"
    ).fetchall()
    assert rows == [], "fully-capable Claude harness must not degrade"


def test_run_build_records_degradations_for_bare_harness(tmp_path, monkeypatch) -> None:
    """Story 20.5-002 AC3: a harness with capability gaps records each fallback."""
    from sdlc.harness import HarnessConfig

    bare = HarnessConfig(
        name="codex",
        command="codex exec",
        parser="codex-exec",
        capabilities={
            "worktree_isolation": False,
            "parallel": False,
            "json_contract": True,
            "usage_tracking": False,
            "rate_limit_aware": False,
        },
    )
    # The default-slot resolver returns Claude; force the recording helper to see
    # a non-Claude harness so the degradation paths fire.
    monkeypatch.setattr("sdlc.build.resolve_harness", lambda *a, **k: bare)
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
    rows = conn.execute(
        "SELECT message FROM events WHERE source='degradation'"
    ).fetchall()
    joined = "\n".join(r[0] for r in rows)
    # A serial run requests serial mode, so no parallel→serial line; the
    # telemetry gaps (usage, rate-limit) are always recorded.
    assert "unavailable" in joined
    assert "rate-limit" in joined


def test_run_build_degradation_recording_is_best_effort(tmp_path, monkeypatch) -> None:
    """Story 20.5-002 AC3: a failure while recording degradations never fails the
    build. The recorder is best-effort, so a raising dependency is swallowed and
    the run still completes with no degradation events written."""
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)

    def _boom(*_a, **_k):
        raise RuntimeError("harness resolution exploded")

    # Force the recorder's first call to raise so the except guard fires.
    monkeypatch.setattr("sdlc.build.resolve_harness", _boom)
    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    assert isinstance(result, BuildResult)
    conn = _open(db)
    rows = conn.execute(
        "SELECT message FROM events WHERE source='degradation'"
    ).fetchall()
    assert rows == [], "a recorder failure must write nothing, not crash the build"


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
# Registry integration (Story 11.2-001)
# ---------------------------------------------------------------------------

def test_run_build_registers_and_marks_finished(tmp_path) -> None:
    from sdlc.registry import Registry

    db = tmp_path / "ledger.db"
    registry = Registry(tmp_path / "registry.json")
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
        registry=registry,
    )
    records = registry.records()
    assert len(records) == 1
    rec = records[0]
    assert rec.scope == "epic-99"
    assert rec.db == str(db.resolve())
    assert rec.total == 3
    # Clean close-out: terminal status + finished_at stamped, counts reconciled.
    assert rec.status == "DONE"
    assert rec.finished_at
    assert rec.completed == 3


def test_registry_helpers_swallow_io_errors(tmp_path) -> None:
    """A registry whose IO raises OSError must never fail a build (best-effort)."""
    from sdlc.build import _registry_finish, _registry_register

    class _BrokenRegistry:
        def register(self, record):
            raise OSError("disk full")

        def mark_finished(self, run_id, status, *, completed=None):
            raise PermissionError("read-only filesystem")  # subclass of OSError

    broken = _BrokenRegistry()
    # Neither helper may propagate the OSError; both are no-ops on failure.
    _registry_register(broken, "run-1", "epic-99", tmp_path / "l.db", 3)
    _registry_finish(broken, "run-1", "DONE", 3)


def test_run_build_survives_registry_io_failure(tmp_path) -> None:
    """An end-to-end build with a failing registry still completes cleanly."""
    class _BrokenRegistry:
        def register(self, record):
            raise OSError("disk full")

        def mark_finished(self, run_id, status, *, completed=None):
            raise OSError("disk full")

    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=_sample_queue(),
        ledger=Ledger(tmp_path / "ledger.db"),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
        registry=_BrokenRegistry(),
    )
    assert result.completed == 3
    assert result.failed == 0


def test_run_build_without_registry_is_unaffected(tmp_path) -> None:
    # The default path: existing callers pass no registry and nothing breaks.
    db = tmp_path / "ledger.db"
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    assert result.completed == 3


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


# ---------------------------------------------------------------------------
# Story 23.2-002: gate the merge on the change request's CI/pipeline status
# ---------------------------------------------------------------------------


def _gate_story_queue() -> list[Story]:
    """A single mapped-target story so the merge CI gate engages on its CR."""
    return [Story("g1-001", "Gate story", "23", "pipeline-on-gitlab",
                  "epic-23.md", "Should", 2, "py", [])]


def test_merge_gate_blocks_on_red_pipeline(tmp_path, monkeypatch) -> None:
    """A failed CR pipeline blocks the merge and routes the story to bugfix (AC2)."""
    from sdlc import build_issue

    monkeypatch.setattr(build_issue, "change_request_status", lambda *a, **k: ih.CR_FAILED)
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    opts = BuildOptions(scope="epic-23", skip_preflight=True, sequential=True, auto=True)
    result = run_build(opts, queue=_gate_story_queue(), ledger=Ledger(db),
                       dispatcher=dispatcher, preflight=lambda: True)
    # The merge agent is NEVER dispatched — the gate blocked before merge.
    assert ("merge", "g1-001") not in dispatcher.calls
    # The red pipeline routed the story into the bugfix loop, not a merge.
    assert ("bugfix", "g1-001") in dispatcher.calls
    assert result.completed == 0
    assert result.failed == 1


def test_merge_gate_passes_on_green_pipeline(tmp_path, monkeypatch) -> None:
    """A green CR pipeline lets the merge proceed (AC3) with no bugfix detour."""
    from sdlc import build_issue

    monkeypatch.setattr(build_issue, "change_request_status", lambda *a, **k: ih.CR_SUCCESS)
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    opts = BuildOptions(scope="epic-23", skip_preflight=True, sequential=True, auto=True)
    result = run_build(opts, queue=_gate_story_queue(), ledger=Ledger(db),
                       dispatcher=dispatcher, preflight=lambda: True)
    assert ("merge", "g1-001") in dispatcher.calls
    assert ("bugfix", "g1-001") not in dispatcher.calls
    assert result.completed == 1


def test_merge_gate_no_ci_allows_by_default(tmp_path, monkeypatch) -> None:
    """A project with no CI signal degrades to a warning + merge under allow (AC4)."""
    from sdlc import build_issue

    monkeypatch.setattr(build_issue, "change_request_status", lambda *a, **k: ih.CR_NONE)
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    opts = BuildOptions(scope="epic-23", skip_preflight=True, sequential=True, auto=True)
    result = run_build(opts, queue=_gate_story_queue(), ledger=Ledger(db),
                       dispatcher=dispatcher, preflight=lambda: True)
    assert ("merge", "g1-001") in dispatcher.calls
    assert result.completed == 1


def test_merge_gate_no_ci_deny_blocks(tmp_path, monkeypatch) -> None:
    """The no-CI policy is configurable: deny blocks a CI-less merge (AC4)."""
    from sdlc import build_issue

    monkeypatch.setattr(build_issue, "change_request_status", lambda *a, **k: ih.CR_NONE)
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    opts = BuildOptions(scope="epic-23", skip_preflight=True, sequential=True,
                        auto=True, ci_gate_no_ci="deny")
    result = run_build(opts, queue=_gate_story_queue(), ledger=Ledger(db),
                       dispatcher=dispatcher, preflight=lambda: True)
    assert ("merge", "g1-001") not in dispatcher.calls
    assert result.completed == 0


def test_merge_gate_unmapped_story_is_unchanged(tmp_path) -> None:
    """A story with no host mapping skips the gate — the merge path is unchanged."""
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    opts = BuildOptions(scope="epic-23", skip_preflight=True, sequential=True, auto=True)
    # change_request_status is NOT patched: with no inventory mapping it returns
    # None, the gate skips, and the merge dispatches exactly as before this story.
    result = run_build(opts, queue=_gate_story_queue(), ledger=Ledger(db),
                       dispatcher=dispatcher, preflight=lambda: True)
    assert ("merge", "g1-001") in dispatcher.calls
    assert result.completed == 1


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


# ---------------------------------------------------------------------------
# R4: done-skip + --rebuild
# ---------------------------------------------------------------------------

def _story(sid: str, *, deps=None, done=False) -> Story:
    return Story(
        sid, f"Story {sid}", sid.split(".", 1)[0].zfill(2), "x",
        "epic-x.md", "P1", 1, "py", deps or [], done,
    )


def test_run_build_skips_done_stories(tmp_path) -> None:
    """A story the epic marks Done is recorded SKIPPED with an event, not built."""
    db = tmp_path / "l.db"
    disp = FakeDispatcher()
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    result = run_build(
        opts,
        queue=[_story("99.1-001", done=True), _story("99.1-002", done=False)],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.skipped == 1
    assert result.completed == 1
    assert result.story_status["99.1-001"] == "SKIPPED"
    # The done story was never dispatched.
    assert all(sid != "99.1-001" for _, sid in disp.calls)
    # A skip event is recorded for the audit trail.
    conn = _open(db)
    events = conn.execute(
        "SELECT message FROM events WHERE story_id = '99.1-001'"
    ).fetchall()
    assert any("skipped" in row[0].lower() for row in events)


def test_rebuild_forces_done_stories(tmp_path) -> None:
    """`--rebuild` rebuilds stories that are marked Done."""
    db = tmp_path / "l.db"
    disp = FakeDispatcher()
    opts = BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True, rebuild=True
    )
    result = run_build(
        opts,
        queue=[_story("99.1-001", done=True), _story("99.1-002", done=False)],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.skipped == 0
    assert result.completed == 2


# ---------------------------------------------------------------------------
# #227 — discovery done-detection is git-aware: a story merged in a prior run
# whose markdown was never set to Status: Done must be skipped at the partition
# (git-landed), not rebuilt — otherwise re-executing it fails and cascade-blocks
# any newly-added story that depends on it.
# ---------------------------------------------------------------------------


def test_filter_git_landed_moves_landed_to_done_skips(monkeypatch) -> None:
    """A buildable story whose work is git-landed is moved into done_skips (#227)."""
    import sdlc.reconcile as reconcile_mod

    monkeypatch.setattr("sdlc.build._base_ref", lambda root: "origin/main")
    monkeypatch.setattr(
        reconcile_mod,
        "_detect_landing",
        lambda sid, pr, base, root: ("commit-tag", "abc") if sid == "20.1-001" else None,
    )
    landed, fresh = _story("20.1-001"), _story("20.7-001")
    buildable, done_skips = _filter_git_landed([landed, fresh], [])
    assert [s.id for s in buildable] == ["20.7-001"]
    assert [s.id for s in done_skips] == ["20.1-001"]


def test_filter_git_landed_no_base_is_noop(tmp_path, monkeypatch) -> None:
    """No resolvable base ref ⇒ partition unchanged, detector never probed."""
    import sdlc.reconcile as reconcile_mod

    monkeypatch.setattr("sdlc.build._base_ref", lambda root: None)
    calls: list[str] = []
    monkeypatch.setattr(
        reconcile_mod,
        "_detect_landing",
        lambda sid, pr, base, root: calls.append(sid),
    )
    buildable, done_skips = _filter_git_landed(
        [_story("20.7-001"), _story("20.7-002")], [], root=tmp_path
    )
    assert [s.id for s in buildable] == ["20.7-001", "20.7-002"]
    assert done_skips == []
    assert calls == []  # short-circuit before any git probe


def test_run_build_skips_git_landed_story(tmp_path, monkeypatch) -> None:
    """A git-landed (but not markdown-done) story is SKIPPED, never dispatched."""
    import sdlc.reconcile as reconcile_mod

    monkeypatch.setattr("sdlc.build._base_ref", lambda root: "origin/main")
    monkeypatch.setattr(
        reconcile_mod, "_detect_landing", lambda sid, pr, base, root: ("commit-tag", "x")
    )
    disp = FakeDispatcher()
    result = run_build(
        BuildOptions(scope="epic-20", skip_preflight=True, sequential=True),
        queue=[_story("20.1-001", done=False)],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.story_status["20.1-001"] == "SKIPPED"
    assert result.skipped == 1
    assert all(sid != "20.1-001" for _, sid in disp.calls)


def test_run_build_keeps_incomplete_story_buildable(tmp_path, monkeypatch) -> None:
    """Neither markdown-done nor git-landed ⇒ the story still builds on a re-run."""
    import sdlc.reconcile as reconcile_mod

    monkeypatch.setattr("sdlc.build._base_ref", lambda root: "origin/main")
    monkeypatch.setattr(
        reconcile_mod, "_detect_landing", lambda sid, pr, base, root: None
    )
    disp = FakeDispatcher()
    result = run_build(
        BuildOptions(scope="epic-20", skip_preflight=True, sequential=True),
        queue=[_story("20.7-001", done=False)],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.skipped == 0
    assert result.completed == 1
    assert any(sid == "20.7-001" for _, sid in disp.calls)


def test_run_build_new_story_builds_when_dep_is_git_landed(
    tmp_path, monkeypatch
) -> None:
    """#227 repro: an epic re-run with a merged dep + a new dependent.

    The merged dep is git-landed (not markdown-done) so it drops into done_skips
    and out of the cohort DAG; its new dependent then sees a satisfied
    out-of-queue edge and builds instead of being cascade-blocked.
    """
    import sdlc.reconcile as reconcile_mod

    monkeypatch.setattr("sdlc.build._base_ref", lambda root: "origin/main")
    monkeypatch.setattr(
        reconcile_mod,
        "_detect_landing",
        lambda sid, pr, base, root: ("commit-tag", "x") if sid == "20.1-001" else None,
    )
    disp = FakeDispatcher()
    result = run_build(
        BuildOptions(scope="epic-20", skip_preflight=True, sequential=True),
        queue=[
            _story("20.1-001", done=False),
            _story("20.7-001", done=False, deps=["20.1-001"]),
        ],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.story_status["20.1-001"] == "SKIPPED"
    assert result.story_status["20.7-001"] == "DONE"
    assert "20.7-001" not in {
        sid for sid, st in result.story_status.items() if st == "BLOCKED"
    }


def test_run_build_markdown_done_story_is_not_git_probed(
    tmp_path, monkeypatch
) -> None:
    """A markdown-done story is skipped WITHOUT a git probe (the #227 fast path)."""
    import sdlc.reconcile as reconcile_mod

    monkeypatch.setattr("sdlc.build._base_ref", lambda root: "origin/main")
    probed: list[str] = []

    def _spy(sid, pr, base, root):
        probed.append(sid)
        return None

    monkeypatch.setattr(reconcile_mod, "_detect_landing", _spy)
    run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001", done=True), _story("99.1-002", done=False)],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    # The markdown-done story is partitioned into done_skips before the probe and
    # is never passed to the detector; only the genuinely-buildable story is.
    assert "99.1-001" not in probed
    assert probed == ["99.1-002"]


# ---------------------------------------------------------------------------
# R6: preflight detection + timeout/streaming
# ---------------------------------------------------------------------------

def test_parse_rebuild_and_preflight_timeout() -> None:
    opts = parse_build_args(["--rebuild", "--preflight-timeout=120"])
    assert opts.rebuild is True
    assert opts.preflight_timeout == 120


def test_detect_prefers_quality_gate_script(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "quality-gate.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    cmd = detect_test_command(tmp_path)
    assert cmd[0] == "bash" and cmd[1].endswith("scripts/quality-gate.sh")


def test_detect_prefers_make_gate(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("gate:\n\techo hi\n", encoding="utf-8")
    assert detect_test_command(tmp_path) == ["make", "gate"]


def test_detect_pytest_adds_xdist_when_present(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pytest-xdist>=3"]\n', encoding="utf-8"
    )
    assert detect_test_command(tmp_path) == ["uv", "run", "pytest", "-n", "auto"]


def test_detect_pytest_serial_without_xdist(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert detect_test_command(tmp_path) == ["uv", "run", "pytest"]


def test_default_preflight_no_suite_passes(tmp_path) -> None:
    assert default_preflight(root=tmp_path, timeout=5) is True


def test_default_preflight_nonzero_fails(tmp_path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "quality-gate.sh").write_text(
        "#!/usr/bin/env bash\nexit 1\n", encoding="utf-8"
    )
    assert default_preflight(root=tmp_path, timeout=10) is False


def test_default_preflight_times_out(tmp_path, capsys) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "quality-gate.sh").write_text(
        "#!/usr/bin/env bash\nsleep 5\n", encoding="utf-8"
    )
    assert default_preflight(root=tmp_path, timeout=1) is False
    assert "PRE_FLIGHT_TIMEOUT" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Story 12.1-002: recursion guard (sentinel) + per-test timeout
# ---------------------------------------------------------------------------

def test_in_test_sentinel_reads_env(monkeypatch) -> None:
    monkeypatch.delenv(IN_TEST_ENV_VAR, raising=False)
    assert in_test_sentinel() is False
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv(IN_TEST_ENV_VAR, truthy)
        assert in_test_sentinel() is True
    for falsy in ("", "0", "false", "no"):
        monkeypatch.setenv(IN_TEST_ENV_VAR, falsy)
        assert in_test_sentinel() is False


def test_default_preflight_sets_in_test_sentinel(tmp_path) -> None:
    """The preflight subprocess must see SDLC_IN_TEST so a project test that
    invokes the controller's own verbs short-circuits instead of recursing."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "quality-gate.sh").write_text(
        f'#!/usr/bin/env bash\ntest -n "${IN_TEST_ENV_VAR}"\n', encoding="utf-8"
    )
    # Ensure the parent does NOT already export it, proving preflight injects it.
    assert default_preflight(root=tmp_path, timeout=10) is True


def test_default_preflight_does_not_leak_sentinel(tmp_path, monkeypatch) -> None:
    """Setting the sentinel for the child must not mutate the parent's env."""
    monkeypatch.delenv(IN_TEST_ENV_VAR, raising=False)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "quality-gate.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
    )
    default_preflight(root=tmp_path, timeout=10)
    assert IN_TEST_ENV_VAR not in os.environ


def test_detect_pytest_adds_timeout_when_present(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pytest-timeout>=2"]\n', encoding="utf-8"
    )
    cmd = detect_test_command(tmp_path)
    assert f"--timeout={PER_TEST_TIMEOUT}" in cmd
    assert "--timeout-method=thread" in cmd


def test_detect_pytest_no_timeout_without_plugin(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    cmd = detect_test_command(tmp_path)
    assert not any(c.startswith("--timeout") for c in cmd)


def test_detect_pytest_timeout_detected_from_uv_lock(tmp_path) -> None:
    """The plugin scan also covers uv.lock, not just pyproject.toml (Story 12.1-002)."""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        'name = "pytest-timeout"\nversion = "2.3.1"\n', encoding="utf-8"
    )
    cmd = detect_test_command(tmp_path)
    assert f"--timeout={PER_TEST_TIMEOUT}" in cmd
    assert "--timeout-method=thread" in cmd


def test_detect_pytest_timeout_detected_from_requirements(tmp_path) -> None:
    """requirements.txt is the third dep source the plugin scan reads (Story 12.1-002)."""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pytest-timeout>=2\n", encoding="utf-8")
    cmd = detect_test_command(tmp_path)
    assert f"--timeout={PER_TEST_TIMEOUT}" in cmd
    assert "--timeout-method=thread" in cmd


def test_run_build_short_circuits_under_sentinel_with_real_defaults(
    tmp_path, monkeypatch
) -> None:
    """A real run (no injected dispatcher/preflight) under the sentinel must
    short-circuit before any side effect — neither preflight nor dispatch runs."""
    monkeypatch.setenv(IN_TEST_ENV_VAR, "1")

    def _boom() -> bool:
        raise AssertionError("preflight must not run under the sentinel")

    # No dispatcher and no preflight injected → real defaults → guard fires.
    result = run_build(
        BuildOptions(scope="epic-99", sequential=True),
        queue=_sample_queue(),
        ledger=Ledger(tmp_path / "l.db"),
    )
    assert result.skipped_in_test is True
    assert result.completed == 0


def test_run_build_with_fakes_ignores_sentinel(tmp_path, monkeypatch) -> None:
    """AC3: a run that injects a fake dispatcher/preflight is exercising
    orchestration deliberately — the guard must NOT block it, even when the
    sentinel is set (the case during the controller's own preflight)."""
    monkeypatch.setenv(IN_TEST_ENV_VAR, "1")
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=_sample_queue(),
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    assert result.skipped_in_test is False
    assert result.completed == 3


# ---------------------------------------------------------------------------
# Close-out reconciliation wiring (Story 12.3-001)
# ---------------------------------------------------------------------------


def test_run_build_applies_reconcile_reclassifications(tmp_path, monkeypatch) -> None:
    """A real run flips a parked-but-landed story to DONE via reconciliation.

    The story's build keeps failing (parked FAILED), but reconciliation (faked
    here — it does real git I/O in production) finds its work landed on
    origin/main and reclassifies it. The close-out tally must then read DONE, so
    the run terminal is DONE rather than FAILED.
    """
    db = tmp_path / "ledger.db"

    failing = FakeDispatcher(
        overrides={
            ("build", "s1-002"): {
                "branch_name": "feature/s1-002",
                "build_status": "FAILED",
                "commit_sha": "0",
                "error_summary": "nope",
            },
            ("bugfix", "s1-002"): {
                "failure_category": "CODE_BUG",
                "fix_status": "UNFIXED",
                "tests_passing": False,
                "bugs_fixed": 0,
                "tests_fixed": 0,
            },
        }
    )
    # dispatcher is None → the real-run reconcile branch fires; route dispatch
    # through the fake so no subprocess agents spawn.
    monkeypatch.setattr("sdlc.build.dispatch_agent", failing)

    from sdlc.reconcile import ReconcileResult

    calls: list[tuple] = []

    def fake_reconcile(ledger, run_id, root=None, fetch=True):
        calls.append((run_id, fetch))
        ledger.set_story_status(run_id, "s1-002", "DONE")
        return ReconcileResult(
            run_id=run_id,
            reclassified=[
                {"story_id": "s1-002", "from_status": "FAILED",
                 "signal": "is-ancestor", "sha": "cafef00d"}
            ],
            run_status_before="FAILED",
            run_status_after="DONE",
            fetched=True,
        )

    monkeypatch.setattr("sdlc.reconcile.reconcile_run", fake_reconcile)
    # Story 12.4-001: the real-run branch also repositions HEAD between stories
    # via _reposition_head(Path.cwd()). This test runs in the real repo cwd (no
    # chdir), so neutralize that git side effect exactly as reconcile is faked
    # above — otherwise it would check out the base ref in the live checkout.
    monkeypatch.setattr("sdlc.build._reposition_head", lambda root: None)

    # s1-003 depends on s1-001 (which succeeds) so it is not blocked by s1-002.
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True, auto=True)
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=None,
        preflight=lambda: True,
    )

    assert calls and calls[0][1] is True  # reconcile ran with fetch=True
    assert result.failed == 0
    assert result.story_status["s1-002"] == "DONE"
    run_status = _open(db).execute("SELECT status FROM runs").fetchone()[0]
    assert run_status == "DONE"


def test_run_build_skips_reconcile_under_injected_dispatcher(tmp_path, monkeypatch) -> None:
    """Injected fakes (orchestration tests) must not trigger reconcile's git I/O."""
    def _boom(*_a, **_k):
        raise AssertionError("reconcile must not run when a dispatcher is injected")

    monkeypatch.setattr("sdlc.reconcile.reconcile_run", _boom)
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=_sample_queue(),
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    assert result.completed == 3


def test_dry_run_skips_preflight(tmp_path) -> None:
    """A dry run is plan-only — it must not run the preflight gate."""
    def _boom() -> bool:
        raise AssertionError("preflight must not run during a dry run")

    result = run_build(
        BuildOptions(scope="epic-99", dry_run=True, sequential=True),
        queue=_sample_queue(),
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=FakeDispatcher(),
        preflight=_boom,
    )
    assert result.dry_run is True
    assert result.preflight_failed is False


# ---------------------------------------------------------------------------
# R8: transcript output_path is recorded on every stage row
# ---------------------------------------------------------------------------

def test_run_build_records_transcript_output_paths(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    conn = _open(db)
    paths = [r[0] for r in conn.execute("SELECT output_path FROM stages").fetchall()]
    assert paths and all(p for p in paths)        # every stage attempt has a path
    assert all(".sdlc-state.db.logs" in p or ".logs" in p for p in paths)


# ---------------------------------------------------------------------------
# R9: ledger files are kept out of git status via .git/info/exclude
# ---------------------------------------------------------------------------

def test_ensure_repo_ignores_adds_pattern(tmp_path) -> None:
    from sdlc.build import _ensure_repo_ignores

    (tmp_path / ".git" / "info").mkdir(parents=True)
    db = tmp_path / ".sdlc-state.db"
    _ensure_repo_ignores(db)
    exclude = tmp_path / ".git" / "info" / "exclude"
    assert ".sdlc-state.db*" in exclude.read_text(encoding="utf-8")
    _ensure_repo_ignores(db)  # idempotent
    assert exclude.read_text(encoding="utf-8").count(".sdlc-state.db*") == 1


def test_ensure_repo_ignores_no_git_is_noop(tmp_path) -> None:
    from sdlc.build import _ensure_repo_ignores

    _ensure_repo_ignores(tmp_path / ".sdlc-state.db")  # no .git anywhere → no crash
    assert not (tmp_path / ".git").exists()


# ---------------------------------------------------------------------------
# R10: tolerant result parsing + never discard committed work
# ---------------------------------------------------------------------------

class _FencedDispatcher:
    """Mimics dispatch_agent: runs the REAL parser on a ```json-fenced stdout.

    Proves the controller no longer fails a stage just because the agent wrapped
    its result in a markdown fence instead of the sentinel markers (R10).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        from sdlc.contracts import parse_and_validate
        from sdlc.dispatch import AgentResult

        self.calls.append((agent_type, getattr(story, "id", "")))
        payload = _default_payload(agent_type, story)
        stdout = f"work done\n```json\n{json.dumps(payload)}\n```\n"  # no sentinels
        data = parse_and_validate(agent_type, stdout)
        return AgentResult(agent_type=agent_type, data=data, raw=stdout)


class _RaisingDispatcher:
    """Raises a contract error for one agent_type; canned defaults otherwise.

    ``bugfix_fixed=False`` makes the bugfix agent report an unresolved fix, so a
    failing story exits after a single bugfix round (avoids an unrelated latent
    bug where a *second* bugfix round re-inserts the ``bugfix`` stage at the same
    attempt number).
    """

    def __init__(self, raise_on: str = "build", bugfix_fixed: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.raise_on = raise_on
        self.bugfix_fixed = bugfix_fixed

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        from sdlc.contracts import ResultBlockError
        from sdlc.dispatch import AgentResult

        self.calls.append((agent_type, getattr(story, "id", "")))
        if agent_type == self.raise_on:
            raise ResultBlockError("missing <<<RESULT_JSON>>> marker")
        payload = _default_payload(agent_type, story)
        if agent_type == "bugfix" and not self.bugfix_fixed:
            payload = dict(payload, fix_status="FIXED", tests_passing=False)
        return AgentResult(agent_type=agent_type, data=payload, raw="")


def test_fenced_agent_output_builds_successfully(tmp_path) -> None:
    """An agent that wraps its result in ```json (no sentinels) is not a failure."""
    disp = _FencedDispatcher()
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.completed == 1
    assert result.failed == 0
    assert result.story_status["99.1-001"] == "DONE"


def test_contract_error_with_commit_recovers_then_parks(tmp_path, monkeypatch) -> None:
    """Unparseable result + a story commit: re-ask + bugfix are tried, then parked.

    Story 12.1-001 changes the old straight-to-NEEDS_ATTENTION behaviour: an
    envelope re-ask and the bounded bugfix path run first; only when both are
    exhausted is committed work parked NEEDS_ATTENTION (R10 preserved).
    """
    monkeypatch.setattr("sdlc.build.story_commit_exists", lambda sid, root=None: True)
    disp = _RaisingDispatcher(raise_on="build")
    db = tmp_path / "l.db"
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.needs_attention == 1
    assert result.failed == 0
    assert result.story_status["99.1-001"] == "NEEDS_ATTENTION"
    # Recovery was attempted before parking: both an envelope re-ask and the
    # bounded bugfix path ran (AC1, AC2).
    assert any(agent == "bugfix" for agent, _ in disp.calls)
    conn = _open(db)
    msgs = [
        r[0]
        for r in conn.execute(
            "SELECT message FROM events WHERE story_id='99.1-001'"
        ).fetchall()
    ]
    # Each recovery attempt is recorded (AC3) and the operator sees the situation.
    assert any("envelope" in m for m in msgs)
    assert any("committed" in m for m in msgs)


def test_contract_error_without_commit_still_fails(tmp_path, monkeypatch) -> None:
    """No story commit ⇒ unchanged behavior: bugfix loop then FAILED."""
    monkeypatch.setattr("sdlc.build.story_commit_exists", lambda sid, root=None: False)
    disp = _RaisingDispatcher(raise_on="build", bugfix_fixed=False)
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.needs_attention == 0
    assert result.failed == 1
    assert result.story_status["99.1-001"] == "FAILED"
    assert any(agent == "bugfix" for agent, _ in disp.calls)


# ---------------------------------------------------------------------------
# Issue #232 — a committed-but-unvalidated stage is recoverable, not hard FAILED.
# The artifact a stage leaves behind differs by stage: build/coverage/review
# leave a commit on feature/<id>; merge lands a PR. The exhaustion classifier
# must probe the *right* artifact per stage (reusing reconcile's _detect_landing
# for merge) so a merge that actually landed but lost its result block parks
# NEEDS_ATTENTION rather than cascading a false FAILED.
# ---------------------------------------------------------------------------


def test_stage_artifact_exists_is_stage_aware(tmp_path, monkeypatch) -> None:
    """build probes the feature-branch commit; merge probes the merged PR (#232)."""
    import sdlc.reconcile as reconcile_mod
    from sdlc.build import _stage_artifact_exists

    landing_calls = {"n": 0}

    def fake_landing(sid, pr, base, root):
        landing_calls["n"] += 1
        return ("gh-pr-merged", "abc1234")

    monkeypatch.setattr(reconcile_mod, "_detect_landing", fake_landing)

    # build: only the commit-ahead-of-base probe decides; landing is never
    # consulted (a merged-to-main check would wrongly reject an honestly
    # committed-but-unmerged branch). This is the unchanged pre-#232 behaviour.
    monkeypatch.setattr("sdlc.build.story_commit_exists", lambda sid, root=None: True)
    assert _stage_artifact_exists("build", "x", None, root=tmp_path) is True
    monkeypatch.setattr("sdlc.build.story_commit_exists", lambda sid, root=None: False)
    assert _stage_artifact_exists("build", "x", None, root=tmp_path) is False
    assert landing_calls["n"] == 0  # build never falls back to landing detection

    # merge: with no commit ahead of base, the merged-PR landing is the artifact.
    assert _stage_artifact_exists("merge", "x", 100, root=tmp_path) is True
    assert landing_calls["n"] == 1

    # merge ignores the branch-commit signal entirely: a commit on the branch
    # (always present at merge time) is NOT evidence the merge landed, so when
    # landing detection says "not landed" the artifact is absent despite the
    # commit — otherwise an unlanded merge would be masked as recoverable (#232).
    monkeypatch.setattr("sdlc.build.story_commit_exists", lambda sid, root=None: True)
    monkeypatch.setattr(reconcile_mod, "_detect_landing", lambda sid, pr, base, root: None)
    assert _stage_artifact_exists("merge", "x", 100, root=tmp_path) is False


def test_merge_contract_error_with_landing_recovers_not_failed(
    tmp_path, monkeypatch
) -> None:
    """A merge whose result block is unparseable but whose PR landed parks
    NEEDS_ATTENTION, never hard FAILED (#232).

    ``story_commit_exists`` (a commit-ahead-of-base probe) cannot see a landing
    once the branch has merged/squashed away, so the pre-#232 classifier would
    fail the merge outright. Reusing reconcile's ``_detect_landing`` recognises
    the merged PR and preserves the work.
    """
    import sdlc.reconcile as reconcile_mod

    # No commit ahead of base (branch merged away) ...
    monkeypatch.setattr("sdlc.build.story_commit_exists", lambda sid, root=None: False)
    # ... but the PR provably landed.
    monkeypatch.setattr(
        reconcile_mod,
        "_detect_landing",
        lambda sid, pr, base, root: ("gh-pr-merged", "cafef00d"),
    )
    disp = _RaisingDispatcher(raise_on="merge", bugfix_fixed=False)
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp,
        preflight=lambda: True,
        # tmp_path has no .git: the #227 partition probe short-circuits so this
        # exercises mid-build merge-stage recovery, not the partition skip.
        root=tmp_path,
    )
    assert result.story_status["99.1-001"] == "NEEDS_ATTENTION"
    assert result.needs_attention == 1
    assert result.failed == 0


def test_merge_contract_error_without_artifact_still_fails(
    tmp_path, monkeypatch
) -> None:
    """No commit and no landing ⇒ unchanged hard FAILED (#232 regression guard)."""
    import sdlc.reconcile as reconcile_mod

    monkeypatch.setattr("sdlc.build.story_commit_exists", lambda sid, root=None: False)
    monkeypatch.setattr(
        reconcile_mod, "_detect_landing", lambda sid, pr, base, root: None
    )
    disp = _RaisingDispatcher(raise_on="merge", bugfix_fixed=False)
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.story_status["99.1-001"] == "FAILED"
    assert result.failed == 1
    assert result.needs_attention == 0


def test_merge_contract_error_unlanded_with_branch_commit_still_fails(
    tmp_path, monkeypatch
) -> None:
    """An unlanded merge is FAILED even though feature/<id> carries commits (#232).

    This is the realistic merge-stage state: build/coverage/review already
    authored commits, so ``story_commit_exists`` is true. A branch commit is NOT
    evidence the merge landed, so the merge artifact probe must rely solely on
    landing detection — otherwise a genuine merge failure (conflict, gh error)
    whose result block was lost would be masked as recoverable.
    """
    import sdlc.reconcile as reconcile_mod

    # Realistic: the branch carries the earlier reviewed commits ...
    monkeypatch.setattr("sdlc.build.story_commit_exists", lambda sid, root=None: True)
    # ... but the merge never landed.
    monkeypatch.setattr(
        reconcile_mod, "_detect_landing", lambda sid, pr, base, root: None
    )
    disp = _RaisingDispatcher(raise_on="merge", bugfix_fixed=False)
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp,
        preflight=lambda: True,
        root=tmp_path,
    )
    assert result.story_status["99.1-001"] == "FAILED"
    assert result.failed == 1
    assert result.needs_attention == 0


def test_recovered_merge_converges_to_done_on_reconcile(
    tmp_path, monkeypatch
) -> None:
    """reconcile converges a #232-parked merge to DONE once the landing shows.

    The recoverable NEEDS_ATTENTION is idempotent with ``sdlc reconcile``: a
    later reconcile re-checks the parked story against the landing and flips it
    to its true status (DONE), exactly as it would for any other parked story.
    """
    import sdlc.reconcile as reconcile_mod
    from sdlc.reconcile import reconcile_run

    monkeypatch.setattr("sdlc.build.story_commit_exists", lambda sid, root=None: False)
    monkeypatch.setattr(
        reconcile_mod,
        "_detect_landing",
        lambda sid, pr, base, root: ("gh-pr-merged", "cafef00d"),
    )
    db = tmp_path / "l.db"
    disp = _RaisingDispatcher(raise_on="merge", bugfix_fixed=False)
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
        # tmp_path has no .git: the #227 partition probe short-circuits so this
        # exercises mid-build merge-stage recovery, not the partition skip.
        root=tmp_path,
    )
    assert result.story_status["99.1-001"] == "NEEDS_ATTENTION"

    # fetch=False keeps the pass offline-deterministic; the monkeypatched
    # _detect_landing supplies the landing signal reconcile keys off.
    rec = reconcile_run(Ledger(db), root=tmp_path, fetch=False)
    assert [r["story_id"] for r in rec.reclassified] == ["99.1-001"]
    assert {
        r["story_id"]: r["status"] for r in Ledger(db).story_rows(rec.run_id)
    }["99.1-001"] == "DONE"


# ---------------------------------------------------------------------------
# Story 12.1-001 — recover a missing/malformed result envelope before parking
# ---------------------------------------------------------------------------

# The verbatim phrase the envelope re-ask prompt carries, used by the test
# dispatcher to tell a re-ask apart from the original stage dispatch.
_REASK_SENTINEL = "envelope-only re-ask"


class _EnvelopeReaskDispatcher:
    """An agent that omits the result envelope on the real stage but emits a
    valid one when re-asked for the envelope only (Story 12.1-001 AC1/AC4).

    The original stage dispatch raises ``ResultBlockError`` (missing envelope);
    the envelope-only re-ask — recognised by the sentinel phrase its prompt
    carries — returns a schema-valid, success-reporting result, so the stage is
    recovered without ever entering the bugfix path. ``reask_succeeds=False``
    makes the re-ask fail too, so recovery falls through to the bugfix path.
    """

    def __init__(
        self,
        stage: str = "build",
        reask_succeeds: bool = True,
        recover_after_bugfix: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.stage = stage
        self.reask_succeeds = reask_succeeds
        self.recover_after_bugfix = recover_after_bugfix
        self._bugfixed = False

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        from sdlc.contracts import ResultBlockError
        from sdlc.dispatch import AgentResult

        is_reask = _REASK_SENTINEL in prompt
        self.calls.append(
            (agent_type, getattr(story, "id", ""), "reask" if is_reask else "stage")
        )
        if agent_type == "bugfix":
            self._bugfixed = True
        if agent_type == self.stage and not is_reask:
            # The stage stays broken until a bugfix has run (when enabled).
            if not (self.recover_after_bugfix and self._bugfixed):
                raise ResultBlockError("missing <<<RESULT_JSON>>> marker")
        if agent_type == self.stage and is_reask and not self.reask_succeeds:
            raise ResultBlockError("re-ask still missing <<<RESULT_JSON>>> marker")
        return AgentResult(
            agent_type=agent_type, data=_default_payload(agent_type, story), raw=""
        )


def test_missing_envelope_recovered_by_reask(tmp_path) -> None:
    """A stage that omits its envelope is recovered by a re-ask — no bugfix (AC1/AC4)."""
    disp = _EnvelopeReaskDispatcher(stage="build", reask_succeeds=True)
    db = tmp_path / "l.db"
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.completed == 1
    assert result.failed == 0
    assert result.needs_attention == 0
    assert result.story_status["99.1-001"] == "DONE"
    # The envelope re-ask ran; the heavier bugfix path was never needed (AC1).
    assert any(kind == "reask" for _, _, kind in disp.calls)
    assert all(agent != "bugfix" for agent, _, _ in disp.calls)
    # The recovery attempt is recorded in the ledger events (AC3).
    conn = _open(db)
    msgs = [
        r[0]
        for r in conn.execute(
            "SELECT message FROM events WHERE story_id='99.1-001'"
        ).fetchall()
    ]
    assert any("re-ask" in m for m in msgs)


def test_reask_failure_falls_through_to_bugfix(tmp_path, monkeypatch) -> None:
    """When the re-ask fails too, recovery routes through the bugfix path (AC2)."""
    monkeypatch.setattr("sdlc.build.story_commit_exists", lambda sid, root=None: False)
    disp = _EnvelopeReaskDispatcher(
        stage="build", reask_succeeds=False, recover_after_bugfix=True
    )
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    # The re-ask is tried first; when it fails, the bounded bugfix path runs and
    # the retried stage then succeeds → the story completes, not stranded (AC2).
    assert any(kind == "reask" for _, _, kind in disp.calls)
    assert any(agent == "bugfix" for agent, _, _ in disp.calls)
    # The re-ask precedes the bugfix for the same failure (cheaper attempt first).
    kinds = [(agent, kind) for agent, _, kind in disp.calls]
    assert kinds.index(("build", "reask")) < kinds.index(("bugfix", "stage"))
    assert result.story_status["99.1-001"] == "DONE"


def test_envelope_recovery_records_each_attempt(tmp_path, monkeypatch) -> None:
    """Every recovery attempt (re-ask + bugfix) is logged to the ledger (AC3)."""
    monkeypatch.setattr("sdlc.build.story_commit_exists", lambda sid, root=None: False)
    disp = _EnvelopeReaskDispatcher(stage="build", reask_succeeds=False)
    db = tmp_path / "l.db"
    run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    conn = _open(db)
    # A 'reask' stage attempt row exists and an event names the re-ask.
    stages = [
        r[0]
        for r in conn.execute(
            "SELECT stage_name FROM stages WHERE story_id='99.1-001'"
        ).fetchall()
    ]
    assert "reask" in stages
    msgs = [
        r[0]
        for r in conn.execute(
            "SELECT message FROM events WHERE story_id='99.1-001'"
        ).fetchall()
    ]
    assert any("re-ask" in m for m in msgs)


def test_envelope_reask_prompt_is_envelope_only() -> None:
    """The re-ask prompt asks only for the result block — not a rebuild (AC1)."""
    from sdlc.build import render_envelope_reask_prompt
    from sdlc.contracts import RESULT_END_MARKER, RESULT_START_MARKER

    story = _story("99.1-001")
    prompt = render_envelope_reask_prompt("build", story, BuildOptions(), None)
    assert _REASK_SENTINEL in prompt
    assert RESULT_START_MARKER in prompt
    assert RESULT_END_MARKER in prompt
    # It validates against the build stage's own schema: the wrapper now embeds
    # that schema's literal required fields rather than naming the schema file.
    assert "branch_name" in prompt


class _ReaskNonSuccessDispatcher:
    """Stage omits its envelope; the re-ask returns a schema-valid response that
    nonetheless reports a *non-success* status (Story 12.1-001 AC2).

    This exercises the ``_reask_envelope`` branch where the re-ask yields a valid
    result but ``_stage_succeeded`` is False — recovery is not satisfied and must
    fall through to the bugfix path, which then recovers the stage.
    """

    def __init__(self, stage: str = "build") -> None:
        self.calls: list[tuple[str, str]] = []
        self.stage = stage
        self._bugfixed = False

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        from sdlc.contracts import ResultBlockError
        from sdlc.dispatch import AgentResult

        is_reask = _REASK_SENTINEL in prompt
        self.calls.append((agent_type, "reask" if is_reask else "stage"))
        if agent_type == "bugfix":
            self._bugfixed = True
        if agent_type == self.stage and not is_reask and not self._bugfixed:
            raise ResultBlockError("missing <<<RESULT_JSON>>> marker")
        payload = dict(_default_payload(agent_type, story))
        if agent_type == self.stage and is_reask:
            # Valid envelope, but it reports the stage did NOT succeed.
            payload["build_status"] = "FAILED"
        return AgentResult(agent_type=agent_type, data=payload, raw="")


def test_reask_non_success_status_falls_through_to_bugfix(tmp_path, monkeypatch) -> None:
    """A re-ask that reports a non-success status is a failed recovery (AC2)."""
    monkeypatch.setattr("sdlc.build.story_commit_exists", lambda sid, root=None: False)
    disp = _ReaskNonSuccessDispatcher(stage="build")
    db = tmp_path / "l.db"
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    # Re-ask ran and returned a result, but its non-success status routed recovery
    # through the bugfix path; the retried stage then completes the story (AC2).
    assert any(kind == "reask" for _, kind in disp.calls)
    assert any(agent == "bugfix" for agent, _ in disp.calls)
    assert result.story_status["99.1-001"] == "DONE"
    # The non-success re-ask is logged distinctly from a dispatch error (AC3).
    conn = _open(db)
    msgs = [
        r[0]
        for r in conn.execute(
            "SELECT message FROM events WHERE story_id='99.1-001'"
        ).fetchall()
    ]
    assert any("non-success" in m for m in msgs)


def test_envelope_reask_prompt_includes_pr_hint() -> None:
    """When a PR is already known, the re-ask names it so the agent reports it."""
    from sdlc.build import render_envelope_reask_prompt

    prompt = render_envelope_reask_prompt("build", _story("99.1-001"), BuildOptions(), 4242)
    assert "PR #4242" in prompt


def test_story_commit_exists_no_git_is_false(tmp_path) -> None:
    """The git probe never raises and returns False outside a git repo."""
    from sdlc.build import story_commit_exists

    assert story_commit_exists("99.1-001", root=tmp_path) is False


def test_prompts_show_verbatim_result_wrapper() -> None:
    """Every rendered prompt shows the exact sentinel wrapper and forbids fences."""
    from sdlc.build import (
        render_bugfix_prompt,
        render_build_prompt,
        render_coverage_prompt,
        render_merge_prompt,
        render_review_prompt,
    )
    from sdlc.contracts import RESULT_END_MARKER, RESULT_START_MARKER

    story = _story("99.1-001")
    opts = BuildOptions()
    prompts = [
        render_build_prompt(story, opts),
        render_coverage_prompt(story, opts),
        render_review_prompt(story, 7),
        render_merge_prompt(story, 7),
        render_bugfix_prompt(story, "build", "boom"),
    ]
    for p in prompts:
        assert RESULT_START_MARKER in p
        assert RESULT_END_MARKER in p
        assert "no markdown code fences" in p


def test_result_wrapper_forbids_background_handoff() -> None:
    """Run b8fdbc71 (story 27.1-003, merge attempt 3): the merge agent parked
    its CI wait on a background watcher plus a scheduled wakeup and ended the
    turn with no final text — in a one-shot headless dispatch the wakeup never
    fires, so the response carried no result block at all and the stage failed
    on a contract violation. The shared wrapper must spell the session model
    out to every dispatched agent."""
    from sdlc.contracts import _result_wrapper

    wrapper = _result_wrapper("merge-agent-response.schema.json")
    assert "one-shot" in wrapper
    assert "background" in wrapper
    assert "scheduled wakeup" in wrapper
    assert "blocking foreground" in wrapper


def test_merge_prompt_requires_synchronous_check_wait() -> None:
    """The rebase the merge prompt mandates restarts the change request's
    required checks; the agent must wait for them in the foreground and report
    FAILED honestly if still pending — never defer to a background wait."""
    from sdlc.build import render_merge_prompt

    prompt = render_merge_prompt(_story("99.1-001"), 7)
    assert "restarts" in prompt
    assert "blocking foreground" in prompt
    assert 'merge_status="FAILED"' in prompt


def test_review_prompt_distrusts_implementer_report() -> None:
    """Story 26.2-002: the review-stage prompt treats the implementer's
    self-report as unverified claims and bounds off-diff exploration to a
    concrete named risk (pattern: superpowers task-reviewer-prompt)."""
    from sdlc.build import render_review_prompt

    prompt = render_review_prompt(_story("99.1-001"), 7)
    assert "do not trust" in prompt.lower()
    assert "unverified claims" in prompt
    assert "kept it simple per YAGNI" in prompt
    assert "concrete named risk" in prompt
    # The distrust scope names each self-report surface, and off-diff
    # exploration is bounded to a named risk the reviewer must justify.
    assert "the PR description, commit" in prompt
    assert "Inspect code outside the diff only" in prompt
    assert "name both the risk and what you checked" in prompt


def test_review_prompt_distrust_survives_doc_currency_off(monkeypatch) -> None:
    """Story 26.2-002: the distrust hardening is unconditional — it must ride
    the review prompt even when the documentation-currency lens is disabled,
    exercising the ``docs_dimension`` else-branch it is emitted alongside."""
    from sdlc.build import render_review_prompt
    from sdlc.doc_currency import DOC_CURRENCY_ENV

    monkeypatch.setenv(DOC_CURRENCY_ENV, "off")
    prompt = render_review_prompt(_story("99.1-001"), None)
    # Docs lens off ⇒ no documentation-currency dimension...
    assert "documentation-currency dimension" not in prompt
    # ...but the distrust + bounded-exploration instructions still stand,
    # and the None PR number renders without error.
    assert "PR #None" in prompt
    assert "do not trust" in prompt.lower()
    assert "unverified claims" in prompt
    assert "concrete named risk" in prompt


# ---------------------------------------------------------------------------
# Over-engineering lens advisory dispatch (issue #445)
# ---------------------------------------------------------------------------

def _lens_config(tmp_path: Path, *, enabled: bool, command: str) -> Path:
    p = tmp_path / "overengineering-lens.yaml"
    p.write_text(
        f"enabled: {'true' if enabled else 'false'}\n"
        "policy: advisory\n"
        f"command: {command}\n",
        encoding="utf-8",
    )
    return p


def test_overengineering_lens_disabled_by_default_no_dispatch(tmp_path) -> None:
    """Disabled (the bundled default) ⇒ no lens event, no subprocess spend."""
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
    rows = conn.execute(
        "SELECT message FROM events WHERE message LIKE '%over-engineering lens%'"
    ).fetchall()
    assert rows == []


def test_overengineering_lens_enabled_surfaces_findings_advisory_only(
    tmp_path, monkeypatch
) -> None:
    """Enabled ⇒ dispatched after review, findings logged, story still merges."""
    script = tmp_path / "lens.sh"
    script.write_text(
        "#!/bin/sh\n"
        'printf \'{"summary": "found a cut", "findings": '
        '[{"category": "unused_code", "file": "src/x.py", "line": 3, '
        '"reason": "dead branch"}]}\'\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    config_path = _lens_config(tmp_path, enabled=True, command=str(script))
    monkeypatch.setattr(
        "sdlc.role_routing.bundled_config_path", lambda name: config_path
    )

    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    # Advisory only — never blocks: every story still reaches merge.
    assert result.completed == 3
    assert result.failed == 0

    conn = _open(db)
    rows = conn.execute(
        "SELECT level, message FROM events WHERE message LIKE '%over-engineering lens%'"
    ).fetchall()
    assert rows, "expected an advisory ledger event per reviewed story"
    assert all(level == "info" for level, _ in rows)
    joined = "\n".join(m for _, m in rows)
    assert "src/x.py:3" in joined
    assert "dead branch" in joined


def test_overengineering_lens_failure_never_fails_the_review_stage(
    tmp_path, monkeypatch
) -> None:
    """A lens that errors out is logged as a warning, not a stage failure."""
    config_path = _lens_config(
        tmp_path, enabled=True, command="definitely-not-a-real-binary-xyz"
    )
    monkeypatch.setattr(
        "sdlc.role_routing.bundled_config_path", lambda name: config_path
    )

    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    assert result.completed == 3
    assert result.failed == 0

    conn = _open(db)
    rows = conn.execute(
        "SELECT level, message FROM events WHERE message LIKE '%over-engineering lens%'"
    ).fetchall()
    assert rows, "expected the lens error to be logged"
    assert all(level == "warn" for level, _ in rows)
    assert all("ignored" in m for _, m in rows)


class _RecordingLedger:
    """Bare event_log recorder — exercises the helper without a real DB."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, str, str]] = []

    def event_log(
        self, run_id: str, story_id: str, level: str, source: str, message: str
    ) -> None:
        self.events.append((run_id, story_id, level, source, message))


def test_overengineering_advisory_noop_when_no_pr_yet(monkeypatch) -> None:
    """``pr_number is None`` ⇒ no-op before even resolving the lens config."""
    monkeypatch.setattr(
        "sdlc.role_routing.bundled_config_path",
        lambda name: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    ledger = _RecordingLedger()
    story = _sample_queue()[0]

    _dispatch_overengineering_advisory(story, None, ledger, "run-1")

    assert ledger.events == []


def test_overengineering_advisory_noop_when_config_unresolvable(monkeypatch) -> None:
    """Bundled config missing/unresolvable ⇒ no-op, no ledger event."""
    monkeypatch.setattr("sdlc.role_routing.bundled_config_path", lambda name: None)
    ledger = _RecordingLedger()
    story = _sample_queue()[0]

    _dispatch_overengineering_advisory(story, 42, ledger, "run-1")

    assert ledger.events == []


def test_overengineering_advisory_unexpected_error_logged_as_warn(
    tmp_path, monkeypatch
) -> None:
    """A non-lens exception (e.g. a bug in dispatch) is caught, never raised."""
    config_path = _lens_config(tmp_path, enabled=True, command="unused")
    monkeypatch.setattr(
        "sdlc.role_routing.bundled_config_path", lambda name: config_path
    )

    def _boom(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr("sdlc.overengineering.dispatch_overengineering_lens", _boom)
    ledger = _RecordingLedger()
    story = _sample_queue()[0]

    _dispatch_overengineering_advisory(story, 42, ledger, "run-1")

    assert len(ledger.events) == 1
    _, _, level, _, message = ledger.events[0]
    assert level == "warn"
    assert "unexpected error" in message
    assert "ignored" in message
    assert "boom" in message


# ---------------------------------------------------------------------------
# bugfix stage rows must use distinct attempt numbers (no UNIQUE collision)
# ---------------------------------------------------------------------------

class _FlakyDispatcher:
    """Fails named stages on their first dispatch, succeeds after; bugfix fixes."""

    def __init__(self, fail_first: set[str]) -> None:
        self.calls: list[tuple[str, str]] = []
        self.seen: dict[str, int] = {}
        self.fail_first = set(fail_first)

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        from sdlc.dispatch import AgentResult

        self.calls.append((agent_type, getattr(story, "id", "")))
        n = self.seen.get(agent_type, 0)
        self.seen[agent_type] = n + 1
        payload = dict(_default_payload(agent_type, story))
        if agent_type in self.fail_first and n == 0:
            if agent_type == "build":
                payload["build_status"] = "FAILED"
            elif agent_type == "coverage":
                payload["coverage_status"] = "FAIL"
        return AgentResult(agent_type=agent_type, data=payload, raw="")


def test_repeated_bugfix_same_stage_no_unique_collision(tmp_path) -> None:
    """Two bugfix rounds on one stage get distinct attempts (was a UNIQUE crash)."""
    disp = FakeDispatcher(
        overrides={
            ("build", "99.1-001"): {
                "branch_name": "feature/99.1-001",
                "build_status": "FAILED",
                "commit_sha": "x",
                "error_summary": "boom",
            }
        }
    )
    db = tmp_path / "l.db"
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.failed == 1  # build never passes → FAILED, but no crash
    rows = _open(db).execute(
        "SELECT attempt FROM stages WHERE stage_name='bugfix' ORDER BY attempt"
    ).fetchall()
    assert [r[0] for r in rows] == [1, 2]


def test_bugfix_across_stages_uses_distinct_attempts(tmp_path) -> None:
    """A bugfix in two different stages gets distinct attempts (cross-stage collision)."""
    disp = _FlakyDispatcher(fail_first={"build", "coverage"})
    db = tmp_path / "l.db"
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.completed == 1
    assert result.story_status["99.1-001"] == "DONE"
    rows = _open(db).execute(
        "SELECT attempt FROM stages WHERE stage_name='bugfix' ORDER BY attempt"
    ).fetchall()
    assert [r[0] for r in rows] == [1, 2]


# ---------------------------------------------------------------------------
# Token/cost capture: schema migration, ledger write, read API, wiring
# ---------------------------------------------------------------------------

def _old_schema_db(db: Path) -> None:
    """Create a pre-token-capture ledger: the full schema minus the six usage
    columns on `stages` (mirrors a real ledger built before this feature)."""
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, scope TEXT, started_at TIMESTAMP, "
        "  finished_at TIMESTAMP, mode TEXT, total_stories INTEGER DEFAULT 0, "
        "  completed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, status TEXT NOT NULL);"
        "CREATE TABLE stories (run_id TEXT, story_id TEXT, epic_id TEXT, title TEXT, "
        "  priority TEXT, points INTEGER, agent_type TEXT, branch TEXT, "
        "  pr_number INTEGER, current_stage TEXT, status TEXT NOT NULL, "
        "  PRIMARY KEY(run_id, story_id));"
        "CREATE TABLE stages (run_id TEXT, story_id TEXT, stage_name TEXT, "
        "  attempt INTEGER DEFAULT 1, status TEXT NOT NULL, started_at TIMESTAMP, "
        "  finished_at TIMESTAMP, failure_category TEXT, output_path TEXT, "
        "  PRIMARY KEY(run_id, story_id, stage_name, attempt));"
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, "
        "  story_id TEXT, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP, level TEXT NOT NULL, "
        "  source TEXT, message TEXT NOT NULL);"
        "CREATE TABLE _migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "  applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);"
    )
    conn.commit()
    conn.close()


def _stage_columns(db: Path) -> set[str]:
    import sqlite3
    with sqlite3.connect(db) as conn:
        return {r[1] for r in conn.execute("PRAGMA table_info(stages)").fetchall()}


_USAGE_COLS = {
    "session_id", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens", "cost_usd",
}


def test_init_migrates_existing_db_adds_usage_columns(tmp_path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db)
    assert not (_USAGE_COLS & _stage_columns(db))  # none present initially
    Ledger(db).init()
    assert _USAGE_COLS <= _stage_columns(db)


def test_init_migration_preserves_existing_rows(tmp_path) -> None:
    import sqlite3
    db = tmp_path / "old.db"
    _old_schema_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO runs(id, status) VALUES ('r1','DONE')")
        conn.execute("INSERT INTO stories(run_id, story_id, status) VALUES ('r1','s1','DONE')")
        conn.execute(
            "INSERT INTO stages(run_id, story_id, stage_name, attempt, status) "
            "VALUES ('r1','s1','build',1,'DONE')"
        )
    Ledger(db).init()
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status, input_tokens FROM stages WHERE run_id='r1'"
        ).fetchone()
    assert row[0] == "DONE"      # pre-existing data intact
    assert row[1] is None        # new column defaults NULL


def test_init_migration_is_idempotent(tmp_path) -> None:
    import sqlite3
    db = tmp_path / "old.db"
    _old_schema_db(db)
    Ledger(db).init()
    Ledger(db).init()  # second run must not raise (duplicate-column ALTER avoided)
    with sqlite3.connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM _migrations WHERE version=1").fetchone()[0]
    assert n == 1


def test_stage_set_usage_persists_and_breakdown_exposes_tokens(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "s1", "99", "S", "P1", 2, "py", "", None, "TODO")
    ledger.stage_start(run_id, "s1", "build", 1)
    ledger.stage_finish(run_id, "s1", "build", 1, "DONE")
    ledger.stage_set_usage(
        run_id, "s1", "build", 1, session_id="sess-x",
        input_tokens=100, output_tokens=20, cache_read_tokens=4000,
        cache_creation_tokens=300, cost_usd=0.07,
    )
    breakdown = ledger.stage_breakdown(run_id)
    build = breakdown["s1"][0]
    assert build["tokens"] == 4420
    assert build["cost_usd"] == 0.07
    assert build["session_id"] == "sess-x"


def test_list_runs_includes_token_and_cost_totals(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    runs = Ledger(db).list_runs()
    assert runs and runs[0]["total_tokens"] is not None
    assert runs[0]["total_tokens"] > 0
    assert runs[0]["total_cost_usd"] > 0


def test_list_runs_null_totals_when_no_usage(tmp_path) -> None:
    """A run whose stages recorded no usage reports None totals (renders '—')."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "s1", "99", "S", "P1", 2, "py", "", None, "DONE")
    ledger.stage_start(run_id, "s1", "build", 1)
    ledger.stage_finish(run_id, "s1", "build", 1, "DONE")
    runs = ledger.list_runs()
    assert runs[0]["total_tokens"] is None
    assert runs[0]["total_cost_usd"] is None


def test_status_snapshot_run_usage_and_per_story_totals(tmp_path) -> None:
    from sdlc.build import status_snapshot
    db = tmp_path / "ledger.db"
    run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    snap = status_snapshot(Ledger(db))
    usage = snap["run"]["usage"]
    assert usage is not None
    assert usage["total_tokens"] == usage["input"] + usage["output"] + usage["cache_read"] + usage["cache_creation"]
    assert usage["cost_usd"] > 0
    # Each story aggregates its stages (4 stages × sample tokens for a clean run).
    s = snap["stories"][0]
    assert s["tokens"] is not None and s["tokens"] > 0
    assert s["cost_usd"] is not None


def test_status_snapshot_skipped_story_cells_render_skipped(tmp_path) -> None:
    """A story skipped wholesale (no stage rows) renders all four cells SKIPPED,
    not PENDING; a real built story is unaffected (Issue #130)."""
    from sdlc.build import status_snapshot

    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    # Skipped story: already Done in a prior run, so no stage rows are written.
    ledger.story_upsert(run_id, "s1", "99", "S", "P1", 2, "py", "", None, "SKIPPED")
    # Normal story with a recorded build stage.
    ledger.story_upsert(run_id, "s2", "99", "S", "P1", 2, "py", "", None, "DONE")
    ledger.stage_start(run_id, "s2", "build", 1)
    ledger.stage_finish(run_id, "s2", "build", 1, "DONE")

    snap = status_snapshot(ledger, run_id)
    by_id = {s["story_id"]: s for s in snap["stories"]}

    skipped_stages = {st["name"]: st["status"] for st in by_id["s1"]["stages"]}
    assert skipped_stages == {
        "build": "SKIPPED",
        "coverage": "SKIPPED",
        "review": "SKIPPED",
        "merge": "SKIPPED",
    }

    # The built story keeps its real stage row and PENDING placeholders.
    built_stages = {st["name"]: st["status"] for st in by_id["s2"]["stages"]}
    assert built_stages["build"] == "DONE"
    assert built_stages["coverage"] == "PENDING"
    assert built_stages["merge"] == "PENDING"


def test_status_snapshot_exposes_run_and_story_durations(tmp_path) -> None:
    """Durations come from the persisted stage/run timestamps (Story 11.2-005)."""
    import sqlite3

    from sdlc.build import status_snapshot

    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "s1", "99", "S", "P1", 2, "py", "", None, "DONE")
    ledger.stage_start(run_id, "s1", "build", 1)
    ledger.stage_finish(run_id, "s1", "build", 1, "DONE")
    # Pin known timestamps so the computed span is deterministic.
    with sqlite3.connect(tmp_path / "ledger.db") as conn:
        conn.execute(
            "UPDATE runs SET started_at='2026-06-20 11:00:00', "
            "finished_at='2026-06-20 11:04:12' WHERE id=?",
            (run_id,),
        )
        conn.execute(
            "UPDATE stages SET started_at='2026-06-20 11:00:30', "
            "finished_at='2026-06-20 11:03:30' WHERE run_id=? AND story_id='s1'",
            (run_id,),
        )

    snap = status_snapshot(ledger, run_id)
    assert snap["run"]["duration_seconds"] == 252  # 4m 12s
    assert snap["stories"][0]["duration_seconds"] == 180  # 3m


def test_status_snapshot_in_progress_story_duration_is_elapsed(tmp_path) -> None:
    """An unfinished stage yields a positive, non-null elapsed-so-far span."""
    from sdlc.build import status_snapshot

    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "s1", "99", "S", "P1", 2, "py", "", None, "IN_PROGRESS")
    ledger.stage_start(run_id, "s1", "build", 1)  # started, not finished

    snap = status_snapshot(ledger, run_id)
    # In-progress run + in-flight story: both elapsed-so-far, never None/negative.
    assert snap["run"]["duration_seconds"] is not None
    assert snap["run"]["duration_seconds"] >= 0
    assert snap["stories"][0]["duration_seconds"] is not None
    assert snap["stories"][0]["duration_seconds"] >= 0


def test_list_runs_exposes_duration_seconds(tmp_path) -> None:
    """The runs-browser rows carry a duration for the overview (Story 11.2-005)."""
    import sqlite3

    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    with sqlite3.connect(tmp_path / "ledger.db") as conn:
        conn.execute(
            "UPDATE runs SET started_at='2026-06-20 11:00:00', "
            "finished_at='2026-06-20 12:03:00' WHERE id=?",
            (run_id,),
        )
    rows = ledger.list_runs()
    assert rows[0]["duration_seconds"] == 3780  # 1h 03m


def test_run_build_records_usage_on_stage_rows(tmp_path) -> None:
    import sqlite3
    db = tmp_path / "ledger.db"
    run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens, "
            "cache_creation_tokens, cost_usd, session_id FROM stages "
            "WHERE story_id='s1-001' AND stage_name='build' AND attempt=1"
        ).fetchone()
    assert rows == (100, 20, 4000, 300, 0.05, "sess-build")


# ---------------------------------------------------------------------------
# Live running token/cost accrual per story+stage (Story 11.1-003)
# ---------------------------------------------------------------------------

def _assistant_usage(session_id: str | None = None, **usage: int) -> dict:
    event: dict = {"type": "assistant", "message": {"content": [], "usage": usage}}
    if session_id is not None:
        event["session_id"] = session_id
    return event


def _stage_usage_row(ledger: Ledger, run_id: str, story_id: str, stage: str, attempt: int = 1):
    breakdown = ledger.stage_breakdown(run_id)
    for row in breakdown.get(story_id, []):
        if row["name"] == stage and row["attempt"] == attempt:
            return row
    raise AssertionError(f"no {stage} attempt {attempt} for {story_id}")


def test_progress_sink_accrues_running_usage_mid_stage(tmp_path) -> None:
    """The sink writes a running token total to the stage row as events arrive."""
    from sdlc.build import _make_progress_sink

    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "s1", "99", "S", "P1", 2, "py", "", None, "IN_PROGRESS")
    ledger.stage_start(run_id, "s1", "build", 1)

    sink = _make_progress_sink(ledger, run_id, "s1", "build", 1)
    sink(_assistant_usage(session_id="sess-1", input_tokens=100, output_tokens=20))
    # Visible mid-stage, before the stage finishes.
    row = _stage_usage_row(ledger, run_id, "s1", "build")
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 20
    assert row["session_id"] == "sess-1"

    # A second turn accrues on top of the first (running total grows).
    sink(_assistant_usage(input_tokens=150, output_tokens=30))
    row = _stage_usage_row(ledger, run_id, "s1", "build")
    assert row["input_tokens"] == 250
    assert row["output_tokens"] == 50


def test_progress_sink_without_usage_leaves_columns_null(tmp_path) -> None:
    """A streamed agent that carries no usage leaves the row NULL (renders '—')."""
    from sdlc.build import _make_progress_sink

    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "s1", "99", "S", "P1", 2, "py", "", None, "IN_PROGRESS")
    ledger.stage_start(run_id, "s1", "build", 1)

    sink = _make_progress_sink(ledger, run_id, "s1", "build", 1)
    # A real `system` init event carries a session_id but no usage — it must not
    # write an all-zero token row (regression: would render "0" instead of "—").
    sink({"type": "system", "subtype": "init", "session_id": "sess-1"})
    sink({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})

    row = _stage_usage_row(ledger, run_id, "s1", "build")
    assert row["tokens"] is None
    assert row["cost_usd"] is None
    assert row["session_id"] is None


def test_final_usage_reconciles_over_live_accrual(tmp_path) -> None:
    """On stage completion the authoritative total overwrites the accrued figure."""
    from sdlc.build import _make_progress_sink, _record_stage_usage
    from sdlc.dispatch import AgentResult

    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(run_id, "s1", "99", "S", "P1", 2, "py", "", None, "IN_PROGRESS")
    ledger.stage_start(run_id, "s1", "build", 1)

    # Live accrual records a partial running figure.
    sink = _make_progress_sink(ledger, run_id, "s1", "build", 1)
    sink(_assistant_usage(input_tokens=100, output_tokens=20))
    sink(_assistant_usage(input_tokens=150, output_tokens=30))

    # Stage completes: the final result envelope carries the authoritative total.
    result = AgentResult(
        agent_type="build",
        data={},
        raw="",
        usage={
            "input_tokens": 260,
            "output_tokens": 55,
            "cache_read_input_tokens": 4000,
            "cache_creation_input_tokens": 300,
        },
        cost_usd=0.09,
        session_id="sess-final",
    )
    _record_stage_usage(ledger, run_id, "s1", "build", 1, result)

    row = _stage_usage_row(ledger, run_id, "s1", "build")
    # Final value wins — not the sum of live (250/50) and final (260/55).
    assert row["input_tokens"] == 260
    assert row["output_tokens"] == 55
    assert row["cache_read_tokens"] == 4000
    assert row["cost_usd"] == 0.09
    assert row["session_id"] == "sess-final"


def test_live_accrual_surfaces_in_status_snapshot(tmp_path) -> None:
    """Per-run + per-story usage breakdown reflects live accrual before completion."""
    from sdlc.build import _make_progress_sink, status_snapshot

    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.story_upsert(run_id, "s1", "99", "S", "P1", 2, "py", "", None, "IN_PROGRESS")
    ledger.stage_start(run_id, "s1", "build", 1)

    sink = _make_progress_sink(ledger, run_id, "s1", "build", 1)
    sink(_assistant_usage(input_tokens=100, output_tokens=20))

    snap = status_snapshot(ledger, run_id)
    story = next(s for s in snap["stories"] if s["story_id"] == "s1")
    assert story["tokens"] == 120
    assert snap["run"]["usage"]["total_tokens"] == 120


def test_read_api_tolerates_unmigrated_db(tmp_path) -> None:
    """list_runs/status_snapshot read a pre-token ledger without crashing.

    The dashboard reads read-only and never migrates; an old DB (no usage
    columns) must render with None totals, not raise 'no such column'.
    """
    import sqlite3
    from sdlc.build import status_snapshot
    db = tmp_path / "old.db"
    _old_schema_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO runs(id, scope, status, mode) VALUES ('r1','s','DONE','serial')")
        conn.execute("INSERT INTO stories(run_id, story_id, status) VALUES ('r1','s1','DONE')")
        conn.execute(
            "INSERT INTO stages(run_id, story_id, stage_name, attempt, status) "
            "VALUES ('r1','s1','build',1,'DONE')"
        )
    ledger = Ledger(db)  # NOT init()'d — simulates a viewer on an old ledger
    runs = ledger.list_runs()
    assert runs[0]["total_tokens"] is None and runs[0]["total_cost_usd"] is None
    snap = status_snapshot(ledger, "r1")
    assert snap["run"]["usage"] is None
    assert snap["stories"][0]["tokens"] is None
    assert snap["stories"][0]["stages"][0]["tokens"] is None


# ---------------------------------------------------------------------------
# Story 11.1-002 — sub-stage progress events to the ledger
# ---------------------------------------------------------------------------

_EVENT_PROGRESS_COLS = {"stage", "kind"}


def _event_columns(db: Path) -> set[str]:
    import sqlite3
    with sqlite3.connect(db) as conn:
        return {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}


def test_init_migrates_existing_db_adds_event_progress_columns(tmp_path) -> None:
    db = tmp_path / "old.db"
    _old_schema_db(db)
    assert not (_EVENT_PROGRESS_COLS & _event_columns(db))  # none present initially
    Ledger(db).init()
    assert _EVENT_PROGRESS_COLS <= _event_columns(db)


def test_event_migration_preserves_existing_rows(tmp_path) -> None:
    import sqlite3
    db = tmp_path / "old.db"
    _old_schema_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO events(run_id, story_id, level, source, message) "
            "VALUES ('r1','s1','info','controller','hello')"
        )
    Ledger(db).init()
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT message, stage, kind FROM events").fetchone()
    assert row[0] == "hello"   # pre-existing audit row intact
    assert row[1] is None and row[2] is None  # new columns default NULL


def test_progress_log_persists_kind_and_stage(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()  # events has no FK, so a bare progress row needs no run/story
    ledger.progress_log("r1", "11.1-002", "build", "file_changed", "editing cli.py")
    latest = ledger.latest_progress("r1")
    assert latest["11.1-002"]["kind"] == "file_changed"
    assert latest["11.1-002"]["stage"] == "build"
    assert latest["11.1-002"]["message"] == "editing cli.py"


def test_latest_progress_keeps_only_newest_per_story(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    ledger.progress_log("r1", "s1", "build", "agent_started", "agent started")
    ledger.progress_log("r1", "s1", "build", "file_changed", "editing a.py")
    ledger.progress_log("r1", "s2", "coverage", "test_run", "running tests")
    latest = ledger.latest_progress("r1")
    assert latest["s1"]["message"] == "editing a.py"   # newest for s1 wins
    assert latest["s2"]["kind"] == "test_run"


def test_progress_events_excluded_from_recent_events(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    ledger.event_log("r1", "s1", "info", "controller", "stage started")
    ledger.progress_log("r1", "s1", "build", "tool_use", "$ git status")
    recent = ledger.recent_events("r1")
    messages = [e["message"] for e in recent]
    assert "stage started" in messages
    assert "$ git status" not in messages  # progress never floods the audit log


def test_latest_progress_tolerates_unmigrated_db(tmp_path) -> None:
    """A read-only viewer on a pre-11.1-002 ledger gets {}, not a crash."""
    db = tmp_path / "old.db"
    _old_schema_db(db)
    assert Ledger(db).latest_progress("r1") == {}


def test_latest_progress_returns_empty_when_db_absent(tmp_path) -> None:
    """No ledger file yet (status polled before the run wrote it) → {}, not a crash."""
    db = tmp_path / "never-created.db"
    assert not db.exists()
    assert Ledger(db).latest_progress("r1") == {}


class _StreamingDispatcher:
    """A fake dispatcher that replays stream events into ``on_progress``.

    Models the real streamed dispatch (Story 11.1-001/002): each canned event is
    handed to the controller's progress sink before a successful AgentResult is
    returned, so the wiring from dispatch → ledger progress rows is exercised
    end-to-end without a real subprocess.
    """

    def __init__(self, events) -> None:
        self.events = events

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        from sdlc.dispatch import AgentResult

        sink = kwargs.get("on_progress")
        if sink is not None:
            for ev in self.events:
                sink(ev)
        return AgentResult(
            agent_type=agent_type,
            data=_default_payload(agent_type, story),
            raw="",
        )


def test_streamed_stage_records_substage_activity(tmp_path) -> None:
    """A build run with a streaming dispatcher lands sub-stage progress rows."""
    db = tmp_path / "ledger.db"
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/cli.py"}}
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "uv run pytest"}}
        ]}},
    ]
    opts = BuildOptions(scope="11.1-002", skip_coverage=True, skip_preflight=True)
    run_build(
        opts,
        queue=[_story("11.1-002")],
        ledger=Ledger(db),
        dispatcher=_StreamingDispatcher(events),
        preflight=lambda: True,
        root=tmp_path,  # hermetic: keep the #227 git-landed probe off the real repo
    )
    rid = Ledger(db).latest_run_id()
    latest = Ledger(db).latest_progress(rid)
    # The last streamed milestone for the story is the test run.
    assert latest["11.1-002"]["kind"] == "test_run"


def test_substage_activity_surfaces_in_status_snapshot(tmp_path) -> None:
    """status_snapshot attaches the latest sub-stage activity to each story."""
    from sdlc.build import status_snapshot
    db = tmp_path / "ledger.db"
    ledger = Ledger(db)
    ledger.init()
    rid = ledger.run_create("11.1-002", "serial")
    ledger.story_upsert(
        rid, "s1", "epic-11", "Sub-stage story", "Should", 3,
        "python-backend-engineer", "feature/s1", None, "IN_PROGRESS",
    )
    ledger.stage_start(rid, "s1", "build", 1)
    ledger.progress_log(rid, "s1", "build", "file_changed", "editing cli.py")
    snap = status_snapshot(ledger, rid)
    story = next(s for s in snap["stories"] if s["story_id"] == "s1")
    assert story["activity"]["message"] == "editing cli.py"


def test_inflight_story_marked_in_progress_during_dispatch(tmp_path) -> None:
    """A story is IN_PROGRESS (not TODO) while its stages run, so `sdlc status`
    can show live sub-stage activity for it (Story 11.1-002).

    Regression: previously a story went TODO → terminal with no IN_PROGRESS
    window, so the status view never rendered activity for a real in-flight
    build and `counts.in_progress` was always 0 mid-run.
    """
    from sdlc.build import status_snapshot
    db = tmp_path / "ledger.db"
    captured: dict = {}

    class _Capturing:
        def __call__(self, agent_type, prompt, story=None, **kwargs):
            from sdlc.dispatch import AgentResult
            sink = kwargs.get("on_progress")
            if sink is not None:
                sink({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "cli.py"}}
                ]}})
            if agent_type == "build":
                # Observe the run state *while* the build stage is executing.
                lg = Ledger(db)
                rid = lg.latest_run_id()
                captured["snap"] = status_snapshot(lg, rid)
            return AgentResult(
                agent_type=agent_type,
                data=_default_payload(agent_type, story),
                raw="",
            )

    run_build(
        BuildOptions(scope="s1", skip_coverage=True, skip_preflight=True),
        queue=[_story("s1")],
        ledger=Ledger(db),
        dispatcher=_Capturing(),
        preflight=lambda: True,
    )
    snap = captured["snap"]
    story = next(s for s in snap["stories"] if s["story_id"] == "s1")
    assert story["status"] == "IN_PROGRESS"          # in-flight, not TODO
    assert snap["counts"]["in_progress"] == 1        # count is accurate mid-run
    assert story["activity"]["message"] == "editing cli.py"  # renders activity


# ---------------------------------------------------------------------------
# Commit-message commitlint enforcement (Story 12.2-002)
# ---------------------------------------------------------------------------

# A minimal ruleset standing in for the repo's .commitlintrc.json.
_COMMITLINT_RULES = {
    "rules": {
        "type-enum": [2, "always", ["feat", "fix", "chore", "docs", "test"]],
        "type-empty": [2, "never"],
        "subject-empty": [2, "never"],
        "subject-case": [2, "always", "lower-case"],
        "subject-full-stop": [2, "never", "."],
        "header-max-length": [2, "always", 72],
    }
}

# A header that breaks subject-case (capital) and subject-full-stop.
_BAD_COMMIT = "feat(controller): Add the thing."
_GOOD_COMMIT = "feat(controller): add the thing"


class _CommitLintDispatcher:
    """An agent whose freshly-authored commit is amended on a commit-lint re-ask.

    Tracks ``compliant`` head state so the monkeypatched ``_commit_message`` knows
    which header to return. Each commit-authoring **stage** dispatch leaves a
    fresh (non-compliant) commit; a commit-lint **re-ask** (recognised by the
    sentinel word its prompt carries) amends it compliant — unless
    ``fix_on_reask=False``, which keeps it broken to exercise the bounded
    exhaustion path. Modelling per-commit state (rather than a single latch) lets
    the build *and* coverage commits each be linted independently.
    """

    def __init__(self, fix_on_reask: bool = True, start_compliant: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fix_on_reask = fix_on_reask
        self.compliant = start_compliant

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        from sdlc.dispatch import AgentResult

        is_lint = "commitlint" in prompt
        self.calls.append(
            (agent_type, getattr(story, "id", ""), "commitlint" if is_lint else "stage")
        )
        if is_lint:
            self.compliant = self.fix_on_reask  # re-ask amends iff allowed
        elif agent_type in ("build", "coverage", "bugfix"):
            self.compliant = False  # a new commit-authoring stage → fresh commit
        return AgentResult(
            agent_type=agent_type, data=_default_payload(agent_type, story), raw=""
        )


def _patch_commitlint(monkeypatch, disp, config, *, good=_GOOD_COMMIT, bad=_BAD_COMMIT):
    monkeypatch.setattr("sdlc.build.load_commitlint_config", lambda root: config)
    monkeypatch.setattr(
        "sdlc.build._commit_message",
        lambda ref, root=None: good if disp.compliant else bad,
    )


def test_noncompliant_commit_triggers_reask_and_recovers(tmp_path, monkeypatch) -> None:
    """A commitlint-violating build commit is amended via a bounded re-ask (AC1)."""
    disp = _CommitLintDispatcher(fix_on_reask=True)
    _patch_commitlint(monkeypatch, disp, _COMMITLINT_RULES)
    db = tmp_path / "l.db"
    result = run_build(
        BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.story_status["99.1-001"] == "DONE"
    # A commit-lint re-ask was dispatched against the build agent.
    assert any(kind == "commitlint" for _, _, kind in disp.calls)
    # It is recorded as a 'commitlint' stage row and a compliance event (AC1).
    conn = _open(db)
    stages = [
        r[0] for r in conn.execute(
            "SELECT stage_name FROM stages WHERE story_id='99.1-001'"
        ).fetchall()
    ]
    assert "commitlint" in stages
    msgs = [
        r[0] for r in conn.execute(
            "SELECT message FROM events WHERE story_id='99.1-001'"
        ).fetchall()
    ]
    assert any("violates commitlint" in m for m in msgs)
    assert any("commitlint-compliant" in m for m in msgs)


def test_no_commitlint_config_is_a_noop(tmp_path, monkeypatch) -> None:
    """With no commitlint config the controller invents no rules (AC2)."""
    disp = _CommitLintDispatcher()
    monkeypatch.setattr("sdlc.build.load_commitlint_config", lambda root: None)
    result = run_build(
        BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.story_status["99.1-001"] == "DONE"
    assert all(kind != "commitlint" for _, _, kind in disp.calls)


def test_compliant_commit_has_no_reask(tmp_path, monkeypatch) -> None:
    """A compliant build commit changes nothing — no re-ask (AC3)."""
    disp = _CommitLintDispatcher()
    # _commit_message always returns a compliant header regardless of `amended`.
    _patch_commitlint(monkeypatch, disp, _COMMITLINT_RULES, bad=_GOOD_COMMIT)
    result = run_build(
        BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.story_status["99.1-001"] == "DONE"
    assert all(kind != "commitlint" for _, _, kind in disp.calls)


def test_exhausted_commit_lint_parks_needs_attention(tmp_path, monkeypatch) -> None:
    """An unfixable message is bounded, then parked — never advanced to a PR."""
    from sdlc.build import MAX_COMMITLINT_REASK

    disp = _CommitLintDispatcher(fix_on_reask=False)  # re-ask never amends
    _patch_commitlint(monkeypatch, disp, _COMMITLINT_RULES)
    db = tmp_path / "l.db"
    result = run_build(
        BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    # The story is parked, not advanced: a non-compliant header must not reach a PR.
    assert result.story_status["99.1-001"] == "NEEDS_ATTENTION"
    # The pipeline stopped at the build gate — review/merge never ran (work preserved).
    advanced = {agent for agent, _, _ in disp.calls}
    assert "review" not in advanced and "merge" not in advanced
    # The re-ask was bounded by MAX_COMMITLINT_REASK.
    lint_calls = [c for c in disp.calls if c[2] == "commitlint"]
    assert len(lint_calls) == MAX_COMMITLINT_REASK
    msgs = [
        r[0] for r in _open(db).execute(
            "SELECT message FROM events WHERE story_id='99.1-001'"
        ).fetchall()
    ]
    assert any("still violates commitlint" in m for m in msgs)


def test_commit_lint_park_blocks_dependents(tmp_path, monkeypatch) -> None:
    """A dependent of a commit-lint-parked story is BLOCKED, not built on unmerged work."""
    disp = _CommitLintDispatcher(fix_on_reask=False)  # parks 99.1-001 NEEDS_ATTENTION
    _patch_commitlint(monkeypatch, disp, _COMMITLINT_RULES)
    db = tmp_path / "l.db"
    result = run_build(
        BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True, sequential=True),
        queue=[_story("99.1-001"), _story("99.1-002", deps=["99.1-001"])],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.story_status["99.1-001"] == "NEEDS_ATTENTION"
    # The dependent is blocked: its dependency's work is committed but unmerged.
    assert result.story_status["99.1-002"] == "BLOCKED"
    # The dependent was never dispatched (no build attempt against unmerged work).
    assert all(sid != "99.1-002" for _, sid, _ in disp.calls)


def test_bugfix_stage_commit_is_also_linted(tmp_path, monkeypatch) -> None:
    """The bugfix agent's commit is linted too — not only pipeline stages (AC1)."""
    state = {"build_attempts": 0}

    class _BuildThenBugfix:
        """Build fails once → bugfix fixes it → both author lintable commits."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.compliant = False

        def __call__(self, agent_type, prompt, story=None, **kwargs):
            from sdlc.dispatch import AgentResult

            is_lint = "commitlint" in prompt
            self.calls.append((agent_type, "commitlint" if is_lint else "stage"))
            if is_lint:
                self.compliant = True
                return AgentResult(agent_type, _default_payload(agent_type, story), "")
            self.compliant = False  # any stage dispatch leaves a fresh commit
            if agent_type == "build":
                state["build_attempts"] += 1
                if state["build_attempts"] == 1:
                    return AgentResult(
                        "build",
                        {
                            "branch_name": "feature/99.1-001",
                            "build_status": "FAILED",
                            "commit_sha": "0",
                            "error_summary": "boom",
                        },
                        "",
                    )
            return AgentResult(agent_type, _default_payload(agent_type, story), "")

    disp = _BuildThenBugfix()
    _patch_commitlint(monkeypatch, disp, _COMMITLINT_RULES)
    result = run_build(
        BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True, sequential=True, auto=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.story_status["99.1-001"] == "DONE"
    # The bugfix agent ran and its commit was linted (a 'bugfix' commit-lint re-ask).
    assert ("bugfix", "stage") in disp.calls
    assert ("bugfix", "commitlint") in disp.calls


def test_envelope_recovered_commit_is_linted(tmp_path, monkeypatch) -> None:
    """An envelope-recovered build commit is still commitlint-checked (AC1).

    A stage that drops its result envelope is recovered by the 12.1-001
    envelope-only re-ask; that path must not smuggle a non-compliant header
    past the commit-lint gate to the PR.
    """
    class _EnvelopeThenLint:
        """Build omits its envelope, recovers on the envelope re-ask, then its
        (non-compliant) commit is linted and amended on a commit-lint re-ask."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.compliant = False

        def __call__(self, agent_type, prompt, story=None, **kwargs):
            from sdlc.contracts import ResultBlockError
            from sdlc.dispatch import AgentResult

            is_lint = "commitlint" in prompt
            is_env_reask = _REASK_SENTINEL in prompt
            kind = "commitlint" if is_lint else ("envelope" if is_env_reask else "stage")
            self.calls.append((agent_type, kind))
            if is_lint:
                self.compliant = True
                return AgentResult(agent_type, _default_payload(agent_type, story), "")
            # The first plain build dispatch drops its envelope (ContractError).
            if agent_type == "build" and not is_env_reask:
                raise ResultBlockError("missing <<<RESULT_JSON>>> marker")
            self.compliant = False  # recovered commit is non-compliant
            return AgentResult(agent_type, _default_payload(agent_type, story), "")

    disp = _EnvelopeThenLint()
    _patch_commitlint(monkeypatch, disp, _COMMITLINT_RULES)
    result = run_build(
        BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.story_status["99.1-001"] == "DONE"
    # The envelope re-ask recovered the stage, and its commit was still linted.
    assert ("build", "envelope") in disp.calls
    assert ("build", "commitlint") in disp.calls


def test_commit_lint_reask_prompt_is_amend_only() -> None:
    """The re-ask prompt asks only to amend the message, not change code."""
    from sdlc.build import render_commit_lint_reask_prompt
    from sdlc.contracts import RESULT_END_MARKER, RESULT_START_MARKER

    prompt = render_commit_lint_reask_prompt(
        "build", _story("99.1-001"), _BAD_COMMIT,
        ["subject-case: ...", "subject-full-stop: ..."],
    )
    assert "commit --amend" in prompt
    assert "Do NOT change any code" in prompt
    assert "subject-case" in prompt and "subject-full-stop" in prompt
    assert RESULT_START_MARKER in prompt and RESULT_END_MARKER in prompt


def test_commit_lint_reask_prompt_uses_stage_schema() -> None:
    """The re-ask validates against the re-asked stage's own schema."""
    from sdlc.build import render_commit_lint_reask_prompt

    # Each re-ask embeds its stage schema's literal required fields (the wrapper
    # no longer names the schema file), so a stage-specific field proves routing.
    cov = render_commit_lint_reask_prompt("coverage", _story("99.1-001"), _BAD_COMMIT, ["x"])
    assert "coverage_pct" in cov
    assert "'coverage'" in cov
    bug = render_commit_lint_reask_prompt("bugfix", _story("99.1-001"), _BAD_COMMIT, ["x"])
    assert "failure_category" in bug


def test_coverage_stage_commit_is_also_linted(tmp_path, monkeypatch) -> None:
    """Commit-lint covers the coverage agent's commit, not just build (AC1)."""
    disp = _CommitLintDispatcher(fix_on_reask=True)
    _patch_commitlint(monkeypatch, disp, _COMMITLINT_RULES)
    db = tmp_path / "l.db"
    result = run_build(
        # Coverage stage runs (not skipped) so its commit is linted too.
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.story_status["99.1-001"] == "DONE"
    # The commit-lint re-ask was dispatched against both build and coverage.
    lint_agents = {agent for agent, _, kind in disp.calls if kind == "commitlint"}
    assert {"build", "coverage"} <= lint_agents


def test_commit_message_reads_branch_head(monkeypatch) -> None:
    """``_commit_message`` returns the tip message, trimming the trailing newline."""
    import subprocess

    from sdlc.build import _commit_message

    def _fake_git(root, *args):
        assert args[:2] == ("log", "-1")
        return subprocess.CompletedProcess(args, 0, stdout="feat: x\n\nbody\n", stderr="")

    monkeypatch.setattr("sdlc.build._git", _fake_git)
    assert _commit_message("feature/99.1-001", Path("/repo")) == "feat: x\n\nbody"


def test_commit_message_none_on_git_failure(monkeypatch) -> None:
    """A non-zero git exit makes ``_commit_message`` degrade to ``None``."""
    import subprocess

    from sdlc.build import _commit_message

    monkeypatch.setattr(
        "sdlc.build._git",
        lambda root, *args: subprocess.CompletedProcess(args, 128, stdout="", stderr="no ref"),
    )
    assert _commit_message("feature/missing") is None


def test_commit_message_none_on_git_error(monkeypatch) -> None:
    """``_commit_message`` swallows OS/subprocess errors and returns ``None``."""
    import subprocess

    from sdlc.build import _commit_message

    def _boom(root, *args):
        raise subprocess.SubprocessError("git vanished")

    monkeypatch.setattr("sdlc.build._git", _boom)
    assert _commit_message("feature/99.1-001") is None


def test_unreadable_commit_skips_lint(tmp_path, monkeypatch) -> None:
    """An unreadable commit message degrades the lint gate to a no-op (R10)."""
    disp = _CommitLintDispatcher()
    monkeypatch.setattr("sdlc.build.load_commitlint_config", lambda root: _COMMITLINT_RULES)
    # The HEAD message can never be read → lint is skipped, build still completes.
    monkeypatch.setattr("sdlc.build._commit_message", lambda ref, root=None: None)
    result = run_build(
        BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.story_status["99.1-001"] == "DONE"
    assert all(kind != "commitlint" for _, _, kind in disp.calls)


def _kind_of(prompt: str) -> str:
    """Classify a dispatched prompt for the commit-lint fixtures (12.2-004)."""
    if "envelope-only re-ask" in prompt:
        return "envelope"
    if "commitlint" in prompt:
        return "commitlint"
    return "stage"


def test_build_prompt_commit_subject_is_compliant_by_construction() -> None:
    """The build prompt supplies a compliant header, not the raw title (12.2-004 AC1/AC2)."""
    from sdlc.build import render_build_prompt

    # A long, Title-Case title that would blow header-max-length + subject-case.
    long_title = (
        "Reconcile Story Status Against Origin Main And Recompute The Run Terminal"
    )
    story = Story(
        "12.3-001", long_title, "12", "controller-robustness",
        "docs/stories/epic-12.md", "Must", 8, "py", [], False,
    )
    prompt = render_build_prompt(story, BuildOptions())
    header = next(
        line.strip()
        for line in prompt.splitlines()
        if line.strip().startswith("feat(controller-robustness):")
    )
    # The constructed header passes commitlint by construction — no re-ask needed.
    assert lint_commit_message(header, _COMMITLINT_RULES) == []
    # The (#id) tag reconciliation keys off is preserved.
    assert header.endswith("(#12.3-001)")
    # The raw Title-Case title is never used verbatim as the commit subject.
    assert f"feat(controller-robustness): {long_title}" not in prompt


def test_build_prompt_aborts_rather_than_committing_off_feature_branch() -> None:
    """Issue #214: branch-creation failure must fail the build, never commit elsewhere."""
    from sdlc.build import render_build_prompt

    prompt = render_build_prompt(_story("99.1-001"), BuildOptions())
    assert "BUILD_STATUS: FAILED" in prompt
    # The instruction must tie the failure to branch creation and forbid a fallback commit.
    lowered = prompt.lower()
    assert "branch" in lowered
    assert "do not commit" in lowered


# Story 23.2-001: the build's change-request prompts open a PR on GitHub and an
# MR on GitLab, routed through the adapter's change-request terms.


def test_build_prompt_github_is_unchanged_pr_wording() -> None:
    """The default (GitHub) build prompt still says PR — byte-identical (AC2)."""
    from sdlc.build import render_build_prompt

    prompt = render_build_prompt(_story("23.2-001"), BuildOptions(skip_coverage=True))
    assert "Push and create PR; include the PR number in the result block." in prompt
    assert "MR" not in prompt
    assert "glab" not in prompt


def test_build_prompt_gitlab_opens_a_merge_request() -> None:
    """On a GitLab target the build agent opens an MR via glab (AC1/AC2)."""
    from sdlc.build import render_build_prompt
    from sdlc.issue_host import GITLAB_CR_TERMS

    prompt = render_build_prompt(
        _story("23.2-001"), BuildOptions(skip_coverage=True), cr_terms=GITLAB_CR_TERMS,
    )
    assert "Push and create MR (`glab mr create`); include the MR iid in the result block." in prompt
    # The GitHub PR wording is gone on the GitLab path.
    assert "create PR" not in prompt


def test_build_prompt_close_link_uses_host_noun() -> None:
    """The close-link instruction follows the host noun (PR vs MR)."""
    from sdlc.build import render_build_prompt
    from sdlc.issue_host import GITLAB_CR_TERMS

    gh = render_build_prompt(
        _story("23.2-001"), BuildOptions(skip_coverage=True), close_link="Closes #7",
    )
    assert 'When you open the PR, include "Closes #7"' in gh
    gl = render_build_prompt(
        _story("23.2-001"), BuildOptions(skip_coverage=True),
        close_link="Closes #7", cr_terms=GITLAB_CR_TERMS,
    )
    assert 'When you open the MR, include "Closes #7"' in gl
    assert "merging the MR auto-closes" in gl


def test_build_prompt_cuts_branch_from_supplied_base_ref() -> None:
    """A GitLab build cuts feature/<id> from the host default branch (AC3)."""
    from sdlc.build import render_build_prompt

    prompt = render_build_prompt(
        _story("23.2-001"), BuildOptions(), base_ref="origin/develop",
    )
    assert "git checkout -b feature/23.2-001 origin/develop" in prompt


# Story 27.3-002: the story's own epic section is embedded into the build and
# coverage prompts (replacing the read-the-epic instruction); oversized or
# missing sections fall back to today's read-it-yourself prompt — a truncated
# spec is never injected.

_SECTION_27 = (
    "##### Story 27.3-002: Story-section injection\n"
    "**Priority**: Must Have\n"
    "**Story Points**: 3\n\n"
    "**Acceptance Criteria**:\n"
    "- **Given** the parsed epic **Then** the section is embedded verbatim\n\n"
    "**Dependencies**: None\n"
    "**Risk Level**: Medium"
)


def _sectioned_story(section: str = _SECTION_27) -> Story:
    return Story(
        "27.3-002", "Story-section injection", "27",
        "performance-token-optimization", "docs/stories/epic-27.md",
        "Must", 3, "py", [], False, section,
    )


def test_build_prompt_embeds_story_section_verbatim() -> None:
    """The section replaces the 'Read {epic_file} and find the story' step (AC1)."""
    from sdlc.build import render_build_prompt

    prompt = render_build_prompt(_sectioned_story(), BuildOptions())
    assert _SECTION_27 in prompt
    assert "find the full story section" not in prompt
    assert "do not re-read the epic file" in prompt


def test_build_prompt_oversized_section_falls_back_untruncated() -> None:
    """A section over the cap falls back — no truncated spec is injected (AC2)."""
    from sdlc.build import STORY_SECTION_MAX_CHARS, render_build_prompt

    big = "##### Story 27.3-002: Big\n" + "x" * STORY_SECTION_MAX_CHARS
    prompt = render_build_prompt(_sectioned_story(big), BuildOptions())
    assert (
        "2. Read docs/stories/epic-27.md and find the full story section "
        "for 27.3-002" in prompt
    )
    assert "xxxx" not in prompt


def test_build_prompt_section_exactly_at_cap_is_embedded() -> None:
    """The cap is exclusive: a section of exactly the max size still embeds."""
    from sdlc.build import STORY_SECTION_MAX_CHARS, render_build_prompt

    head = "##### Story 27.3-002: Boundary\n"
    exact = head + "x" * (STORY_SECTION_MAX_CHARS - len(head))
    assert len(exact) == STORY_SECTION_MAX_CHARS
    prompt = render_build_prompt(_sectioned_story(exact), BuildOptions())
    assert exact in prompt
    assert "do not re-read the epic file" in prompt


def test_build_prompt_section_block_names_source_epic() -> None:
    """The injected block cites the epic file it was captured from."""
    from sdlc.build import render_build_prompt

    prompt = render_build_prompt(_sectioned_story(), BuildOptions())
    assert "## Story Specification (from docs/stories/epic-27.md)" in prompt


def test_build_prompt_without_section_keeps_read_instruction() -> None:
    """A story with no captured section renders today's prompt unchanged."""
    from sdlc.build import render_build_prompt

    prompt = render_build_prompt(_story("99.1-001"), BuildOptions())
    assert "2. Read epic-x.md and find the full story section for 99.1-001" in prompt
    assert "Story Specification" not in prompt


def test_coverage_prompt_embeds_story_section_verbatim() -> None:
    """The coverage prompt receives the same injection treatment (AC3)."""
    from sdlc.build import render_coverage_prompt

    prompt = render_coverage_prompt(_sectioned_story(), BuildOptions())
    assert _SECTION_27 in prompt


def test_coverage_prompt_oversized_section_falls_back_untruncated() -> None:
    """Oversized sections never leak (even truncated) into the coverage prompt."""
    from sdlc.build import STORY_SECTION_MAX_CHARS, render_coverage_prompt

    big = "##### Story 27.3-002: Big\n" + "x" * STORY_SECTION_MAX_CHARS
    prompt = render_coverage_prompt(_sectioned_story(big), BuildOptions())
    assert "xxxx" not in prompt
    assert "Story Specification" not in prompt


def test_coverage_prompt_without_section_is_unchanged() -> None:
    """A sectionless story keeps today's coverage prompt byte-identical."""
    from sdlc.build import render_coverage_prompt

    prompt = render_coverage_prompt(_story("99.1-001"), BuildOptions())
    assert "Story Specification" not in prompt


def test_coverage_prompt_gitlab_opens_a_merge_request() -> None:
    """The coverage agent opens an MR on a GitLab target (AC1/AC2)."""
    from sdlc.build import render_coverage_prompt
    from sdlc.issue_host import GITLAB_CR_TERMS

    gh = render_coverage_prompt(_story("23.2-001"), BuildOptions())
    assert "Push, open the PR, then emit the result block." in gh

    gl = render_coverage_prompt(
        _story("23.2-001"), BuildOptions(), cr_terms=GITLAB_CR_TERMS,
    )
    assert "Push, open the MR (`glab mr create`), then emit the result block." in gl


class _PromptCapturingDispatcher(FakeDispatcher):
    """A FakeDispatcher that also records each stage's rendered prompt."""

    def __init__(self, overrides=None) -> None:
        super().__init__(overrides)
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        self.prompts.append((agent_type, prompt))
        return super().__call__(agent_type, prompt, story=story, **kwargs)


def test_build_on_gitlab_target_opens_an_mr_and_records_cr_ref(tmp_path) -> None:
    """A GitLab-mapped story drives the build agent to open an MR; the cr_ref lands
    in the ledger — the GitHub PR path is untouched (Story 23.2-001 AC1/AC2)."""
    from sdlc.build import run_build

    db = tmp_path / "l.db"
    ledger = Ledger(db)
    ledger.init()
    # Map the story to a GitLab target so the build resolves GitLab CR terms.
    ledger.inventory_upsert_specs([("23.2-001", "23", "23.2", "t", 5, "High")])
    ledger.inventory_set_mapping("23.2-001", "gitlab", "5")

    disp = _PromptCapturingDispatcher()
    opts = BuildOptions(scope="epic-23", skip_preflight=True, sequential=True)
    run_build(
        opts,
        queue=[_story("23.2-001")],
        ledger=ledger,
        dispatcher=disp,
        preflight=lambda: True,
        root=tmp_path,  # hermetic: keep the #227 git-landed probe off the real repo
    )

    build_prompt = next(p for a, p in disp.prompts if a == "build")
    coverage_prompt = next(p for a, p in disp.prompts if a == "coverage")
    # The coverage agent (the default PR-opener) is told to open an MR via glab.
    assert "open the MR (`glab mr create`)" in coverage_prompt
    assert "open the PR" not in coverage_prompt
    # The build agent's hand-off references the MR, never a PR.
    assert "opens the MR" in build_prompt
    # The MR iid (cr_ref) reported by the coverage agent is recorded in the ledger.
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT pr_number FROM stories WHERE story_id = '23.2-001'"
        ).fetchone()
    assert row is not None and row[0] == 100


def test_origin_default_ref_resolves_head(tmp_path, monkeypatch) -> None:
    """`_origin_default_ref` reads origin/HEAD, else falls back to origin/main (AC3)."""
    from sdlc import build as build_mod

    def _fake_git(root, *args):
        import subprocess as _sp
        if args[:2] == ("symbolic-ref", "--quiet"):
            return _sp.CompletedProcess(args, 0, "refs/remotes/origin/develop\n", "")
        return _sp.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(build_mod, "_git", _fake_git)
    assert build_mod._origin_default_ref(tmp_path) == "origin/develop"

    # origin/HEAD unset → byte-identical default for GitHub repos (AC2).
    monkeypatch.setattr(
        build_mod, "_git",
        lambda root, *a: __import__("subprocess").CompletedProcess(a, 1, "", ""),
    )
    assert build_mod._origin_default_ref(tmp_path) == "origin/main"


def test_origin_default_ref_degrades_on_git_failure(tmp_path, monkeypatch) -> None:
    """`_origin_default_ref` falls back to origin/main when `_git` itself raises (AC2)."""
    from sdlc import build as build_mod

    def _raise_oserror(root, *args):
        raise OSError("git not found on PATH")

    monkeypatch.setattr(build_mod, "_git", _raise_oserror)
    assert build_mod._origin_default_ref(tmp_path) == "origin/main"

    def _raise_subprocess(root, *args):
        import subprocess as _sp
        raise _sp.SubprocessError("symbolic-ref timed out")

    monkeypatch.setattr(build_mod, "_git", _raise_subprocess)
    assert build_mod._origin_default_ref(tmp_path) == "origin/main"


def test_coverage_prompt_commit_subject_is_compliant_by_construction() -> None:
    """The coverage agent commits too — its supplied header is compliant (12.2-004 AC1)."""
    from sdlc.build import render_coverage_prompt

    long_title = (
        "Reconcile Story Status Against Origin Main And Recompute The Run Terminal"
    )
    story = Story(
        "12.3-001", long_title, "12", "controller-robustness",
        "docs/stories/epic-12.md", "Must", 8, "py", [], False,
    )
    prompt = render_coverage_prompt(story, BuildOptions())
    header = next(
        line.strip()
        for line in prompt.splitlines()
        if line.strip().startswith("test(controller-robustness):")
    )
    assert lint_commit_message(header, _COMMITLINT_RULES) == []
    assert header.endswith("(#12.3-001)")


def test_bugfix_prompt_commit_subject_is_compliant_by_construction() -> None:
    """The bugfix agent commits its fix — its supplied header is compliant (12.2-004 AC1)."""
    from sdlc.build import render_bugfix_prompt

    long_title = (
        "Reconcile Story Status Against Origin Main And Recompute The Run Terminal"
    )
    story = Story(
        "12.3-001", long_title, "12", "controller-robustness",
        "docs/stories/epic-12.md", "Must", 8, "py", [], False,
    )
    prompt = render_bugfix_prompt(story, "build", "boom")
    header = next(
        line.strip()
        for line in prompt.splitlines()
        if line.strip().startswith("fix(controller-robustness):")
    )
    assert lint_commit_message(header, _COMMITLINT_RULES) == []
    assert header.endswith("(#12.3-001)")


def test_commit_lint_malformed_reask_envelope_is_recovered(tmp_path, monkeypatch) -> None:
    """AC4: a malformed commit-lint re-ask reply routes through envelope recovery, not a park."""
    from sdlc.contracts import ContractError
    from sdlc.dispatch import AgentResult

    class _AmendThenMalformed:
        """The amend lands but the re-ask reply omits its envelope; recovery rescues it."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.compliant = False

        def __call__(self, agent_type, prompt, story=None, **kwargs):
            kind = _kind_of(prompt)
            self.calls.append((agent_type, kind))
            if kind == "commitlint":
                # The amend itself lands (commit becomes compliant) but the reply
                # omits the result envelope — exactly the run 7df64f19 failure.
                self.compliant = True
                raise ContractError("missing required field 'branch_name'")
            if kind == "stage" and agent_type in ("build", "coverage", "bugfix"):
                self.compliant = False  # a fresh commit-authoring stage
            return AgentResult(agent_type, _default_payload(agent_type, story), "")

    disp = _AmendThenMalformed()
    _patch_commitlint(monkeypatch, disp, _COMMITLINT_RULES)
    db = tmp_path / "l.db"
    result = run_build(
        BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    # Recovered, not parked: the story completes.
    assert result.story_status["99.1-001"] == "DONE"
    # The malformed re-ask was routed through the envelope-only recovery path.
    assert any(kind == "envelope" for _, kind in disp.calls)
    msgs = [
        r[0] for r in _open(db).execute(
            "SELECT message FROM events WHERE story_id='99.1-001'"
        ).fetchall()
    ]
    assert any("routing through envelope recovery" in m for m in msgs)


def test_commit_lint_recovered_envelope_then_unreadable_commit_parks(
    tmp_path, monkeypatch
) -> None:
    """AC4: envelope recovery succeeds but the amended commit then reads back unreadable.

    The recovery rescues the malformed re-ask reply, yet the post-amend
    ``_commit_message`` read degrades to ``None`` (e.g. the branch ref vanished
    mid-flight). The loop must break and the story park — never silently advance a
    commit it could not re-lint — with work preserved (R10).
    """
    from sdlc.contracts import ContractError
    from sdlc.dispatch import AgentResult

    class _RecoverThenUnreadable:
        """The amend's re-ask reply is malformed; recovery lands, then the commit is unreadable."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.recovered = False

        def __call__(self, agent_type, prompt, story=None, **kwargs):
            kind = _kind_of(prompt)
            self.calls.append((agent_type, kind))
            if kind == "commitlint":
                raise ContractError("missing required field 'branch_name'")
            if kind == "envelope":
                self.recovered = True  # the envelope-only recovery dispatch lands
            return AgentResult(agent_type, _default_payload(agent_type, story), "")

    disp = _RecoverThenUnreadable()
    monkeypatch.setattr(
        "sdlc.build.load_commitlint_config", lambda root: _COMMITLINT_RULES
    )
    # Non-compliant before recovery; unreadable once the recovery has run.
    monkeypatch.setattr(
        "sdlc.build._commit_message",
        lambda ref, root=None: None if disp.recovered else _BAD_COMMIT,
    )
    db = tmp_path / "l.db"
    result = run_build(
        BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    # Recovery dispatched, but the unreadable post-amend commit breaks the loop → park.
    assert result.story_status["99.1-001"] == "NEEDS_ATTENTION"
    assert any(kind == "envelope" for _, kind in disp.calls)
    advanced = {agent for agent, _ in disp.calls}
    assert "review" not in advanced and "merge" not in advanced


def test_commit_lint_reask_dispatch_error_parks(tmp_path, monkeypatch) -> None:
    """A re-ask whose envelope recovery also fails is bounded, then parks (12.2-004 AC4)."""
    from sdlc.dispatch import AgentDispatchError, AgentResult

    class _ReaskRaises:
        """Both the commit-lint re-ask and its envelope recovery blow up."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.compliant = False

        def __call__(self, agent_type, prompt, story=None, **kwargs):
            kind = _kind_of(prompt)
            self.calls.append((agent_type, kind))
            if kind in ("commitlint", "envelope"):
                raise AgentDispatchError("agent crashed during amend")
            self.compliant = False
            return AgentResult(agent_type, _default_payload(agent_type, story), "")

    disp = _ReaskRaises()
    _patch_commitlint(monkeypatch, disp, _COMMITLINT_RULES)
    db = tmp_path / "l.db"
    result = run_build(
        BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    # Recovery also failed; the story is parked, never advanced (work preserved).
    assert result.story_status["99.1-001"] == "NEEDS_ATTENTION"
    advanced = {agent for agent, _ in disp.calls}
    assert "review" not in advanced and "merge" not in advanced
    # The malformed re-ask was routed through envelope recovery before parking.
    assert any(kind == "envelope" for _, kind in disp.calls)
    msgs = [
        r[0] for r in _open(db).execute(
            "SELECT message FROM events WHERE story_id='99.1-001'"
        ).fetchall()
    ]
    assert any("routing through envelope recovery" in m for m in msgs)
    # The unrecovered re-ask is recorded as a FAILED 'commitlint' stage row.
    stage_statuses = _open(db).execute(
        "SELECT status FROM stages WHERE story_id='99.1-001' AND stage_name='commitlint'"
    ).fetchall()
    assert any(s[0] == "FAILED" for s in stage_statuses)


def test_commit_lint_unreadable_after_reask_parks(tmp_path, monkeypatch) -> None:
    """If HEAD becomes unreadable after a re-ask, the loop bails out and parks."""
    from sdlc.dispatch import AgentResult

    class _ReaskThenUnreadable:
        """Build leaves a bad commit; after the re-ask dispatch HEAD reads as None."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.reasked = False

        def __call__(self, agent_type, prompt, story=None, **kwargs):
            is_lint = "commitlint" in prompt
            self.calls.append((agent_type, "commitlint" if is_lint else "stage"))
            if is_lint:
                self.reasked = True
            return AgentResult(agent_type, _default_payload(agent_type, story), "")

    disp = _ReaskThenUnreadable()
    monkeypatch.setattr("sdlc.build.load_commitlint_config", lambda root: _COMMITLINT_RULES)
    # Non-compliant before the re-ask; unreadable (None) once a re-ask has run.
    monkeypatch.setattr(
        "sdlc.build._commit_message",
        lambda ref, root=None: None if disp.reasked else _BAD_COMMIT,
    )
    db = tmp_path / "l.db"
    result = run_build(
        BuildOptions(scope="epic-99", skip_coverage=True, skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    # One re-ask ran, HEAD then read as None → the loop broke and the story parked.
    assert result.story_status["99.1-001"] == "NEEDS_ATTENTION"
    assert sum(1 for _, kind in disp.calls if kind == "commitlint") == 1
    advanced = {agent for agent, _ in disp.calls}
    assert "review" not in advanced and "merge" not in advanced


# ---------------------------------------------------------------------------
# AWAITING_APPROVAL: a high-risk merge block is parked, not failed (12.3-003)
# ---------------------------------------------------------------------------

def _high_risk_merge_block(reason: str = "BLOCKED_HIGH_RISK") -> dict:
    """A schema-valid merge response that surfaces a high-risk approval block.

    The merge schema enum is only MERGED|FAILED|SKIPPED, so the block is
    surfaced *additively* via a ``block_reason`` field (extra properties are
    allowed). The merge did not land, so ``merge_status`` is FAILED and the
    SHA / timestamp are empty — which the schema permits for a non-MERGED
    outcome (Story 12.3-003).
    """
    return {
        "pr_number": 100,
        "merge_status": "FAILED",
        "merge_sha": "",
        "merged_at": "",
        "block_reason": reason,
    }


def test_high_risk_block_passes_real_merge_schema_validation() -> None:
    """The block response a real merge agent emits must pass schema validation.

    Regression for the gap where ``merge_sha``'s unconditional ``minLength: 1``
    rejected every non-merged response — which would route the block to the
    contract-error path and never reach the awaiting-approval short-circuit.
    """
    from sdlc.contracts import SchemaValidationError, validate_response

    # A high-risk block (empty SHA/timestamp) validates.
    assert validate_response("merge", _high_risk_merge_block()) == _high_risk_merge_block()
    # A real merge is held to the stricter contract — a non-empty SHA *and* a
    # non-empty timestamp are still required when merge_status == MERGED.
    with pytest.raises(SchemaValidationError):
        validate_response("merge", {
            "pr_number": 100, "merge_status": "MERGED",
            "merge_sha": "", "merged_at": "2026-06-21T00:00:00Z",
        })
    with pytest.raises(SchemaValidationError):
        validate_response("merge", {
            "pr_number": 100, "merge_status": "MERGED",
            "merge_sha": "cafef00d", "merged_at": "",
        })
    # The canonical successful merge still passes.
    good = {
        "pr_number": 100, "merge_status": "MERGED",
        "merge_sha": "cafef00d", "merged_at": "2026-06-21T00:00:00Z",
    }
    assert validate_response("merge", good) == good


def test_high_risk_block_detected_as_awaiting_approval() -> None:
    from sdlc.build import _merge_awaiting_approval

    # block_reason field — the primary, additive signal.
    assert _merge_awaiting_approval("merge", _high_risk_merge_block()) is True
    # Case-insensitive.
    assert _merge_awaiting_approval("merge", _high_risk_merge_block("blocked_high_risk")) is True
    # Free-text fallback when the reason rides in error_summary instead.
    assert _merge_awaiting_approval(
        "merge", {"merge_status": "FAILED", "error_summary": "PR is BLOCKED_HIGH_RISK, no risk-approved label"}
    ) is True
    # A plain merge failure is NOT awaiting approval.
    assert _merge_awaiting_approval("merge", {"merge_status": "FAILED"}) is False
    # Only the merge stage can be awaiting approval.
    assert _merge_awaiting_approval("build", _high_risk_merge_block()) is False


def test_high_risk_merge_block_parks_awaiting_approval_no_bugfix(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    # Two independent stories whose merges are high-risk-blocked; the bugfix loop
    # must NOT run (it cannot self-approve) and each is parked AWAITING_APPROVAL.
    dispatcher = FakeDispatcher(
        overrides={
            ("merge", "s1-001"): _high_risk_merge_block(),
            ("merge", "s1-002"): _high_risk_merge_block(),
        }
    )
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True, auto=True)
    result = run_build(
        opts,
        queue=_sample_queue()[:2],
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    # No bugfix agent was ever dispatched — the block short-circuits before it.
    assert not any(a == "bugfix" for a, _ in dispatcher.calls)
    # Stories are parked AWAITING_APPROVAL, and the run is NOT failed.
    assert result.failed == 0
    assert result.story_status["s1-001"] == "AWAITING_APPROVAL"
    conn = _open(db)
    run_status = conn.execute("SELECT status FROM runs").fetchone()[0]
    assert run_status == "AWAITING_APPROVAL"


def test_awaiting_approval_run_terminal_not_failed(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    # A single-story run whose only outcome is awaiting approval.
    dispatcher = FakeDispatcher(
        overrides={("merge", "s1-001"): _high_risk_merge_block()}
    )
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True, auto=True)
    result = run_build(
        opts,
        queue=[_sample_queue()[0]],
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert result.failed == 0
    assert result.awaiting_approval == 1
    conn = _open(db)
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "AWAITING_APPROVAL"


def test_awaiting_approval_blocks_dependents_like_needs_attention(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    # s1-001 is high-risk-blocked; s1-003 depends on it → s1-003 is BLOCKED
    # (an unmerged dependency cannot be safely built upon).
    dispatcher = FakeDispatcher(
        overrides={("merge", "s1-001"): _high_risk_merge_block()}
    )
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True, auto=True)
    result = run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert result.story_status["s1-001"] == "AWAITING_APPROVAL"
    assert result.story_status["s1-003"] == "BLOCKED"


def test_needs_attention_takes_precedence_over_awaiting_approval(
    tmp_path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    from sdlc.contracts import SchemaValidationError

    def raise_schema_error():
        raise SchemaValidationError("build-agent response is missing 'branch_name'")

    # s1-002's build is unparseable but its work is committed → parked
    # NEEDS_ATTENTION (R10). s1-001 awaits approval. A mixed run reports the
    # more-urgent NEEDS_ATTENTION (never hides stuck work), and never FAILED.
    monkeypatch.setattr("sdlc.build.story_commit_exists", lambda sid, root=None: True)
    dispatcher = FakeDispatcher(
        overrides={
            ("merge", "s1-001"): _high_risk_merge_block(),
            ("build", "s1-002"): raise_schema_error,
        }
    )
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True, auto=True)
    # Two independent stories only (s1-003 depends on s1-001 and would BLOCK).
    result = run_build(
        opts,
        queue=_sample_queue()[:2],
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert result.story_status["s1-001"] == "AWAITING_APPROVAL"
    assert result.story_status["s1-002"] == "NEEDS_ATTENTION"
    conn = _open(db)
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "NEEDS_ATTENTION"


def test_status_snapshot_counts_awaiting_approval(tmp_path) -> None:
    from sdlc.build import status_snapshot

    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher(
        overrides={("merge", "s1-001"): _high_risk_merge_block()}
    )
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True, auto=True)
    result = run_build(
        opts,
        queue=[_sample_queue()[0]],
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    snap = status_snapshot(Ledger(db), result.run_id)
    assert snap["counts"]["awaiting_approval"] == 1


# ---------------------------------------------------------------------------
# Story 25.1-001: deterministic gate-only-block recognition (host-side re-check)
# ---------------------------------------------------------------------------


def _gate_checks(
    labels=("risk:high",),
    checks=(
        ("High-risk file approval gate", "failed"),
        ("tests", "success"),
    ),
):
    """A CR view whose only red check is the high-risk gate (the parked shape)."""
    return ih.ChangeRequestChecks(labels=tuple(labels), checks=tuple(checks))


def test_gate_only_block_predicate_positive_cases() -> None:
    from sdlc.build import _gate_only_block

    # GitHub: the gate workflow job is the only red check → gate-only block.
    assert _gate_only_block(_gate_checks()) is True
    # GitLab: the CI-template job name matches too.
    assert _gate_only_block(
        _gate_checks(checks=(("risk-gate", ih.CR_FAILED), ("tests", ih.CR_SUCCESS)))
    ) is True
    # Names and labels match case-insensitively.
    assert _gate_only_block(
        _gate_checks(
            labels=("Risk:High",),
            checks=(("HIGH-RISK FILE APPROVAL GATE", ih.CR_FAILED),),
        )
    ) is True


def test_gate_only_block_predicate_negative_cases() -> None:
    from sdlc.build import _gate_only_block

    # A second red check → a real merge failure, never parked (AC4).
    assert _gate_only_block(
        _gate_checks(checks=(
            ("High-risk file approval gate", ih.CR_FAILED),
            ("tests", ih.CR_FAILED),
        ))
    ) is False
    # No risk:high label → whatever failed, it is not the gate.
    assert _gate_only_block(_gate_checks(labels=("story",))) is False
    # risk-approved present → the gate is satisfied; a red check is real.
    assert _gate_only_block(_gate_checks(labels=("risk:high", "risk-approved"))) is False
    # Nothing failing → nothing to reclassify.
    assert _gate_only_block(
        _gate_checks(checks=(("High-risk file approval gate", ih.CR_SUCCESS),))
    ) is False
    # Another check still in flight → ambiguous, never park early.
    assert _gate_only_block(
        _gate_checks(checks=(
            ("High-risk file approval gate", ih.CR_FAILED),
            ("tests", ih.CR_PENDING),
        ))
    ) is False
    # An empty view (no labels, no checks) is never a gate block.
    assert _gate_only_block(ih.ChangeRequestChecks()) is False


def test_merge_ci_gate_block_gate_only_parks_awaiting_approval(
    tmp_path, monkeypatch
) -> None:
    """The epic-23 run-0541804d gap on the build path: the gate check is already
    red, so the CI gate blocks *before* the merge agent can report
    BLOCKED_HIGH_RISK — the deterministic CR re-check must park the story
    AWAITING_APPROVAL, never route it into the bugfix loop."""
    from sdlc import build_issue

    monkeypatch.setattr(build_issue, "change_request_status", lambda *a, **k: ih.CR_FAILED)
    monkeypatch.setattr(build_issue, "change_request_checks", lambda *a, **k: _gate_checks())
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher()
    opts = BuildOptions(scope="epic-25", skip_preflight=True, sequential=True, auto=True)
    result = run_build(opts, queue=[_sample_queue()[0]], ledger=Ledger(db),
                       dispatcher=dispatcher, preflight=lambda: True)
    # The merge agent was never dispatched (CI gate blocked pre-dispatch) and
    # the bugfix loop never ran (it cannot self-approve).
    assert ("merge", "s1-001") not in dispatcher.calls
    assert not any(a == "bugfix" for a, _ in dispatcher.calls)
    assert result.story_status["s1-001"] == "AWAITING_APPROVAL"
    assert result.failed == 0
    conn = _open(db)
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "AWAITING_APPROVAL"


def test_merge_agent_failure_reclassified_by_host_recheck(tmp_path, monkeypatch) -> None:
    """A merge agent that reports FAILED *without* the block_reason marker is
    still parked when the CR itself shows a gate-only block (25.1-001: the
    free-text detection is advisory; the check rollup is authoritative)."""
    from sdlc import build_issue

    monkeypatch.setattr(build_issue, "change_request_checks", lambda *a, **k: _gate_checks())
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher(
        overrides={("merge", "s1-001"): {
            "pr_number": 100, "merge_status": "FAILED",
            "merge_sha": "", "merged_at": "",
        }}
    )
    opts = BuildOptions(scope="epic-25", skip_preflight=True, sequential=True, auto=True)
    result = run_build(opts, queue=[_sample_queue()[0]], ledger=Ledger(db),
                       dispatcher=dispatcher, preflight=lambda: True)
    assert not any(a == "bugfix" for a, _ in dispatcher.calls)
    assert result.story_status["s1-001"] == "AWAITING_APPROVAL"
    assert result.failed == 0


def test_merge_non_gate_failure_still_routes_to_bugfix(tmp_path, monkeypatch) -> None:
    """A merge blocked by something other than the gate (a second red check) is
    a real failure: the re-check must not park it (AC4 — no false positives)."""
    from sdlc import build_issue

    monkeypatch.setattr(
        build_issue, "change_request_checks",
        lambda *a, **k: _gate_checks(checks=(
            ("High-risk file approval gate", ih.CR_FAILED),
            ("tests", ih.CR_FAILED),
        )),
    )
    db = tmp_path / "ledger.db"
    dispatcher = FakeDispatcher(
        overrides={("merge", "s1-001"): {
            "pr_number": 100, "merge_status": "FAILED",
            "merge_sha": "", "merged_at": "",
        }}
    )
    opts = BuildOptions(scope="epic-25", skip_preflight=True, sequential=True, auto=True)
    result = run_build(opts, queue=[_sample_queue()[0]], ledger=Ledger(db),
                       dispatcher=dispatcher, preflight=lambda: True)
    assert any(a == "bugfix" for a, _ in dispatcher.calls)
    assert result.story_status["s1-001"] == "FAILED"
    assert result.awaiting_approval == 0


def test_merge_gate_recheck_skipped_without_cr_ref(tmp_path, monkeypatch) -> None:
    """A merge failure with no recorded CR ref is never re-checked: there is no
    CR to consult, so the host is not touched and the failure routing stays
    unchanged (25.1-001 — the seam is only read when a cr_ref exists)."""
    from sdlc import build_issue
    from sdlc.build import _merge_gate_only_block

    monkeypatch.setattr(
        build_issue, "change_request_checks",
        lambda *a, **k: pytest.fail("must not consult the host without a cr_ref"),
    )
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    assert _merge_gate_only_block(ledger, "run-1", _story("25.1-001"), None) is False


# ---------------------------------------------------------------------------
# Story 12.4-001: cut story branches from origin/main + reposition HEAD
# ---------------------------------------------------------------------------


def test_build_prompt_branches_from_origin_main() -> None:
    """The build prompt cuts ``feature/<id>`` from a freshly-fetched ``origin/main``.

    Story 12.4-001 AC1: a base-less ``git checkout -b feature/<id>`` lets the
    branch stack on whatever HEAD happened to be; the prompt must fetch and cut
    from the remote base instead.
    """
    from sdlc.build import render_build_prompt

    story = _story("12.4-001")
    prompt = render_build_prompt(story, BuildOptions())
    assert f"git checkout -b feature/{story.id} origin/main" in prompt
    assert "git fetch origin" in prompt
    # The old base-less form must be gone (it is a prefix of the new one, so
    # assert the new form is the only checkout instruction).
    assert f"git checkout -b feature/{story.id}\n" not in prompt


def test_reposition_head_checks_out_local_branch_not_detached(monkeypatch) -> None:
    """``_reposition_head`` lands on the local branch, not detached origin/main (AC2).

    ``_base_ref`` yields the remote-tracking ref ``origin/main``; checking that
    out would detach HEAD. The helper strips ``origin/`` and checks out the local
    ``main`` branch when it exists.
    """
    import subprocess

    from sdlc.build import _reposition_head

    calls: list[tuple[str, ...]] = []

    def _fake_git(root, *args):
        calls.append(args)
        # The local-branch existence probe succeeds → land on the local branch.
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("sdlc.build._git", _fake_git)
    monkeypatch.setattr("sdlc.build._base_ref", lambda root: "origin/main")
    _reposition_head(Path("/repo"))
    assert ("checkout", "main") in calls  # local branch, not detached origin/main
    assert ("checkout", "origin/main") not in calls
    # R10: it only checks out — it never deletes a feature branch or its commits.
    assert all("-D" not in a and a[:1] != ("branch",) for a in calls)


def test_reposition_head_falls_back_when_no_local_branch(monkeypatch) -> None:
    """No local branch ⇒ fall back to the ref ``_base_ref`` returned (non-fatal)."""
    import subprocess

    from sdlc.build import _reposition_head

    calls: list[tuple[str, ...]] = []

    def _fake_git(root, *args):
        calls.append(args)
        # The local-branch existence probe fails (no refs/heads/main).
        rc = 1 if args[:1] == ("rev-parse",) else 0
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="")

    monkeypatch.setattr("sdlc.build._git", _fake_git)
    monkeypatch.setattr("sdlc.build._base_ref", lambda root: "origin/main")
    _reposition_head(Path("/repo"))
    assert ("checkout", "origin/main") in calls  # fell back to the remote ref


def test_reposition_head_no_base_is_noop(monkeypatch) -> None:
    """No discoverable base ⇒ ``_reposition_head`` does nothing (never raises)."""
    import subprocess

    from sdlc.build import _reposition_head

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "sdlc.build._git",
        lambda root, *args: calls.append(args)
        or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr("sdlc.build._base_ref", lambda root: None)
    _reposition_head(Path("/repo"))
    assert calls == []  # no base → no checkout attempted


def test_reposition_head_is_non_fatal(monkeypatch) -> None:
    """A git/OS error during reposition is swallowed (best-effort, AC4/R10)."""
    import subprocess

    from sdlc.build import _reposition_head

    def _boom(root, *args):
        raise subprocess.SubprocessError("git vanished")

    monkeypatch.setattr("sdlc.build._base_ref", lambda root: "main")
    monkeypatch.setattr("sdlc.build._git", _boom)
    # Must not raise.
    _reposition_head(Path("/repo"))


def _git_run(cwd, *args):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_branch_from_origin_main_prevents_transitive_landing(tmp_path) -> None:
    """A failed story's commits never ride a later story's merge onto origin/main.

    Story 12.4-001 AC3 + DoD regression: with branches cut from ``origin/main``
    and HEAD repositioned between stories (via the real ``_reposition_head``), a
    story that fails before merge does not appear on ``origin/main`` and a later
    successful story does not transitively land it. The failed branch and its
    commits are preserved (R10).
    """
    import subprocess

    from sdlc.build import _reposition_head

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True, text=True,
    )
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", str(origin), str(work)],
        check=True, capture_output=True, text=True,
    )
    _git_run(work, "config", "user.email", "t@example.com")
    _git_run(work, "config", "user.name", "Test")

    (work / "README").write_text("base\n")
    _git_run(work, "add", "-A")
    _git_run(work, "commit", "-m", "chore: base")
    _git_run(work, "push", "origin", "main")

    # Story A fails before merge: branch from origin/main, commit, never merged.
    _git_run(work, "fetch", "origin")
    _git_run(work, "checkout", "-b", "feature/A", "origin/main")
    (work / "a.txt").write_text("A\n")
    _git_run(work, "add", "-A")
    _git_run(work, "commit", "-m", "feat: a (#A)")

    # Controller repositions HEAD off the leftover feature branch (the fix).
    _reposition_head(work)
    head = _git_run(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    # Lands on the local ``main`` branch — not detached at ``origin/main`` and
    # not left on the story's feature branch.
    assert head == "main"

    # Story B succeeds: branch from a fresh origin/main, commit, merge, push.
    _git_run(work, "fetch", "origin")
    _git_run(work, "checkout", "-b", "feature/B", "origin/main")
    (work / "b.txt").write_text("B\n")
    _git_run(work, "add", "-A")
    _git_run(work, "commit", "-m", "feat: b (#B)")
    _git_run(work, "checkout", "main")
    _git_run(work, "merge", "--no-ff", "feature/B", "-m", "merge: b")
    _git_run(work, "push", "origin", "main")

    landed = _git_run(work, "log", "origin/main", "--format=%s").stdout
    assert "feat: b (#B)" in landed  # B's work shipped
    assert "feat: a (#A)" not in landed  # A did NOT transitively land

    # R10: A's branch and commit are preserved, not deleted.
    assert (
        _git_run(work, "rev-parse", "--verify", "feature/A").returncode == 0
    )


# ---------------------------------------------------------------------------
# Issue #104: ContextOverflowError fail-fast — no bugfix loop, FAILED fast
# ---------------------------------------------------------------------------


class _ContextOverflowDispatcher:
    """Raises ContextOverflowError for a specific stage; canned defaults otherwise.

    Models the agent whose merge (or build) dispatch returns an is_error
    envelope reporting a prompt-too-long / context-window overflow. Tracks
    every call so the test can assert no bugfix was dispatched.
    """

    def __init__(self, overflow_on: str = "build") -> None:
        self.calls: list[tuple[str, str]] = []
        self.overflow_on = overflow_on

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        from sdlc.dispatch import AgentResult, ContextOverflowError

        self.calls.append((agent_type, getattr(story, "id", "")))
        if agent_type == self.overflow_on:
            raise ContextOverflowError(
                f"{agent_type} agent exceeded context window: "
                "Prompt is too long · the request is ~1180341 tokens (limit 1000000)"
            )
        payload = _default_payload(agent_type, story)
        return AgentResult(agent_type=agent_type, data=payload, raw="")


def test_context_overflow_on_build_fails_fast_without_bugfix(tmp_path) -> None:
    """Issue #104: ContextOverflowError on the build stage fails the story FAILED
    immediately with failure_category='context-overflow', and no bugfix is ever
    dispatched (the bugfix loop would only re-overflow).
    """
    db = tmp_path / "l.db"
    disp = _ContextOverflowDispatcher(overflow_on="build")
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    # Story must be FAILED — not NEEDS_ATTENTION and not still in-progress.
    assert result.failed == 1
    assert result.completed == 0
    assert result.story_status["99.1-001"] == "FAILED"

    # The bugfix agent must NEVER have been called — re-dispatching into an
    # overflowed context would only re-overflow.
    dispatched_types = [a for a, _ in disp.calls]
    assert "bugfix" not in dispatched_types, (
        f"bugfix was dispatched despite context overflow; calls={disp.calls}"
    )

    # The stage row must record failure_category='context-overflow' so the
    # dashboard and sdlc status can surface the root cause distinctly.
    conn = _open(db)
    row = conn.execute(
        "SELECT status, failure_category FROM stages "
        "WHERE story_id='99.1-001' AND stage_name='build' ORDER BY attempt DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "no build stage row found"
    assert row[0] == "FAILED"
    assert row[1] == "context-overflow", (
        f"expected failure_category='context-overflow', got {row[1]!r}"
    )


def test_context_overflow_on_merge_fails_fast_without_bugfix(tmp_path) -> None:
    """Issue #104: ContextOverflowError on the merge stage also fails fast.

    The merge stage is the dominant real-world overflow site (#104) — the
    merge prompt ingested the full progress file. Ensure the fail-fast guard
    fires on any stage, not just build.
    """
    db = tmp_path / "l.db"
    disp = _ContextOverflowDispatcher(overflow_on="merge")
    result = run_build(
        BuildOptions(scope="epic-99", skip_preflight=True, sequential=True),
        queue=[_story("99.1-001")],
        ledger=Ledger(db),
        dispatcher=disp,
        preflight=lambda: True,
    )
    assert result.failed == 1
    assert result.story_status["99.1-001"] == "FAILED"

    dispatched_types = [a for a, _ in disp.calls]
    assert "bugfix" not in dispatched_types, (
        f"bugfix was dispatched despite context overflow on merge; calls={disp.calls}"
    )

    conn = _open(db)
    row = conn.execute(
        "SELECT status, failure_category FROM stages "
        "WHERE story_id='99.1-001' AND stage_name='merge' ORDER BY attempt DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "no merge stage row found"
    assert row[0] == "FAILED"
    assert row[1] == "context-overflow"


def test_context_overflow_is_subclass_of_agent_dispatch_error() -> None:
    """Issue #104: ContextOverflowError is still an AgentDispatchError for
    graceful degradation in any caller that only catches the base class.
    """
    from sdlc.dispatch import AgentDispatchError, ContextOverflowError

    exc = ContextOverflowError("too long")
    assert isinstance(exc, AgentDispatchError)


# ---------------------------------------------------------------------------
# _result_wrapper embeds a literal, schema-derived required-field skeleton so
# agents stop paraphrasing the required keys (the contract-drift bug).
# ---------------------------------------------------------------------------

def _fill_skeleton(skeleton: str) -> str:
    """Turn a wrapper skeleton into a concrete, schema-valid JSON string.

    Picks the first enum literal for ``"A|B"`` hints and substitutes sample
    values for the typed placeholders so the advertised shape can be validated.
    """
    # Collapse the unquoted boolean hint first so its pipe cannot be swept into
    # the quoted-enum match below (the prior key's closing quote would otherwise
    # act as an opening quote around ``: <bool>, "``).
    filled = skeleton.replace("true|false", "true")
    filled = re.sub(
        r'"([^"|]+(?:\|[^"|]+)+)"',
        lambda m: '"' + m.group(1).split("|")[0] + '"',
        filled,
    )
    filled = filled.replace('"<string>"', '"sample"')
    filled = filled.replace("<integer>", "1")
    filled = filled.replace("<number>", "1.0")
    return filled


def test_build_result_wrapper_embeds_literal_field_skeleton() -> None:
    """The build wrapper shows the real field names + enum literals, not a
    pointer to an unreadable schema file."""
    from sdlc.build import _result_wrapper

    wrapper = _result_wrapper("build-agent-response.schema.json")
    for substring in ("branch_name", "build_status", "commit_sha", "SUCCESS|FAILED"):
        assert substring in wrapper
    assert "the JSON object per" not in wrapper


@pytest.mark.parametrize(
    "agent_type,schema_filename", sorted(AGENT_SCHEMAS.items())
)
def test_result_wrapper_lists_every_required_field(
    agent_type: str, schema_filename: str
) -> None:
    """Each wrapper names every field in its schema's ``required`` array."""
    from sdlc.build import _result_wrapper
    from sdlc.contracts import load_schema

    wrapper = _result_wrapper(schema_filename)
    for name in load_schema(agent_type).get("required", []):
        assert name in wrapper, f"{agent_type}: missing required field {name!r}"


@pytest.mark.parametrize(
    "agent_type,schema_filename", sorted(AGENT_SCHEMAS.items())
)
def test_result_wrapper_skeleton_round_trips_through_contracts(
    agent_type: str, schema_filename: str
) -> None:
    """The advertised skeleton, filled with sample values, satisfies the
    enforced schema — proving the wrapper cannot tell agents to emit an invalid
    shape."""
    from sdlc.build import _result_wrapper
    from sdlc.contracts import (
        RESULT_END_MARKER,
        RESULT_START_MARKER,
        parse_and_validate,
    )

    wrapper = _result_wrapper(schema_filename)
    start = wrapper.index(RESULT_START_MARKER) + len(RESULT_START_MARKER)
    end = wrapper.index(RESULT_END_MARKER)
    skeleton = wrapper[start:end].strip()

    obj = json.loads(_fill_skeleton(skeleton))  # also proves it is valid JSON
    response = f"{RESULT_START_MARKER}\n{json.dumps(obj)}\n{RESULT_END_MARKER}"
    assert parse_and_validate(agent_type, response) == obj


def test_result_wrapper_reexport_is_byte_identical() -> None:
    """Issue #435: `_result_wrapper` moved to contracts.py; build.py re-exports
    it, so a rendered pipeline prompt's tail is byte-identical to the contracts
    version — the relocation cannot drift the pipeline's prompt output."""
    from sdlc import contracts
    from sdlc.build import _result_wrapper, render_build_prompt

    # Same object, not a diverging copy.
    assert _result_wrapper is contracts._result_wrapper

    expected_tail = contracts._result_wrapper("build-agent-response.schema.json")
    prompt = render_build_prompt(_story("99.1-001"), BuildOptions())
    assert prompt.endswith(expected_tail)


def test_field_hint_unknown_type_falls_back_to_generic_placeholder() -> None:
    """A schema property with no/compound type keeps the skeleton well-formed
    via a generic string placeholder (relocated helper, issue #435)."""
    from sdlc.contracts import _field_hint

    assert _field_hint({}) == '"<value>"'
    # An array field advertises the array shape; its element uses the item hint,
    # falling back to the generic placeholder when the items have no known type.
    assert _field_hint({"type": "array"}) == '["<value>"]'
    assert _field_hint({"type": "array", "items": {"type": "string"}}) == '["<string>"]'


# --- Story 22.5-001 AC1: the run's actor is stamped from host identity --------
# These wire the identity helpers (resolved + unit-tested in test_identity.py)
# into the real run-create path; the reviewer flagged that the primitives existed
# but `run_build` never called them, leaving `runs.actor` always NULL.


def _gh_adapter(login: str | None, *, missing_cli: bool = False):
    """A GitHubAdapter whose `gh api user` returns `login` (or fails)."""
    from sdlc import issue_host as ih

    if missing_cli:
        def runner(argv, timeout=None):
            raise FileNotFoundError(argv[0])
        return ih.GitHubAdapter(runner=runner)

    def runner(argv, timeout=None):
        if "api user" in " ".join(argv):
            return ih.RunResult(returncode=0, stdout=f"{login}\n", stderr="")
        return ih.RunResult(returncode=0, stdout="", stderr="")

    return ih.GitHubAdapter(runner=runner)


def _actor_of(db: Path) -> str | None:
    row = _open(db).execute("SELECT actor FROM runs").fetchone()
    return row[0] if row else None


def test_run_build_stamps_actor_from_adapter(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
        actor_adapter=_gh_adapter("octocat"),
    )
    assert _actor_of(db) == "octocat"


def test_run_build_without_adapter_stamps_unknown(tmp_path) -> None:
    # No adapter (host indeterminate) must still attribute the run — `unknown`,
    # never NULL and never a crash (AC3).
    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
    )
    assert _actor_of(db) == "unknown"


def test_run_build_unauthenticated_adapter_stamps_unknown(tmp_path) -> None:
    # Host CLI present but no auth → identity degrades to `unknown` (AC3).
    db = tmp_path / "ledger.db"
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
        actor_adapter=_gh_adapter(None, missing_cli=True),
    )
    assert _actor_of(db) == "unknown"


def test_stamp_run_actor_helper_none_adapter_is_unknown(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("all", "serial")
    assert _stamp_run_actor(ledger, run_id, None) == "unknown"
    assert ledger.run_get_actor(run_id) == "unknown"


def test_stamp_run_actor_helper_with_adapter_stamps_login(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("all", "serial")
    assert _stamp_run_actor(ledger, run_id, _gh_adapter("alice")) == "alice"
    assert ledger.run_get_actor(run_id) == "alice"


# --- Story 22.6-001: adoption-time status seed -------------------------------


def test_build_done_story_ids_returns_only_done(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("all", "serial")
    ledger.story_upsert(run_id, "1.1-001", "1", "t", "P1", 2, "py", "", None, "DONE")
    ledger.story_upsert(run_id, "1.1-002", "1", "t", "P1", 2, "py", "", None, "FAILED")
    ledger.story_upsert(run_id, "1.1-003", "1", "t", "P1", 2, "py", "", None, "DONE")
    assert ledger.build_done_story_ids() == {"1.1-001", "1.1-003"}


def test_build_done_story_ids_empty_when_no_runs(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    assert ledger.build_done_story_ids() == set()
