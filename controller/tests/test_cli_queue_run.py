# ABOUTME: Behavior tests for `sdlc queue run` — the foreground scheduler (Story 32.1-002).
# ABOUTME: Flag plumbing, drain summary, --json, and the Ctrl-C exit code.

from __future__ import annotations

import json

from typer.testing import CliRunner

from sdlc.cli import app
from sdlc.scheduler import SchedulerResult

runner = CliRunner()


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))


def _capture(monkeypatch, result: SchedulerResult) -> dict:
    seen: dict = {}

    def fake_run_queue(store, **kwargs):
        seen["store"] = store
        seen.update(kwargs)
        return result

    monkeypatch.setattr("sdlc.scheduler.run_queue", fake_run_queue)
    return seen


def test_queue_run_on_an_empty_queue_exits_zero(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    _capture(monkeypatch, SchedulerResult())
    result = runner.invoke(app, ["queue", "run"])
    assert result.exit_code == 0, result.output
    assert "0 started" in result.output


def test_queue_run_passes_the_slot_cap_and_follow_flags(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    seen = _capture(monkeypatch, SchedulerResult(started=2, done=2))

    result = runner.invoke(
        app, ["queue", "run", "--slots", "4", "--follow", "--poll-interval", "7.5"]
    )
    assert result.exit_code == 0, result.output
    config = seen["config"]
    assert config.slots == 4
    assert config.follow is True
    assert config.poll_seconds == 7.5


def test_queue_run_rejects_a_zero_slot_cap(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    _capture(monkeypatch, SchedulerResult())
    result = runner.invoke(app, ["queue", "run", "--slots", "0"])
    assert result.exit_code != 0


def test_queue_run_json_emits_the_drain_summary(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    _capture(monkeypatch, SchedulerResult(started=3, done=2, parked=1))

    result = runner.invoke(app, ["queue", "run", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["started"] == 3
    assert payload["done"] == 2
    assert payload["parked"] == 1


def test_queue_run_exits_130_when_interrupted(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    _capture(monkeypatch, SchedulerResult(started=1, interrupted=True))

    result = runner.invoke(app, ["queue", "run"])
    assert result.exit_code == 130
    assert "interrupted" in result.output.lower()


def test_queue_run_exits_one_when_a_job_failed(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    _capture(monkeypatch, SchedulerResult(started=1, failed=1))

    result = runner.invoke(app, ["queue", "run"])
    assert result.exit_code == 1


def test_queue_run_creates_the_store_when_absent(tmp_path, monkeypatch) -> None:
    """A host that never enqueued anything can still start a drain."""
    _isolate(tmp_path, monkeypatch)
    _capture(monkeypatch, SchedulerResult())
    result = runner.invoke(app, ["queue", "run"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "queue.db").exists()
