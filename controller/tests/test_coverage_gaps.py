# ABOUTME: Additional tests targeting uncovered branches from Story 7.3-001 QA gate.
# ABOUTME: Covers bugfix exhaustion, dispatch OS error, render view skip,
# ABOUTME: CLI build preflight/failure exit paths, discovery edge cases,
# ABOUTME: and several minor build.py branches missed by the baseline suite.

from __future__ import annotations

import subprocess
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdlc.build import (
    BuildOptions,
    Ledger,
    parse_build_args,
    run_build,
    _stage_succeeded,
    _stage_failure_summary,
    _extract_pr,
)
from sdlc.cohort import Story
from sdlc.cli import app
from sdlc.dispatch import AgentDispatchError, AgentResult, dispatch_agent
from sdlc.discovery import discover_queue, parse_epic_file


runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers shared across this module
# ---------------------------------------------------------------------------

def _story(sid: str, deps: list[str] | None = None) -> Story:
    return Story(
        id=sid,
        title=f"Story {sid}",
        epic_id="99",
        epic_name="sample",
        epic_file="epic-99.md",
        priority="P2",
        points=2,
        agent_type="py",
        dependencies=list(deps or []),
    )


def _sample_queue() -> list[Story]:
    return [_story("s1-001"), _story("s1-002"), _story("s1-003", deps=["s1-001"])]


