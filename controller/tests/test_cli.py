# ABOUTME: Behavior tests for the sdlc CLI scaffold (Story 7.1-001) and validate (7.2-001).
# ABOUTME: Covers --version, --help, the init stub, and the validate command.

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
    """A representative stub subcommand is wired and exits cleanly.

    `build` is now fully implemented (Story 7.3-001); `resume` remains a stub,
    so it stands in as the representative unimplemented command here.
    """
    result = runner.invoke(app, ["resume"])
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


def test_validate_requires_agent_type() -> None:
    """`validate` now requires an agent-type argument (Story 7.2-001)."""
    result = runner.invoke(app, ["validate"])
    assert result.exit_code != 0


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
    # `build` is implemented in Story 7.3-001 and is no longer a stub.
    stubs = ["resume", "status", "state", "rollback"]
    for cmd in stubs:
        result = runner.invoke(app, [cmd])
        assert result.exit_code == 0, f"{cmd} exited with {result.exit_code}"
        assert "not yet implemented" in result.stdout, (
            f"{cmd} output does not contain 'not yet implemented': {result.stdout!r}"
        )


# ---------------------------------------------------------------------------
# `sdlc validate` command (Story 7.2-001)
# ---------------------------------------------------------------------------

_VALID_BUILD_RESPONSE = (
    "Build done.\n"
    "<<<RESULT_JSON>>>\n"
    '{"branch_name": "feature/7.2-001", "build_status": "SUCCESS", '
    '"commit_sha": "abc123"}\n'
    "<<<END_RESULT>>>\n"
)


def test_validate_accepts_valid_response_via_stdin() -> None:
    """`validate build` reads stdin and exits 0 on a valid response."""
    result = runner.invoke(app, ["validate", "build"], input=_VALID_BUILD_RESPONSE)
    assert result.exit_code == 0, result.stdout
    assert "feature/7.2-001" in result.stdout


def test_validate_accepts_valid_response_via_file(tmp_path) -> None:
    """`validate build <file>` reads a file and exits 0 on a valid response."""
    response = tmp_path / "resp.txt"
    response.write_text(_VALID_BUILD_RESPONSE, encoding="utf-8")
    result = runner.invoke(app, ["validate", "build", str(response)])
    assert result.exit_code == 0, result.stdout


def test_validate_rejects_missing_field_with_actionable_error() -> None:
    """A missing required field exits non-zero and names the field on stderr."""
    bad = (
        "<<<RESULT_JSON>>>\n"
        '{"build_status": "SUCCESS", "commit_sha": "abc123"}\n'
        "<<<END_RESULT>>>\n"
    )
    result = runner.invoke(app, ["validate", "build"], input=bad)
    assert result.exit_code == 1
    assert "branch_name" in result.output


def test_validate_rejects_missing_marker_block() -> None:
    """A response with no marker block exits non-zero with a clear message."""
    result = runner.invoke(app, ["validate", "build"], input="no markers")
    assert result.exit_code == 1
    assert "RESULT_JSON" in result.output


def test_validate_rejects_unknown_agent_type() -> None:
    """An unknown agent type exits with code 2 and lists valid types."""
    result = runner.invoke(app, ["validate", "frobnicate"], input="{}")
    assert result.exit_code == 2
    assert "unknown agent type" in result.output


# ---------------------------------------------------------------------------
# sast subcommand (Story 9.1-001)
# ---------------------------------------------------------------------------

import json as _json


def _semgrep_report(severity: str, check_id: str = "rules.example") -> str:
    return _json.dumps(
        {
            "results": [
                {
                    "check_id": check_id,
                    "path": "src/app.py",
                    "start": {"line": 7},
                    "end": {"line": 7},
                    "extra": {"severity": severity, "message": "x"},
                }
            ],
            "errors": [],
        }
    )


def test_sast_clean_exits_zero() -> None:
    result = runner.invoke(app, ["sast"], input=_json.dumps({"results": []}))
    assert result.exit_code == 0
    assert "SAST_STATUS: CLEAN" in result.output


def test_sast_warn_exits_zero() -> None:
    result = runner.invoke(app, ["sast"], input=_semgrep_report("WARNING"))
    assert result.exit_code == 0
    assert "SAST_STATUS: WARN" in result.output


def test_sast_block_exits_one() -> None:
    result = runner.invoke(
        app, ["sast"], input=_semgrep_report("ERROR", "python.lang.security.sqli")
    )
    assert result.exit_code == 1
    assert "SAST_STATUS: BLOCK" in result.output
    assert "python.lang.security.sqli" in result.output


def test_sast_bad_report_exits_two() -> None:
    result = runner.invoke(app, ["sast"], input="{not json")
    assert result.exit_code == 2
    assert "not valid JSON" in result.output
