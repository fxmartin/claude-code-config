# ABOUTME: Tests for Story 35.5-008 — a queue command run from an SSH shell borrows the installed
# ABOUTME: worker agent's SDLC_QUEUE_TOKEN, so `sdlc reconcile` can update the fleet record.

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

import sdlc.doctor as doctor
from sdlc.queue_client import QueueClient, finish_fleet_run, open_queue, resolve_queue_token

from test_reconcile_fleet import TOKEN, _dead_url, _record, _row, live, store  # noqa: F401

AGENT_TOKEN = "agent-token"


def _agent_plist(tmp_path: Path, token: str | None = AGENT_TOKEN) -> Path:
    path = tmp_path / "com.fxmartin.sdlc-worker.plist"
    env = {"SDLC_QUEUE_TOKEN": token} if token is not None else {}
    path.write_bytes(plistlib.dumps({"Label": "com.fxmartin.sdlc-worker", "EnvironmentVariables": env}))
    return path


@pytest.fixture
def installed_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    plist = _agent_plist(tmp_path)
    monkeypatch.setattr(doctor, "default_worker_plist", lambda: plist)
    return plist


# --- resolve_queue_token ------------------------------------------------------------


def test_the_shells_token_wins_over_the_agents(installed_agent, monkeypatch) -> None:
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", "shell-token")
    assert resolve_queue_token() == "shell-token"


def test_an_ssh_shell_without_a_token_borrows_the_agents(installed_agent) -> None:
    assert resolve_queue_token() == AGENT_TOKEN


def test_a_systemd_unit_token_is_borrowed_too(tmp_path, monkeypatch) -> None:
    unit = tmp_path / "sdlc-worker.service"
    unit.write_text("[Service]\nEnvironment=SDLC_QUEUE_TOKEN=unit-token\n")
    monkeypatch.setattr(doctor, "default_worker_unit", lambda: unit)
    assert resolve_queue_token() == "unit-token"


def test_explicit_agent_paths_override_the_installed_defaults(tmp_path) -> None:
    assert resolve_queue_token(agent_path=_agent_plist(tmp_path, "given")) == "given"


def test_no_token_anywhere_is_none(tmp_path) -> None:
    assert resolve_queue_token() is None
    assert resolve_queue_token(agent_path=_agent_plist(tmp_path, token=None)) is None


@pytest.mark.parametrize("sentinel", ["SDLC_IN_TEST", "SDLC_FLEET_DISABLED"])
def test_a_dispatched_agent_never_reads_the_agents_token(
    installed_agent, monkeypatch, sentinel
) -> None:
    def explode(*_a, **_k):
        raise AssertionError("the agent's token must not be read")

    monkeypatch.setattr(doctor, "_installed_worker_agent", explode)
    monkeypatch.setenv(sentinel, "1")
    assert resolve_queue_token() is None


# --- finish_fleet_run ---------------------------------------------------------------


def test_reconcile_over_ssh_updates_the_fleet_record_with_the_agents_token(
    live, store, monkeypatch, tmp_path  # noqa: F811
) -> None:
    store.put_fleet_run(_record())
    monkeypatch.setenv("SDLC_QUEUE_URL", live)
    plist = _agent_plist(tmp_path, TOKEN)
    monkeypatch.setattr(doctor, "default_worker_plist", lambda: plist)

    assert finish_fleet_run("run-1", "DONE", 2) is None

    assert _row(store)["status"] == "DONE"


def test_a_wrong_shell_token_is_not_replaced_by_the_agents(
    live, store, monkeypatch, tmp_path  # noqa: F811
) -> None:
    store.put_fleet_run(_record())
    monkeypatch.setenv("SDLC_QUEUE_URL", live)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", "wrong")
    plist = _agent_plist(tmp_path, TOKEN)
    monkeypatch.setattr(doctor, "default_worker_plist", lambda: plist)

    warning = finish_fleet_run("run-1", "DONE", 2)

    assert warning is not None and "sdlc queue runs prune run-1" in warning
    assert _row(store)["status"] == "AWAITING_APPROVAL"


def test_no_token_anywhere_keeps_the_stale_record_warning(live, store, monkeypatch) -> None:  # noqa: F811
    store.put_fleet_run(_record())
    monkeypatch.setenv("SDLC_QUEUE_URL", live)

    warning = finish_fleet_run("run-1", "DONE", 2)

    assert warning is not None and "sdlc queue runs prune run-1" in warning
    assert _row(store)["status"] == "AWAITING_APPROVAL"


@pytest.mark.parametrize("sentinel", ["SDLC_IN_TEST", "SDLC_FLEET_DISABLED"])
def test_inside_a_dispatched_agent_nothing_is_pushed_and_the_agent_token_is_unread(
    live, store, monkeypatch, sentinel  # noqa: F811
) -> None:
    store.put_fleet_run(_record())
    monkeypatch.setenv("SDLC_QUEUE_URL", live)
    monkeypatch.setenv(sentinel, "1")

    def explode(*_a, **_k):
        raise AssertionError("the agent's token must not be read")

    monkeypatch.setattr(doctor, "_installed_worker_agent", explode)

    assert finish_fleet_run("run-1", "DONE", 2) is None
    assert _row(store)["status"] == "AWAITING_APPROVAL"


# --- other queue commands -----------------------------------------------------------


def test_open_queue_uses_the_same_fallback(installed_agent, monkeypatch) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", _dead_url())
    queue = open_queue()
    assert isinstance(queue, QueueClient) and queue._token == AGENT_TOKEN
