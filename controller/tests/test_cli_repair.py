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
