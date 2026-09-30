# ABOUTME: Issue #739 — the run ledger is kept out of `git status` in every repo
# ABOUTME: the controller runs in: on Ledger.init(), in worktrees, and doctor warns.
from __future__ import annotations

import subprocess
from pathlib import Path

from sdlc.build import Ledger, _ensure_repo_ignores
from sdlc.doctor import check_ledger_ignored, run_doctor

LEDGER_FILES = (
    ".sdlc-state.db",
    ".sdlc-state.db-shm",
    ".sdlc-state.db-wal",
    ".sdlc-state.db.logs/issue-1-build-1.log",
)


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=check
    )


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A repo on ``main`` with one committed file and no ignore rules."""
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "CLAUDE.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "CLAUDE.md")
    _git(root, "commit", "-q", "-m", "chore: base")
    _git(root, "branch", "-M", "main")
    return root


def _touch_ledger_files(root: Path) -> None:
    for rel in LEDGER_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")


def _untracked(root: Path) -> list[str]:
    out = _git(root, "status", "--porcelain", "--untracked-files=all").stdout
    return [line[3:] for line in out.splitlines()]


# ---------------------------------------------------------------------------
# The ledger is ignored wherever it is created — not only by `sdlc build`
# ---------------------------------------------------------------------------


def test_ledger_init_keeps_every_ledger_file_out_of_git_status(tmp_path) -> None:
    """local-ci-cd showed `?? .sdlc-state.db{,-shm,-wal}` after `sdlc fix` runs:
    the R9 exclude was wired into run_build only. Ledger.init() is the one
    place every creator (build, fix, issues init) passes through."""
    root = _init_repo(tmp_path)
    Ledger(root / ".sdlc-state.db").init()
    _touch_ledger_files(root)
    assert _untracked(root) == []


def test_ensure_repo_ignores_leaves_an_already_ignoring_repo_untouched(tmp_path) -> None:
    root = _init_repo(tmp_path)
    (root / ".gitignore").write_text(".sdlc-state.db*\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-q", "-m", "chore: ignore ledger")

    exclude = root / ".git" / "info" / "exclude"  # git init ships a template one
    before = exclude.read_text(encoding="utf-8") if exclude.exists() else None

    _ensure_repo_ignores(root / ".sdlc-state.db")

    after = exclude.read_text(encoding="utf-8") if exclude.exists() else None
    assert after == before
    _touch_ledger_files(root)
    assert _untracked(root) == []


def test_ensure_repo_ignores_works_from_a_linked_worktree(tmp_path) -> None:
    """A worktree's `.git` is a file; the exclude lives in the common git dir.
    Story worktrees are exactly where the controller opens ledgers mid-run."""
    root = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _git(root, "worktree", "add", "-q", "-b", "feature/x", str(wt))
    assert (wt / ".git").is_file()

    _ensure_repo_ignores(wt / ".sdlc-state.db")

    _touch_ledger_files(wt)
    assert _untracked(wt) == []
    # Written once, to the shared exclude — the main checkout is covered too.
    _touch_ledger_files(root)
    assert _untracked(root) == []
    exclude = root / ".git" / "info" / "exclude"
    assert exclude.read_text(encoding="utf-8").count(".sdlc-state.db*") == 1


# ---------------------------------------------------------------------------
# doctor names an unignored ledger
# ---------------------------------------------------------------------------


def test_check_ledger_ignored_warns_when_the_ledger_shows_in_git_status(tmp_path) -> None:
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    db.write_bytes(b"")

    finding = check_ledger_ignored(root, db)

    assert finding.check == "ledger"
    assert finding.status == "WARN"
    assert ".sdlc-state.db" in finding.detail
    assert "info/exclude" in finding.remedy or ".gitignore" in finding.remedy


def test_check_ledger_ignored_is_clean_once_excluded(tmp_path) -> None:
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    Ledger(db).init()
    assert check_ledger_ignored(root, db).status == "CLEAN"


def test_check_ledger_ignored_is_clean_with_no_ledger_or_no_repo(tmp_path) -> None:
    root = _init_repo(tmp_path)
    assert check_ledger_ignored(root, root / ".sdlc-state.db").status == "CLEAN"
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / ".sdlc-state.db").write_bytes(b"")
    assert check_ledger_ignored(plain, plain / ".sdlc-state.db").status == "CLEAN"


def test_run_doctor_includes_the_ledger_ignore_check(tmp_path) -> None:
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    Ledger(db).init()
    _git(root, "rm", "-q", "--cached", "--ignore-unmatch", ".sdlc-state.db")
    report = run_doctor(
        repo_root=root, claude_dir=tmp_path / "claude", db_path=db,
        queue_path=tmp_path / "queue.db", dep_probe=lambda tool: True,
    )
    names = [f.name for f in report.findings]
    assert "Ledger ignored by git" in names
