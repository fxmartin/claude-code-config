# ABOUTME: Tests for Story 35.5-004 — a dispatched agent resolves no fleet, SDLC_IN_TEST blocks the
# ABOUTME: registry push, and `DELETE /runs/<id>` + `sdlc queue runs prune` remove a bogus run record.

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

import sdlc.queue_client as qc
from sdlc.cli import app
from sdlc.queue import QueueStore
from sdlc.queue_client import (
    QueueClient,
    QueueRefused,
    open_queue,
    push_fleet_run,
    resolve_queue_url,
)
from sdlc.queue_server import AccessPolicy
from sdlc.queue_server import make_server as make_queue_server
from sdlc.registry import RunRecord

TOKEN = "s3cret-token"
_FAST_SHUTDOWN = {"poll_interval": 0.01}
runner = CliRunner()


def _record(run_id: str = "run-1") -> RunRecord:
    return RunRecord(
        run_id=run_id, repo="/r", db="/r/.db", scope="epic-99", pid=1,
        status="IN_PROGRESS", started_at="2026-10-10T22:36:00+00:00", worker="m3max",
    )


class _Live:
    def __init__(self, store: QueueStore) -> None:
        policy = AccessPolicy(token=TOKEN, networks=("127.0.0.0/8",))
        self.server = make_queue_server(store, policy, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs=_FAST_SHUTDOWN, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=10)


@pytest.fixture
def store(tmp_path: Path) -> QueueStore:
    s = QueueStore(tmp_path / "service-queue.db")
    s.init()
    return s


@pytest.fixture
def live(store: QueueStore):
    running = _Live(store)
    yield running
    running.stop()


# --- SDLC_FLEET_DISABLED: fleet resolution inside an agent ------------------------


def _user_config(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / qc.USER_CONFIG_FILENAME).write_text("queue_url: http://fleet.example:8790\n")
    return home


def test_the_user_config_file_resolves_without_the_disable_marker(tmp_path: Path) -> None:
    assert resolve_queue_url(home=_user_config(tmp_path)) == "http://fleet.example:8790"


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_the_disable_marker_beats_the_user_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("SDLC_FLEET_DISABLED", value)
    assert resolve_queue_url(home=_user_config(tmp_path)) is None


def test_the_disable_marker_beats_an_explicit_queue_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SDLC_FLEET_DISABLED", "1")
    monkeypatch.setenv("SDLC_QUEUE_URL", "http://fleet.example:8790")
    assert resolve_queue_url(home=_user_config(tmp_path)) is None


def test_the_disable_marker_beats_a_repo_level_queue_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / qc.QUEUE_CONFIG_FILENAME).write_text("queue_url: http://fleet.example:8790\n")
    monkeypatch.setenv("SDLC_FLEET_DISABLED", "1")
    assert resolve_queue_url(cwd=tmp_path, home=tmp_path / "nohome") is None


def test_a_falsy_disable_marker_leaves_the_fleet_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SDLC_FLEET_DISABLED", "0")
    assert resolve_queue_url(home=_user_config(tmp_path)) == "http://fleet.example:8790"


def test_the_disable_marker_is_read_from_another_process_environment(tmp_path: Path) -> None:
    home = _user_config(tmp_path)
    assert resolve_queue_url(home=home, environ={"SDLC_FLEET_DISABLED": "1"}) is None
    assert resolve_queue_url(home=home, environ={}) == "http://fleet.example:8790"


def test_a_disabled_fleet_opens_the_local_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", "http://fleet.example:8790")
    monkeypatch.setenv("SDLC_FLEET_DISABLED", "1")
    assert isinstance(open_queue(), QueueStore)


def test_a_disabled_fleet_never_pushes_a_run(
    live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)
    monkeypatch.setenv("SDLC_FLEET_DISABLED", "1")
    push_fleet_run(_record())
    assert store.list_fleet_runs() == []


# --- SDLC_IN_TEST blocks the registry push ---------------------------------------


def test_in_test_blocks_the_registry_push(
    live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)
    monkeypatch.setenv("SDLC_IN_TEST", "1")
    push_fleet_run(_record())
    assert store.list_fleet_runs() == []


def test_the_push_lands_when_neither_marker_is_set(
    live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)
    push_fleet_run(_record())
    assert [r["run_id"] for r in store.list_fleet_runs()] == ["run-1"]


def test_in_test_leaves_the_local_ledger_registration_untouched(
    tmp_path: Path, live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sdlc.build import _registry_register
    from sdlc.registry import Registry

    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)
    monkeypatch.setenv("SDLC_IN_TEST", "1")
    registry = Registry(tmp_path / "registry.json")
    _registry_register(registry, "run-9", "epic-9", tmp_path / "l.db", 4, repo=tmp_path)
    assert [r.run_id for r in registry.records()] == ["run-9"]
    assert store.list_fleet_runs() == []


