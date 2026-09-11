# ABOUTME: Issue #685 — operator files must survive a run: deny floor, drift check, self-report.
# ABOUTME: Real temp git repos for the snapshot; dispatch is faked, the ledger is real SQLite.

from __future__ import annotations

import fnmatch
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from test_dispatch import _FakePopen, _stream_result_event, _wrap

from sdlc.build import operator_files_damage, operator_files_snapshot
from sdlc.dispatch import (
    DENY_BASELINE_ENV,
    DESTRUCTIVE_DENY_FLOOR,
    READ_ONLY_ROLES,
    AgentResult,
    dispatch_agent,
    resolve_agent_cmd,
    resolve_deny_rules,
)
from sdlc.fix_issue import FixOptions, _self_reported_mistake, run_fix
from sdlc.harness import resolve_harness
from sdlc.issue_host import RunResult
from sdlc.ledger_view import Ledger

# The commands the #685 review agent (and its cousins) could use to destroy
# uncommitted operator state in the shared checkout.
_DESTRUCTIVE_COMMANDS = (
    "rm -f /repo/REVIEW.md 2>/dev/null",
    "rm -rf notes/",
    "git clean -fd",
    "git clean -fdx",
    "git checkout -- .",
    "git checkout -- REVIEW.md",
    "git restore REVIEW.md",
    "git restore --staged .",
    "git reset --hard",
    "git reset --hard HEAD~1",
    "git stash",
    "git stash push -u",
)


def _bash_matches(rules: list[str], command: str) -> bool:
    return any(
        rule.startswith("Bash(") and fnmatch.fnmatch(command, rule[len("Bash("):-1])
        for rule in rules
    )


def _deny_arg(cmd: list[str]) -> list[str]:
    return cmd[cmd.index("--disallowedTools") + 1].split(",")


# ---------------------------------------------------------------------------
# Defect 1: a destructive-action floor for read-only roles
# ---------------------------------------------------------------------------


def test_read_only_roles_cover_review_investigation_and_summary() -> None:
    assert {"review", "investigation", "summary"} <= READ_ONLY_ROLES


@pytest.mark.parametrize("role", sorted(READ_ONLY_ROLES))
def test_read_only_role_denies_every_destructive_command(role, monkeypatch) -> None:
    monkeypatch.delenv(DENY_BASELINE_ENV, raising=False)
    rules = resolve_deny_rules(role)
    for command in _DESTRUCTIVE_COMMANDS:
        assert _bash_matches(rules, command), f"{role} may still run {command!r}"


@pytest.mark.parametrize("role", ["build", "bugfix", "coverage", "merge", "e2e", None])
def test_writing_roles_keep_the_plain_baseline(role, monkeypatch) -> None:
    """Build/bugfix legitimately delete files — the floor is scoped to read-only roles."""
    monkeypatch.delenv(DENY_BASELINE_ENV, raising=False)
    rules = resolve_deny_rules(role)
    assert not any(rule in rules for rule in DESTRUCTIVE_DENY_FLOOR)


def test_floor_does_not_block_read_only_git_work(monkeypatch) -> None:
    """A reviewer still reads history and diffs, and checks out a PR branch."""
    monkeypatch.delenv(DENY_BASELINE_ENV, raising=False)
    rules = resolve_deny_rules("review")
    for command in (
        "git diff origin/main...HEAD",
        "git log --oneline -5",
        "git status --porcelain",
        "git checkout feature/issue-1",
        "gh pr diff 100",
        "grep -rn rm src/",
    ):
        assert not _bash_matches(rules, command), f"floor wrongly blocks {command!r}"


def test_empty_baseline_override_keeps_the_read_only_floor(monkeypatch) -> None:
    """The per-repo opt-out relaxes the secret/egress baseline, not operator-data safety."""
    monkeypatch.setenv(DENY_BASELINE_ENV, "")
    assert resolve_deny_rules("review") == list(DESTRUCTIVE_DENY_FLOOR)
    assert resolve_deny_rules("build") == []


