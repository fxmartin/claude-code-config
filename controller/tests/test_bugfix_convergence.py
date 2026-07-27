# ABOUTME: Tests for issue #527 — the bugfix loop must converge on stories whose
# ABOUTME: review findings span multiple files (push, coupled tests, budget, resume).

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from sdlc import build as build_mod
from sdlc.build import (
    MAX_BUGFIX_ATTEMPTS,
    BuildOptions,
    BuildResult,
    Ledger,
    render_bugfix_prompt,
    run_build,
)
from sdlc.cli import app
from sdlc.cohort import Story
from sdlc.dispatch import AgentResult

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _story(sid: str = "c1-001") -> Story:
    return Story(sid, "Convergence story", "99", "sample", "epic-99.md", "P1",
                 2, "py", [])


def _queue() -> list[Story]:
    return [_story()]


_PAYLOADS = {
    "build": {
        "branch_name": "feature/c1-001",
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
        "failure_category": "CODE_BUG",
        "fix_status": "FIXED",
        "tests_passing": True,
        "bugs_fixed": 1,
        "tests_fixed": 2,
    },
}


class _Dispatcher:
    """Canned schema-valid responses; ``overrides`` swaps one agent's payload."""

    def __init__(self, overrides: dict | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.overrides = overrides or {}

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        sid = getattr(story, "id", "")
        self.calls.append((agent_type, sid))
        payload = self.overrides.get(agent_type, _PAYLOADS[agent_type])
        if callable(payload):
            payload = payload()
        return AgentResult(agent_type=agent_type, data=payload, raw="")

    def count(self, agent_type: str) -> int:
        return sum(1 for a, _ in self.calls if a == agent_type)


def _opts(**kw) -> BuildOptions:
    return BuildOptions(
        scope="epic-99", skip_preflight=True, sequential=True, auto=True, **kw
    )


def _rejecting_review(change_count: int):
    """A review agent that always rejects, reporting ``change_count`` findings."""
    return {
        "pr_number": 100,
        "approval_status": "CHANGES_NEEDED",
        "change_count": change_count,
        "final_status": "REJECTED",
    }


# ---------------------------------------------------------------------------
# AC1 — the bugfix loop must push its commit to the head review then scores
# ---------------------------------------------------------------------------

def test_successful_bugfix_pushes_story_branch(tmp_path, monkeypatch) -> None:
    """Review reads the *remote* CR diff, so an unpushed bugfix is invisible (#527).

    Without a push the retried review re-scores the stale remote head forever —
    exactly the four cellar stories that burned all three attempts.
    """
    pushed: list[str] = []
    monkeypatch.setattr(
        build_mod, "_push_bugfix_commit",
        lambda story, workdir, ledger, run_id: (pushed.append(story.id) or True),
    )
    state = {"n": 0}

    def flaky_review():
        state["n"] += 1
        return _PAYLOADS["review"] if state["n"] > 1 else _rejecting_review(2)

    disp = _Dispatcher(overrides={"review": flaky_review})
    result = run_build(
        _opts(), queue=_queue(), ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp, preflight=lambda: True, root=tmp_path,
    )
    assert result.completed == 1
    assert pushed == ["c1-001"]


def test_bugfix_push_failure_parks_needs_attention(tmp_path, monkeypatch) -> None:
    """A push that fails must park the story, never retry against a stale head."""
    monkeypatch.setattr(
        build_mod, "_push_bugfix_commit",
        lambda story, workdir, ledger, run_id: False,
    )
    state = {"n": 0}

    def flaky_review():
        state["n"] += 1
        return _PAYLOADS["review"] if state["n"] > 1 else _rejecting_review(2)

    disp = _Dispatcher(overrides={"review": flaky_review})
    result = run_build(
        _opts(), queue=_queue(), ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp, preflight=lambda: True, root=tmp_path,
    )
    assert result.needs_attention == 1
    assert result.completed == 0
    # The stage was not retried against the stale head.
    assert disp.count("review") == 1


def test_push_bugfix_commit_no_local_branch_is_noop(tmp_path, monkeypatch) -> None:
    """No ``feature/<id>`` locally ⇒ the bugfix authored nothing to push (no-op)."""
    calls: list[tuple] = []

    def fake_git(root, *args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(build_mod, "_git", fake_git)
    monkeypatch.setattr(
        build_mod, "_git_push",
        lambda root, branch: (_ for _ in ()).throw(AssertionError("must not push")),
    )
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    assert build_mod._push_bugfix_commit(_story(), tmp_path, ledger, run_id) is True
    assert calls


def test_push_bugfix_commit_success(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        build_mod, "_git",
        lambda root, *args: subprocess.CompletedProcess(args, 0, "", ""),
    )
    pushed: list[str] = []

    def fake_push(root, branch):
        pushed.append(branch)
        return subprocess.CompletedProcess(["git", "push"], 0, "", "")

    monkeypatch.setattr(build_mod, "_git_push", fake_push)
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    assert build_mod._push_bugfix_commit(_story(), tmp_path, ledger, run_id) is True
    assert pushed == ["feature/c1-001"]


def test_push_bugfix_commit_failure_logs_and_returns_false(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        build_mod, "_git",
        lambda root, *args: subprocess.CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(
        build_mod, "_git_push",
        lambda root, branch: subprocess.CompletedProcess(
            ["git", "push"], 1, "", "! [rejected] non-fast-forward"
        ),
    )
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    assert build_mod._push_bugfix_commit(_story(), tmp_path, ledger, run_id) is False
    messages = " ".join(
        r["message"] for r in ledger.recent_events(run_id, limit=20)
    )
    assert "push" in messages
    assert "non-fast-forward" in messages


def test_push_bugfix_commit_subprocess_error_returns_false(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        build_mod, "_git",
        lambda root, *args: subprocess.CompletedProcess(args, 0, "", ""),
    )

    def boom(root, branch):
        raise subprocess.SubprocessError("git vanished")

    monkeypatch.setattr(build_mod, "_git_push", boom)
    ledger = Ledger(tmp_path / "l.db")
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    assert build_mod._push_bugfix_commit(_story(), tmp_path, ledger, run_id) is False


# ---------------------------------------------------------------------------
# AC2 — the bugfix prompt must demand the coupled tests + a full-suite re-run
# ---------------------------------------------------------------------------

def test_bugfix_prompt_demands_coupled_tests_and_full_suite() -> None:
    """A code fix that leaves its coupled tests red never turns review green."""
    prompt = render_bugfix_prompt(_story(), "review", "findings: 3")
    lowered = prompt.lower()
    assert "coupled" in lowered
    assert "same commit" in lowered
    assert "full" in lowered and "suite" in lowered
    assert "tests_passing" in prompt


# ---------------------------------------------------------------------------
# AC3 — the attempt budget scales with the review's finding spread
# ---------------------------------------------------------------------------

def _review_result(change_count: int) -> AgentResult:
    return AgentResult(
        agent_type="review", data=_rejecting_review(change_count), raw=""
    )


def test_bugfix_budget_scales_with_review_change_count() -> None:
    budget = build_mod._bugfix_budget("review", _review_result(3))
    assert budget > MAX_BUGFIX_ATTEMPTS


def test_bugfix_budget_low_change_count_keeps_default() -> None:
    assert build_mod._bugfix_budget("review", _review_result(1)) == MAX_BUGFIX_ATTEMPTS
    assert build_mod._bugfix_budget("review", _review_result(0)) == MAX_BUGFIX_ATTEMPTS


def test_bugfix_budget_non_review_stage_unaffected() -> None:
    """Only a review rejection carries finding spread — build/coverage/merge don't."""
    spread = AgentResult(agent_type="build", data={"change_count": 9}, raw="")
    for stage in ("build", "coverage", "merge"):
        assert build_mod._bugfix_budget(stage, spread) == MAX_BUGFIX_ATTEMPTS


def test_bugfix_budget_missing_or_malformed_result_keeps_default() -> None:
    assert build_mod._bugfix_budget("review", None) == MAX_BUGFIX_ATTEMPTS
    empty = AgentResult(agent_type="review", data={}, raw="")
    assert build_mod._bugfix_budget("review", empty) == MAX_BUGFIX_ATTEMPTS
    junk = AgentResult(agent_type="review", data={"change_count": "many"}, raw="")
    assert build_mod._bugfix_budget("review", junk) == MAX_BUGFIX_ATTEMPTS


def test_wide_review_rejection_gets_an_extra_bugfix_round(
    tmp_path, monkeypatch
) -> None:
    """A cross-file rejection must get more rounds than the flat constant allows."""
    monkeypatch.setattr(
        build_mod, "_push_bugfix_commit",
        lambda story, workdir, ledger, run_id: True,
    )
    disp = _Dispatcher(overrides={"review": _rejecting_review(4)})
    run_build(
        _opts(), queue=_queue(), ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp, preflight=lambda: True, root=tmp_path,
    )
    assert disp.count("bugfix") > MAX_BUGFIX_ATTEMPTS


def test_narrow_review_rejection_keeps_the_default_rounds(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        build_mod, "_push_bugfix_commit",
        lambda story, workdir, ledger, run_id: True,
    )
    disp = _Dispatcher(overrides={"review": _rejecting_review(1)})
    run_build(
        _opts(), queue=_queue(), ledger=Ledger(tmp_path / "l.db"),
        dispatcher=disp, preflight=lambda: True, root=tmp_path,
    )
    assert disp.count("bugfix") == MAX_BUGFIX_ATTEMPTS


# ---------------------------------------------------------------------------
# AC4 — never silently rebuild a story a prior run parked on an open CR
# ---------------------------------------------------------------------------

def _seed_prior_run(
    db: Path, status: str, *, pr_number: int | None = 42, stages: bool = True
) -> tuple[Ledger, str]:
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.story_upsert(
        run_id, "c1-001", "99", "Convergence story", "P1", 2, "py", "",
        pr_number, status,
    )
    if stages:
        ledger.stage_start(run_id, "c1-001", "build", 1)
        ledger.stage_finish(run_id, "c1-001", "build", 1, "DONE")
        ledger.stage_start(run_id, "c1-001", "review", 1)
        ledger.stage_finish(run_id, "c1-001", "review", 1, "FAILED", "review-error")
    ledger.run_update_status(run_id, "FAILED")
    return ledger, run_id


def test_parked_story_with_open_cr_blocks_a_silent_rebuild(tmp_path) -> None:
    """Re-running `sdlc build` would re-cut the branch and collide with the fix."""
    ledger, prior = _seed_prior_run(tmp_path / "l.db", "FAILED")
    disp = _Dispatcher()
    result = run_build(
        _opts(), queue=_queue(), ledger=ledger, dispatcher=disp,
        preflight=lambda: True, root=tmp_path,
    )
    assert result.resume_required
    entry = result.resume_required[0]
    assert entry["story_id"] == "c1-001"
    assert entry["run_id"] == prior
    assert entry["status"] == "FAILED"
    # Nothing was dispatched — the queue never ran.
    assert disp.calls == []


def test_parked_story_message_names_the_full_run_id_and_resume(tmp_path) -> None:
    ledger, prior = _seed_prior_run(tmp_path / "l.db", "NEEDS_ATTENTION")
    result = run_build(
        _opts(), queue=_queue(), ledger=ledger, dispatcher=_Dispatcher(),
        preflight=lambda: True, root=tmp_path,
    )
    message = build_mod.format_resume_required(result.resume_required)
    assert prior in message
    assert "sdlc resume --run" in message
    assert "full" in message.lower()


def test_story_with_no_prior_history_is_not_blocked(tmp_path) -> None:
    result = run_build(
        _opts(), queue=_queue(), ledger=Ledger(tmp_path / "l.db"),
        dispatcher=_Dispatcher(), preflight=lambda: True, root=tmp_path,
    )
    assert result.resume_required == []
    assert result.completed == 1


def test_story_whose_prior_run_is_done_is_not_blocked(tmp_path) -> None:
    ledger, _ = _seed_prior_run(tmp_path / "l.db", "DONE")
    result = run_build(
        _opts(), queue=_queue(), ledger=ledger, dispatcher=_Dispatcher(),
        preflight=lambda: True, root=tmp_path,
    )
    assert result.resume_required == []
    assert result.completed == 1


def test_parked_story_without_a_change_request_is_not_blocked(tmp_path) -> None:
    """No open CR ⇒ no branch/PR collision to protect against."""
    ledger, _ = _seed_prior_run(tmp_path / "l.db", "FAILED", pr_number=None)
    result = run_build(
        _opts(), queue=_queue(), ledger=ledger, dispatcher=_Dispatcher(),
        preflight=lambda: True, root=tmp_path,
    )
    assert result.resume_required == []
    assert result.completed == 1


def test_parked_story_without_stage_rows_is_not_blocked(tmp_path) -> None:
    """A story the prior run never actually executed has nothing to collide with."""
    ledger, _ = _seed_prior_run(tmp_path / "l.db", "FAILED", stages=False)
    result = run_build(
        _opts(), queue=_queue(), ledger=ledger, dispatcher=_Dispatcher(),
        preflight=lambda: True, root=tmp_path,
    )
    assert result.resume_required == []
    assert result.completed == 1


def test_rebuild_flag_overrides_the_parked_guard(tmp_path) -> None:
    """`--rebuild` is explicit operator intent — the guard must not stand in it."""
    ledger, _ = _seed_prior_run(tmp_path / "l.db", "FAILED")
    result = run_build(
        _opts(rebuild=True), queue=_queue(), ledger=ledger,
        dispatcher=_Dispatcher(), preflight=lambda: True, root=tmp_path,
    )
    assert result.resume_required == []
    assert result.completed == 1


def test_bugfix_budget_non_dict_result_data_keeps_default() -> None:
    """A contract error can leave ``result.data`` as ``None`` (not even ``{}``)."""
    broken = AgentResult(agent_type="review", data=None, raw="")
    assert build_mod._bugfix_budget("review", broken) == MAX_BUGFIX_ATTEMPTS


def test_cli_build_resume_required_exits_1_and_prints_resume_path(
    tmp_path, monkeypatch
) -> None:
    """`sdlc build` refuses and names the resume path for a parked story (#527)."""
    stories_dir = tmp_path / "docs" / "stories"
    stories_dir.mkdir(parents=True)
    (stories_dir / "epic-99-sample.md").write_text(
        "##### Story 99.1-001: One\n**Priority**: P1\n**Points**: 1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    conflicts = [
        {"story_id": "99.1-001", "run_id": "abc-123", "status": "FAILED",
         "pr_number": 42}
    ]
    monkeypatch.setattr(
        build_mod, "run_build", lambda *a, **k: BuildResult(resume_required=conflicts)
    )
    result = runner.invoke(app, ["build", "epic-99"])
    assert result.exit_code == 1
    assert "sdlc resume --run" in result.output
    assert "abc-123" in result.output
