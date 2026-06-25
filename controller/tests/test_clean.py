# ABOUTME: Tests for `sdlc clean` — safe workspace garbage collection (Story 15.3-001).
# ABOUTME: Real temp git repos + a temp registry; asserts dry-run, squash-merge, live safety.

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import sdlc.clean as clean_mod
from sdlc.build import Ledger
from sdlc.clean import plan_clean, run_clean
from sdlc.registry import Registry, RunRecord


# --- git fixture helpers ----------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    """A repo on branch ``main`` with one base commit. No remote."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "chore: base")
    _git(root, "branch", "-M", "main")
    return root


def _make_feature_branch(root: Path, story_id: str) -> str:
    """Cut feature/<story_id> with one commit, return tip sha, leave HEAD on main."""
    branch = f"feature/{story_id}"
    _git(root, "checkout", "-q", "-b", branch)
    (root / f"{story_id}.txt").write_text("work\n", encoding="utf-8")
    _git(root, "add", f"{story_id}.txt")
    _git(root, "commit", "-q", "-m", f"feat: work (#{story_id})")
    sha = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "checkout", "-q", "main")
    return sha


def _branches(root: Path) -> set[str]:
    out = _git(root, "branch", "--format=%(refname:short)").stdout
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


# --- ledger / registry helpers ---------------------------------------------


def _seed_story(db_path: Path, run_id: str, story_id: str, status: str) -> None:
    ledger = Ledger(db_path)
    ledger.story_upsert(
        run_id, story_id, "15", story_id, "P1", 1, "general-purpose", "", None, "TODO"
    )
    ledger.set_story_status(run_id, story_id, status)


def _new_run(db_path: Path, status: str = "DONE") -> str:
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("epic-15", "serial")
    ledger.run_update_status(run_id, status)
    return run_id


def _registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "registry.json")


def _register(
    reg: Registry, run_id: str, repo: Path, pid: int, *, finished: bool
) -> None:
    reg.register(
        RunRecord(
            run_id=run_id,
            repo=str(repo.resolve()),
            db=str(repo / ".sdlc-state.db"),
            scope="epic-15",
            pid=pid,
            status="DONE" if finished else "IN_PROGRESS",
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-02T00:00:00+00:00" if finished else None,
        )
    )


def _dead_pid() -> int:
    # A pid that is overwhelmingly unlikely to be live in a test environment.
    return 2_000_000_000


# --- worktree helpers -------------------------------------------------------


def _add_worktree(root: Path, run_prefix: str, story_id: str) -> Path:
    """Create an agent-* worktree the way the controller names them."""
    wt_dir = root / ".claude" / "worktrees"
    wt_dir.mkdir(parents=True, exist_ok=True)
    path = wt_dir / f"agent-{run_prefix}-{story_id}"
    _git(root, "worktree", "add", "--detach", "--force", str(path), "HEAD")
    return path


# ---------------------------------------------------------------------------
# Dry-run default
# ---------------------------------------------------------------------------


def test_dry_run_default_removes_nothing(tmp_path):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")
    sha = _make_feature_branch(root, "15.3-009")
    _seed_story(db, run_id, "15.3-009", "DONE")

    plan = plan_clean(root=root, db_path=db, registry=_registry(tmp_path))

    # The merged branch is a candidate, but nothing is removed (dry-run default).
    assert plan.forced is False
    assert any(c.kind == "branch" and c.name == "feature/15.3-009" for c in plan.candidates)
    assert all(c.removed is False for c in plan.candidates)
    assert "feature/15.3-009" in _branches(root)  # still there
    # The branch tip is still reachable.
    assert _git(root, "rev-parse", "feature/15.3-009").stdout.strip() == sha


# ---------------------------------------------------------------------------
# Squash-merge detection via ledger DONE / gh — not `git branch --merged`
# ---------------------------------------------------------------------------


def test_squash_merged_branch_detected_via_ledger_done(tmp_path):
    """A squash-merged branch is unmerged to `git branch --merged` but DONE in ledger."""
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")
    _make_feature_branch(root, "15.3-010")
    _seed_story(db, run_id, "15.3-010", "DONE")

    # Sanity: git itself does NOT consider it merged (the bug the story calls out).
    merged_out = _git(root, "branch", "--merged", "main").stdout
    assert "feature/15.3-010" not in merged_out

    plan = plan_clean(
        root=root, db_path=db, registry=_registry(tmp_path),
        gh_merged_fn=lambda branch, root: False,  # gh offline → ledger carries it
    )
    branch_cands = [c.name for c in plan.candidates if c.kind == "branch"]
    assert "feature/15.3-010" in branch_cands


