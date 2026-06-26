# ABOUTME: Behavior tests for the `sdlc repair` subcommand (Story 15.1-003).
# ABOUTME: Filesystem-only; drives the verb via --root/--claude-dir overrides.

from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner

from sdlc.cli import app
from sdlc.repair import MANAGED_LINKS

runner = CliRunner()


def _is_file_artifact(src_rel: str) -> bool:
    return src_rel.endswith((".md", ".json", ".sh"))


def _seed_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for _dest_rel, src_rel in MANAGED_LINKS:
        if src_rel == ".":
            continue
        target = repo / src_rel
        if _is_file_artifact(src_rel):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# {src_rel}\n", encoding="utf-8")
        else:
            target.mkdir(parents=True, exist_ok=True)
    return repo


def _link_all(repo: Path, claude_dir: Path) -> None:
    claude_dir.mkdir(parents=True, exist_ok=True)
    for dest_rel, src_rel in MANAGED_LINKS:
        dest = claude_dir / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(repo / src_rel, dest)


def test_repair_healthy_install_is_noop(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    _link_all(repo, claude_dir)

    result = runner.invoke(
        app, ["repair", "--root", str(repo), "--claude-dir", str(claude_dir)]
    )

    assert result.exit_code == 0
    assert "healthy" in result.output.lower()


def test_repair_restores_missing_link(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    _link_all(repo, claude_dir)
    (claude_dir / "CLAUDE.md").unlink()

    result = runner.invoke(
        app, ["repair", "--root", str(repo), "--claude-dir", str(claude_dir)]
    )

    assert result.exit_code == 0
    assert "CLAUDE.md" in result.output
    dest = claude_dir / "CLAUDE.md"
    assert dest.is_symlink()
    assert Path(os.readlink(dest)).resolve() == (repo / "CLAUDE.md").resolve()


def test_repair_refuses_worktree_root(tmp_path: Path) -> None:
    # Guard (#179): repair re-points every managed ~/.claude link at --root. If
    # --root is an ephemeral agent worktree (.claude/worktrees/agent-*), those
    # links dangle the moment the worktree is torn down, clobbering the live
    # install. The verb must refuse before mutating anything. Seed the managed
    # set inside the worktree so only the guard — not a missing source — stops it.
    wt = tmp_path / ".claude" / "worktrees" / "agent-test-1"
    repo = _seed_repo(wt)
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()  # empty target — everything would be "missing"

    result = runner.invoke(
        app, ["repair", "--root", str(repo), "--claude-dir", str(claude_dir)]
    )

    assert result.exit_code != 0
    assert "worktree" in result.output.lower()
    # No managed symlink was created in the target.
    assert list(claude_dir.iterdir()) == []


def test_repair_dry_run_reports_without_acting(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    claude_dir = tmp_path / "claude"
    _link_all(repo, claude_dir)
    (claude_dir / "hooks").unlink()

    result = runner.invoke(
        app,
        ["repair", "--dry-run", "--root", str(repo), "--claude-dir", str(claude_dir)],
    )

    assert result.exit_code == 0
    assert "dry-run" in result.output.lower()
    assert "hooks" in result.output
    # Filesystem untouched.
    assert not (claude_dir / "hooks").exists()
