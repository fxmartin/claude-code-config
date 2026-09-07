# ABOUTME: Tests for `sdlc fix --enqueue` (Story 32.1-001).
# ABOUTME: Verifies a job lands in the queue store instead of a real fix running.

from __future__ import annotations

import json

import sdlc.fix_issue as fx
from typer.testing import CliRunner

from sdlc.cli import app

runner = CliRunner()


def test_fix_enqueue_records_job_and_does_not_run(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))

    called = False

    def _boom(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("run_fix must not be called with --enqueue")

    monkeypatch.setattr(fx, "run_fix", _boom)

    result = runner.invoke(app, ["fix", "123", "--enqueue"])
    assert result.exit_code == 0, result.output
    assert not called
    assert "queued" in result.output.lower()

    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    jobs = store.list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.kind == "fix"
    assert job.scope == "123"
    assert job.repo == str(tmp_path.resolve())
    assert job.state == "queued"


def test_fix_batch_enqueue_uses_target_as_scope(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.setattr(fx, "run_fix_batch", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("run_fix_batch must not be called with --enqueue")
    ))

    result = runner.invoke(app, ["fix", "all", "--enqueue"])
    assert result.exit_code == 0, result.output

    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    job = store.list_jobs()[0]
    assert job.kind == "fix"
    assert job.scope == "all"


def test_fix_enqueue_freezes_options_json(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))

    result = runner.invoke(app, ["fix", "123", "--enqueue", "--skip-coverage"])
    assert result.exit_code == 0, result.output

    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    flags = json.loads(store.list_jobs()[0].options)
    assert "--enqueue" not in flags
    assert "--skip-coverage" in flags