def test_unmerged_branch_is_protected(tmp_path):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="NEEDS_ATTENTION")
    _make_feature_branch(root, "15.3-011")
    _seed_story(db, run_id, "15.3-011", "NEEDS_ATTENTION")

    plan = plan_clean(
        root=root, db_path=db, registry=_registry(tmp_path),
        gh_merged_fn=lambda branch, root: False,
    )
    assert all(c.name != "feature/15.3-011" for c in plan.candidates)
    assert any(
        c.kind == "branch" and c.name == "feature/15.3-011" for c in plan.protected
    )


def test_gh_merged_signal_detects_branch_without_ledger_done(tmp_path):
    """gh PR MERGED alone makes a branch a candidate even if the ledger isn't DONE."""
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="NEEDS_ATTENTION")
    _make_feature_branch(root, "15.3-012")
    _seed_story(db, run_id, "15.3-012", "NEEDS_ATTENTION")

    plan = plan_clean(
        root=root, db_path=db, registry=_registry(tmp_path),
        gh_merged_fn=lambda branch, root: branch == "feature/15.3-012",
    )
    assert any(
        c.kind == "branch" and c.name == "feature/15.3-012" for c in plan.candidates
    )


# ---------------------------------------------------------------------------
# Live-run safety (registry + pid)
# ---------------------------------------------------------------------------


def test_live_run_branch_never_touched(tmp_path):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="IN_PROGRESS")
    _make_feature_branch(root, "15.3-013")
    _seed_story(db, run_id, "15.3-013", "IN_PROGRESS")

    reg = _registry(tmp_path)
    _register(reg, run_id, root, os.getpid(), finished=False)  # live: our own pid

    plan = plan_clean(root=root, db_path=db, registry=reg)
    assert all(c.name != "feature/15.3-013" for c in plan.candidates)
    assert any(
        c.kind == "branch" and c.name == "feature/15.3-013" for c in plan.protected
    )


def test_live_run_worktree_never_touched(tmp_path):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="IN_PROGRESS")
    prefix = run_id.split("-")[0]
    wt = _add_worktree(root, prefix, "15.3-014")

    reg = _registry(tmp_path)
    _register(reg, run_id, root, os.getpid(), finished=False)  # live

    plan = plan_clean(root=root, db_path=db, registry=reg)
    assert all(Path(c.path or "") != wt for c in plan.candidates)
    assert any(c.kind == "worktree" and Path(c.path) == wt for c in plan.protected)


def test_dead_run_worktree_is_candidate(tmp_path):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="IN_PROGRESS")  # ledger still says in-progress
    prefix = run_id.split("-")[0]
    wt = _add_worktree(root, prefix, "15.3-015")

    reg = _registry(tmp_path)
    _register(reg, run_id, root, _dead_pid(), finished=False)  # crashed: dead pid

    plan = plan_clean(root=root, db_path=db, registry=reg)
    assert any(c.kind == "worktree" and Path(c.path) == wt for c in plan.candidates)


