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


# --- `_format_age` edge cases (Story 32.1-001) ------------------------------


def test_format_age_minutes_hours_and_days() -> None:
    from datetime import datetime, timedelta, timezone

    from sdlc.cli import _format_age

    now = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    assert _format_age(now, (now - timedelta(minutes=3)).isoformat()) == "3m"
    assert _format_age(now, (now - timedelta(hours=2)).isoformat()) == "2h"
    assert _format_age(now, (now - timedelta(days=5)).isoformat()) == "5d"


def test_format_age_naive_created_at_uses_now_tzinfo() -> None:
    from datetime import datetime, timedelta, timezone

    from sdlc.cli import _format_age

    now = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    naive = (now - timedelta(minutes=10)).replace(tzinfo=None).isoformat()
    assert _format_age(now, naive) == "10m"


def test_format_age_malformed_created_at_returns_placeholder() -> None:
    from datetime import datetime, timezone

    from sdlc.cli import _format_age

    now = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    assert _format_age(now, "not-a-timestamp") == "?"


# --- review follow-ups (bugfix #32.1-002) ---------------------------------


def _job_id(output: str) -> int:
    return json.loads(output)[0]["id"]


def _park_blocked(job_id: int, tmp_path) -> None:
    """Drive a job to `blocked` the way the scheduler's version check does."""
    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store._set_state(job_id, "running")
    store.finish_job(job_id, "blocked", reason="reinstall the controller")


def test_queue_requeue_re_arms_a_blocked_job(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    runner.invoke(app, ["queue", "add", "build", "epic-5", "--repo", str(tmp_path)])
    job_id = _job_id(runner.invoke(app, ["queue", "list", "--json"]).output)
    _park_blocked(job_id, tmp_path)

    result = runner.invoke(app, ["queue", "requeue", str(job_id)])
    assert result.exit_code == 0, result.output

    after = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)
    assert after[0]["state"] == "queued"
    assert after[0]["reason"] is None


def test_queue_requeue_refuses_a_running_job(tmp_path, monkeypatch) -> None:
    from sdlc.queue import QueueStore

    _isolate(tmp_path, monkeypatch)
    runner.invoke(app, ["queue", "add", "build", "epic-5", "--repo", str(tmp_path)])
    job_id = _job_id(runner.invoke(app, ["queue", "list", "--json"]).output)
    QueueStore(tmp_path / "queue.db")._set_state(job_id, "running")

    result = runner.invoke(app, ["queue", "requeue", str(job_id)])
    assert result.exit_code == 2
    assert "error" in result.output.lower()


def test_queue_requeue_unknown_id_exits_nonzero(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    result = runner.invoke(app, ["queue", "requeue", "999"])
    assert result.exit_code != 0
    assert "error" in result.output.lower()


def test_queue_cancel_accepts_a_blocked_job(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    runner.invoke(app, ["queue", "add", "build", "epic-5", "--repo", str(tmp_path)])
    job_id = _job_id(runner.invoke(app, ["queue", "list", "--json"]).output)
    _park_blocked(job_id, tmp_path)

    result = runner.invoke(app, ["queue", "cancel", str(job_id)])
    assert result.exit_code == 0, result.output

    after = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)
    assert after[0]["state"] == "cancelled"


# --- Story 32.2-002: the parked change request in `queue list` --------------


def _park(tmp_path, monkeypatch) -> int:
    """Add a job, give it a run, and park it on PR #12."""
    from datetime import datetime, timezone

    from sdlc.queue import QueueStore, default_queue_path

    runner.invoke(app, ["queue", "add", "build", "epic-5", "--repo", str(tmp_path)])
    store = QueueStore(default_queue_path())
    job_id = store.list_jobs()[0].id
    store.claim_job(
        job_id, claimed_by="host:1", lease_seconds=90,
        now=datetime(2026, 9, 7, tzinfo=timezone.utc),
    )
    store.attach_run(job_id, "run-abc")
    store.park_job(job_id, pr_number=12, reason="awaiting approval", poll_after=None)
    return job_id


def test_queue_list_shows_the_parked_change_request(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    _park(tmp_path, monkeypatch)

    result = runner.invoke(app, ["queue", "list"])

    assert result.exit_code == 0, result.output
    assert "PR" in result.output
    assert "#12" in result.output
    assert "parked" in result.output


def test_queue_list_json_carries_the_pr_and_next_poll(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    _park(tmp_path, monkeypatch)

    result = runner.invoke(app, ["queue", "list", "--json"])

    rows = json.loads(result.output)
    assert rows[0]["state"] == "parked"
    assert rows[0]["pr_number"] == 12
    assert "poll_after" in rows[0]


def test_a_parked_job_can_be_cancelled_from_the_cli(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    job_id = _park(tmp_path, monkeypatch)

    result = runner.invoke(app, ["queue", "cancel", str(job_id)])

    assert result.exit_code == 0, result.output
    rows = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)
    assert rows[0]["state"] == "cancelled"


def test_a_parked_job_can_be_requeued_from_the_cli(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    job_id = _park(tmp_path, monkeypatch)

    result = runner.invoke(app, ["queue", "requeue", str(job_id)])

    assert result.exit_code == 0, result.output
    assert "will resume its run" in result.output