# --- DELETE /runs/<id> ------------------------------------------------------------


def _delete(live: _Live, path: str, *, token: str | None = TOKEN) -> tuple[int, object]:
    req = urllib.request.Request(live.url + path, method="DELETE")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None)


def test_delete_run_removes_the_record(live: _Live, store: QueueStore) -> None:
    client = QueueClient(live.url, token=TOKEN)
    client.put_fleet_run(_record("ghost"))
    client.put_fleet_run(_record("keeper"))
    assert _delete(live, "/runs/ghost") == (200, {"ok": True})
    assert [r["run_id"] for r in store.list_fleet_runs()] == ["keeper"]


def test_delete_run_unknown_id_is_404(live: _Live) -> None:
    status, payload = _delete(live, "/runs/nope")
    assert status == 404
    assert "nope" in payload["error"]


def test_delete_run_requires_authentication(live: _Live, store: QueueStore) -> None:
    QueueClient(live.url, token=TOKEN).put_fleet_run(_record("ghost"))
    assert _delete(live, "/runs/ghost", token=None)[0] == 403
    assert _delete(live, "/runs/ghost", token="wrong")[0] == 403
    assert [r["run_id"] for r in store.list_fleet_runs()] == ["ghost"]


def test_delete_run_on_the_runs_collection_is_not_allowed(live: _Live) -> None:
    assert _delete(live, "/runs")[0] == 405


def test_the_client_deletes_a_run_and_reports_whether_it_existed(live: _Live) -> None:
    client = QueueClient(live.url, token=TOKEN)
    client.put_fleet_run(_record("ghost"))
    assert client.delete_fleet_run("ghost") is True
    assert client.delete_fleet_run("ghost") is False
    assert client.list_fleet_runs() == []


def test_the_client_delete_with_a_wrong_token_is_refused(live: _Live) -> None:
    with pytest.raises(QueueRefused):
        QueueClient(live.url, token="wrong").delete_fleet_run("ghost")


def test_a_run_id_with_reserved_characters_is_quoted(live: _Live, store: QueueStore) -> None:
    client = QueueClient(live.url, token=TOKEN)
    client.put_fleet_run(_record("a b/c"))
    assert client.delete_fleet_run("a b/c") is True
    assert store.list_fleet_runs() == []


def test_a_pruned_run_drops_off_the_dashboards_fleet_view(
    live: _Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sdlc.dashboard import _FleetView

    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)
    client = QueueClient(live.url, token=TOKEN)
    client.put_fleet_run(_record("ghost"))
    assert [r["run_id"] for r in _FleetView().snapshot()["runs"]] == ["ghost"]
    client.delete_fleet_run("ghost")
    assert _FleetView().snapshot()["runs"] == []


# --- sdlc queue runs prune --------------------------------------------------------


def test_prune_removes_the_record_from_the_fleet(
    live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)
    QueueClient(live.url, token=TOKEN).put_fleet_run(_record("ghost"))
    result = runner.invoke(app, ["queue", "runs", "prune", "ghost"])
    assert result.exit_code == 0, result.output
    assert "ghost" in result.output
    assert store.list_fleet_runs() == []


def test_prune_of_an_unknown_run_exits_nonzero(
    live: _Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)
    result = runner.invoke(app, ["queue", "runs", "prune", "nope"])
    assert result.exit_code == 2
    assert "nope" in result.output


def test_prune_with_a_bad_token_exits_nonzero(
    live: _Live, store: QueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", "wrong")
    QueueClient(live.url, token=TOKEN).put_fleet_run(_record("ghost"))
    result = runner.invoke(app, ["queue", "runs", "prune", "ghost"])
    assert result.exit_code != 0
    assert [r["run_id"] for r in store.list_fleet_runs()] == ["ghost"]


# --- local-mode parity ------------------------------------------------------------


def test_the_local_store_prunes_a_fleet_run_row_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    local = QueueStore(tmp_path / "queue.db")
    local.init()
    local.put_fleet_run(_record("ghost"))
    result = runner.invoke(app, ["queue", "runs", "prune", "ghost"])
    assert result.exit_code == 0, result.output
    assert local.list_fleet_runs() == []
    assert local.delete_fleet_run("ghost") is False


def test_with_no_fleet_configured_the_push_is_still_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[RunRecord] = []
    monkeypatch.setattr(QueueClient, "put_fleet_run", lambda self, record: called.append(record))
    push_fleet_run(_record())
    assert called == []