def test_dirty_worktree_is_protected(tmp_path):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")  # terminal
    prefix = run_id.split("-")[0]
    wt = _add_worktree(root, prefix, "15.3-016")
    (wt / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")  # make it dirty

    plan = plan_clean(root=root, db_path=db, registry=_registry(tmp_path))
    assert all(Path(c.path or "") != wt for c in plan.candidates)
    assert any(
        c.kind == "worktree" and Path(c.path) == wt and "dirt" in c.reason.lower()
        for c in plan.protected
    )


# ---------------------------------------------------------------------------
# Stale transcript logs
# ---------------------------------------------------------------------------


def test_stale_logs_are_candidates_live_logs_protected(tmp_path):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    terminal_run = _new_run(db, status="DONE")
    live_run = _new_run(db, status="IN_PROGRESS")

    logs_root = Path(f"{db}.logs")
    (logs_root / terminal_run).mkdir(parents=True)
    (logs_root / terminal_run / "a.log").write_text("x", encoding="utf-8")
    (logs_root / live_run).mkdir(parents=True)

    reg = _registry(tmp_path)
    _register(reg, live_run, root, os.getpid(), finished=False)

    plan = plan_clean(root=root, db_path=db, registry=reg)
    log_cands = {c.name for c in plan.candidates if c.kind == "logs"}
    log_prot = {c.name for c in plan.protected if c.kind == "logs"}
    assert terminal_run in log_cands
    assert live_run in log_prot


# ---------------------------------------------------------------------------
# Execution under --force: removes, logs, recoverable, no remote mutation
# ---------------------------------------------------------------------------


def test_force_removes_branch_worktree_and_logs(tmp_path):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")
    prefix = run_id.split("-")[0]

    sha = _make_feature_branch(root, "15.3-017")
    _seed_story(db, run_id, "15.3-017", "DONE")
    wt = _add_worktree(root, prefix, "15.3-017")
    logs_root = Path(f"{db}.logs")
    (logs_root / run_id).mkdir(parents=True)

    plan = run_clean(
        root=root, db_path=db, registry=_registry(tmp_path), force=True,
        gh_merged_fn=lambda branch, root: False,
    )

    assert plan.forced is True
    # branch deleted but tip still reachable via reflog (recoverable, R10/AC5).
    assert "feature/15.3-017" not in _branches(root)
    reflog = _git(root, "reflog", "--no-abbrev").stdout
    assert sha in reflog
    # worktree removed from disk and deregistered.
    assert not wt.exists()
    wt_list = _git(root, "worktree", "list", "--porcelain").stdout
    assert str(wt) not in wt_list
    # stale log dir removed.
    assert not (logs_root / run_id).exists()
    # every candidate marked removed.
    assert all(c.removed for c in plan.candidates)


def test_force_never_mutates_remote(tmp_path, monkeypatch):
    """clean must never run a remote-mutating git verb (push/fetch)."""
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")
    _make_feature_branch(root, "15.3-018")
    _seed_story(db, run_id, "15.3-018", "DONE")

    seen: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def _spy(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)):
            seen.append(tuple(str(x) for x in cmd))
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(clean_mod.subprocess, "run", _spy)
    run_clean(
        root=root, db_path=db, registry=_registry(tmp_path), force=True,
        gh_merged_fn=lambda branch, root: False,
    )
    for cmd in seen:
        assert "push" not in cmd, f"clean must not push: {cmd}"
        # no `git fetch` either — clean works purely on local + read-only gh.
        if "git" in cmd[0] or cmd[0] == "git":
            assert "fetch" not in cmd, f"clean must not fetch: {cmd}"


def test_empty_repo_yields_empty_plan(tmp_path):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    plan = plan_clean(root=root, db_path=db, registry=_registry(tmp_path))
    assert plan.candidates == []
    assert plan.total == 0


# ---------------------------------------------------------------------------
# CleanPlan kind properties / to_dict
# ---------------------------------------------------------------------------


def test_plan_kind_properties_partition_candidates(tmp_path):
    """`.worktrees` / `.branches` / `.logs` filter candidates by kind."""
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")
    prefix = run_id.split("-")[0]

    _make_feature_branch(root, "15.3-030")
    _seed_story(db, run_id, "15.3-030", "DONE")
    wt = _add_worktree(root, prefix, "15.3-030")
    (Path(f"{db}.logs") / run_id).mkdir(parents=True)

    plan = plan_clean(
        root=root, db_path=db, registry=_registry(tmp_path),
        gh_merged_fn=lambda branch, root: False,
    )

    assert {c.kind for c in plan.worktrees} == {"worktree"}
    assert {c.kind for c in plan.branches} == {"branch"}
    assert {c.kind for c in plan.logs} == {"logs"}
    assert plan.total == len(plan.worktrees) + len(plan.branches) + len(plan.logs)
    assert any(Path(c.path) == wt for c in plan.worktrees)
    d = plan.to_dict()
    assert d["total"] == plan.total and d["removed"] == 0


# ---------------------------------------------------------------------------
# Worktree edge cases: untracked debris, missing-on-disk, locked, cwd
# ---------------------------------------------------------------------------


