# ABOUTME: Tests for `sdlc status --markdown` — the portable handoff export.
# ABOUTME: Story 15.1-002. Covers the renderer (active/idle/pending-approval) + CLI.

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sdlc.build import Ledger, status_snapshot
from sdlc.cli import app
from sdlc.status import format_markdown

runner = CliRunner()


# --- fixtures --------------------------------------------------------------


def _doctor_dict(status: str = "CLEAN", *, home: Path | None = None) -> dict:
    """A minimal doctor report dict, optionally embedding a home path to scrub."""
    install_detail = "all 11 managed paths present"
    if home is not None:
        install_detail = f"all 11 managed paths present under {home}/.claude"
    return {
        "status": status,
        "findings": [
            {
                "check": "install",
                "name": "Install integrity",
                "status": "CLEAN",
                "detail": install_detail,
                "remedy": "",
            },
            {
                "check": "dependency",
                "name": "SAST scanner (semgrep)",
                "status": status,
                "detail": "semgrep not found (a feature that uses it will be unavailable)",
                "remedy": "install semgrep — `uv tool install semgrep`",
            },
        ],
    }


def _seed_active(db_path: Path) -> str:
    """A two-story run: one DONE (PR #42), one IN_PROGRESS on build."""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("all", "parallel")
    ledger.set_total(run_id, 2)
    ledger.event_log(run_id, "", "info", "controller", "run started: scope=all mode=parallel")
    ledger.story_upsert(run_id, "34.5-003", "34", "Build the thing", "high", 3, "backend", "", None, "TODO")
    ledger.story_upsert(run_id, "34.6-001", "34", "Wire the API", "medium", 2, "backend", "", None, "TODO")
    ledger.stage_start(run_id, "34.5-003", "build", 1)
    ledger.stage_finish(run_id, "34.5-003", "build", 1, "DONE")
    ledger.set_story_pr(run_id, "34.5-003", 42)
    ledger.set_story_status(run_id, "34.5-003", "DONE")
    ledger.stage_start(run_id, "34.6-001", "build", 1)
    ledger.set_story_status(run_id, "34.6-001", "IN_PROGRESS")
    return run_id


def _seed_pending_approval(db_path: Path) -> str:
    """A run with one story parked AWAITING_APPROVAL (risk-gate)."""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("all", "serial")
    ledger.set_total(run_id, 1)
    ledger.story_upsert(run_id, "12.3-003", "12", "Touch a high-risk path", "high", 2, "backend", "", None, "TODO")
    ledger.stage_start(run_id, "12.3-003", "build", 1)
    ledger.stage_finish(run_id, "12.3-003", "build", 1, "DONE")
    ledger.set_story_pr(run_id, "12.3-003", 77)
    ledger.set_story_status(run_id, "12.3-003", "AWAITING_APPROVAL")
    return run_id


# --- renderer --------------------------------------------------------------


def test_format_markdown_idle_has_sections(tmp_path: Path) -> None:
    """With no run, the export still carries readiness + a clear 'no run' note."""
    snap = status_snapshot(Ledger(tmp_path / ".sdlc-state.db"))
    md = format_markdown(snap, _doctor_dict())
    assert md.startswith("# SDLC Status Report")
    assert "## Readiness" in md
    assert "## Install health" in md
    assert "No active or recent run" in md
    assert "## Pending approvals" in md


def test_format_markdown_active_run_table(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_active(db)
    snap = status_snapshot(Ledger(db))
    md = format_markdown(snap, _doctor_dict())
    # Run summary line + a story/stage table.
    assert run_id[:8] in md
    assert "IN_PROGRESS" in md
    assert "34.5-003" in md and "34.6-001" in md
    assert "#42" in md
    # A markdown table header for the stories.
    assert "| Story |" in md
    assert "| --- |" in md or "|---|" in md.replace(" ", "")


def test_format_markdown_lists_pending_approvals(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_pending_approval(db)
    snap = status_snapshot(Ledger(db))
    md = format_markdown(snap, _doctor_dict())
    assert "## Pending approvals" in md
    assert "12.3-003" in md
    assert "#77" in md
    assert "None" not in md.split("## Pending approvals", 1)[1].split("##", 1)[0]


def test_format_markdown_no_pending_approvals_says_none(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_active(db)
    snap = status_snapshot(Ledger(db))
    md = format_markdown(snap, _doctor_dict())
    section = md.split("## Pending approvals", 1)[1]
    assert "None" in section


def test_format_markdown_scrubs_home_path(tmp_path: Path) -> None:
    """Absolute home paths (which leak a username) are scrubbed to ~."""
    home = tmp_path / "Users" / "fxmartin"
    home.mkdir(parents=True)
    snap = status_snapshot(Ledger(tmp_path / ".sdlc-state.db"))
    md = format_markdown(snap, _doctor_dict(status="WARN", home=home), home=home)
    assert str(home) not in md
    assert "~/.claude" in md


def test_format_markdown_doctor_overall_status(tmp_path: Path) -> None:
    snap = status_snapshot(Ledger(tmp_path / ".sdlc-state.db"))
    md = format_markdown(snap, _doctor_dict(status="WARN"))
    assert "WARN" in md
    # A non-clean finding surfaces its remedy.
    assert "uv tool install semgrep" in md


# --- CLI -------------------------------------------------------------------


def test_cli_markdown_emits_report(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_active(db)
    result = runner.invoke(app, ["status", "--db", str(db), "--markdown"])
    assert result.exit_code == 0
    assert result.stdout.startswith("# SDLC Status Report")
    assert "## Readiness" in result.stdout


def test_cli_markdown_write_to_file(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed_active(db)
    out = tmp_path / "handoff.md"
    result = runner.invoke(
        app, ["status", "--db", str(db), "--markdown", "--write", str(out)]
    )
    assert result.exit_code == 0
    assert out.is_file()
    assert out.read_text(encoding="utf-8").startswith("# SDLC Status Report")
    # The path written is echoed for the operator.
    assert str(out) in result.stdout


def test_cli_markdown_idle(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    result = runner.invoke(app, ["status", "--db", str(db), "--markdown"])
    assert result.exit_code == 0
    assert "No active or recent run" in result.stdout


def test_cli_status_human_unchanged(tmp_path: Path) -> None:
    """The default human status output is untouched by the markdown addition."""
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_active(db)
    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0
    assert run_id[:8] in result.stdout
    assert "# SDLC Status Report" not in result.stdout


def test_cli_status_json_unchanged(tmp_path: Path) -> None:
    """The --json snapshot is untouched by the markdown addition."""
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed_active(db)
    result = runner.invoke(app, ["status", "--db", str(db), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run"]["id"] == run_id
