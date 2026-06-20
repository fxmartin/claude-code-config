# ABOUTME: Tests for `sdlc rollback` CLI wiring and the removed `init` verb (10.2-001).
# ABOUTME: Seeds a multi-story ledger, drives rollback via CliRunner, checks guards.

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from sdlc.cli import app

from test_rollback import _seed_three

runner = CliRunner()


def test_rollback_resets_stories_after_checkpoint(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_three(db)
    result = runner.invoke(app, ["rollback", "--to", "99.1-001", "--db", str(db)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "99.1-002" in out and "99.1-003" in out  # the reset stories
    assert "not yet implemented" not in out


def test_rollback_unknown_checkpoint_exits_nonzero(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_three(db)
    result = runner.invoke(app, ["rollback", "--to", "99.9-999", "--db", str(db)])
    assert result.exit_code != 0
    assert "99.9-999" in result.output


def test_rollback_refuses_merged_pr_exits_nonzero(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_three(db, third_merged=True)
    result = runner.invoke(app, ["rollback", "--to", "99.1-001", "--db", str(db)])
    assert result.exit_code != 0
    assert "merged" in result.output.lower()


def test_rollback_no_run_exits_nonzero(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(app, ["rollback", "--to", "99.1-001", "--db", str(db)])
    assert result.exit_code != 0


def test_rollback_is_not_a_stub(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_three(db)
    result = runner.invoke(app, ["rollback", "--to", "99.1-002", "--db", str(db)])
    assert "not yet implemented" not in result.output


def test_rollback_to_latest_checkpoint_is_noop(tmp_path: Path) -> None:
    """Rolling back to the last story resets nothing and exits 0 with a notice."""
    db = tmp_path / ".sdlc-state.db"
    _seed_three(db)
    result = runner.invoke(app, ["rollback", "--to", "99.1-003", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "nothing to roll back" in result.output
    assert "99.1-003" in result.output


# --- init is removed -------------------------------------------------------


def test_init_command_is_removed() -> None:
    """`init` was resolved by removal (Story 10.2-001) — invoking it is an error."""
    result = runner.invoke(app, ["init"])
    assert result.exit_code != 0  # no such command


def test_help_does_not_list_init() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # `init` no longer appears as a command in the help output.
    assert "rollback" in result.output
    assert "init" not in result.output
