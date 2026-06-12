# ABOUTME: Behavior tests for the `sdlc sync-check` subcommand (Story 7.4-001).
# ABOUTME: Filesystem-only; verifies the parity verdict surfaces through the CLI.

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from sdlc.cli import app

runner = CliRunner()


def _seed(dir_path: Path, skills: dict[str, str]) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    for name, body in skills.items():
        (dir_path / f"{name}.md").write_text(body, encoding="utf-8")
    return dir_path


def test_sync_check_in_sync_exits_zero(tmp_path: Path) -> None:
    src = _seed(tmp_path / "src", {"roast": "x", "coverage": "y"})
    consumer = _seed(tmp_path / "consumer", {"roast": "x", "coverage": "y"})

    result = runner.invoke(app, ["sync-check", str(src), str(consumer)])

    assert result.exit_code == 0
    assert "in sync" in result.output.lower()


def test_sync_check_drift_exits_nonzero_and_names_skill(tmp_path: Path) -> None:
    src = _seed(tmp_path / "src", {"roast": "new"})
    consumer = _seed(tmp_path / "consumer", {"roast": "old"})

    result = runner.invoke(app, ["sync-check", str(src), str(consumer)])

    assert result.exit_code == 1
    assert "roast" in result.output
    assert "drift" in result.output.lower()


def test_sync_check_missing_dir_exits_two(tmp_path: Path) -> None:
    src = _seed(tmp_path / "src", {"roast": "x"})

    result = runner.invoke(app, ["sync-check", str(src), str(tmp_path / "nope")])

    assert result.exit_code == 2
    assert "error" in result.output.lower()
