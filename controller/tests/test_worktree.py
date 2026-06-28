# ABOUTME: Tests for controller-owned per-story git worktree isolation (Story 17.2-001).
# ABOUTME: Real temp git repos; agent dispatch is faked so no Claude agent runs.

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sdlc.build import (
    BuildOptions,
    Ledger,
    WorktreeError,
    _prepare_story_workdir,
    _refresh_base_ref,
    _run_story,
    create_story_worktree,
)
from sdlc.cohort import Story


# ---------------------------------------------------------------------------
# helpers
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


# ---------------------------------------------------------------------------
# create_story_worktree: the lifecycle mechanism
# ---------------------------------------------------------------------------

def test_create_story_worktree_makes_isolated_checkout(tmp_path) -> None:
    """A per-story worktree is created and registered with git (AC1)."""
    work = _repo_with_origin(tmp_path)
    path = create_story_worktree(work, "17.2-001", "run-abc12345")

    assert path.is_dir()
    # The directory git tracks as a live worktree includes ours.
    live = _git(work, "worktree", "list", "--porcelain").stdout
    assert str(path.resolve()) in {
        str(Path(p).resolve())
        for p in (
            line.removeprefix("worktree ")
            for line in live.splitlines()
            if line.startswith("worktree ")
        )
    }


def test_create_story_worktree_is_git_locked(tmp_path) -> None:
    """The worktree is git-locked so the Stop-hook reaper cannot remove it
    mid-build (#180). A locked worktree emits a ``locked`` line in porcelain."""
    work = _repo_with_origin(tmp_path)
    path = create_story_worktree(work, "17.2-001", "run-abc12345")

    porcelain = _git(work, "worktree", "list", "--porcelain").stdout
    locked: set[str] = set()
    current: str | None = None
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            current = str(Path(line.removeprefix("worktree ")).resolve())
        elif line.startswith("locked") and current is not None:
            locked.add(current)
    assert str(path.resolve()) in locked


def test_reattached_worktree_is_git_locked(tmp_path) -> None:
    """Re-entry (resume) re-attaches an existing worktree and re-asserts the lock
    so a previously-unlocked checkout is protected on resume (#180)."""
    work = _repo_with_origin(tmp_path)
    path = create_story_worktree(work, "17.2-001", "run-abc12345")
    # Simulate a pre-fix checkout that was never locked, then resume re-attaches.
    _git(work, "worktree", "unlock", str(path))
    again = create_story_worktree(work, "17.2-001", "run-abc12345")
    assert again == path

    porcelain = _git(work, "worktree", "list", "--porcelain").stdout
    locked: set[str] = set()
    current: str | None = None
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            current = str(Path(line.removeprefix("worktree ")).resolve())
        elif line.startswith("locked") and current is not None:
            locked.add(current)
    assert str(path.resolve()) in locked


def test_worktree_path_matches_sweeper_convention(tmp_path) -> None:
    """The worktree lives under .claude/worktrees/agent-* (orphan-sweeper compatible)."""
    work = _repo_with_origin(tmp_path)
    path = create_story_worktree(work, "17.2-001", "run-abc12345")

    assert path.parent == work / ".claude" / "worktrees"
    assert path.name.startswith("agent-")
    assert "17.2-001" in path.name


