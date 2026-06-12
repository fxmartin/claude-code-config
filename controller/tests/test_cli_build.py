# ABOUTME: Behavior tests for the wired `sdlc build` command (Story 7.3-001).
# ABOUTME: Exercises arg passthrough + dry-run without dispatching real agents.

from __future__ import annotations

from typer.testing import CliRunner

from sdlc.cli import app

runner = CliRunner()

_SAMPLE_EPIC = """# Epic 99

##### Story 99.1-001: One
**Priority**: P1
**Points**: 1
**Dependencies**: None.

##### Story 99.1-002: Two
**Priority**: P2
**Points**: 2
**Dependencies**: Story 99.1-001.
"""


def _make_project(tmp_path):
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-99-sample.md").write_text(_SAMPLE_EPIC, encoding="utf-8")
    return tmp_path


def test_build_dry_run_lists_queue(tmp_path, monkeypatch) -> None:
    """`sdlc build epic-99 --dry-run` reports the plan and dispatches nothing."""
    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["build", "epic-99", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output.lower()
    assert "2 stories" in result.output


def test_build_rejects_unknown_flag(tmp_path, monkeypatch) -> None:
    """An unknown flag exits with code 2 and an actionable message."""
    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["build", "epic-99", "--frobnicate"])
    assert result.exit_code == 2
    assert "unknown" in result.output.lower()


def test_build_limit_truncates_in_dry_run(tmp_path, monkeypatch) -> None:
    """`--limit=1` truncates the dry-run plan (dependency pull-in aside)."""
    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["build", "epic-99", "--dry-run", "--limit=1"])
    assert result.exit_code == 0, result.output
    # 99.1-001 has no deps so the plan is exactly 1 story.
    assert "1 stories" in result.output


def test_build_unknown_scope_dry_run_is_empty(tmp_path, monkeypatch) -> None:
    """An unmatched scope yields an empty plan rather than an error."""
    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["build", "epic-77", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "0 stories" in result.output