def test_floor_is_not_duplicated_when_the_override_already_lists_it(monkeypatch) -> None:
    monkeypatch.setenv(DENY_BASELINE_ENV, "Bash(rm *),Bash(ssh *)")
    rules = resolve_deny_rules("review")
    assert rules.count("Bash(rm *)") == 1
    assert "Bash(ssh *)" in rules


def test_review_command_carries_the_floor(monkeypatch) -> None:
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    monkeypatch.delenv(DENY_BASELINE_ENV, raising=False)
    assert "Bash(rm *)" in _deny_arg(resolve_agent_cmd(role="review"))
    assert "Bash(rm *)" not in _deny_arg(resolve_agent_cmd(role="build"))


def test_explicit_cmd_still_owns_its_posture_for_read_only_roles() -> None:
    assert resolve_agent_cmd(["my", "agent"], role="review") == ["my", "agent"]


def test_dispatched_review_subprocess_receives_the_floor(monkeypatch) -> None:
    """Regression (#685): the review agent's real argv denies `rm`, `git clean`, …"""
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    monkeypatch.delenv(DENY_BASELINE_ENV, raising=False)
    seen: dict = {}
    review = {
        "pr_number": 1, "approval_status": "APPROVED",
        "change_count": 0, "final_status": "APPROVED",
    }

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        return _FakePopen([_stream_result_event(_wrap(review))])

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    dispatch_agent("review", "prompt")
    deny = _deny_arg(seen["cmd"])
    for command in _DESTRUCTIVE_COMMANDS:
        assert _bash_matches(deny, command), f"review agent may still run {command!r}"


def test_routed_builtin_harness_carries_the_floor_for_its_stage(monkeypatch) -> None:
    """A `--harness` map routes fix stages through `to_argv(stage=…)` — floor included."""
    monkeypatch.delenv("SDLC_AGENT_CMD", raising=False)
    monkeypatch.delenv(DENY_BASELINE_ENV, raising=False)
    harness = resolve_harness(None, env={})
    assert "Bash(rm *)" in _deny_arg(harness.to_argv(stage="review"))
    assert "Bash(rm *)" not in _deny_arg(harness.to_argv(stage="build"))


# ---------------------------------------------------------------------------
# Defect 2a: pre-existing operator files are snapshotted and re-checked
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "tracked.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "tracked.md")
    _git(root, "commit", "-q", "-m", "chore: base")
    return root


def test_snapshot_is_none_outside_a_git_repo(tmp_path) -> None:
    assert operator_files_snapshot(tmp_path) is None
    assert operator_files_damage(tmp_path, None) == []


def test_untouched_operator_files_report_no_damage(tmp_path) -> None:
    root = _init_repo(tmp_path)
    (root / "REVIEW.md").write_text("362 lines of review\n", encoding="utf-8")
    snap = operator_files_snapshot(root)
    assert snap is not None and "REVIEW.md" in snap
    assert operator_files_damage(root, snap) == []


def test_deleted_untracked_operator_file_is_reported(tmp_path) -> None:
    """The #685 incident: `rm -f REVIEW.md` on an untracked, unrecoverable file."""
    root = _init_repo(tmp_path)
    (root / "REVIEW.md").write_text("362 lines of review\n", encoding="utf-8")
    snap = operator_files_snapshot(root)
    (root / "REVIEW.md").unlink()
    assert operator_files_damage(root, snap) == ["REVIEW.md (deleted)"]


def test_modified_operator_files_are_reported(tmp_path) -> None:
    root = _init_repo(tmp_path)
    (root / "notes").mkdir()
    (root / "notes" / "scratch.md").write_text("mine\n", encoding="utf-8")
    (root / "tracked.md").write_text("operator edit\n", encoding="utf-8")
    snap = operator_files_snapshot(root)
    (root / "notes" / "scratch.md").write_text("agent\n", encoding="utf-8")
    _git(root, "checkout", "--", "tracked.md")  # reverted to HEAD — operator edit gone
    assert sorted(operator_files_damage(root, snap)) == [
        "notes/scratch.md (modified)",
        "tracked.md (modified)",
    ]


