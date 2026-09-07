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
    rows = json.loads(result.output)["jobs"]
    assert len(rows) == 1
    assert rows[0]["kind"] == "fix"
    assert rows[0]["scope"] == "42"
    assert rows[0]["state"] == "queued"


def test_queue_cancel(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    runner.invoke(app, ["queue", "add", "build", "epic-5", "--repo", str(tmp_path)])
    list_result = runner.invoke(app, ["queue", "list", "--json"])
    job_id = json.loads(list_result.output)["jobs"][0]["id"]

    cancel = runner.invoke(app, ["queue", "cancel", str(job_id)])
    assert cancel.exit_code == 0, cancel.output

    after = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)["jobs"]
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
    job_id = json.loads(list_result.output)["jobs"][0]["id"]

    result = runner.invoke(app, ["queue", "prioritise", str(job_id), "urgent"])
    assert result.exit_code == 0, result.output

    after = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)["jobs"]
    assert after[0]["priority"] == "urgent"


def test_queue_prioritise_rejects_unknown_class(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    runner.invoke(app, ["queue", "add", "build", "epic-5", "--repo", str(tmp_path)])
    list_result = runner.invoke(app, ["queue", "list", "--json"])
    job_id = json.loads(list_result.output)["jobs"][0]["id"]

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
    return json.loads(output)["jobs"][0]["id"]


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

    after = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)["jobs"]
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

    after = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)["jobs"]
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

    rows = json.loads(result.output)["jobs"]
    assert rows[0]["state"] == "parked"
    assert rows[0]["pr_number"] == 12
    assert "poll_after" in rows[0]


def test_a_parked_job_can_be_cancelled_from_the_cli(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    job_id = _park(tmp_path, monkeypatch)

    result = runner.invoke(app, ["queue", "cancel", str(job_id)])

    assert result.exit_code == 0, result.output
    rows = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)["jobs"]
    assert rows[0]["state"] == "cancelled"


def test_a_parked_job_can_be_requeued_from_the_cli(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    job_id = _park(tmp_path, monkeypatch)

    result = runner.invoke(app, ["queue", "requeue", str(job_id)])

    assert result.exit_code == 0, result.output
    assert "will resume its run" in result.output


# --- Story 32.3-001: priority classes + budgets on the CLI ------------------


def test_queue_add_derives_the_priority_class(tmp_path, monkeypatch) -> None:
    """`fix` outranks `build` when --priority is omitted."""
    from sdlc.queue import PRIORITY_CLASSES

    _isolate(tmp_path, monkeypatch)
    runner.invoke(app, ["queue", "add", "build", "epic-5", "--repo", str(tmp_path)])
    runner.invoke(app, ["queue", "add", "fix", "42", "--repo", str(tmp_path)])

    rows = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)["jobs"]
    by_kind = {r["kind"]: r["priority"] for r in rows}
    assert PRIORITY_CLASSES.index(by_kind["fix"]) > PRIORITY_CLASSES.index(
        by_kind["build"]
    )


def test_queue_add_label_raises_a_bug_above_an_enhancement(tmp_path, monkeypatch) -> None:
    from sdlc.queue import PRIORITY_CLASSES

    _isolate(tmp_path, monkeypatch)
    runner.invoke(
        app, ["queue", "add", "fix", "1", "--repo", str(tmp_path), "--label", "bug"]
    )
    runner.invoke(
        app,
        ["queue", "add", "fix", "2", "--repo", str(tmp_path), "--label", "enhancement"],
    )

    rows = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)["jobs"]
    by_scope = {r["scope"]: r["priority"] for r in rows}
    assert PRIORITY_CLASSES.index(by_scope["1"]) > PRIORITY_CLASSES.index(
        by_scope["2"]
    )


