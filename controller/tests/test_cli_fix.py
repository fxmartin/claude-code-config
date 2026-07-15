# ABOUTME: Behavior tests for the wired `sdlc fix` CLI verb (issue #436, PR1).
# ABOUTME: Exercises arg parsing + exit-code translation without dispatching real agents.

from __future__ import annotations

from typer.testing import CliRunner

import sdlc.fix_issue as fx
from sdlc.cli import app
from sdlc.fix_issue import FixResult

runner = CliRunner()


def _stub_run_fix(monkeypatch, result: FixResult):
    """Patch run_fix so the CLI exercises only parsing + exit-code translation."""
    monkeypatch.setattr(fx, "run_fix", lambda opts, **kwargs: result)


def test_fix_help_lists_verb() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "fix" in result.output


def test_fix_batch_target_coming_later(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["fix", "all"])
    assert result.exit_code == 2
    assert "later release" in result.output.lower()


def test_fix_non_numeric_issue(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["fix", "frobnicate"])
    assert result.exit_code == 2
    assert "invalid issue" in result.output.lower()


def test_fix_unknown_flag(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["fix", "1", "--frobnicate"])
    assert result.exit_code == 2
    assert "unknown flag" in result.output.lower()


def test_fix_missing_issue(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["fix"])
    assert result.exit_code == 2
    assert "missing issue" in result.output.lower()


def test_fix_done_exits_zero(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_run_fix(monkeypatch, FixResult(issue=1, run_id="r", status="DONE", pr_number=100))
    result = runner.invoke(app, ["fix", "1"])
    assert result.exit_code == 0, result.output
    assert "DONE" in result.output
    assert "PR #100" in result.output


def test_fix_failed_exits_one(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_run_fix(monkeypatch, FixResult(issue=1, run_id="r", status="FAILED"))
    result = runner.invoke(app, ["fix", "1"])
    assert result.exit_code == 1
    assert "FAILED" in result.output


def test_fix_aborted_exits_one(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_run_fix(
        monkeypatch,
        FixResult(issue=1, aborted=True, abort_reason="issue is closed", status="ABORTED"),
    )
    result = runner.invoke(app, ["fix", "1"])
    assert result.exit_code == 1
    assert "aborted" in result.output.lower()
    assert "closed" in result.output.lower()


def test_fix_preflight_failure_exits_one(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_run_fix(monkeypatch, FixResult(issue=1, preflight_failed=True, status="FAILED"))
    result = runner.invoke(app, ["fix", "1"])
    assert result.exit_code == 1
    assert "PRE_FLIGHT_FAILURE" in result.output


def test_fix_investigation_blocked_exits_one(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_run_fix(
        monkeypatch,
        FixResult(
            issue=1, run_id="r", status="ABORTED",
            investigation_blocked=True, block_reason="needs a design decision",
        ),
    )
    result = runner.invoke(app, ["fix", "1"])
    assert result.exit_code == 1
    assert "blocked" in result.output.lower()