def test_files_created_after_the_snapshot_are_not_damage(tmp_path) -> None:
    root = _init_repo(tmp_path)
    snap = operator_files_snapshot(root)
    (root / "agent-output.txt").write_text("new\n", encoding="utf-8")
    (root / "tracked.md").write_text("the fix itself\n", encoding="utf-8")
    assert operator_files_damage(root, snap) == []


def test_controller_ledger_churn_is_not_operator_damage(tmp_path) -> None:
    """The ledger DB (and its logs) grow every stage — never an operator file."""
    root = _init_repo(tmp_path)
    (root / ".sdlc-state.db").write_bytes(b"v1")
    (root / ".sdlc-state.db.logs").mkdir()
    (root / ".sdlc-state.db.logs" / "a.log").write_text("x", encoding="utf-8")
    snap = operator_files_snapshot(root)
    assert snap == {}
    (root / ".sdlc-state.db").write_bytes(b"v2")
    assert operator_files_damage(root, snap) == []


def test_damage_check_degrades_when_the_tree_becomes_uninspectable(tmp_path) -> None:
    """A snapshot taken, then a vanished checkout, must not crash the run."""
    root = _init_repo(tmp_path)
    (root / "REVIEW.md").write_text("x\n", encoding="utf-8")
    snap = operator_files_snapshot(root)
    assert operator_files_damage(tmp_path / "gone", snap) == ["REVIEW.md (deleted)"]


def test_snapshot_returns_none_when_git_invocation_raises(tmp_path, monkeypatch) -> None:
    """An un-inspectable tree (git missing, timeout) disables the check, not the run."""
    root = _init_repo(tmp_path)

    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr("sdlc.build._git", boom)
    assert operator_files_snapshot(root) is None


def test_progress_view_path_is_excluded_from_the_snapshot(tmp_path) -> None:
    """The controller's own render (issue #610) is churn, never operator data."""
    root = _init_repo(tmp_path)
    (root / "docs" / "stories").mkdir(parents=True)
    (root / "docs" / "stories" / ".build-progress.md").write_text("render\n", encoding="utf-8")
    assert operator_files_snapshot(root) == {}


def test_symlinked_operator_file_digests_its_target_string(tmp_path) -> None:
    """A symlink is compared by its target string, not by following and hashing it."""
    root = _init_repo(tmp_path)
    (root / "link.txt").symlink_to("REVIEW.md")
    snap = operator_files_snapshot(root)
    assert snap == {"link.txt": "link:REVIEW.md"}
    assert operator_files_damage(root, snap) == []
    (root / "link.txt").unlink()
    (root / "link.txt").symlink_to("other.md")
    assert operator_files_damage(root, snap) == ["link.txt (modified)"]


def test_pre_deleted_tracked_file_is_never_checked_again(tmp_path) -> None:
    """A tracked file the operator deleted before the run records None and stays unchecked."""
    root = _init_repo(tmp_path)
    (root / "tracked.md").unlink()
    snap = operator_files_snapshot(root)
    assert snap == {"tracked.md": None}
    (root / "tracked.md").write_text("agent recreated me\n", encoding="utf-8")
    assert operator_files_damage(root, snap) == []


# ---------------------------------------------------------------------------
# Defect 2b: an agent's self-reported destructive action is surfaced
# ---------------------------------------------------------------------------

_ADMISSION = (
    "I made a mistake — I ran a command that deleted `REVIEW.md`, an untracked "
    "file shown in your git status snapshot, without being asked to."
)


def _result(raw: str) -> AgentResult:
    return AgentResult(agent_type="review", data={}, raw=raw)


