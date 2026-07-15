# ABOUTME: Tests for the single-issue `sdlc fix` controller pipeline (issue #436, PR1).
# ABOUTME: Agent dispatch + gh are mocked; the ledger is a real temp SQLite DB.

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sdlc.dispatch import AgentDispatchError, AgentResult, RateLimitError
from sdlc.fix_issue import (
    FIX_STAGE_MODELS,
    FixConfigError,
    FixIssue,
    FixIssueError,
    FixOptions,
    detect_agent_type,
    fetch_issue,
    fix_model,
    issue_story,
    parse_fix_args,
    run_fix,
    stop_reason,
)
from sdlc.issue_host import RunResult
from sdlc.ledger_view import Ledger


# ---------------------------------------------------------------------------
# Fake gh runner + fake dispatcher
# ---------------------------------------------------------------------------


def _issue_json(
    number=1, state="OPEN", assignees=None, labels=None, title="Bug", body="boom"
) -> str:
    return json.dumps(
        {
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "assignees": [{"login": a} for a in (assignees or [])],
            "labels": [{"name": name} for name in (labels or [])],
        }
    )


class FakeGh:
    """Record argv and return canned RunResults for `gh issue view` / `gh api user`."""

    def __init__(self, issue_payload: str, *, user="me", issue_rc=0, issue_err=""):
        self.issue_payload = issue_payload
        self.user = user
        self.issue_rc = issue_rc
        self.issue_err = issue_err
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if "issue view" in joined:
            return RunResult(self.issue_rc, self.issue_payload, self.issue_err)
        if "api user" in joined:
            return RunResult(0, self.user, "")
        return RunResult(0, "", "")


def _default_payload(agent_type: str) -> dict:
    return {
        "investigation": {
            "root_cause": "off-by-one in loop",
            "complexity": "simple",
            "fix_approach": "clamp the index",
            "files_to_modify": ["src/loop.py"],
            "risk": "low",
            "investigation_status": "READY",
        },
        "build": {
            "branch_name": "feature/issue-1",
            "build_status": "SUCCESS",
            "commit_sha": "deadbeef",
        },
        "coverage": {
            "pr_number": 100,
            "pr_url": "https://example/pull/100",
            "coverage_pct": 95.0,
            "tests_added": 2,
            "coverage_status": "PASS",
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
            "merged_at": "2026-07-15T00:00:00Z",
        },
        "bugfix": {
            "failure_category": "TEST_BUG",
            "root_cause": "assertion used wrong operator",
            "fix_status": "FIXED",
            "tests_passing": True,
            "bugs_fixed": 1,
            "tests_fixed": 1,
        },
        "summary": {"summary_markdown": "## Fix complete"},
    }[agent_type]


class RecordingDispatcher:
    """Record (agent_type, model) and return canned responses.

    ``overrides`` maps an agent_type to a dict payload or a callable ``(n)->dict``
    where ``n`` is the zero-based call index for that agent_type (so a stage can
    fail its first attempt and pass the retry).
    """

    def __init__(self, overrides=None):
        self.calls: list[tuple[str, str | None]] = []
        self.counts: dict[str, int] = {}
        self.overrides = overrides or {}

    def __call__(self, agent_type, prompt, *, story=None, model=None,
                 transcript_path=None, on_progress=None, **kwargs):
        self.calls.append((agent_type, model))
        n = self.counts.get(agent_type, 0)
        self.counts[agent_type] = n + 1
        if agent_type in self.overrides:
            payload = self.overrides[agent_type]
            if callable(payload):
                payload = payload(n)
            if isinstance(payload, Exception):
                raise payload
        else:
            payload = _default_payload(agent_type)
        if isinstance(payload, Exception):
            raise payload
        return AgentResult(agent_type=agent_type, data=payload, raw="")

    def agents(self) -> list[str]:
        return [a for a, _ in self.calls]

    def model_for(self, agent_type: str) -> str | None:
        for a, m in self.calls:
            if a == agent_type:
                return m
        return None


