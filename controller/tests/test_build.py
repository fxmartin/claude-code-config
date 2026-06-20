# ABOUTME: Tests for the build-stories state machine port (Story 7.3-001).
# ABOUTME: Agent dispatch is mocked; the ledger is a real temp SQLite DB.

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sdlc.build import (
    BuildOptions,
    BuildResult,
    Ledger,
    default_preflight,
    detect_test_command,
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
    # It must steer the agent away from redoing the work / new commits.
    assert "build-agent-response.schema.json" in prompt


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