@pytest.mark.parametrize("text", [
    _ADMISSION,
    "Review done. I accidentally deleted NOTES.md while cleaning up.",
    "I shouldn't have touched that file.",
    "I should not have run git clean.",
    "I mistakenly overwrote the operator's config.",
])
def test_self_reported_mistake_is_detected_in_the_final_text(text) -> None:
    quote = _self_reported_mistake(_result(text), None)
    assert quote is not None and quote in text


@pytest.mark.parametrize("text", [
    "",
    "APPROVED — no issues found.",
    "I deleted the dead helper and its test as the plan asked.",
    'The scan matches admissions such as "I made a mistake" in agent text.',
    "Phrases like `I shouldn't have` are flagged by the new scan.",
])
def test_ordinary_or_quoted_text_is_not_an_admission(text) -> None:
    assert _self_reported_mistake(_result(text), None) is None


def test_mid_stream_admission_in_the_transcript_is_detected(tmp_path) -> None:
    """The #685 admission came mid-stream, not in the final result text."""
    tpath = tmp_path / "review.log"
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "rm -f REVIEW.md"}},
        ]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": _ADMISSION}]}},
        # Tool output (e.g. a file the agent read) is not the agent speaking.
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "I made a mistake in the docs"},
        ]}},
    ]
    tpath.write_text("\n".join(json.dumps(e) for e in events) + "\nnot json\n", encoding="utf-8")
    quote = _self_reported_mistake(_result("APPROVED"), tpath)
    assert quote is not None and quote.startswith("I made a mistake")


def test_tool_output_alone_is_not_an_admission(tmp_path) -> None:
    tpath = tmp_path / "review.log"
    tpath.write_text(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "I made a mistake"}]}}) + "\n", encoding="utf-8")
    assert _self_reported_mistake(_result("APPROVED"), tpath) is None


def test_unreadable_transcript_is_ignored(tmp_path) -> None:
    assert _self_reported_mistake(None, tmp_path / "missing.log") is None


def test_malformed_assistant_events_are_skipped(tmp_path) -> None:
    tpath = tmp_path / "review.log"
    events = [
        {"type": "assistant", "message": "not a dict"},
        {"type": "assistant", "message": {"content": "not a list"}},
        ["not", "an", "event"],
    ]
    tpath.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    assert _self_reported_mistake(None, tpath) is None


def test_bugfix_rewriting_an_operator_file_parks_the_run(tmp_path) -> None:
    """The drift check also runs after a bugfix dispatch, not only core stages."""
    root = _init_repo(tmp_path)
    (root / "NOTES.md").write_text("keep\n", encoding="utf-8")

    class _FailingReview(_Dispatcher):
        def __call__(self, agent_type, prompt, **kwargs):
            if agent_type == "bugfix":
                self.agents.append(agent_type)
                (root / "NOTES.md").write_text("clobbered\n", encoding="utf-8")
                return AgentResult(agent_type="bugfix", raw="", data={
                    "failure_category": "TEST_BUG", "root_cause": "x",
                    "fix_status": "FIXED", "tests_passing": True,
                    "bugs_fixed": 1, "tests_fixed": 1,
                })
            result = super().__call__(agent_type, prompt, **kwargs)
            if agent_type == "review":
                result = AgentResult(agent_type="review", raw="", data={
                    **_PAYLOADS["review"], "approval_status": "CHANGES_REQUESTED",
                    "final_status": "CHANGES_REQUESTED",
                })
            return result

    dispatch = _FailingReview()
    result = _fix(root, dispatch)
    assert result.status == "NEEDS_ATTENTION"
    assert "bugfix" in dispatch.agents and "merge" not in dispatch.agents
    assert any("NOTES.md (modified)" in m for m in _events(root / ".sdlc-state.db"))


# ---------------------------------------------------------------------------
# The pipeline: `sdlc fix` parks NEEDS_ATTENTION instead of merging
# ---------------------------------------------------------------------------


class _Gh:
    def __call__(self, argv, *, env=None, timeout=None):
        joined = " ".join(argv)
        if "issue view" in joined:
            return RunResult(0, json.dumps({
                "number": 1, "title": "Bug", "body": "boom", "state": "OPEN",
                "assignees": [], "labels": [],
            }), "")
        if "api user" in joined:
            return RunResult(0, "me", "")
        return RunResult(0, "", "")