def test_untracked_agent_dir_is_candidate_and_live_prefix_protected(tmp_path):
    """An `agent-*` dir git no longer registers is reclaimable — unless live owns it."""
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    live_run = _new_run(db, status="IN_PROGRESS")
    live_prefix = live_run.split("-")[0]

    wt_dir = root / ".claude" / "worktrees"
    wt_dir.mkdir(parents=True)
    # Untracked, terminal-owned → candidate.
    (wt_dir / "agent-deadbeef-15.3-031").mkdir()
    # Untracked but owned by a live run prefix → protected.
    (wt_dir / f"agent-{live_prefix}-15.3-032").mkdir()
    # A non-agent dir and a stray file are ignored entirely.
    (wt_dir / "not-an-agent").mkdir()
    (wt_dir / "stray.txt").write_text("x", encoding="utf-8")

    reg = _registry(tmp_path)
    _register(reg, live_run, root, os.getpid(), finished=False)

    plan = plan_clean(root=root, db_path=db, registry=reg)
    wt_cand = {c.name for c in plan.candidates if c.kind == "worktree"}
    wt_prot = {c.name for c in plan.protected if c.kind == "worktree"}
    assert "agent-deadbeef-15.3-031" in wt_cand
    assert f"agent-{live_prefix}-15.3-032" in wt_prot
    assert "not-an-agent" not in wt_cand and "stray.txt" not in wt_cand


def test_registered_worktree_missing_on_disk_is_candidate(tmp_path):
    """A registered worktree whose directory vanished is reclaimable."""
    import shutil as _shutil

    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")
    prefix = run_id.split("-")[0]
    wt = _add_worktree(root, prefix, "15.3-033")
    _shutil.rmtree(wt)  # registration remains, directory is gone

    plan = plan_clean(root=root, db_path=db, registry=_registry(tmp_path))
    assert any(
        c.kind == "worktree" and Path(c.path) == wt and "missing" in c.reason
        for c in plan.candidates
    )


def test_locked_worktree_owned_by_live_run_is_protected(tmp_path):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="IN_PROGRESS")
    prefix = run_id.split("-")[0]
    wt = _add_worktree(root, prefix, "15.3-034")
    _git(root, "worktree", "lock", str(wt))

    reg = _registry(tmp_path)
    _register(reg, run_id, root, os.getpid(), finished=False)

    plan = plan_clean(root=root, db_path=db, registry=reg)
    assert any(
        c.kind == "worktree" and Path(c.path) == wt and "locked" in c.reason
        for c in plan.protected
    )


def test_cwd_agent_worktree_is_protected(tmp_path, monkeypatch):
    """An `agent-*` worktree that is the current working directory is spared."""
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")  # terminal — would otherwise be a candidate
    prefix = run_id.split("-")[0]
    wt = _add_worktree(root, prefix, "15.3-035")

    monkeypatch.chdir(wt)  # cwd == the worktree → "active worktree (main/cwd)"
    plan = plan_clean(root=root, db_path=db, registry=_registry(tmp_path))
    assert any(
        c.kind == "worktree" and Path(c.path).name == wt.name and "cwd" in c.reason
        for c in plan.protected
    )


# ---------------------------------------------------------------------------
# Branch edge cases: current branch, default gh signal
# ---------------------------------------------------------------------------


def test_current_feature_branch_is_protected(tmp_path):
    """The checked-out feature branch is never a candidate."""
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")
    _make_feature_branch(root, "15.3-036")
    _seed_story(db, run_id, "15.3-036", "DONE")  # DONE, yet HEAD is on it
    _git(root, "checkout", "-q", "feature/15.3-036")

    plan = plan_clean(
        root=root, db_path=db, registry=_registry(tmp_path),
        gh_merged_fn=lambda branch, root: False,
    )
    assert all(c.name != "feature/15.3-036" for c in plan.candidates)
    assert any(
        c.kind == "branch" and c.name == "feature/15.3-036"
        and c.reason == "current branch"
        for c in plan.protected
    )


def test_default_gh_signal_used_when_ledger_not_done(tmp_path):
    """With no ledger DONE, the real `gh` helper runs and (offline) protects the branch."""
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    _make_feature_branch(root, "15.3-037")  # no ledger row at all

    # gh_merged_fn=None → the real _gh_branch_merged runs; no remote/PR → False.
    plan = plan_clean(root=root, db_path=db, registry=_registry(tmp_path))
    assert any(
        c.kind == "branch" and c.name == "feature/15.3-037" for c in plan.protected
    )


def test_gh_branch_merged_handles_missing_gh(tmp_path, monkeypatch):
    """The gh helper is best-effort: a missing/erroring binary yields False, never raises."""
    root = _init_repo(tmp_path)

    def _boom(*a, **k):
        raise FileNotFoundError("gh not installed")

    monkeypatch.setattr(clean_mod.subprocess, "run", _boom)
    assert clean_mod._gh_branch_merged("feature/x", root) is False


