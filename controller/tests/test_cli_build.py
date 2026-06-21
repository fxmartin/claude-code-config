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


def test_build_short_circuits_under_test_sentinel(tmp_path, monkeypatch) -> None:
    """Story 12.1-002: with SDLC_IN_TEST set, a bare `sdlc build` must NOT run
    real orchestration — it exits 0 with a clear note and dispatches no agent.

    This is the regression guard: a project test that invokes `sdlc build` bare
    during the controller's preflight no longer hangs the suite. We boobytrap the
    real preflight and dispatch so a regression fails fast instead of recursing
    into pytest-within-pytest or spawning a real agent.
    """
    from sdlc.build import IN_TEST_ENV_VAR
    import sdlc.build as build_mod

    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(IN_TEST_ENV_VAR, "1")

    def _boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("real preflight/dispatch must not run under the sentinel")

    monkeypatch.setattr(build_mod, "dispatch_agent", _boom)
    monkeypatch.setattr(build_mod, "default_preflight", _boom)

    result = runner.invoke(app, ["build", "epic-99"])
    assert result.exit_code == 0, result.output
    assert IN_TEST_ENV_VAR in result.output


def test_build_dry_run_still_works_under_sentinel(tmp_path, monkeypatch) -> None:
    """AC3: the guard blocks only real orchestration. A dry-run plan does not
    recurse, so it must still run (and report the plan) even with the sentinel
    set — which is exactly the case during the controller's own preflight."""
    from sdlc.build import IN_TEST_ENV_VAR

    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(IN_TEST_ENV_VAR, "1")
    result = runner.invoke(app, ["build", "epic-99", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output.lower()
    assert "2 stories" in result.output


def test_build_scope_error_still_works_under_sentinel(tmp_path, monkeypatch) -> None:
    """AC3: arg/scope validation runs before the guard, so a bad scope still
    errors with exit 2 under the sentinel rather than being swallowed."""
    from sdlc.build import IN_TEST_ENV_VAR

    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(IN_TEST_ENV_VAR, "1")
    result = runner.invoke(app, ["build", "epic-77", "--dry-run"])
    assert result.exit_code == 2, result.output
    assert "matched no stories" in result.output


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
        "--budget",
        "--budget-policy",
    ):
        assert flag in _BUILD_EPILOG, f"{flag} missing from build epilog"
    assert "epic-NN" in _BUILD_EPILOG and "X.Y-NNN" in _BUILD_EPILOG


def test_build_reports_budget_stop_with_notional_label(tmp_path, monkeypatch) -> None:
    """Story 14.1-001: a budget-gated stop prints the labelled-notional $ and
    exits non-zero (a paused run is not fully done)."""
    import sdlc.build as build_mod
    from sdlc.build import BuildResult

    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    def _fake_run_build(opts, **kwargs):
        return BuildResult(
            completed=1, run_id="run-x", budget_stopped=True,
            budget_policy="pause", accrued_tokens=12345, notional_cost_usd=0.62,
        )

    monkeypatch.setattr(build_mod, "run_build", _fake_run_build)
    result = runner.invoke(app, ["build", "epic-99", "--budget=10000"])

    assert result.exit_code == 1, result.output  # paused ≠ clean
    assert "budget ceiling crossed" in result.output
    assert "12345 tokens accrued" in result.output
    assert "not billed on subscription" in result.output
