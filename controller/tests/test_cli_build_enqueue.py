# ABOUTME: Tests for `sdlc build --enqueue` (Story 32.1-001).
# ABOUTME: Verifies a job lands in the queue store instead of a real build running.

from __future__ import annotations

import json

from typer.testing import CliRunner

from sdlc.cli import app

runner = CliRunner()

_SAMPLE_EPIC = """# Epic 99

##### Story 99.1-001: One
**Priority**: P1
**Points**: 1
**Dependencies**: None.
"""


def _make_project(tmp_path):
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-99-sample.md").write_text(_SAMPLE_EPIC, encoding="utf-8")
    return tmp_path


def test_build_enqueue_records_job_and_does_not_run(tmp_path, monkeypatch) -> None:
    """`--enqueue` records a queued job and never calls run_build."""
    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))

    called = False

    def _boom(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("run_build must not be called with --enqueue")

    monkeypatch.setattr("sdlc.build.run_build", _boom)

    result = runner.invoke(app, ["build", "epic-99", "--enqueue"])
    assert result.exit_code == 0, result.output
    assert not called
    assert "queued" in result.output.lower()

    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    jobs = store.list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.kind == "build"
    assert job.scope == "epic-99"
    assert job.repo == str(tmp_path.resolve())
    assert job.state == "queued"
    assert job.run_id is None


def test_build_enqueue_freezes_options_json(tmp_path, monkeypatch) -> None:
    """The enqueued job's options are the exact CLI flags, minus --enqueue itself."""
    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))

    result = runner.invoke(
        app, ["build", "epic-99", "--enqueue", "--auto", "--skip-coverage"]
    )
    assert result.exit_code == 0, result.output

    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    job = store.list_jobs()[0]
    flags = json.loads(job.options)
    assert "--enqueue" not in flags
    assert "--auto" in flags
    assert "--skip-coverage" in flags


def test_build_without_enqueue_is_unaffected(tmp_path, monkeypatch) -> None:
    """Omitting --enqueue keeps behaviour byte-identical to today (dry-run path)."""
    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))

    result = runner.invoke(app, ["build", "epic-99", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output.lower()

    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    assert store.list_jobs() == []
