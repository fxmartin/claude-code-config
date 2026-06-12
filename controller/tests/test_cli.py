# ABOUTME: Behavior tests for the sdlc CLI scaffold (Story 7.1-001).
# ABOUTME: Covers --version, --help subcommand listing, and the init stub.

from __future__ import annotations

import tomllib
from pathlib import Path

from typer.testing import CliRunner

from sdlc import __version__
from sdlc.cli import PLANNED_SUBCOMMANDS, app

runner = CliRunner()


def _pyproject_version() -> str:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_version_matches_pyproject() -> None:
    """`sdlc --version` returns the version declared in pyproject.toml."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert _pyproject_version() in result.stdout


def test_package_version_constant_matches_pyproject() -> None:
    """The packaged __version__ constant stays in sync with pyproject.toml."""
    assert __version__ == _pyproject_version()


def test_help_lists_all_planned_subcommands() -> None:
    """`sdlc --help` lists every planned subcommand, even unimplemented stubs."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in PLANNED_SUBCOMMANDS:
        assert name in result.stdout


def test_planned_subcommands_cover_success_metrics() -> None:
    """The planned set matches the subcommands named in the Epic-07 metrics."""
    expected = {"build", "resume", "status", "state", "validate", "rollback", "init"}
    assert expected.issubset(set(PLANNED_SUBCOMMANDS))


def test_each_subcommand_has_one_line_description() -> None:
    """Every planned subcommand exposes a non-empty one-line help string."""
    for name, summary in PLANNED_SUBCOMMANDS.items():
        assert summary, f"{name} is missing a one-line description"
        assert "\n" not in summary, f"{name} description must be one line"


def test_init_stub_runs() -> None:
    """The init stub is invocable and exits cleanly while unimplemented."""
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert "init" in result.stdout.lower()


def test_unimplemented_stub_runs() -> None:
    """A representative stub subcommand is wired and exits cleanly."""
    result = runner.invoke(app, ["build"])
    assert result.exit_code == 0
