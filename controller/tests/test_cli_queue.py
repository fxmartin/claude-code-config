# ABOUTME: Behavior tests for `sdlc queue list|add|cancel|prioritise` (Story 32.1-001).

from __future__ import annotations

import json

from typer.testing import CliRunner

from sdlc.cli import app

runner = CliRunner()


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))


def test_queue_list_empty(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    result = runner.invoke(app, ["queue", "list"])
    assert result.exit_code == 0, result.output
    assert "no jobs" in result.output.lower()


def test_queue_add_then_list(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    add = runner.invoke(
        app, ["queue", "add", "build", "epic-5", "--repo", str(tmp_path)]
    )
    assert add.exit_code == 0, add.output
    assert "queued" in add.output.lower()

    result = runner.invoke(app, ["queue", "list"])
    assert result.exit_code == 0, result.output
    assert "epic-5" in result.output
    assert "build" in result.output
    assert "queued" in result.output.lower()


def test_queue_add_rejects_bad_kind(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    result = runner.invoke(app, ["queue", "add", "frobnicate", "epic-5"])
    assert result.exit_code == 2
    assert "error" in result.output.lower()


def test_queue_list_json(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    runner.invoke(app, ["queue", "add", "fix", "42", "--repo", str(tmp_path)])
    result = runner.invoke(app, ["queue", "list", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["kind"] == "fix"
    assert rows[0]["scope"] == "42"
    assert rows[0]["state"] == "queued"


def test_queue_cancel(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    runner.invoke(app, ["queue", "add", "build", "epic-5", "--repo", str(tmp_path)])
    list_result = runner.invoke(app, ["queue", "list", "--json"])
    job_id = json.loads(list_result.output)[0]["id"]

    cancel = runner.invoke(app, ["queue", "cancel", str(job_id)])
    assert cancel.exit_code == 0, cancel.output

    after = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)
    assert after[0]["state"] == "cancelled"


def test_queue_cancel_unknown_id_exits_nonzero(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    result = runner.invoke(app, ["queue", "cancel", "999"])
    assert result.exit_code != 0
    assert "error" in result.output.lower()


def test_queue_prioritise(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    runner.invoke(app, ["queue", "add", "build", "epic-5", "--repo", str(tmp_path)])
    list_result = runner.invoke(app, ["queue", "list", "--json"])
    job_id = json.loads(list_result.output)[0]["id"]

    result = runner.invoke(app, ["queue", "prioritise", str(job_id), "urgent"])
    assert result.exit_code == 0, result.output

    after = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)
    assert after[0]["priority"] == "urgent"


def test_queue_prioritise_rejects_unknown_class(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    runner.invoke(app, ["queue", "add", "build", "epic-5", "--repo", str(tmp_path)])
    list_result = runner.invoke(app, ["queue", "list", "--json"])
    job_id = json.loads(list_result.output)[0]["id"]

    result = runner.invoke(app, ["queue", "prioritise", str(job_id), "asap"])
    assert result.exit_code == 2
