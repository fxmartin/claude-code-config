# ABOUTME: The worker heartbeat carries `self_check: {ok, at, reason}` (Story 35.2-007) — stored,
# ABOUTME: served over HTTP, shown by `queue workers`, and a failing one has zero free slots.

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from sdlc.cli import app
from sdlc.queue import QueueError, QueueStore
from sdlc.queue_client import QueueClient
from sdlc.queue_server import AccessPolicy, make_server
from sdlc.queue_worker import WorkerProfile

runner = CliRunner()
T0 = datetime(2026, 10, 4, 9, 0, 0, tzinfo=timezone.utc)
GOOD = {"ok": True, "at": T0.isoformat(), "reason": None}
BAD = {"ok": False, "at": T0.isoformat(), "reason": "agent produced no output in 90s — a dialog"}


@pytest.fixture
def store(tmp_path) -> QueueStore:
    s = QueueStore(tmp_path / "queue.db")
    s.init()
    return s


def _register(store: QueueStore, name: str, **overrides):
    caps = {
        "host": name, "pools": ["claude-m3"], "harnesses": ["claude"],
        "repos": ["agentic-coding-monitor"], "slots": 2, "slots_free": 2, "now": T0,
    }
    caps.update(overrides)
    return store.register_worker(name, **caps)


def _job(store: QueueStore) -> int:
    return store.add_job(repo="/r/agentic-coding-monitor", kind="build", scope="epic-1")


# --- the store ------------------------------------------------------------------


def test_the_heartbeat_stores_and_returns_the_self_check(store) -> None:
    record = _register(store, "m3max", self_check=GOOD)
    assert record.self_check == GOOD
    assert store.get_worker("m3max").self_check == GOOD


def test_a_worker_that_never_reported_has_no_self_check_and_still_runs_agents(store) -> None:
    record = _register(store, "m3max")
    assert record.self_check is None
    assert record.can_run_agents


def test_each_heartbeat_replaces_the_self_check(store) -> None:
    _register(store, "m3max", self_check=BAD)
    assert not store.get_worker("m3max").can_run_agents
    _register(store, "m3max", self_check=GOOD)
    assert store.get_worker("m3max").can_run_agents


@pytest.mark.parametrize("bad", [{"at": "x"}, {"ok": "yes"}, [True], "ok"])
def test_a_malformed_self_check_is_refused(store, bad) -> None:
    with pytest.raises(QueueError, match="self_check"):
        _register(store, "m3max", self_check=bad)


def test_an_older_queue_db_gains_the_column_in_place(tmp_path) -> None:
    path = tmp_path / "old.db"
    s = QueueStore(path)
    s.init()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE workers DROP COLUMN self_check")
        conn.execute("DELETE FROM _migrations WHERE version >= 13")
    s2 = QueueStore(path)
    s2.init()
    assert _register(s2, "m3max", self_check=GOOD).self_check == GOOD


# --- the matcher ----------------------------------------------------------------


def test_a_worker_that_cannot_run_agents_claims_nothing(store) -> None:
    _register(store, "m3max", self_check=BAD)
    _job(store)
    assert store.claimable_for_worker("m3max", store.list_jobs(), now=T0) == []


def test_a_healthy_worker_is_not_deferred_to_a_broken_peer_with_more_slots(store) -> None:
    _register(store, "m3max", slots=1, slots_free=1, self_check=GOOD)
    _register(store, "xps", slots=8, slots_free=8, self_check=BAD)
    _job(store)
    assert [j.id for j in store.claimable_for_worker("m3max", store.list_jobs(), now=T0)]


# --- over HTTP ------------------------------------------------------------------


@pytest.fixture
def client(store):
    server = make_server(
        store, AccessPolicy(token="tok", networks=("127.0.0.0/8",)), "127.0.0.1", 0
    )
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield QueueClient(f"http://127.0.0.1:{server.server_address[1]}", token="tok")
    server.shutdown()
    server.server_close()
    thread.join(timeout=10)


def test_the_self_check_round_trips_through_the_service(client, store) -> None:
    record = client.register_worker("m3max", host="m3", slots=2, slots_free=2, self_check=BAD)
    assert record.self_check == BAD
    assert client.get_worker("m3max").self_check == BAD
    assert store.get_worker("m3max").self_check == BAD


def test_the_service_rejects_a_malformed_self_check(client) -> None:
    with pytest.raises(Exception, match="self_check"):
        client.register_worker("m3max", host="m3", self_check={"ok": "maybe"})


# --- the profile ----------------------------------------------------------------


def test_register_with_passes_the_self_check_through(store) -> None:
    profile = WorkerProfile(name="m3max", host="m3", repos=["agentic-coding-monitor"])
    profile.register_with(store, slots=2, slots_free=2, now=T0, self_check=GOOD)
    assert store.get_worker("m3max").self_check == GOOD


# --- `sdlc queue workers` -------------------------------------------------------


@pytest.fixture
def cli_store(tmp_path, monkeypatch) -> QueueStore:
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.delenv("SDLC_QUEUE_URL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    s = QueueStore(tmp_path / "queue.db")
    s.init()
    return s


def _fresh(**caps):
    return {"now": datetime.now(timezone.utc) - timedelta(seconds=5), **caps}


def test_workers_shows_an_online_worker_that_cannot_run_agents_as_such(cli_store) -> None:
    _register(cli_store, "m3max", **_fresh(self_check=BAD))
    _register(cli_store, "xps", **_fresh(self_check=GOOD))

    out = runner.invoke(app, ["queue", "workers"]).output

    m3 = next(line for line in out.splitlines() if line.startswith("m3max"))
    xps = next(line for line in out.splitlines() if line.startswith("xps"))
    assert "online" in m3 and "cannot run agents" in m3 and "0/2" in m3
    assert "cannot run agents" not in xps and "2/2" in xps


def test_workers_json_carries_the_self_check(cli_store) -> None:
    _register(cli_store, "m3max", **_fresh(self_check=BAD))
    payload = json.loads(runner.invoke(app, ["queue", "workers", "--json"]).output)
    assert payload["workers"][0]["self_check"] == BAD
