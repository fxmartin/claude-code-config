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


def test_build_unmatched_scope_errors(tmp_path, monkeypatch) -> None:
    """R3: an unmatched non-`all` scope is an error (exit 2), not a hollow success."""
    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["build", "epic-77", "--dry-run"])
    assert result.exit_code == 2, result.output
    assert "matched no stories" in result.output.lower()


def test_build_unmatched_story_scope_errors(tmp_path, monkeypatch) -> None:
    """R3: a story id that resolves to no story exits 2."""
    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["build", "99.9-999", "--dry-run"])
    assert result.exit_code == 2, result.output


def test_build_all_empty_still_exits_zero(tmp_path, monkeypatch) -> None:
    """R3 leaves `all` alone — an empty `all` run is a benign 0-story success."""
    (tmp_path / "docs" / "stories").mkdir(parents=True)  # no epic files
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["build", "all", "--dry-run"])
    assert result.exit_code == 0, result.output


def test_build_single_story_scope_dry_run(tmp_path, monkeypatch) -> None:
    """R2: a story-id scope plans exactly that one story."""
    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["build", "99.1-002", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "1 stories" in result.output


def test_build_help_lists_flags_and_scopes() -> None:
    """R1: build's help epilog documents every flag and scope form.

    Asserts against ``_BUILD_EPILOG`` — the text wired into the command via
    ``@app.command(epilog=...)`` — rather than the rendered ``--help`` output,
    which Rich reflows differently per terminal width/environment (it renders
    fine locally but collapses on CI runners). This keeps the R1 guarantee
    deterministic while still confirming ``build --help`` runs cleanly.
    """
    from sdlc.cli import _BUILD_EPILOG

    assert runner.invoke(app, ["build", "--help"]).exit_code == 0
    for flag in (
        "--dry-run",
        "--auto",
        "--skip-coverage",
        "--skip-preflight",
        "--rebuild",
        "--sequential",
        "--limit",
        "--coverage-threshold",
        "--preflight-timeout",
    ):
        assert flag in _BUILD_EPILOG, f"{flag} missing from build epilog"
    assert "epic-NN" in _BUILD_EPILOG and "X.Y-NNN" in _BUILD_EPILOG