def test_two_stories_get_separate_worktrees_and_branches(tmp_path) -> None:
    """Concurrent stories build in separate worktrees on separate branches (AC2)."""
    work = _repo_with_origin(tmp_path)
    p1 = create_story_worktree(work, "17.2-001", "run-abc12345")
    p2 = create_story_worktree(work, "17.2-002", "run-abc12345")

    assert p1 != p2
    assert p1.is_dir() and p2.is_dir()
    # Each agent can cut its own feature branch inside its worktree — distinct
    # branches, one shared object store, no shared index.
    _git(p1, "checkout", "-b", "feature/17.2-001", "origin/main")
    _git(p2, "checkout", "-b", "feature/17.2-002", "origin/main")
    b1 = _git(p1, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    b2 = _git(p2, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert b1 == "feature/17.2-001"
    assert b2 == "feature/17.2-002"


def test_create_worktree_raises_outside_a_repo(tmp_path) -> None:
    """In a non-git directory worktree creation raises WorktreeError (handled upstream)."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(WorktreeError):
        create_story_worktree(not_a_repo, "17.2-001", "run-abc12345")


def test_create_worktree_raises_when_worktrees_dir_unmakeable(tmp_path) -> None:
    """An unmakeable .claude/worktrees dir surfaces as WorktreeError, not OSError."""
    # A *file* at .claude makes `.claude/worktrees` mkdir(parents=True) fail with
    # an OSError subclass; create_story_worktree must wrap it as WorktreeError so
    # the caller's recoverable fallback fires instead of an uncaught crash.
    (tmp_path / ".claude").write_text("not a directory\n")
    with pytest.raises(WorktreeError, match="could not create"):
        create_story_worktree(tmp_path, "17.2-001", "run-abc12345")


def test_create_worktree_wraps_git_invocation_error(tmp_path, monkeypatch) -> None:
    """If invoking git raises (e.g. binary missing), it surfaces as WorktreeError."""
    # Resolve the base ref without git, then make the `git worktree add` call
    # raise an OSError — the branch that turns a subprocess launch failure into a
    # recoverable WorktreeError rather than an uncaught exception.
    monkeypatch.setattr("sdlc.build._base_ref", lambda root: "HEAD")

    def _no_git(*a, **k):
        raise OSError("git: command not found")

    monkeypatch.setattr("sdlc.build._git", _no_git)
    with pytest.raises(WorktreeError, match="git worktree add failed"):
        create_story_worktree(tmp_path, "17.2-001", "run-abc12345")


def test_lock_failure_is_best_effort_create_still_succeeds(tmp_path, monkeypatch) -> None:
    """An OSError from `git worktree lock` is swallowed — the lock is
    best-effort (#180). The worktree is created and returned even when locking
    fails, covering the except branch of _lock_story_worktree (lines 2599-2600)."""
    import sdlc.build as build_mod

    work = _repo_with_origin(tmp_path)
    _real_git = build_mod._git

    def _git_fail_on_lock(root, *args):
        # Let every git call through except the lock itself.
        if len(args) >= 2 and args[0] == "worktree" and args[1] == "lock":
            raise OSError("permission denied: .git/worktrees/agent-x/locked")
        return _real_git(root, *args)

    monkeypatch.setattr(build_mod, "_git", _git_fail_on_lock)
    # Must not raise — lock failure is deliberately swallowed so it never aborts a build.
    path = create_story_worktree(work, "17.2-001", "run-abc12345")
    assert path.is_dir()
    # Worktree is still git-registered (the add succeeded; only the lock was skipped).
    live = _git(work, "worktree", "list", "--porcelain").stdout
    assert str(path.resolve()) in {
        str(Path(p).resolve())
        for p in (
            line.removeprefix("worktree ")
            for line in live.splitlines()
            if line.startswith("worktree ")
        )
    }



# ---------------------------------------------------------------------------
# _refresh_base_ref: each cohort branches from the current origin/main (#231)
# ---------------------------------------------------------------------------

def _push_sibling_merge(origin: Path, tmp_path: Path) -> str:
    """Advance the *remote* main via a second clone, returning the new tip SHA.

    Simulates an earlier cohort merging and pushing to ``main`` mid-run, leaving
    the primary checkout's local ``origin/main`` ref stale until it refetches.
    """
    other = tmp_path / "sibling"
    subprocess.run(
        ["git", "clone", str(origin), str(other)],
        check=True, capture_output=True, text=True,
    )
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "Test")
    (other / "sibling.txt").write_text("merged by an earlier cohort\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "feat: sibling change")
    _git(other, "push", "origin", "main")
    return _git(other, "rev-parse", "HEAD").stdout.strip()


def test_refresh_advances_base_so_later_cohort_uses_merged_tip(tmp_path) -> None:
    """#231: after an earlier cohort merges, a per-cohort refresh means a later
    cohort's worktree branches from the new merged tip — not the run-start SHA."""
    work = _repo_with_origin(tmp_path)
    origin = tmp_path / "origin.git"

    # Cohort 1's worktree is cut from the run-start tip.
    run_start_sha = _git(work, "rev-parse", "origin/main").stdout.strip()
    p1 = create_story_worktree(work, "17.2-001", "run-abc12345")
    assert _git(p1, "rev-parse", "HEAD").stdout.strip() == run_start_sha

    # A sibling cohort merges to main; work's local origin/main is now stale.
    merged_sha = _push_sibling_merge(origin, tmp_path)
    assert merged_sha != run_start_sha
    assert _git(work, "rev-parse", "origin/main").stdout.strip() == run_start_sha

    # The per-cohort refresh pulls the new tip into work's origin/main...
    _refresh_base_ref(work)
    assert _git(work, "rev-parse", "origin/main").stdout.strip() == merged_sha

    # ...so cohort 2's worktree is created from the merged tip, not the stale one.
    p2 = create_story_worktree(work, "17.2-002", "run-abc12345")
    assert _git(p2, "rev-parse", "HEAD").stdout.strip() == merged_sha


def test_refresh_base_ref_offline_degrades_without_raising(tmp_path, monkeypatch) -> None:
    """#231: a failed fetch (offline) must never abort the run — it degrades to the
    current local ref, and a worktree can still be created from that ref."""
    import sdlc.build as build_mod

    work = _repo_with_origin(tmp_path)
    local_sha = _git(work, "rev-parse", "origin/main").stdout.strip()
    _real_git = build_mod._git

    def _git_fail_on_fetch(root, *args):
        if args and args[0] == "fetch":
            raise subprocess.SubprocessError("offline: could not reach origin")
        return _real_git(root, *args)

    monkeypatch.setattr(build_mod, "_git", _git_fail_on_fetch)
    # Must not raise despite the fetch failure.
    _refresh_base_ref(work)
    # Degrades to the unchanged local ref; the worktree still builds from it.
    path = create_story_worktree(work, "17.2-001", "run-abc12345")
    assert _git(path, "rev-parse", "HEAD").stdout.strip() == local_sha


def test_worktree_still_locked_after_base_refresh(tmp_path) -> None:
    """#231 regression: refreshing the base before creation must not disturb the
    #180 lock — the new worktree is still git-locked against the Stop-hook reaper."""
    work = _repo_with_origin(tmp_path)
    _refresh_base_ref(work)
    path = create_story_worktree(work, "17.2-001", "run-abc12345")

    porcelain = _git(work, "worktree", "list", "--porcelain").stdout
    locked: set[str] = set()
    current: str | None = None
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            current = str(Path(line.removeprefix("worktree ")).resolve())
        elif line.startswith("locked") and current is not None:
            locked.add(current)
    assert str(path.resolve()) in locked


def test_serial_path_never_creates_a_worktree_so_refresh_is_moot(tmp_path) -> None:
    """#231: serial / single-story runs build in the shared root and never create a
    per-story worktree, so cohort base-refresh cannot change their behaviour."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-17", "serial")
    ledger.story_upsert(
        run_id, "17.2-001", "17", "Worktree", "P2", 5, "py", "", None, "TODO"
    )
    # --sequential and an effective cap of 1 both reuse the root (no worktree).
    for opts in (BuildOptions(sequential=True), BuildOptions(concurrency=1)):
        workdir = _prepare_story_workdir(
            opts, _story("17.2-001"), ledger, run_id, real_run=True
        )
        assert workdir is None
    assert ledger.story_worktree(run_id, "17.2-001") is None


# ---------------------------------------------------------------------------
# Ledger: the worktree path is recorded and read back
# ---------------------------------------------------------------------------

def test_ledger_records_and_reads_worktree_path(tmp_path) -> None:
    """The chosen worktree path is persisted on the story row (AC1)."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-17", "parallel")
    ledger.story_upsert(
        run_id, "17.2-001", "17", "Worktree", "P2", 5, "py", "", None, "TODO"
    )
    assert ledger.story_worktree(run_id, "17.2-001") is None

    ledger.set_story_worktree(run_id, "17.2-001", "/tmp/wt/agent-x-17.2-001")
    assert ledger.story_worktree(run_id, "17.2-001") == "/tmp/wt/agent-x-17.2-001"


def test_worktree_column_added_by_migration(tmp_path) -> None:
    """A pre-existing ledger missing worktree_path gains it via ensure_migrated."""
    import sqlite3

    db = tmp_path / "ledger.db"
    # Build a stories table that predates the worktree column.
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE runs (id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE stories (
            run_id TEXT NOT NULL, story_id TEXT NOT NULL, status TEXT NOT NULL,
            PRIMARY KEY (run_id, story_id)
        );
        """
    )
    conn.commit()
    conn.close()

    Ledger(db).ensure_migrated()

    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stories)").fetchall()}
    conn.close()
    assert "worktree_path" in cols


# ---------------------------------------------------------------------------
# _prepare_story_workdir: when isolation kicks in
# ---------------------------------------------------------------------------

def test_prepare_workdir_sequential_reuses_root(tmp_path) -> None:
    """--sequential keeps today's shared-root behaviour: no worktree (AC3)."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-17", "serial")
    ledger.story_upsert(
        run_id, "17.2-001", "17", "Worktree", "P2", 5, "py", "", None, "TODO"
    )
    opts = BuildOptions(sequential=True)
    workdir = _prepare_story_workdir(
        opts, _story("17.2-001"), ledger, run_id, real_run=True
    )
    assert workdir is None
    assert ledger.story_worktree(run_id, "17.2-001") is None


def test_prepare_workdir_concurrency_one_reuses_root(tmp_path) -> None:
    """`--concurrency=1` (not --sequential) is byte-for-byte the serial path:
    an effective cap of 1 means no concurrency, so it must reuse the shared root
    just like --sequential — never create a worktree (Story 17.1-001 AC3).

    Regression: keying the decision off ``opts.sequential`` alone leaked a
    per-story worktree into a real ``--concurrency=1`` run, diverging from the
    serial path. Fake-dispatcher tests miss it (real_run=False short-circuits),
    so this asserts the real-run decision directly.
    """
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-17", "parallel")
    ledger.story_upsert(
        run_id, "17.2-001", "17", "Worktree", "P2", 5, "py", "", None, "TODO"
    )
    opts = BuildOptions(concurrency=1)  # parallel mode, but cap of 1
    assert opts.sequential is False
    workdir = _prepare_story_workdir(
        opts, _story("17.2-001"), ledger, run_id, real_run=True
    )
    assert workdir is None
    assert ledger.story_worktree(run_id, "17.2-001") is None


def test_prepare_workdir_fake_run_reuses_root(tmp_path) -> None:
    """A fake-dispatcher (test) run never touches the real repo: no worktree."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-17", "parallel")
    ledger.story_upsert(
        run_id, "17.2-001", "17", "Worktree", "P2", 5, "py", "", None, "TODO"
    )
    workdir = _prepare_story_workdir(
        BuildOptions(), _story("17.2-001"), ledger, run_id, real_run=False
    )
    assert workdir is None


def test_prepare_workdir_parallel_creates_and_records(tmp_path, monkeypatch) -> None:
    """A real parallel run creates a worktree and records its path (AC1)."""
    work = _repo_with_origin(tmp_path)
    monkeypatch.chdir(work)
    ledger = Ledger(work / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-17", "parallel")
    ledger.story_upsert(
        run_id, "17.2-001", "17", "Worktree", "P2", 5, "py", "", None, "TODO"
    )
    workdir = _prepare_story_workdir(
        BuildOptions(), _story("17.2-001"), ledger, run_id, real_run=True
    )
    assert workdir is not None and workdir.is_dir()
    assert ledger.story_worktree(run_id, "17.2-001") == str(workdir)


def test_prepare_workdir_failure_falls_back_to_root(tmp_path, monkeypatch) -> None:
    """A worktree-creation failure degrades to the shared root, never fatal."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-17", "parallel")
    ledger.story_upsert(
        run_id, "17.2-001", "17", "Worktree", "P2", 5, "py", "", None, "TODO"
    )

    def _boom(*a, **k):
        raise WorktreeError("no repo here")

    monkeypatch.setattr("sdlc.build.create_story_worktree", _boom)
    workdir = _prepare_story_workdir(
        BuildOptions(), _story("17.2-001"), ledger, run_id, real_run=True
    )
    assert workdir is None  # fell back to the root


# ---------------------------------------------------------------------------
# _run_story: the per-story workdir reaches the dispatcher as cwd
# ---------------------------------------------------------------------------

class _CwdRecordingDispatcher:
    """A fake dispatcher that records the cwd each stage was dispatched with."""

    def __init__(self) -> None:
        self.cwds: list[Path | None] = []

    def __call__(self, agent_type, prompt, story=None, **kwargs):
        from sdlc.dispatch import AgentResult

        self.cwds.append(kwargs.get("cwd"))
        sid = getattr(story, "id", "x")
        payload = {
            "build": {
                "branch_name": f"feature/{sid}",
                "build_status": "SUCCESS",
                "commit_sha": "deadbeef",
            },
            "coverage": {
                "pr_number": 100, "pr_url": "https://e/pull/100",
                "coverage_pct": 95.0, "tests_added": 3,
                "coverage_status": "PASS", "security_status": "PASS",
            },
            "review": {
                "pr_number": 100, "approval_status": "APPROVED",
                "change_count": 0, "final_status": "APPROVED",
            },
            "merge": {
                "pr_number": 100, "merge_status": "MERGED",
                "merge_sha": "cafef00d", "merged_at": "2026-06-12T00:00:00Z",
            },
        }[agent_type]
        return AgentResult(agent_type=agent_type, data=payload, raw="")


def test_run_story_threads_workdir_as_cwd(tmp_path) -> None:
    """Every stage dispatch for a story carries the per-story worktree cwd (AC1)."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-17", "parallel")
    ledger.story_upsert(
        run_id, "17.2-001", "17", "Worktree", "P2", 5, "py", "", None, "TODO"
    )
    wt = tmp_path / "agent-x-17.2-001"
    wt.mkdir()
    dispatcher = _CwdRecordingDispatcher()
    status = _run_story(
        _story("17.2-001"),
        BuildOptions(skip_coverage=True),
        ledger,
        run_id,
        dispatcher,
        tmp_path / "logs",
        workdir=wt,
    )
    assert status == "DONE"
    assert dispatcher.cwds  # at least one stage ran
    assert all(c == wt for c in dispatcher.cwds)


def test_run_story_without_workdir_passes_no_cwd(tmp_path) -> None:
    """No workdir → dispatch gets cwd=None (shared-root back-compat, AC3)."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.init()
    run_id = ledger.run_create("epic-17", "parallel")
    ledger.story_upsert(
        run_id, "17.2-001", "17", "Worktree", "P2", 5, "py", "", None, "TODO"
    )
    dispatcher = _CwdRecordingDispatcher()
    _run_story(
        _story("17.2-001"),
        BuildOptions(skip_coverage=True),
        ledger,
        run_id,
        dispatcher,
        tmp_path / "logs",
    )
    assert dispatcher.cwds and all(c is None for c in dispatcher.cwds)
