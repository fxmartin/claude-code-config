# ABOUTME: Tests for safe per-story worktree teardown, orphan-sweep safety, and
# ABOUTME: resume re-attach under concurrency (Story 17.2-002). Real temp git repos.

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from sdlc.build import (
    BuildOptions,
    Ledger,
    _prepare_story_workdir,
    _teardown_story_workdir,
    _worktree_registered_paths,
    create_story_worktree,
    remove_story_worktree,
)
from sdlc.cohort import Story


# ---------------------------------------------------------------------------
# helpers (mirror test_worktree.py so the two suites read alike)
# ---------------------------------------------------------------------------

def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _repo_with_origin(tmp_path: Path) -> Path:
    """A clone with an ``origin`` remote carrying a base commit on ``main``."""
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
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "README").write_text("base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "chore: base")
    _git(work, "push", "origin", "main")
    _git(work, "fetch", "origin")
    return work


def _story(sid: str) -> Story:
    return Story(sid, f"Story {sid}", "17", "parallel", "epic-17.md", "P2", 5, "py", [])


def _registered(work: Path) -> set[Path]:
    live = _git(work, "worktree", "list", "--porcelain").stdout
    return {
        Path(line.removeprefix("worktree ")).resolve()
        for line in live.splitlines()
        if line.startswith("worktree ")
    }


# ---------------------------------------------------------------------------
# remove_story_worktree: the close-out mechanism (AC1)
# ---------------------------------------------------------------------------

def test_remove_worktree_drops_checkout_but_keeps_branch_and_commits(tmp_path) -> None:
    """Removing a story's worktree deregisters + deletes it, but its feature
    branch and committed work survive (R10 — committed work is never discarded)."""
    work = _repo_with_origin(tmp_path)
    path = create_story_worktree(work, "17.2-002", "run-abc12345")
    # The agent cuts its feature branch inside the worktree and commits work.
    _git(path, "checkout", "-b", "feature/17.2-002", "origin/main")
    (path / "feature.txt").write_text("work\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "feat: story work")
    sha = _git(path, "rev-parse", "HEAD").stdout.strip()

    assert remove_story_worktree(work, path) is True

    assert not path.exists()
    assert path.resolve() not in _registered(work)
    # Branch ref + its commit are preserved (the PR/branch is the deliverable).
    branches = _git(work, "branch", "--list", "feature/17.2-002").stdout
    assert "feature/17.2-002" in branches
    assert _git(work, "rev-parse", "feature/17.2-002").stdout.strip() == sha


def test_remove_worktree_is_idempotent_when_already_gone(tmp_path) -> None:
    """Removing a path that was never (or is no longer) a worktree is a safe
    no-op — teardown can run after a crash already cleaned the checkout."""
    work = _repo_with_origin(tmp_path)
    phantom = work / ".claude" / "worktrees" / "agent-x-never-made"
    # Never raises; returns True (nothing left to leak).
    assert remove_story_worktree(work, phantom) is True


def test_remove_worktree_outside_repo_is_non_fatal(tmp_path) -> None:
    """Outside a git repo, removal degrades to a no-op rather than raising."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert remove_story_worktree(plain, plain / "nope") is False


# ---------------------------------------------------------------------------
# _teardown_story_workdir: ledger-driven close-out (AC1)
# ---------------------------------------------------------------------------

def test_teardown_removes_recorded_worktree(tmp_path, monkeypatch) -> None:
    """Close-out reads the story's recorded worktree and removes it (AC1)."""
    work = _repo_with_origin(tmp_path)
    monkeypatch.chdir(work)
    ledger = Ledger(work / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-17", "parallel")
    ledger.story_upsert(
        run_id, "17.2-002", "17", "Teardown", "P2", 5, "py", "", None, "TODO"
    )
    workdir = _prepare_story_workdir(
        BuildOptions(), _story("17.2-002"), ledger, run_id, real_run=True
    )
    assert workdir is not None and workdir.is_dir()

    _teardown_story_workdir(ledger, run_id, "17.2-002", real_run=True)

    assert not workdir.exists()
    assert workdir.resolve() not in _registered(work)


def test_teardown_noop_when_no_worktree_recorded(tmp_path) -> None:
    """A story that built in the shared root (NULL worktree_path) is a no-op."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-17", "serial")
    ledger.story_upsert(
        run_id, "17.2-002", "17", "Teardown", "P2", 5, "py", "", None, "TODO"
    )
    # No worktree recorded → nothing to remove, must not raise.
    _teardown_story_workdir(ledger, run_id, "17.2-002", real_run=True)
    assert ledger.story_worktree(run_id, "17.2-002") is None


def test_teardown_skipped_for_fake_run(tmp_path, monkeypatch) -> None:
    """A fake-dispatcher run never created a worktree → teardown must not touch
    the real repo even if asked."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-17", "parallel")
    ledger.story_upsert(
        run_id, "17.2-002", "17", "Teardown", "P2", 5, "py", "", None, "TODO"
    )

    called = {"hit": False}

    def _boom(*a, **k):
        called["hit"] = True
        raise AssertionError("must not remove on a fake run")

    monkeypatch.setattr("sdlc.build.remove_story_worktree", _boom)
    _teardown_story_workdir(ledger, run_id, "17.2-002", real_run=False)
    assert called["hit"] is False


def test_teardown_only_touches_its_own_story_worktree(tmp_path, monkeypatch) -> None:
    """Per-story teardown is keyed by story id and never races/removes a peer's
    in-flight worktree (AC2 — no removal of a worktree still needed)."""
    work = _repo_with_origin(tmp_path)
    monkeypatch.chdir(work)
    ledger = Ledger(work / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-17", "parallel")
    for sid in ("17.2-001", "17.2-002"):
        ledger.story_upsert(
            run_id, sid, "17", sid, "P2", 5, "py", "", None, "TODO"
        )
    wt1 = _prepare_story_workdir(
        BuildOptions(), _story("17.2-001"), ledger, run_id, real_run=True
    )
    wt2 = _prepare_story_workdir(
        BuildOptions(), _story("17.2-002"), ledger, run_id, real_run=True
    )
    assert wt1 is not None and wt2 is not None

    # Tear down only story 17.2-002; the still-in-flight 17.2-001 must survive.
    _teardown_story_workdir(ledger, run_id, "17.2-002", real_run=True)

    assert not wt2.exists()
    assert wt1.is_dir()
    assert wt1.resolve() in _registered(work)


# ---------------------------------------------------------------------------
# _worktree_registered_paths: the in-flight guard used by teardown / sweep
# ---------------------------------------------------------------------------

def test_registered_paths_reports_live_worktrees(tmp_path) -> None:
    """The helper reports git's live worktrees so callers never sweep one that
    is still in use (AC2)."""
    work = _repo_with_origin(tmp_path)
    path = create_story_worktree(work, "17.2-002", "run-abc12345")
    paths = _worktree_registered_paths(work)
    assert path.resolve() in paths
    # The repo root itself is always a registered worktree.
    assert work.resolve() in paths


def test_registered_paths_empty_outside_repo(tmp_path) -> None:
    """Outside a git repo the helper returns an empty set, never raises."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _worktree_registered_paths(plain) == set()


# ---------------------------------------------------------------------------
# resume re-attach: create_story_worktree is deterministic on re-entry (AC3)
# ---------------------------------------------------------------------------

def test_create_worktree_reattaches_when_already_registered(tmp_path) -> None:
    """Re-entering a story whose worktree is still live re-attaches to the same
    path instead of failing a second `git worktree add` (AC3)."""
    work = _repo_with_origin(tmp_path)
    first = create_story_worktree(work, "17.2-002", "run-abc12345")
    # The agent had cut its feature branch and committed inside the worktree.
    _git(first, "checkout", "-b", "feature/17.2-002", "origin/main")
    (first / "wip.txt").write_text("in progress\n")
    _git(first, "add", "-A")
    _git(first, "commit", "-m", "feat: wip")

    # Resume calls create again with the same identifiers — must not raise.
    second = create_story_worktree(work, "17.2-002", "run-abc12345")
    assert second.resolve() == first.resolve()
    assert second.resolve() in _registered(work)
    # Re-attach preserved the in-flight branch/commit (no destructive recreate).
    assert (second / "wip.txt").exists()


def test_resume_end_crash_closeout_tears_down_recorded_worktree(
    tmp_path, monkeypatch
) -> None:
    """An end-crash story (all stages DONE, status never finalised) is closed out
    on resume **without dispatching** — its recorded worktree must still be torn
    down, not leaked. Regression: the end-crash close-out path skipped teardown,
    so the original build's worktree survived past resume (AC2/AC3 — no leak)."""
    from sdlc import resume as resume_mod
    from sdlc.resume import run_resume
    from test_build import FakeDispatcher
    from test_resume import _make_project

    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 2)
    ledger.event_log(
        run_id, "", "info", "config", json.dumps({"skip_coverage": True})
    )
    ledger.story_upsert(
        run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO"
    )
    ledger.story_upsert(
        run_id, "99.1-002", "99", "Two", "P2", 2, "general-purpose", "", None, "TODO"
    )
    # 99.1-001: every stage DONE but the status was never finalised — the resume
    # closes it out as DONE via the no-dispatch end-crash branch. It recorded a
    # worktree the close-out must tear down rather than leak.
    for stage in ("build", "review", "merge"):
        ledger.stage_start(run_id, "99.1-001", stage, 1)
        ledger.stage_finish(run_id, "99.1-001", stage, 1, "DONE")
    ledger.set_story_pr(run_id, "99.1-001", 100)
    ledger.set_story_status(run_id, "99.1-001", "IN_PROGRESS")
    ledger.set_story_worktree(run_id, "99.1-001", "/tmp/agent-x-99.1-001")
    # 99.1-002: genuinely incomplete (review interrupted) so the run has real
    # work to resume and the cohort loop actually executes the end-crash branch.
    ledger.stage_start(run_id, "99.1-002", "build", 1)
    ledger.stage_finish(run_id, "99.1-002", "build", 1, "DONE")
    ledger.set_story_pr(run_id, "99.1-002", 100)
    ledger.stage_start(run_id, "99.1-002", "review", 1)  # left IN_PROGRESS
    ledger.set_story_status(run_id, "99.1-002", "IN_PROGRESS")

    torn_down: list[str] = []
    monkeypatch.setattr(
        resume_mod,
        "_teardown_story_workdir",
        lambda _l, _r, sid, *, real_run: torn_down.append(sid),
    )

    run_resume(
        "epic-99", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path
    )

    assert "99.1-001" in torn_down  # end-crash close-out reached teardown


def test_resume_end_crash_only_run_finalises_and_tears_down(
    tmp_path, monkeypatch
) -> None:
    """When *every* non-terminal story is end-crash (all stages done, status
    never finalised), the resume has no stage to dispatch but still has work:
    close those stories out. The run must flow through the cohort-loop end-crash
    branch — story finalised DONE, worktree torn down — rather than short-circuit
    `nothing_to_resume` and strand the run IN_PROGRESS with a leaked worktree."""
    from sdlc import resume as resume_mod
    from sdlc.resume import run_resume
    from test_build import FakeDispatcher
    from test_resume import _make_project

    _make_project(tmp_path)
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    run_id = ledger.run_create("epic-99", "serial")
    ledger.set_total(run_id, 1)
    ledger.event_log(
        run_id, "", "info", "config", json.dumps({"skip_coverage": True})
    )
    ledger.story_upsert(
        run_id, "99.1-001", "99", "One", "P1", 1, "general-purpose", "", None, "TODO"
    )
    # The only story: every stage DONE, status never finalised (end-crash), and
    # a recorded worktree. No stage is dispatchable, but it still needs closing.
    for stage in ("build", "review", "merge"):
        ledger.stage_start(run_id, "99.1-001", stage, 1)
        ledger.stage_finish(run_id, "99.1-001", stage, 1, "DONE")
    ledger.set_story_pr(run_id, "99.1-001", 100)
    ledger.set_story_status(run_id, "99.1-001", "IN_PROGRESS")
    ledger.set_story_worktree(run_id, "99.1-001", "/tmp/agent-x-99.1-001")

    torn_down: list[str] = []
    monkeypatch.setattr(
        resume_mod,
        "_teardown_story_workdir",
        lambda _l, _r, sid, *, real_run: torn_down.append(sid),
    )

    result = run_resume(
        "epic-99", ledger=Ledger(db), dispatcher=FakeDispatcher(), root=tmp_path
    )

    # Coherent close-out: not stranded resumable, story finalised, worktree gone.
    assert result.nothing_to_resume is False
    assert result.story_status["99.1-001"] == "DONE"
    rows = {r["story_id"]: r["status"] for r in Ledger(db).story_rows(run_id)}
    assert rows["99.1-001"] == "DONE"
    assert "99.1-001" in torn_down


def test_create_worktree_recreates_after_stale_dir(tmp_path) -> None:
    """A crash can leave a worktree directory git no longer tracks. Re-creating
    detects the stale dir, clears it, and yields a fresh live worktree (AC3 —
    deterministic, no duplicate-add failure)."""
    work = _repo_with_origin(tmp_path)
    stale = work / ".claude" / "worktrees" / "agent-run-17.2-002"
    stale.mkdir(parents=True)
    (stale / "leftover").write_text("crash debris\n")
    # The short run prefix is run_id.split("-")[0]; "run-..." → "run".
    path = create_story_worktree(work, "17.2-002", "run-deadbeef")

    assert path == stale
    assert path.resolve() in _registered(work)
    # The debris was cleared by the recreate.
    assert not (stale / "leftover").exists()