def test_queue_list_shows_the_budget(tmp_path, monkeypatch) -> None:
    """AC4: the budget is visible in `queue list`."""
    from sdlc.queue import budget_for

    _isolate(tmp_path, monkeypatch)
    runner.invoke(
        app,
        ["queue", "add", "build", "epic-5", "--repo", str(tmp_path),
         "--priority", "low"],
    )
    result = runner.invoke(app, ["queue", "list"])
    assert result.exit_code == 0, result.output
    assert "BUDGET" in result.output
    assert budget_for("low").label() in result.output


def test_queue_prioritise_restamps_the_budget(tmp_path, monkeypatch) -> None:
    from sdlc.queue import budget_for

    _isolate(tmp_path, monkeypatch)
    runner.invoke(
        app,
        ["queue", "add", "build", "epic-5", "--repo", str(tmp_path),
         "--priority", "low"],
    )
    assert runner.invoke(app, ["queue", "prioritise", "1", "urgent"]).exit_code == 0

    rows = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)["jobs"]
    assert rows[0]["priority"] == "urgent"
    assert json.loads(rows[0]["budget"]) == budget_for("urgent").to_dict()


# --- the queue-level rate-limit pause (Story 32.2-001) --------------------


def _pause(tmp_path, **kwargs):
    """Record a host pause on the isolated queue; return its reset instant."""
    from datetime import datetime, timedelta, timezone

    from sdlc.queue import QueueStore

    store = QueueStore(tmp_path / "queue.db")
    store.init()
    now = datetime.now(timezone.utc)
    until = now + timedelta(seconds=kwargs.pop("seconds", 600))
    store.pause_dispatch(until=until, reason="rate limited (reset recorded by the run)",
                         run_id="run-abcdef12", repo=str(tmp_path), source="reset-epoch",
                         now=now, **kwargs)
    return store, until


def test_queue_list_shows_the_pause_as_the_queues_own_state(tmp_path, monkeypatch) -> None:
    """One banner for the queue, not one parked row per repo (AC4)."""
    _isolate(tmp_path, monkeypatch)
    store, until = _pause(tmp_path)
    store.add_job(repo=str(tmp_path), kind="fix", scope="42")

    result = runner.invoke(app, ["queue", "list"])
    assert result.exit_code == 0, result.output
    assert "queue paused" in result.output.lower()
    assert until.isoformat() in result.output
    assert "rate limited" in result.output.lower()
    # Still exactly one job row — the pause is queue state, not a job state.
    assert result.output.count("queued") == 1


def test_queue_list_banner_shows_even_with_no_jobs(tmp_path, monkeypatch) -> None:
    """An empty-but-paused queue must not read as "nothing going on"."""
    _isolate(tmp_path, monkeypatch)
    _pause(tmp_path)
    result = runner.invoke(app, ["queue", "list"])
    assert result.exit_code == 0, result.output
    assert "queue paused" in result.output.lower()
    assert "no jobs" in result.output.lower()


def test_queue_list_json_carries_pause_and_jobs(tmp_path, monkeypatch) -> None:
    """`--json` is `{pause, jobs}` so the pause has somewhere to live (AC4)."""
    _isolate(tmp_path, monkeypatch)
    store, until = _pause(tmp_path)
    store.add_job(repo=str(tmp_path), kind="fix", scope="42")

    result = runner.invoke(app, ["queue", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pause"]["paused_until"] == until.isoformat()
    assert payload["pause"]["run_id"] == "run-abcdef12"
    assert [j["scope"] for j in payload["jobs"]] == ["42"]


def test_queue_list_json_reports_no_pause_as_null(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    runner.invoke(app, ["queue", "add", "fix", "42", "--repo", str(tmp_path)])
    payload = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)
    assert payload["pause"] is None
    assert len(payload["jobs"]) == 1


def test_queue_list_ignores_an_elapsed_pause(tmp_path, monkeypatch) -> None:
    """A window that already reopened is not the queue's state any more."""
    _isolate(tmp_path, monkeypatch)
    _pause(tmp_path, seconds=-1)
    result = runner.invoke(app, ["queue", "list"])
    assert "queue paused" not in result.output.lower()
    assert json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)["pause"] is None
