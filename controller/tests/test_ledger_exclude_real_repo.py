# ABOUTME: Story 12.2-005 / issue #835 — the ledger exclude is written only into a
# ABOUTME: git dir git itself recognises; a stray or fake `.git` is never created.
from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from sdlc import build
from sdlc.build import Ledger, _ensure_repo_ignores
from sdlc.cli import app

PATTERN = ".sdlc-state.db*"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "CLAUDE.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "CLAUDE.md")
    _git(root, "commit", "-q", "-m", "chore: base")
    return root


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Every path under ``root`` (dirs map to None) — to prove nothing changed."""
    return {
        str(p.relative_to(root)): (None if p.is_dir() else p.read_bytes())
        for p in sorted(root.rglob("*"))
    }


def test_no_enclosing_repo_creates_no_git(tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    Ledger(plain / ".sdlc-state.db").init()
    assert not (plain / ".git").exists()
    assert not (tmp_path / ".git").exists()


def test_empty_fake_git_dir_is_left_untouched(tmp_path) -> None:
    """An empty `.git` dir is not a repo to git — no `info/` may appear in it."""
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "sub"
    sub.mkdir()
    Ledger(sub / ".sdlc-state.db").init()
    assert list((tmp_path / ".git").iterdir()) == []


def test_fake_git_dir_holding_only_info_is_not_written(tmp_path) -> None:
    """The shape #835 left in /tmp: `.git/info/exclude` and nothing else."""
    info = tmp_path / ".git" / "info"
    info.mkdir(parents=True)
    (info / "exclude").write_text("# stray\n", encoding="utf-8")
    before = _snapshot(tmp_path / ".git")

    _ensure_repo_ignores(tmp_path / ".sdlc-state.db")

    assert _snapshot(tmp_path / ".git") == before


def test_real_repo_gets_the_pattern_exactly_once(tmp_path) -> None:
    root = _init_repo(tmp_path)
    exclude = root / ".git" / "info" / "exclude"
    _ensure_repo_ignores(root / ".sdlc-state.db")
    _ensure_repo_ignores(root / ".sdlc-state.db")
    assert exclude.read_text(encoding="utf-8").split().count(PATTERN) == 1


def test_real_repo_without_info_dir_still_gets_the_exclude(tmp_path) -> None:
    """`git init --template=` ships no `info/`; creating it in a real git dir is fine."""
    root = _init_repo(tmp_path)
    info = root / ".git" / "info"
    for child in info.iterdir():
        child.unlink()
    info.rmdir()

    _ensure_repo_ignores(root / ".sdlc-state.db")

    assert PATTERN in (info / "exclude").read_text(encoding="utf-8").split()


def test_ledger_in_a_subdirectory_targets_the_repo_git_dir(tmp_path) -> None:
    root = _init_repo(tmp_path)
    sub = root / "a" / "b"
    sub.mkdir(parents=True)
    _ensure_repo_ignores(sub / ".sdlc-state.db")
    exclude = root / ".git" / "info" / "exclude"
    assert exclude.read_text(encoding="utf-8").split().count(PATTERN) == 1
    assert not (sub / ".git").exists()


def test_linked_worktree_writes_the_shared_exclude_once(tmp_path) -> None:
    root = _init_repo(tmp_path)
    wt = tmp_path / "wt"
    _git(root, "worktree", "add", "-q", "-b", "feature/x", str(wt))
    assert (wt / ".git").is_file()

    _ensure_repo_ignores(wt / ".sdlc-state.db")
    _ensure_repo_ignores(wt / ".sdlc-state.db")

    shared = root / ".git" / "info" / "exclude"
    assert shared.read_text(encoding="utf-8").split().count(PATTERN) == 1
    private = Path(_git(wt, "rev-parse", "--absolute-git-dir").stdout.strip())
    assert not (private / "info").exists()


def test_check_ignore_failure_writes_nothing(tmp_path, monkeypatch) -> None:
    """check-ignore exit 128 means git could not say — never a licence to write."""
    root = _init_repo(tmp_path)
    exclude = root / ".git" / "info" / "exclude"
    before = exclude.read_text(encoding="utf-8")
    real_git = build._git

    def fake_git(where: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "check-ignore":
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: not a git repository")
        return real_git(where, *args)

    monkeypatch.setattr(build, "_git", fake_git)
    _ensure_repo_ignores(root / ".sdlc-state.db")
    assert exclude.read_text(encoding="utf-8") == before


def test_check_ignore_oserror_writes_nothing(tmp_path, monkeypatch) -> None:
    root = _init_repo(tmp_path)
    exclude = root / ".git" / "info" / "exclude"
    before = exclude.read_text(encoding="utf-8")
    real_git = build._git

    def fake_git(where: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "check-ignore":
            raise OSError("git vanished")
        return real_git(where, *args)

    monkeypatch.setattr(build, "_git", fake_git)
    _ensure_repo_ignores(root / ".sdlc-state.db")
    assert exclude.read_text(encoding="utf-8") == before


def test_no_git_dir_is_subprocess_free(tmp_path, monkeypatch) -> None:
    """Test ledgers outside any repo must not pay for a git subprocess."""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(build, "_git", lambda where, *args: calls.append(args))
    plain = tmp_path / "plain"
    plain.mkdir()
    _ensure_repo_ignores(plain / ".sdlc-state.db")
    assert calls == []


def test_build_usage_error_leaves_the_directory_untouched(tmp_path, monkeypatch) -> None:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    result = CliRunner().invoke(app, ["build", "--bogus"])
    assert result.exit_code != 0
    assert _snapshot(work) == {}
    assert not (tmp_path / ".git").exists()
