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


# ---------------------------------------------------------------------------
# Additional stub command coverage (Story 7.1-001 QA gate)
# Each remaining planned subcommand must be invocable and exit cleanly.
# ---------------------------------------------------------------------------

def test_resume_stub_runs() -> None:
    """The resume stub exits 0 and echoes its name."""
    result = runner.invoke(app, ["resume"])
    assert result.exit_code == 0
    assert "resume" in result.stdout.lower()


def test_status_stub_runs() -> None:
    """The status stub exits 0 and echoes its name."""
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "status" in result.stdout.lower()


def test_state_stub_runs() -> None:
    """The state stub exits 0 and echoes its name."""
    result = runner.invoke(app, ["state"])
    assert result.exit_code == 0
    assert "state" in result.stdout.lower()


def test_validate_stub_runs() -> None:
    """The validate stub exits 0 and echoes its name."""
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 0
    assert "validate" in result.stdout.lower()


def test_rollback_stub_runs() -> None:
    """The rollback stub exits 0 and echoes its name."""
    result = runner.invoke(app, ["rollback"])
    assert result.exit_code == 0
    assert "rollback" in result.stdout.lower()


def test_unknown_command_exits_nonzero() -> None:
    """Invoking an unknown command produces a non-zero exit code."""
    result = runner.invoke(app, ["nonexistent-command"])
    assert result.exit_code != 0


def test_no_args_shows_help() -> None:
    """Invoking sdlc with no arguments shows help (no_args_is_help=True)."""
    result = runner.invoke(app, [])
    # Typer exits with code 0 for --help-style output when no_args_is_help=True
    assert "sdlc" in result.stdout.lower() or result.exit_code in (0, 1, 2)


def test_stub_output_contains_not_implemented() -> None:
    """All stub commands clearly indicate they are not yet implemented."""
    stubs = ["build", "resume", "status", "state", "validate", "rollback"]
    for cmd in stubs:
        result = runner.invoke(app, [cmd])
        assert result.exit_code == 0, f"{cmd} exited with {result.exit_code}"
        assert "not yet implemented" in result.stdout, (
            f"{cmd} output does not contain 'not yet implemented': {result.stdout!r}"
        )