def _ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / ".sdlc-state.db")


def _run_count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        conn.close()


def _story_status(db: Path, story_id: str) -> str:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT status FROM stories WHERE story_id = ?", (story_id,)
        ).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Issue adapter
# ---------------------------------------------------------------------------


def test_fetch_issue_parses_gh_json() -> None:
    gh = FakeGh(_issue_json(number=42, title="Crash", body="stacktrace", labels=["bug"]))
    issue = fetch_issue(42, runner=gh)
    assert issue.number == 42
    assert issue.title == "Crash"
    assert issue.body == "stacktrace"
    assert issue.state == "open"
    assert issue.labels == ("bug",)


def test_fetch_issue_nonzero_exit_raises() -> None:
    gh = FakeGh("", issue_rc=1, issue_err="not found")
    with pytest.raises(FixIssueError, match="not found"):
        fetch_issue(999, runner=gh)


def test_fetch_issue_malformed_json_raises() -> None:
    gh = FakeGh("{not json")
    with pytest.raises(FixIssueError, match="malformed JSON"):
        fetch_issue(1, runner=gh)


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------


def test_stop_reason_closed() -> None:
    issue = FixIssue(1, "t", "b", "closed", (), ())
    assert "closed" in stop_reason(issue, runner=FakeGh(""))


def test_stop_reason_wontfix() -> None:
    issue = FixIssue(1, "t", "b", "open", (), ("wontfix",))
    assert "wontfix" in stop_reason(issue, runner=FakeGh(""))


def test_stop_reason_assigned_elsewhere() -> None:
    issue = FixIssue(1, "t", "b", "open", ("someoneelse",), ())
    reason = stop_reason(issue, runner=FakeGh("", user="me"))
    assert "assigned to someoneelse" in reason


def test_stop_reason_none_when_assigned_to_me() -> None:
    issue = FixIssue(1, "t", "b", "open", ("me",), ())
    assert stop_reason(issue, runner=FakeGh("", user="me")) is None


def test_stop_reason_none_for_plain_open_issue() -> None:
    issue = FixIssue(1, "t", "b", "open", (), ("bug",))
    assert stop_reason(issue, runner=FakeGh("")) is None


def test_stop_reason_assignee_unknown_user_does_not_block() -> None:
    # An assignee check that cannot resolve the current user degrades to proceed.
    gh = FakeGh("", user="")  # api user returns empty -> None
    issue = FixIssue(1, "t", "b", "open", ("other",), ())
    assert stop_reason(issue, runner=gh) is None


# ---------------------------------------------------------------------------
# Story adapter + project detection + model routing
# ---------------------------------------------------------------------------


def test_issue_story_branch_id(tmp_path) -> None:
    story = issue_story(FixIssue(77, "Title", "b", "open", (), ()), root=tmp_path)
    assert story.id == "issue-77"  # → feature/issue-77 via feature/{id}
    assert story.title == "Title"


