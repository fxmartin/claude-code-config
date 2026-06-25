# ABOUTME: Tests for the `sdlc clean` CLI wiring (Story 15.3-001).
# ABOUTME: Drives clean via CliRunner over a real git repo; dry-run vs --force.

from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from sdlc.cli import app

from test_clean import (
    _branches,
    _init_repo,
    _make_feature_branch,
    _new_run,
    _seed_story,
)

runner = CliRunner()


def _run_in(root: Path, *args: str):
    """Invoke the CLI with cwd at ``root`` so default_db_path() resolves there."""
    prev = os.getcwd()
    os.chdir(root)
    try:
        return runner.invoke(app, list(args))
    finally:
        os.chdir(prev)


def test_clean_dry_run_default_keeps_branch(tmp_path):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")
    _make_feature_branch(root, "15.3-020")
    _seed_story(db, run_id, "15.3-020", "DONE")

    result = _run_in(root, "clean")
    assert result.exit_code == 0
    assert "would remove" in result.stdout
    assert "dry-run" in result.stdout
    assert "feature/15.3-020" in _branches(root)  # nothing removed


def test_clean_force_removes_merged_branch(tmp_path):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")
    _make_feature_branch(root, "15.3-021")
    _seed_story(db, run_id, "15.3-021", "DONE")

    result = _run_in(root, "clean", "--force")
    assert result.exit_code == 0
    assert "removed" in result.stdout
    assert "feature/15.3-021" not in _branches(root)


def test_clean_json_emits_plan(tmp_path):
    root = _init_repo(tmp_path)
    db = root / ".sdlc-state.db"
    run_id = _new_run(db, status="DONE")
    _make_feature_branch(root, "15.3-022")
    _seed_story(db, run_id, "15.3-022", "DONE")

    result = _run_in(root, "clean", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["forced"] is False
    names = {c["name"] for c in payload["candidates"]}
    assert "feature/15.3-022" in names


def test_clean_empty_repo_reports_tidy(tmp_path):
    root = _init_repo(tmp_path)
    result = _run_in(root, "clean")
    assert result.exit_code == 0
    assert "nothing to do" in result.stdout