# ---------------------------------------------------------------------------
# Helper degradation: a failing/erroring git never crashes clean
# ---------------------------------------------------------------------------


def _fake_proc(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr="")


def test_registered_worktrees_degrades_on_git_error(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setattr(clean_mod, "_git", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert clean_mod._registered_worktrees(root) == []
    # And on a non-zero return code.
    monkeypatch.setattr(clean_mod, "_git", lambda *a, **k: _fake_proc(1))
    assert clean_mod._registered_worktrees(root) == []


def test_is_dirty_protects_when_status_cannot_run(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setattr(clean_mod, "_git", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert clean_mod._is_dirty(root) is True  # un-inspectable → treat as dirty
    monkeypatch.setattr(clean_mod, "_git", lambda *a, **k: _fake_proc(128))
    assert clean_mod._is_dirty(root) is True


def test_local_feature_branches_degrades_on_git_error(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setattr(clean_mod, "_git", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert clean_mod._local_feature_branches(root) == []
    monkeypatch.setattr(clean_mod, "_git", lambda *a, **k: _fake_proc(1))
    assert clean_mod._local_feature_branches(root) == []


def test_current_branch_degrades_on_git_error(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setattr(clean_mod, "_git", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert clean_mod._current_branch(root) is None
    monkeypatch.setattr(clean_mod, "_git", lambda *a, **k: _fake_proc(1))
    assert clean_mod._current_branch(root) is None


def test_delete_branch_degrades_on_git_error(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setattr(clean_mod, "_git", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    assert clean_mod._delete_branch(root, "feature/x") is False


def test_collect_logs_skips_files_and_survives_iterdir_error(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    logs_root = Path(f"{db}.logs")
    logs_root.mkdir(parents=True)
    (logs_root / "stray.txt").write_text("x", encoding="utf-8")  # a file, not a run dir

    plan = plan_clean(root=root, db_path=db, registry=_registry(tmp_path))
    assert all(c.kind != "logs" for c in plan.candidates)  # the file is ignored

    # iterdir raising is swallowed (best-effort).
    from sdlc.clean import _collect_logs, CleanPlan

    def _boom(self):
        raise OSError("denied")

    monkeypatch.setattr(Path, "iterdir", _boom)
    empty = CleanPlan(root=str(root))
    _collect_logs(db, set(), empty)  # must not raise
    assert empty.candidates == []


# ---------------------------------------------------------------------------
# --force failure handling: errors are recorded, never raised
# ---------------------------------------------------------------------------


def test_force_records_error_when_branch_removal_fails(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")
    _make_feature_branch(root, "15.3-038")
    _seed_story(db, run_id, "15.3-038", "DONE")

    monkeypatch.setattr(clean_mod, "_delete_branch", lambda root, branch: False)
    plan = run_clean(
        root=root, db_path=db, registry=_registry(tmp_path), force=True,
        gh_merged_fn=lambda branch, root: False,
    )
    assert any(c.name == "feature/15.3-038" and not c.removed for c in plan.candidates)
    assert any("feature/15.3-038" in e for e in plan.errors)


def test_force_records_error_when_worktree_removal_raises(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")
    prefix = run_id.split("-")[0]
    _add_worktree(root, prefix, "15.3-039")

    def _boom(root, path):
        raise OSError("cannot remove")

    monkeypatch.setattr(clean_mod, "remove_story_worktree", _boom)
    plan = run_clean(
        root=root, db_path=db, registry=_registry(tmp_path), force=True,
    )
    assert plan.errors  # the OSError is captured, not propagated
    assert any(not c.removed for c in plan.candidates if c.kind == "worktree")


def test_finished_registered_run_is_not_treated_as_live(tmp_path):
    """A registry record with finished_at set is terminal → its branch is reclaimable."""
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")
    _make_feature_branch(root, "15.3-040")
    _seed_story(db, run_id, "15.3-040", "DONE")

    reg = _registry(tmp_path)
    _register(reg, run_id, root, os.getpid(), finished=True)  # finished_at set

    plan = plan_clean(
        root=root, db_path=db, registry=reg,
        gh_merged_fn=lambda branch, root: False,
    )
    assert any(
        c.kind == "branch" and c.name == "feature/15.3-040" for c in plan.candidates
    )