def test_detect_agent_type_python(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert detect_agent_type(tmp_path) == "python-backend-engineer"


def test_detect_agent_type_typescript(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"dependencies":{"typescript":"5"}}', encoding="utf-8")
    assert detect_agent_type(tmp_path) == "backend-typescript-architect"


def test_detect_agent_type_default(tmp_path) -> None:
    assert detect_agent_type(tmp_path) == "general-purpose"


def test_fix_model_map_matches_skill_assignments() -> None:
    opts = FixOptions(issue=1)
    assert fix_model("investigation", opts) == "sonnet"
    assert fix_model("build", opts) == "opus"
    assert fix_model("coverage", opts) == "sonnet"
    assert fix_model("review", opts) == "opus"
    assert fix_model("merge", opts) == "haiku"
    assert fix_model("bugfix", opts) == "opus"
    assert fix_model("summary", opts) == "haiku"


def test_fix_model_override_beats_map() -> None:
    opts = FixOptions(issue=1, model_overrides={"build": "sonnet"})
    assert fix_model("build", opts) == "sonnet"
    assert fix_model("review", opts) == "opus"  # unaffected


# ---------------------------------------------------------------------------
# run_fix — happy path + model routing
# ---------------------------------------------------------------------------


def test_run_fix_happy_path_all_stages_done(tmp_path) -> None:
    gh = FakeGh(_issue_json())
    dispatch = RecordingDispatcher()
    result = run_fix(
        FixOptions(issue=1),
        ledger=_ledger(tmp_path),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=gh,
        root=tmp_path,
    )
    assert result.status == "DONE"
    assert result.pr_number == 100
    agents = dispatch.agents()
    assert {"investigation", "build", "coverage", "review", "merge", "summary"}.issubset(agents)


def test_run_fix_asserts_opus_parity_models(tmp_path) -> None:
    gh = FakeGh(_issue_json())
    dispatch = RecordingDispatcher()
    run_fix(
        FixOptions(issue=1),
        ledger=_ledger(tmp_path),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=gh,
        root=tmp_path,
    )
    # Every stage dispatched on the happy path carries its skill-parity model.
    # (bugfix runs only on failure — asserted in the bugfix-recovery test.)
    for stage in ("investigation", "build", "coverage", "review", "merge", "summary"):
        assert dispatch.model_for(stage) == FIX_STAGE_MODELS[stage], stage


def test_run_fix_exactly_one_run_row(tmp_path) -> None:
    db = tmp_path / ".sdlc-state.db"
    gh = FakeGh(_issue_json())
    run_fix(
        FixOptions(issue=1),
        ledger=Ledger(db),
        dispatcher=RecordingDispatcher(),
        preflight=lambda: True,
        runner=gh,
        root=tmp_path,
    )
    assert _run_count(db) == 1


def test_run_fix_skip_coverage_omits_coverage_stage(tmp_path) -> None:
    gh = FakeGh(_issue_json())
    # In skip-coverage mode the build agent opens the PR, so it carries pr_number.
    dispatch = RecordingDispatcher(
        overrides={
            "build": {
                "branch_name": "feature/issue-1",
                "build_status": "SUCCESS",
                "commit_sha": "deadbeef",
                "pr_number": 100,
            }
        }
    )
    result = run_fix(
        FixOptions(issue=1, skip_coverage=True),
        ledger=_ledger(tmp_path),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=gh,
        root=tmp_path,
    )
    assert result.status == "DONE"
    assert "coverage" not in dispatch.agents()
    assert result.pr_number == 100


# ---------------------------------------------------------------------------
# Investigation BLOCKED
# ---------------------------------------------------------------------------


def test_run_fix_investigation_blocked_aborts(tmp_path) -> None:
    db = tmp_path / ".sdlc-state.db"
    gh = FakeGh(_issue_json())
    dispatch = RecordingDispatcher(
        overrides={
            "investigation": {
                "root_cause": "unclear",
                "complexity": "complex",
                "fix_approach": "needs design decision",
                "files_to_modify": [],
                "risk": "high — ambiguous requirements",
                "investigation_status": "BLOCKED",
            }
        }
    )
    result = run_fix(
        FixOptions(issue=1),
        ledger=Ledger(db),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=gh,
        root=tmp_path,
    )
    assert result.investigation_blocked is True
    assert result.status == "ABORTED"
    # No build/coverage/etc dispatched after a BLOCKED investigation.
    assert dispatch.agents() == ["investigation"]
    assert _run_count(db) == 1  # a run row IS created (investigation ran)
    assert _story_status(db, "issue-1") == "BLOCKED"


# ---------------------------------------------------------------------------
# Bugfix loop
# ---------------------------------------------------------------------------


def test_run_fix_bugfix_recovers_and_retries_stage(tmp_path) -> None:
    gh = FakeGh(_issue_json())
    # build fails its first attempt, succeeds on the retry after the bugfix.
    def build_script(n):
        if n == 0:
            return {"branch_name": "feature/issue-1", "build_status": "FAILED",
                    "commit_sha": "x", "error_summary": "boom"}
        return {"branch_name": "feature/issue-1", "build_status": "SUCCESS", "commit_sha": "y"}

    dispatch = RecordingDispatcher(overrides={"build": build_script})
    result = run_fix(
        FixOptions(issue=1),
        ledger=_ledger(tmp_path),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=gh,
        root=tmp_path,
    )
    assert result.status == "DONE"
    assert dispatch.counts["build"] == 2
    assert dispatch.counts["bugfix"] == 1
    assert dispatch.model_for("bugfix") == "opus"  # skill-parity bugfix model


def test_run_fix_bugfix_bounded_at_two_then_fails(tmp_path) -> None:
    gh = FakeGh(_issue_json())
    # build always fails; bugfix always claims FIXED — the loop must still bound.
    dispatch = RecordingDispatcher(
        overrides={
            "build": {"branch_name": "feature/issue-1", "build_status": "FAILED",
                      "commit_sha": "x", "error_summary": "still broken"},
        }
    )
    result = run_fix(
        FixOptions(issue=1),
        ledger=_ledger(tmp_path),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=gh,
        root=tmp_path,
    )
    assert result.status == "FAILED"
    # 1 first attempt + 2 bugfix retries = 3 build dispatches; bugfix capped at 2.
    assert dispatch.counts["build"] == 3
    assert dispatch.counts["bugfix"] == 2
    # never advanced past build
    assert "merge" not in dispatch.agents()


def test_run_fix_bugfix_unfixed_fails_fast(tmp_path) -> None:
    gh = FakeGh(_issue_json())
    dispatch = RecordingDispatcher(
        overrides={
            "build": {"branch_name": "feature/issue-1", "build_status": "FAILED",
                      "commit_sha": "x", "error_summary": "boom"},
            "bugfix": {"failure_category": "REAL_BUG", "root_cause": "deep",
                       "fix_status": "UNFIXED", "tests_passing": False,
                       "bugs_fixed": 0, "tests_fixed": 0},
        }
    )
    result = run_fix(
        FixOptions(issue=1),
        ledger=_ledger(tmp_path),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=gh,
        root=tmp_path,
    )
    assert result.status == "FAILED"
    # first build FAILED, one bugfix returns UNFIXED → stop; build not retried.
    assert dispatch.counts["build"] == 1
    assert dispatch.counts["bugfix"] == 1


# ---------------------------------------------------------------------------
# Merge parking on the high-risk approval gate
# ---------------------------------------------------------------------------


def test_run_fix_merge_awaiting_approval_parks(tmp_path) -> None:
    gh = FakeGh(_issue_json())
    dispatch = RecordingDispatcher(
        overrides={
            "merge": {"pr_number": 100, "merge_status": "FAILED", "merge_sha": "",
                      "merged_at": "", "block_reason": "BLOCKED_HIGH_RISK"},
        }
    )
    result = run_fix(
        FixOptions(issue=1),
        ledger=_ledger(tmp_path),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=gh,
        root=tmp_path,
    )
    assert result.status == "AWAITING_APPROVAL"
    # parked before any bugfix — the loop cannot self-approve.
    assert "bugfix" not in dispatch.agents()


# ---------------------------------------------------------------------------
# Preflight + stop-condition orchestration
# ---------------------------------------------------------------------------


def test_run_fix_preflight_failure_returns_early(tmp_path) -> None:
    db = tmp_path / ".sdlc-state.db"
    gh = FakeGh(_issue_json())
    dispatch = RecordingDispatcher()
    result = run_fix(
        FixOptions(issue=1),
        ledger=Ledger(db),
        dispatcher=dispatch,
        preflight=lambda: False,
        runner=gh,
        root=tmp_path,
    )
    assert result.preflight_failed is True
    assert dispatch.calls == []  # no dispatch when preflight is red
    assert not db.exists()  # no run row created before preflight passes


def test_run_fix_skip_preflight_does_not_call_preflight(tmp_path) -> None:
    gh = FakeGh(_issue_json())

    def _boom() -> bool:
        raise AssertionError("preflight must not run under --skip-preflight")

    result = run_fix(
        FixOptions(issue=1, skip_preflight=True),
        ledger=_ledger(tmp_path),
        dispatcher=RecordingDispatcher(),
        preflight=_boom,
        runner=gh,
        root=tmp_path,
    )
    assert result.status == "DONE"


def test_run_fix_stop_condition_creates_no_run_row(tmp_path) -> None:
    db = tmp_path / ".sdlc-state.db"
    gh = FakeGh(_issue_json(state="CLOSED"))
    dispatch = RecordingDispatcher()
    result = run_fix(
        FixOptions(issue=1),
        ledger=Ledger(db),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=gh,
        root=tmp_path,
    )
    assert result.aborted is True
    assert "closed" in result.abort_reason
    assert dispatch.calls == []
    assert not db.exists()


def test_run_fix_fetch_error_aborts_cleanly(tmp_path) -> None:
    gh = FakeGh("", issue_rc=1, issue_err="gh: not found")
    result = run_fix(
        FixOptions(issue=1),
        ledger=_ledger(tmp_path),
        dispatcher=RecordingDispatcher(),
        preflight=lambda: True,
        runner=gh,
        root=tmp_path,
    )
    assert result.aborted is True
    assert result.status == "ABORTED"


def test_run_fix_rate_limit_parks(tmp_path) -> None:
    from sdlc.rate_limit import RateLimitSignal

    gh = FakeGh(_issue_json())
    dispatch = RecordingDispatcher(
        overrides={"build": RateLimitError("throttled", signal=RateLimitSignal(source="429"))}
    )
    result = run_fix(
        FixOptions(issue=1),
        ledger=_ledger(tmp_path),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=gh,
        root=tmp_path,
    )
    assert result.status == "RATE_LIMITED"


def test_run_fix_investigation_dispatch_error_fails(tmp_path) -> None:
    gh = FakeGh(_issue_json())
    dispatch = RecordingDispatcher(
        overrides={"investigation": AgentDispatchError("agent crashed")}
    )
    result = run_fix(
        FixOptions(issue=1),
        ledger=_ledger(tmp_path),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=gh,
        root=tmp_path,
    )
    assert result.status == "FAILED"
    assert dispatch.agents() == ["investigation"]


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_parse_fix_args_single_issue() -> None:
    opts = parse_fix_args(["123"])
    assert opts.issue == 123
    assert opts.skip_coverage is False
    assert opts.coverage_threshold == 90


def test_parse_fix_args_flags() -> None:
    opts = parse_fix_args(["7", "--skip-coverage", "--coverage-threshold=80", "--skip-preflight"])
    assert opts.issue == 7
    assert opts.skip_coverage is True
    assert opts.coverage_threshold == 80
    assert opts.skip_preflight is True


def test_parse_fix_args_batch_target_is_coming_later() -> None:
    with pytest.raises(FixConfigError, match="later release"):
        parse_fix_args(["all"])
    with pytest.raises(FixConfigError, match="later release"):
        parse_fix_args(["next"])


def test_parse_fix_args_non_numeric_issue() -> None:
    with pytest.raises(FixConfigError, match="invalid issue"):
        parse_fix_args(["frobnicate"])


def test_parse_fix_args_missing_issue() -> None:
    with pytest.raises(FixConfigError, match="missing issue"):
        parse_fix_args(["--skip-coverage"])


def test_parse_fix_args_unknown_flag() -> None:
    with pytest.raises(FixConfigError, match="unknown flag"):
        parse_fix_args(["1", "--frobnicate"])


def test_parse_fix_args_extra_positional() -> None:
    with pytest.raises(FixConfigError, match="extra argument"):
        parse_fix_args(["1", "2"])