_PAYLOADS = {
    "investigation": {
        "root_cause": "x", "complexity": "LOW", "fix_approach": "y",
        "files_to_modify": ["a.py"], "risk": "low", "investigation_status": "READY",
    },
    "build": {"branch_name": "feature/issue-1", "build_status": "SUCCESS", "commit_sha": "d"},
    "coverage": {
        "pr_number": 100, "pr_url": "u", "coverage_pct": 95.0,
        "tests_added": 1, "coverage_status": "PASS",
    },
    "review": {
        "pr_number": 100, "approval_status": "APPROVED",
        "change_count": 0, "final_status": "APPROVED",
    },
    "merge": {
        "pr_number": 100, "merge_status": "MERGED",
        "merge_sha": "c", "merged_at": "2026-09-11T00:00:00Z",
    },
    "e2e": {"e2e_result": "PASS", "e2e_summary": "ok"},
    "summary": {"summary_markdown": "done"},
}


class _Dispatcher:
    """Canned stage results; ``on_review`` lets the review agent misbehave."""

    def __init__(self, on_review=None, review_raw: str = "") -> None:
        self.agents: list[str] = []
        self.on_review = on_review
        self.review_raw = review_raw

    def __call__(self, agent_type, prompt, *, story=None, model=None,
                 transcript_path=None, on_progress=None, **kwargs):
        self.agents.append(agent_type)
        raw = ""
        if agent_type == "review":
            if self.on_review:
                self.on_review()
            raw = self.review_raw
        return AgentResult(agent_type=agent_type, data=_PAYLOADS[agent_type], raw=raw)


def _events(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT message FROM events").fetchall()]
    finally:
        conn.close()


def _fix(root: Path, dispatch: _Dispatcher):
    return run_fix(
        FixOptions(issue=1, allow_dirty=True),
        ledger=Ledger(root / ".sdlc-state.db"),
        dispatcher=dispatch,
        preflight=lambda: True,
        runner=_Gh(),
        root=root,
    )


def test_review_deleting_an_untracked_operator_file_parks_the_run(tmp_path, capsys) -> None:
    """Regression (#685): the run must not merge after an operator file vanished."""
    root = _init_repo(tmp_path)
    (root / "REVIEW.md").write_text("362 lines\n", encoding="utf-8")
    dispatch = _Dispatcher(on_review=lambda: (root / "REVIEW.md").unlink())
    result = _fix(root, dispatch)
    assert result.status == "NEEDS_ATTENTION"
    assert "merge" not in dispatch.agents
    events = _events(root / ".sdlc-state.db")
    assert any("REVIEW.md (deleted)" in m and "review" in m for m in events)
    assert "REVIEW.md (deleted)" in capsys.readouterr().err


def test_review_self_reporting_a_mistake_parks_the_run(tmp_path, capsys) -> None:
    """Regression (#685): the admission reaches the ledger and the CLI, merge never runs."""
    root = _init_repo(tmp_path)
    dispatch = _Dispatcher(review_raw=_ADMISSION)
    result = _fix(root, dispatch)
    assert result.status == "NEEDS_ATTENTION"
    assert "merge" not in dispatch.agents
    assert any("I made a mistake" in m for m in _events(root / ".sdlc-state.db"))
    assert "I made a mistake" in capsys.readouterr().err


def test_well_behaved_run_with_operator_files_still_merges(tmp_path) -> None:
    """The guard must not park a clean `--allow-dirty` run."""
    root = _init_repo(tmp_path)
    (root / "REVIEW.md").write_text("362 lines\n", encoding="utf-8")
    dispatch = _Dispatcher()
    result = _fix(root, dispatch)
    assert result.status == "DONE"
    assert "merge" in dispatch.agents
    assert (root / "REVIEW.md").read_text(encoding="utf-8") == "362 lines\n"
