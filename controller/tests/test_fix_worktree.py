# ABOUTME: Story 35.5-011 — `sdlc fix` runs each issue in its own worktree, never the checkout.
# ABOUTME: Real temp git repos (origin + clone); agent dispatch and the forge are faked.

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from test_fix_issue import (
    FakeBatchGh,
    FakeGh,
    RecordingDispatcher,
    _batch_issue,
    _issue_json,
)

import sdlc.fix_issue as fix_mod
from sdlc.build import WorktreeError, create_story_worktree
from sdlc.dispatch import RateLimitError, RateLimitSignal
from sdlc.fix_issue import (
    FixBatchOptions,
    FixOptions,
    render_e2e_prompt,
    render_merge_prompt,
    resume_fix,
    run_fix,
    run_fix_batch,
)
from sdlc.ledger_view import Ledger

# --- git fixture helpers ----------------------------------------------------


def _git(cwd: Path | str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _dirty_checkout(tmp_path: Path) -> Path:
    """A clone with ``origin/main``, checked out on ``other`` with uncommitted work.

    Every kind of operator state a fix must leave alone: a commit only on the
    operator's branch, an unstaged edit, a staged file, and an untracked file.
    """
    bare = tmp_path / "origin.git"
    root = tmp_path / "repo"
    root.mkdir()
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@e.c")
    _git(root, "config", "user.name", "T")
    (root / "README.md").write_text("x\n", encoding="utf-8")
    # As in every repo the controller runs in: its worktrees stay out of status.
    (root / ".gitignore").write_text(".claude/worktrees/\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "-q", "origin", "main")
    _git(root, "fetch", "-q", "origin")
    _git(root, "remote", "set-head", "origin", "main")
    _git(root, "checkout", "-q", "-b", "other")
    (root / "wip.txt").write_text("wip\n", encoding="utf-8")
    _git(root, "add", "wip.txt")
    _git(root, "commit", "-q", "-m", "wip on other")
    (root / "README.md").write_text("edited, unstaged\n", encoding="utf-8")
    (root / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(root, "add", "staged.txt")
    (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    return root


def _checkout_state(root: Path) -> tuple[str, ...]:
    """HEAD, branch, index, working tree and status — what a fix must not move."""
    return (
        _git(root, "rev-parse", "HEAD"),
        _git(root, "symbolic-ref", "--short", "HEAD"),
        _git(root, "diff", "--cached"),
        _git(root, "diff"),
        _git(root, "status", "--porcelain", "--untracked-files=all"),
    )


def _registered_worktrees(root: Path) -> set[Path]:
    out = _git(root, "worktree", "list", "--porcelain")
    return {
        Path(line.removeprefix("worktree ")).resolve()
        for line in out.splitlines()
        if line.startswith("worktree ")
    }


def _events(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT message FROM events").fetchall()]
    finally:
        conn.close()


def _branch_subjects(root: Path, branch: str) -> list[str]:
    return _git(root, "log", "--format=%s", f"origin/main..{branch}").splitlines()


class WorktreeAgent(RecordingDispatcher):
    """A fake agent that does the git work a real fix agent does, in its ``cwd``.

    The build stage cuts ``feature/issue-<N>`` off ``origin/main`` and commits —
    exactly the opening ``git checkout -b`` the fix prompts give — so a run that
    dispatched into the checkout would visibly move it. Every stage's ``cwd`` is
    recorded so the test can assert where each agent ran.
    """

    def __init__(self, *args, on_build=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cwds: list[tuple[str, Path | None]] = []
        # (story id, cwd) of every build dispatch — a batch's per-issue placement.
        self.story_cwds: list[tuple[str, Path | None]] = []
        self.on_build = on_build

    def __call__(self, agent_type, prompt, *, story=None, **kwargs):
        cwd = kwargs.get("cwd")
        self.cwds.append((agent_type, Path(cwd) if cwd is not None else None))
        if agent_type == "build":
            self.story_cwds.append((story.id, Path(cwd) if cwd is not None else None))
        if agent_type == "build" and cwd is not None:
            branch = f"feature/{story.id}"
            _git(cwd, "checkout", "-q", "-b", branch, "origin/main")
            (Path(cwd) / "fix.py").write_text("x = 1\n", encoding="utf-8")
            _git(cwd, "add", "fix.py")
            _git(cwd, "commit", "-q", "-m", f"fix: clamp the index ({story.id})")
            if self.on_build is not None:
                self.on_build()
        return super().__call__(agent_type, prompt, story=story, **kwargs)


def _expected_worktree(root: Path, run_id: str, story_id: str = "issue-1") -> Path:
    return root / ".claude" / "worktrees" / f"agent-{run_id.split('-')[0]}-{story_id}"


# ---------------------------------------------------------------------------
# AC1 + AC4: a fix works in its own worktree; a dirty checkout is left untouched
# ---------------------------------------------------------------------------


def test_single_fix_runs_in_its_worktree_and_leaves_a_dirty_checkout_unchanged(
    tmp_path, monkeypatch
) -> None:
    root = _dirty_checkout(tmp_path)
    monkeypatch.chdir(root)
    before = _checkout_state(root)
    agent = WorktreeAgent()
    monkeypatch.setattr(fix_mod, "dispatch_agent", agent)
    ledger = Ledger(tmp_path / ".sdlc-state.db")

    result = run_fix(
        FixOptions(issue=1),
        ledger=ledger,
        preflight=lambda: True,
        runner=FakeGh(_issue_json()),
    )

    # A dirty checkout on another branch is no longer a reason to refuse (#590).
    assert result.status == "DONE"
    workdir = _expected_worktree(root, result.run_id)
    # Every agent — investigation through summary — ran in the worktree.
    assert agent.cwds[0][0] == "investigation"
    assert {cwd for _, cwd in agent.cwds} == {workdir}
    assert ledger.story_worktree(result.run_id, "issue-1") == str(workdir)
    # The fix landed on its own branch, off origin/main, not on the operator's.
    assert _branch_subjects(root, "feature/issue-1") == ["fix: clamp the index (issue-1)"]
    assert _checkout_state(root) == before
    assert _branch_subjects(root, "other") == ["wip on other"]
    # Close-out tore the worktree down; the branch is kept.
    assert not workdir.exists()
    assert workdir.resolve() not in _registered_worktrees(root)
    assert _git(root, "branch", "--list", "feature/issue-1").strip()


def test_batch_with_one_worker_isolates_each_issue_and_never_moves_the_checkout(
    tmp_path, monkeypatch
) -> None:
    root = _dirty_checkout(tmp_path)
    monkeypatch.chdir(root)
    before = _checkout_state(root)
    agent = WorktreeAgent()
    monkeypatch.setattr(fix_mod, "dispatch_agent", agent)
    ledger = Ledger(tmp_path / ".sdlc-state.db")

    result = run_fix_batch(
        FixBatchOptions(target="all", sequential=True),
        ledger=ledger,
        preflight=lambda: True,
        runner=FakeBatchGh([_batch_issue(1), _batch_issue(2)]),
    )

    assert result.status == "DONE", result.summary
    assert result.fixed == 2
    for n in (1, 2):
        workdir = _expected_worktree(root, result.run_id, f"issue-{n}")
        assert (f"issue-{n}", workdir) in agent.story_cwds
        assert not workdir.exists()
        assert _branch_subjects(root, f"feature/issue-{n}") == [
            f"fix: clamp the index (issue-{n})"
        ]
    # No pipeline stage ran in the checkout; the batch never repositioned it.
    pipeline = {"build", "coverage", "review", "merge", "summary"}
    assert all(cwd is not None for a, cwd in agent.cwds if a in pipeline)
    # The batch doc-update agent cuts its own branch too — in its own worktree.
    doc_cwds = [cwd for a, cwd in agent.cwds if a == "doc_update"]
    assert doc_cwds == [_expected_worktree(root, result.run_id, "doc-update")]
    assert not doc_cwds[0].exists()
    assert _checkout_state(root) == before


# ---------------------------------------------------------------------------
# AC2: a fix and a build committing at the same time never cross branches
# ---------------------------------------------------------------------------


def test_concurrent_fix_and_build_commits_never_cross(tmp_path, monkeypatch) -> None:
    root = _dirty_checkout(tmp_path)
    monkeypatch.chdir(root)
    before = _checkout_state(root)

    def build_story_commits_mid_fix() -> None:
        # A `sdlc build` story committing in its own worktree while the fix's
        # build stage is in flight in the same repo.
        build_wt = create_story_worktree(root, "35.5-011", "b0b0b0b0-build")
        _git(build_wt, "checkout", "-q", "-b", "feature/35.5-011", "origin/main")
        (build_wt / "feature.py").write_text("y = 2\n", encoding="utf-8")
        _git(build_wt, "add", "feature.py")
        _git(build_wt, "commit", "-q", "-m", "feat: the build story")

    agent = WorktreeAgent(on_build=build_story_commits_mid_fix)
    monkeypatch.setattr(fix_mod, "dispatch_agent", agent)

    result = run_fix(
        FixOptions(issue=1),
        ledger=Ledger(tmp_path / ".sdlc-state.db"),
        preflight=lambda: True,
        runner=FakeGh(_issue_json()),
    )

    assert result.status == "DONE"
    assert _branch_subjects(root, "feature/issue-1") == ["fix: clamp the index (issue-1)"]
    assert _branch_subjects(root, "feature/35.5-011") == ["feat: the build story"]
    assert _branch_subjects(root, "other") == ["wip on other"]
    assert _checkout_state(root) == before


# ---------------------------------------------------------------------------
# AC5: no worktree → refuse with the reason, never fall back to the checkout
# ---------------------------------------------------------------------------


def test_single_fix_refuses_when_the_worktree_cannot_be_created(
    tmp_path, monkeypatch
) -> None:
    root = _dirty_checkout(tmp_path)
    monkeypatch.chdir(root)
    before = _checkout_state(root)
    agent = WorktreeAgent()
    monkeypatch.setattr(fix_mod, "dispatch_agent", agent)

    def no_worktree(*_args, **_kwargs):
        raise WorktreeError("git worktree add for issue-1 failed: disk full")

    monkeypatch.setattr(fix_mod, "create_story_worktree", no_worktree)
    preflights: list[int] = []
    ledger = Ledger(tmp_path / ".sdlc-state.db")

    result = run_fix(
        FixOptions(issue=1),
        ledger=ledger,
        preflight=lambda: preflights.append(1) or True,
        runner=FakeGh(_issue_json()),
    )

    assert result.status == "ABORTED"
    assert result.aborted is True
    assert "disk full" in result.abort_reason
    assert "worktree" in result.abort_reason
    assert agent.calls == []  # nothing dispatched — not into the checkout either
    assert preflights == []  # refused before preflight burns a test run
    assert ledger.run_row(result.run_id)["status"] == "ABORTED"
    assert _checkout_state(root) == before


def test_batch_issue_fails_rather_than_building_in_the_checkout_without_a_worktree(
    tmp_path, monkeypatch
) -> None:
    root = _dirty_checkout(tmp_path)
    monkeypatch.chdir(root)
    before = _checkout_state(root)
    agent = WorktreeAgent()
    monkeypatch.setattr(fix_mod, "dispatch_agent", agent)

    def no_worktree(*_args, **_kwargs):
        raise WorktreeError("git worktree add failed: disk full")

    monkeypatch.setattr(fix_mod, "create_story_worktree", no_worktree)
    ledger = Ledger(tmp_path / ".sdlc-state.db")

    result = run_fix_batch(
        FixBatchOptions(target="all", sequential=True),
        ledger=ledger,
        preflight=lambda: True,
        runner=FakeBatchGh([_batch_issue(1)]),
    )

    assert result.failed == 1
    assert result.fixed == 0
    assert not any(a == "build" for a, _ in agent.cwds)
    assert any("disk full" in m for m in _events(tmp_path / ".sdlc-state.db"))
    assert _checkout_state(root) == before


# ---------------------------------------------------------------------------
# AC3: a parked fix keeps its worktree and `sdlc resume` continues in it
# ---------------------------------------------------------------------------


def test_parked_fix_keeps_its_worktree_and_resume_continues_in_it(
    tmp_path, monkeypatch
) -> None:
    root = _dirty_checkout(tmp_path)
    monkeypatch.chdir(root)
    before = _checkout_state(root)
    throttled = RateLimitError("throttled", signal=RateLimitSignal(source="429"))
    first = WorktreeAgent(overrides={"coverage": throttled})
    monkeypatch.setattr(fix_mod, "dispatch_agent", first)
    db = tmp_path / ".sdlc-state.db"

    parked = run_fix(
        FixOptions(issue=1),
        ledger=Ledger(db),
        preflight=lambda: True,
        runner=FakeGh(_issue_json()),
    )

    assert parked.status == "RATE_LIMITED"
    workdir = _expected_worktree(root, parked.run_id)
    assert workdir.is_dir()
    assert workdir.resolve() in _registered_worktrees(root)
    assert Ledger(db).story_worktree(parked.run_id, "issue-1") == str(workdir)
    assert _checkout_state(root) == before

    second = WorktreeAgent()
    monkeypatch.setattr(fix_mod, "dispatch_agent", second)
    resumed = resume_fix(parked.run_id, ledger=Ledger(db), runner=FakeGh(_issue_json()))

    assert resumed.status == "DONE"
    assert "build" not in second.agents()  # build was done; not re-dispatched
    assert {cwd for _, cwd in second.cwds} == {workdir}
    assert not workdir.exists()  # torn down once the resumed fix closed
    assert _checkout_state(root) == before


def test_resume_refuses_when_the_worktree_cannot_be_recovered(
    tmp_path, monkeypatch
) -> None:
    root = _dirty_checkout(tmp_path)
    monkeypatch.chdir(root)
    throttled = RateLimitError("throttled", signal=RateLimitSignal(source="429"))
    monkeypatch.setattr(
        fix_mod, "dispatch_agent", WorktreeAgent(overrides={"coverage": throttled})
    )
    db = tmp_path / ".sdlc-state.db"
    parked = run_fix(
        FixOptions(issue=1), ledger=Ledger(db), preflight=lambda: True,
        runner=FakeGh(_issue_json()),
    )
    assert parked.status == "RATE_LIMITED"
    parked_status = Ledger(db).run_row(parked.run_id)["status"]

    def no_worktree(*_args, **_kwargs):
        raise WorktreeError("git worktree add failed: disk full")

    monkeypatch.setattr(fix_mod, "create_story_worktree", no_worktree)
    agent = WorktreeAgent()
    monkeypatch.setattr(fix_mod, "dispatch_agent", agent)

    result = resume_fix(parked.run_id, ledger=Ledger(db), runner=FakeGh(_issue_json()))

    assert result.status == "ABORTED"
    assert "disk full" in result.abort_reason
    assert agent.calls == []
    # Left resumable: the refusal does not touch the parked run's status.
    assert Ledger(db).run_row(parked.run_id)["status"] == parked_status
    assert Ledger(db).story_status(parked.run_id, "issue-1") == "RATE_LIMITED"


# ---------------------------------------------------------------------------
# Prompts: an agent in a worktree must not switch the shared `main`
# ---------------------------------------------------------------------------


def test_merge_and_e2e_prompts_no_longer_return_to_main() -> None:
    issue = fix_mod.FixIssue(1, "t", "b", "open", (), ())
    for prompt in (render_merge_prompt(issue, 100), render_e2e_prompt(issue, 100)):
        assert "git checkout main" not in prompt
        assert "Return to main" not in prompt
