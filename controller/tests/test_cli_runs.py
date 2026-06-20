# ABOUTME: Tests for the `sdlc runs` command (Story 11.2-001).
# ABOUTME: Seeds a registry on disk, then asserts human + --json listings and prune.

from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from sdlc.cli import app
from sdlc.registry import Registry, RunRecord

runner = CliRunner()

DEAD_PID = 2**31 - 1


def _seed(path: Path) -> Registry:
    reg = Registry(path)
    reg.register(
        RunRecord(
            run_id="11111111-aaaa",
            repo="/repo/alpha",
            db="/repo/alpha/.sdlc-state.db",
            scope="epic-11",
            pid=os.getpid(),
            status="IN_PROGRESS",
            started_at="2026-06-20T10:00:00+00:00",
            total=4,
            completed=1,
        )
    )
    reg.register(
        RunRecord(
            run_id="22222222-bbbb",
            repo="/repo/beta",
            db="/repo/beta/.sdlc-state.db",
            scope="all",
            pid=DEAD_PID,
            status="IN_PROGRESS",
            started_at="2026-06-20T09:00:00+00:00",
        )
    )
    return reg


def test_runs_lists_entries(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    _seed(path)
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(path))

    result = runner.invoke(app, ["runs"])
    assert result.exit_code == 0
    assert "11111111" in result.stdout
    assert "epic-11" in result.stdout
    assert "/repo/alpha" in result.stdout
    # The crashed run surfaces as DEAD, not a perpetual in-progress.
    assert "DEAD" in result.stdout
    assert "1/4" in result.stdout


def test_runs_json_emits_view(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    _seed(path)
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(path))

    result = runner.invoke(app, ["runs", "--json"])
    assert result.exit_code == 0
    rows = json.loads(result.stdout)
    by_id = {r["run_id"]: r for r in rows}
    assert by_id["11111111-aaaa"]["state"] == "IN_PROGRESS"
    assert by_id["22222222-bbbb"]["state"] == "DEAD"


def test_runs_empty_registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(path))
    result = runner.invoke(app, ["runs"])
    assert result.exit_code == 0
    assert "no runs" in result.stdout.lower()


def test_runs_prune_removes_dead(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    reg = _seed(path)
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(path))

    result = runner.invoke(app, ["runs", "--prune"])
    assert result.exit_code == 0
    assert {r.run_id for r in reg.records()} == {"11111111-aaaa"}
