# ABOUTME: Behavior tests for `sdlc queue workers`, `queue run --worker/--pool/--host`
# ABOUTME: and the reason line in `queue list` (Story 35.2-001).

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from sdlc.cli import app
from sdlc.queue import QueueStore
from sdlc.queue_worker import WorkerProfile
from sdlc.scheduler import SchedulerResult

runner = CliRunner()


@pytest.fixture
def store(tmp_path, monkeypatch) -> QueueStore:
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.delenv("SDLC_QUEUE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    s = QueueStore(tmp_path / "queue.db")
    s.init()
    return s


def _beat(store: QueueStore, name: str, *, seconds_ago: float = 5, **overrides):
    caps = {
        "host": name,
        "pools": ["claude-m3", "codex-shared"],
        "harnesses": ["claude"],
        "slots": 2,
        "slots_free": 1,
        "now": datetime.now(timezone.utc) - timedelta(seconds=seconds_ago),
    }
    caps.update(overrides)
    return store.register_worker(name, **caps)


# --- queue workers ----------------------------------------------------------------


def test_workers_on_an_empty_registry(store) -> None:
    result = runner.invoke(app, ["queue", "workers"])
    assert result.exit_code == 0, result.output
    assert "no workers registered" in result.output.lower()


def test_workers_lists_pools_slots_heartbeat_and_status(store) -> None:
    _beat(store, "m3max", seconds_ago=5)
    _beat(store, "xps", seconds_ago=600, pools=["claude-shared"], slots=1, slots_free=1)

    result = runner.invoke(app, ["queue", "workers"])

    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.output.splitlines()[1:]}
    assert "claude-m3,codex-shared" in lines["m3max"]
    assert "1/2" in lines["m3max"] and "online" in lines["m3max"]
    assert "5s ago" in lines["m3max"] or "6s ago" in lines["m3max"]
    assert "claude-shared" in lines["xps"] and "1/1" in lines["xps"]
    assert "offline" in lines["xps"] and "10m ago" in lines["xps"]
    header = result.output.splitlines()[0].split()
    assert header == ["NAME", "HOST", "POOLS", "SLOTS", "HEARTBEAT", "STATUS"]


def test_workers_json_carries_every_capability_and_online(store) -> None:
    _beat(store, "m3max", sandbox="podman", repos=["a"])

    result = runner.invoke(app, ["queue", "workers", "--json"])

    assert result.exit_code == 0, result.output
    (worker,) = json.loads(result.output)["workers"]
    assert worker["name"] == "m3max" and worker["online"] is True
    assert worker["sandbox"] == "podman" and worker["repos"] == ["a"]
    assert worker["harnesses"] == ["claude"] and worker["slots_free"] == 1


# --- queue list: the reason --------------------------------------------------------


def test_queue_list_shows_why_a_job_is_unsatisfiable(store, tmp_path) -> None:
    _beat(store, "m3max", repos=["other"])
    store.add_job(
        repo=str(tmp_path),
        kind="build",
        scope="epic-9",
        requirements_json=json.dumps({"repo": "agentic-coding-monitor"}),
    )
    store.stamp_unsatisfiable()

    result = runner.invoke(app, ["queue", "list"])

    assert result.exit_code == 0, result.output
    assert "no eligible worker (needs repo agentic-coding-monitor)" in result.output


# --- queue run --worker --------------------------------------------------------------


def _capture(monkeypatch) -> dict:
    seen: dict = {}

    def fake_run_queue(store, **kwargs):
        seen["store"] = store
        seen.update(kwargs)
        return SchedulerResult()

    monkeypatch.setattr("sdlc.scheduler.run_queue", fake_run_queue)

    def fake_detect(name, **kwargs):
        seen["detect"] = (name, kwargs)
        return WorkerProfile(name=name, host=kwargs.get("host") or "detected-host", pools=list(kwargs["pools"]))

    monkeypatch.setattr("sdlc.queue_worker.detect_worker_profile", fake_detect)
    return seen


def test_run_with_a_worker_registers_the_profile_and_uses_its_name_as_identity(
    store, monkeypatch
) -> None:
    seen = _capture(monkeypatch)

    result = runner.invoke(
        app,
        ["queue", "run", "--worker", "m3max", "--pool", "claude-m3", "--pool", "codex-shared"],
    )

    assert result.exit_code == 0, result.output
    profile = seen["config"].worker
    assert (profile.name, profile.pools) == ("m3max", ["claude-m3", "codex-shared"])
    assert seen["identity"] == "m3max"


def test_a_worker_drain_runs_the_agent_self_check_and_a_plain_one_does_not(
    store, monkeypatch
) -> None:
    from sdlc.worker_selfcheck import run_self_check

    seen = _capture(monkeypatch)
    assert runner.invoke(app, ["queue", "run", "--worker", "m3max"]).exit_code == 0
    assert seen["self_check"] is run_self_check

    seen = _capture(monkeypatch)
    assert runner.invoke(app, ["queue", "run"]).exit_code == 0
    assert seen["self_check"] is None


def test_run_host_overrides_the_detected_host(store, monkeypatch) -> None:
    seen = _capture(monkeypatch)

    result = runner.invoke(app, ["queue", "run", "--worker", "lab", "--host", "home-lab"])

    assert result.exit_code == 0, result.output
    assert seen["config"].worker.host == "home-lab"


def test_run_without_a_worker_is_the_unchanged_single_host_drain(store, monkeypatch) -> None:
    seen = _capture(monkeypatch)

    result = runner.invoke(app, ["queue", "run"])

    assert result.exit_code == 0, result.output
    assert seen["config"].worker is None
    assert seen.get("identity") is None
    assert "detect" not in seen


@pytest.mark.parametrize("flags", [["--pool", "claude-m3"], ["--host", "home-lab"]])
def test_pool_and_host_need_a_worker_name(store, monkeypatch, flags) -> None:
    _capture(monkeypatch)

    result = runner.invoke(app, ["queue", "run", *flags])

    assert result.exit_code == 2
    assert "--worker" in result.output


def test_a_blank_worker_name_or_pool_is_an_error(store, monkeypatch) -> None:
    monkeypatch.setattr("sdlc.scheduler.run_queue", lambda *a, **k: SchedulerResult())

    blank_name = runner.invoke(app, ["queue", "run", "--worker", " "])
    blank_pool = runner.invoke(app, ["queue", "run", "--worker", "w", "--pool", " "])

    assert blank_name.exit_code == 2 and "error" in blank_name.output.lower()
    assert blank_pool.exit_code == 2 and "error" in blank_pool.output.lower()