class FakeDispatcher:
    """Records calls; returns schema-valid canned responses or raises on demand."""

    def __init__(self, overrides=None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.overrides: dict = overrides or {}

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        self.calls.append((agent_type, getattr(story, "id", "")))
        key = (agent_type, getattr(story, "id", None))
        payload_or_exc = self.overrides.get(key)
        if payload_or_exc is not None:
            if callable(payload_or_exc):
                payload_or_exc = payload_or_exc()
            if isinstance(payload_or_exc, Exception):
                raise payload_or_exc
            return AgentResult(agent_type=agent_type, data=payload_or_exc, raw="")
        return AgentResult(agent_type=agent_type, data=_default_payload(agent_type, story), raw="")


def _default_payload(agent_type: str, story) -> dict:
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


def _open_db(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(db)


# ---------------------------------------------------------------------------
# build.py — parse_build_args: extra positionals fold into a canonical scope
# ---------------------------------------------------------------------------

def test_parse_build_args_accepts_second_positional() -> None:
    """Story 19.1-001: a second bare positional is no longer an error — every
    positional is collected and folded into one canonical (sorted, deduped,
    comma-joined) scope label."""
    opts = parse_build_args(["epic-07", "extra"])
    assert opts.scope == "epic-07,extra"


# ---------------------------------------------------------------------------
# build.py — run_update_status non-terminal branch (line 198)
# ---------------------------------------------------------------------------

def test_ledger_run_update_status_non_terminal(tmp_path) -> None:
    """A non-terminal status update (e.g. IN_PROGRESS) must not stamp finished_at."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-07", "parallel")
    # Transition back to IN_PROGRESS (the non-terminal branch).
    ledger.run_update_status(run_id, "IN_PROGRESS")
    conn = _open_db(tmp_path / "ledger.db")
    row = conn.execute(
        "SELECT status, finished_at FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert row[0] == "IN_PROGRESS"
    assert row[1] is None  # finished_at must not be set for non-terminal


# ---------------------------------------------------------------------------
# build.py — render_view called when provided (line 583)
# ---------------------------------------------------------------------------

def test_run_build_calls_render_view_when_provided(tmp_path) -> None:
    """When a render_view hook is given, it is invoked with the run_id."""
    db = tmp_path / "ledger.db"
    rendered: list[str] = []
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    run_build(
        opts,
        queue=_sample_queue(),
        ledger=Ledger(db),
        dispatcher=FakeDispatcher(),
        preflight=lambda: True,
        render_view=rendered.append,
    )
    assert len(rendered) == 1
    assert rendered[0]  # a non-empty run_id was passed


# ---------------------------------------------------------------------------
# build.py — bugfix loop exhaustion (line 635): MAX_BUGFIX_ATTEMPTS exceeded
# ---------------------------------------------------------------------------

def test_bugfix_loop_exhaustion_marks_story_failed(tmp_path) -> None:
    """The MAX_BUGFIX_ATTEMPTS guard fires: story is FAILED when limit is reached.

    We use a stub Ledger that silently swallows stage_start duplicates (the
    real schema enforces UNIQUE per attempt — a pre-existing design constraint
    that limits the real loop to a single bugfix per stage). The stub lets us
    drive the state machine to the MAX_BUGFIX_ATTEMPTS >= N guard on line 635
    to verify its logic in isolation.
    """
    from sdlc.build import MAX_BUGFIX_ATTEMPTS, _run_story

    story = _story("s1-zzz")

    build_call_count = {"n": 0}
    bugfix_call_count = {"n": 0}

    def always_fail_build(agent_type, prompt, story=None, **kw):
        build_call_count["n"] += 1
        from sdlc.dispatch import AgentResult
        return AgentResult(agent_type="build", data={
            "branch_name": "feature/s1-zzz",
            "build_status": "FAILED",
            "commit_sha": "0",
            "error_summary": "persistent boom",
        }, raw="")

    def always_succeed_bugfix(agent_type, prompt, story=None, **kw):
        bugfix_call_count["n"] += 1
        from sdlc.dispatch import AgentResult
        return AgentResult(agent_type="bugfix", data={
            "failure_category": "CODE_BUG",
            "fix_status": "FIXED",
            "tests_passing": True,
            "bugs_fixed": 1,
            "tests_fixed": 0,
        }, raw="")

    def dispatch(agent_type, prompt, story=None, **kw):
        if agent_type == "build":
            return always_fail_build(agent_type, prompt, story=story)
        return always_succeed_bugfix(agent_type, prompt, story=story)

    # A ledger stub that is a no-op so the UNIQUE constraint never fires.
    class _SilentLedger:
        def stage_start(self, *a, **k): pass
        def stage_finish(self, *a, **k): pass
        def event_log(self, *a, **k): pass
        def set_story_pr(self, *a, **k): pass
        def set_story_status(self, *a, **k): pass

    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    outcome = _run_story(story, opts, _SilentLedger(), "run-id", dispatch, tmp_path / "logs")  # type: ignore[arg-type]

    # After MAX_BUGFIX_ATTEMPTS the guard fires and the story is FAILED.
    assert outcome == "FAILED"
    # Bugfix was called exactly MAX_BUGFIX_ATTEMPTS times.
    assert bugfix_call_count["n"] == MAX_BUGFIX_ATTEMPTS
    # Build was called once more than bugfix (the final failure with no more retries).
    assert build_call_count["n"] == MAX_BUGFIX_ATTEMPTS + 1


# ---------------------------------------------------------------------------
# build.py — AgentDispatchError in _dispatch_stage (lines 665-666)
# ---------------------------------------------------------------------------

def test_dispatch_error_routes_to_bugfix(tmp_path) -> None:
    """An AgentDispatchError from the agent subprocess routes to the bugfix loop."""
    db = tmp_path / "ledger.db"

    call_count = {"n": 0}

    def build_with_dispatch_error():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise AgentDispatchError("subprocess timed out")
        return {
            "branch_name": "feature/s1-001",
            "build_status": "SUCCESS",
            "commit_sha": "abc",
        }

    dispatcher = FakeDispatcher(
        overrides={("build", "s1-001"): build_with_dispatch_error}
    )
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    result = run_build(
        opts,
        queue=[_story("s1-001")],
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    # Bugfix was attempted after the dispatch error.
    assert ("bugfix", "s1-001") in dispatcher.calls
    # After the fix the story succeeded.
    assert result.completed >= 1


# ---------------------------------------------------------------------------
# build.py — _stage_succeeded: unknown stage returns False (line 695)
# ---------------------------------------------------------------------------

def test_stage_succeeded_unknown_stage_returns_false() -> None:
    """An unknown stage name is conservatively treated as a failure."""
    assert _stage_succeeded("nonexistent", {}) is False


# ---------------------------------------------------------------------------
# build.py — _stage_failure_summary: non-build stage (line 701)
# ---------------------------------------------------------------------------

def test_stage_failure_summary_non_build_stage() -> None:
    """A non-build stage failure summary uses the generic template."""
    summary = _stage_failure_summary("review", {"final_status": "CHANGES_REQUESTED"})
    assert "review" in summary
    assert "non-success" in summary


# ---------------------------------------------------------------------------
# build.py — _extract_pr: result is None returns current (line 706)
# ---------------------------------------------------------------------------

def test_extract_pr_none_result_returns_current() -> None:
    """When result is None, the existing pr_number is preserved."""
    assert _extract_pr(None, 42) == 42
    assert _extract_pr(None, None) is None


# ---------------------------------------------------------------------------
# build.py — _run_bugfix: ContractError/AgentDispatchError during bugfix
# (lines 729-734)
# ---------------------------------------------------------------------------

def test_bugfix_dispatch_error_story_fails(tmp_path) -> None:
    """An AgentDispatchError during the bugfix agent itself marks the story FAILED."""
    db = tmp_path / "ledger.db"

    dispatcher = FakeDispatcher(
        overrides={
            ("build", "s1-001"): {
                "branch_name": "feature/s1-001",
                "build_status": "FAILED",
                "commit_sha": "0",
                "error_summary": "build error",
            },
            ("bugfix", "s1-001"): AgentDispatchError("bugfix agent unreachable"),
        }
    )
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    result = run_build(
        opts,
        queue=[_story("s1-001")],
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert result.failed == 1
    conn = _open_db(db)
    s_status = conn.execute(
        "SELECT status FROM stories WHERE story_id='s1-001'"
    ).fetchone()[0]
    assert s_status == "FAILED"


def test_bugfix_contract_error_story_fails(tmp_path) -> None:
    """A ContractError during the bugfix agent itself marks the story FAILED."""
    from sdlc.contracts import ContractError

    db = tmp_path / "ledger.db"

    dispatcher = FakeDispatcher(
        overrides={
            ("build", "s1-001"): {
                "branch_name": "feature/s1-001",
                "build_status": "FAILED",
                "commit_sha": "0",
                "error_summary": "build error",
            },
            ("bugfix", "s1-001"): ContractError("bugfix schema invalid"),
        }
    )
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    result = run_build(
        opts,
        queue=[_story("s1-001")],
        ledger=Ledger(db),
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    assert result.failed == 1


# ---------------------------------------------------------------------------
# dispatch.py — FileNotFoundError/OSError path (lines 84-85)
# ---------------------------------------------------------------------------

def test_dispatch_raises_on_file_not_found(monkeypatch) -> None:
    """FileNotFoundError from subprocess is wrapped as AgentDispatchError."""

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("No such file: fake-claude")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AgentDispatchError, match="could not launch"):
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])


def test_dispatch_raises_on_os_error(monkeypatch) -> None:
    """An OSError from subprocess (e.g. permission denied) is also wrapped."""

    def fake_run(cmd, **kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AgentDispatchError, match="could not launch"):
        dispatch_agent("build", "prompt", agent_cmd=["fake-claude"])


# ---------------------------------------------------------------------------
# discovery.py — no story dir (line 111: story_dir is None)
# ---------------------------------------------------------------------------

def test_discover_queue_no_story_dir_returns_empty(tmp_path, monkeypatch) -> None:
    """When neither docs/stories nor stories/ exists, the queue is empty."""
    monkeypatch.chdir(tmp_path)
    # tmp_path has no docs/stories or stories directory.
    assert discover_queue("all") == []


# ---------------------------------------------------------------------------
# discovery.py — name-based scope matching (lines 124-125)
# ---------------------------------------------------------------------------

def test_discover_queue_scope_by_name(tmp_path, monkeypatch) -> None:
    """A bare name (not epic-NN prefix) matches epics whose stem contains it."""
    stories_dir = tmp_path / "docs" / "stories"
    stories_dir.mkdir(parents=True)
    (stories_dir / "epic-07-external-controller.md").write_text(
        "##### Story 7.1-001: Controller init\n"
        "**Priority**: P1\n**Points**: 2\n**Dependencies**: None.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    queue = discover_queue("external-controller")
    assert len(queue) == 1
    assert queue[0].id == "7.1-001"


# ---------------------------------------------------------------------------
# ledger_view.py — render skipped when progress dir absent (line 55)
# ---------------------------------------------------------------------------

def test_render_view_skips_when_progress_dir_absent(tmp_path, monkeypatch) -> None:
    """The render hook is a no-op when docs/stories/ doesn't exist (no crash)."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "sdlc-state.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    # Note: do NOT create docs/stories/.

    import sdlc.ledger_view as lv

    calls: list[object] = []
    monkeypatch.setattr(lv.shutil, "which", lambda _: "/bin/bash")
    monkeypatch.setattr(lv.subprocess, "run", lambda *a, **k: calls.append(a) or type("_C", (), {"returncode": 0})())

    hook = lv.make_render_view(tmp_path / ".sdlc-state.db", tmp_path)
    assert hook is not None
    # Calling the hook when the progress parent dir is absent must not raise.
    hook("run-123")
    # subprocess.run should NOT have been called (the guard returns early).
    assert calls == []


# ---------------------------------------------------------------------------
# CLI build: preflight failure exits with code 1 (cli.py lines 94-98)
# ---------------------------------------------------------------------------

def test_cli_build_preflight_failure_exits_1(tmp_path, monkeypatch) -> None:
    """sdlc build exits 1 and prints an actionable message when preflight fails."""
    stories_dir = tmp_path / "docs" / "stories"
    stories_dir.mkdir(parents=True)
    (stories_dir / "epic-99-sample.md").write_text(
        "##### Story 99.1-001: One\n**Priority**: P1\n**Points**: 1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    # Patch run_build to simulate a preflight failure.
    import sdlc.cli as cli_mod
    from sdlc.build import BuildResult

    monkeypatch.setattr(
        cli_mod,
        "run_build" if hasattr(cli_mod, "run_build") else "_unused",
        lambda *a, **k: BuildResult(preflight_failed=True),
        raising=False,
    )

    # We need to patch the run_build imported inside the build() function.
    import sdlc.build as build_mod

    original_run_build = build_mod.run_build
    monkeypatch.setattr(build_mod, "run_build", lambda *a, **k: BuildResult(preflight_failed=True))

    result = runner.invoke(app, ["build", "epic-99"])
    assert result.exit_code == 1
    assert "pre_flight" in result.output.lower() or "preflight" in result.output.lower()

    monkeypatch.setattr(build_mod, "run_build", original_run_build)


# ---------------------------------------------------------------------------
# CLI build: completed with failures exits with code 1 (cli.py lines 104-108)
# ---------------------------------------------------------------------------

def test_cli_build_completed_with_failures_exits_1(tmp_path, monkeypatch) -> None:
    """sdlc build exits 1 when any story failed (not a preflight issue)."""
    stories_dir = tmp_path / "docs" / "stories"
    stories_dir.mkdir(parents=True)
    (stories_dir / "epic-99-sample.md").write_text(
        "##### Story 99.1-001: One\n**Priority**: P1\n**Points**: 1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    import sdlc.build as build_mod
    from sdlc.build import BuildResult

    monkeypatch.setattr(
        build_mod,
        "run_build",
        lambda *a, **k: BuildResult(completed=0, failed=1, run_id="test-run"),
    )

    result = runner.invoke(app, ["build", "epic-99", "--skip-preflight"])
    assert result.exit_code == 1
    assert "failed" in result.output.lower()


def test_cli_build_completed_with_blocked_exits_1(tmp_path, monkeypatch) -> None:
    """sdlc build exits 1 when any story is blocked."""
    stories_dir = tmp_path / "docs" / "stories"
    stories_dir.mkdir(parents=True)
    (stories_dir / "epic-99-sample.md").write_text(
        "##### Story 99.1-001: One\n**Priority**: P1\n**Points**: 1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    import sdlc.build as build_mod
    from sdlc.build import BuildResult

    monkeypatch.setattr(
        build_mod,
        "run_build",
        lambda *a, **k: BuildResult(completed=0, blocked=1, run_id="test-run"),
    )

    result = runner.invoke(app, ["build", "epic-99", "--skip-preflight"])
    assert result.exit_code == 1


def test_cli_build_all_done_exits_0(tmp_path, monkeypatch) -> None:
    """sdlc build exits 0 when all stories complete without failure."""
    stories_dir = tmp_path / "docs" / "stories"
    stories_dir.mkdir(parents=True)
    (stories_dir / "epic-99-sample.md").write_text(
        "##### Story 99.1-001: One\n**Priority**: P1\n**Points**: 1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    import sdlc.build as build_mod
    from sdlc.build import BuildResult

    monkeypatch.setattr(
        build_mod,
        "run_build",
        lambda *a, **k: BuildResult(completed=1, failed=0, blocked=0, run_id="test-run"),
    )

    result = runner.invoke(app, ["build", "epic-99", "--skip-preflight"])
    assert result.exit_code == 0
    assert "done" in result.output.lower()


# ---------------------------------------------------------------------------
# build.py — _run_story envelope-recovery branch (Story 14.2-003 touched this
# function). A contract-error stage that is recovered by the envelope-only
# re-ask (12.1-001) must still extract any PR number from the recovered result
# and persist it (line ~3051), exactly as a first-pass success would.
# ---------------------------------------------------------------------------

def test_envelope_recovered_stage_persists_pr_number(tmp_path) -> None:
    """A coverage stage recovered via envelope re-ask records its PR number.

    The first ``coverage`` dispatch raises a ContractError (missing/garbled
    result block); the bounded envelope-only re-ask then returns a schema-valid
    coverage payload carrying ``pr_number``. The recovered PR must be persisted
    on the story row just as a normal success would persist it.
    """
    from sdlc.contracts import ContractError

    db = tmp_path / "ledger.db"
    state = {"n": 0}

    def coverage_contract_then_ok():
        state["n"] += 1
        if state["n"] == 1:
            return ContractError("coverage result envelope missing")
        return {
            "pr_number": 4242,
            "pr_url": "https://example/pull/4242",
            "coverage_pct": 96.0,
            "tests_added": 2,
            "coverage_status": "PASS",
            "security_status": "PASS",
        }

    dispatcher = FakeDispatcher(
        overrides={("coverage", "s1-001"): coverage_contract_then_ok}
    )
    # Spy on every set_story_pr call so we can prove the *recovered* PR number was
    # persisted by the envelope-recovery branch (line ~3051) specifically — the
    # final stored value alone can't, since the later merge stage overwrites it
    # with its own PR number and would mask a regression in that branch.
    ledger = Ledger(db)
    pr_persisted: list[int] = []
    _orig_set_story_pr = ledger.set_story_pr

    def _spy_set_story_pr(run_id, story_id, pr_number):
        pr_persisted.append(pr_number)
        return _orig_set_story_pr(run_id, story_id, pr_number)

    ledger.set_story_pr = _spy_set_story_pr  # type: ignore[method-assign]

    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    result = run_build(
        opts,
        queue=[_story("s1-001")],
        ledger=ledger,
        dispatcher=dispatcher,
        preflight=lambda: True,
    )
    # Coverage was dispatched twice: the contract failure, then the re-ask.
    assert sum(1 for a, sid in dispatcher.calls if a == "coverage") == 2
    assert result.completed >= 1
    # The envelope re-ask actually recovered the coverage stage...
    conn = _open_db(db)
    events = [
        r[0]
        for r in conn.execute(
            "SELECT message FROM events WHERE story_id='s1-001'"
        ).fetchall()
    ]
    assert any("envelope re-ask recovered the coverage result" in m for m in events)
    # ...and its recovered PR number (not merge's) was extracted and persisted by
    # the recovery branch. This fails if line ~3051's set_story_pr is removed.
    assert 4242 in pr_persisted


# ---------------------------------------------------------------------------
# build.py — _run_story envelope-recovery branch: an envelope-recovered
# build/coverage stage whose commit still fails commitlint after the bounded
# re-asks must park the story NEEDS_ATTENTION (line ~3062), never advance a
# non-compliant header to a PR. (Function touched by Story 14.2-003.)
# ---------------------------------------------------------------------------

def test_envelope_recovered_stage_parks_on_lint_failure(tmp_path, monkeypatch) -> None:
    """Recovered stage with a still-non-compliant commit parks NEEDS_ATTENTION."""
    from sdlc.contracts import ContractError
    import sdlc.build as build_mod
    from sdlc.build import _run_story

    # The envelope-recovered build stage's commit fails commitlint even after the
    # bounded re-asks: force the shared lint helper to report non-compliant.
    monkeypatch.setattr(build_mod, "_lint_stage_commit", lambda *a, **k: (0, False))

    state = {"n": 0}

    def build_contract_then_ok():
        state["n"] += 1
        if state["n"] == 1:
            return ContractError("build result envelope missing")
        return {
            "branch_name": "feature/s1-001",
            "build_status": "SUCCESS",
            "commit_sha": "abc123",
        }

    dispatcher = FakeDispatcher(
        overrides={("build", "s1-001"): build_contract_then_ok}
    )
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.story_upsert(
        run_id, "s1-001", "epic-99", "sample", "Story s1-001", 2, "py", "", None, "TODO"
    )
    opts = BuildOptions(scope="epic-99", skip_preflight=True, sequential=True)
    outcome = _run_story(
        _story("s1-001"), opts, ledger, run_id, dispatcher, tmp_path / "logs"
    )
    assert outcome == "NEEDS_ATTENTION"
    # The build stage went through the envelope re-ask before parking.
    assert sum(1 for a, sid in dispatcher.calls if a == "build") == 2
