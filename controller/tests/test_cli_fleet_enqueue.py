# ABOUTME: Tests for `--enqueue` targeting the fleet queue, with --host/--pool (Story 35.3-001).
# ABOUTME: Payload shape, pin/pool recorded, flag validation and `sdlc queue list` columns, vs a live in-process service.

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdlc.cli import app
from sdlc.queue import QueueStore
from sdlc.queue_client import QueueClient
from sdlc.queue_server import AccessPolicy, make_server

runner = CliRunner()
TOKEN = "s3cret-token"
ORIGIN = "http://gitlab.test/root/widgets.git"


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A live queue service in tmp, with SDLC_QUEUE_URL pointing at it."""
    store = QueueStore(tmp_path / "service-queue.db")
    store.init()
    server = make_server(store, AccessPolicy(token=TOKEN, networks=("127.0.0.0/8",)), "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SDLC_QUEUE_URL", url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)
    store.url = url  # type: ignore[attr-defined]
    yield store
    server.shutdown()
    server.server_close()
    thread.join(timeout=10)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "widgets"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", ORIGIN], check=True)
    monkeypatch.chdir(root)
    return root


def _only_job(store: QueueStore):
    jobs = store.list_jobs()
    assert len(jobs) == 1
    return jobs[0]


# --- AC1: payload shape ------------------------------------------------------


def test_build_enqueue_lands_on_fleet_with_requirements(fleet, repo) -> None:
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", "--auto"])
    assert result.exit_code == 0, result.output
    assert fleet.url in result.output
    job = _only_job(fleet)
    assert f"job {job.id}" in result.output
    assert (job.kind, job.scope, job.state) == ("build", "12.4-005", "queued")
    assert json.loads(job.options) == ["12.4-005", "--auto"]
    assert json.loads(job.requirements) == {"repo": "widgets", "origin": ORIGIN, "harness": "claude"}
    assert (job.host, job.pool) == (None, None)


def test_requirements_harness_comes_from_flag_and_repo_file(fleet, repo) -> None:
    (repo / ".sdlc-harness.yaml").write_text("harness:\n  default: codex\n  roles:\n    review: claude\n")
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", "--harness", "build=gemini"])
    assert result.exit_code == 0, result.output
    # CLI flag > repo file per role; the set of distinct harnesses the job routes to.
    assert json.loads(_only_job(fleet).requirements)["harness"] == "claude,codex,gemini"


def test_requirements_record_the_sandbox_flag(fleet, repo) -> None:
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", "--sandbox"])
    assert result.exit_code == 0, result.output
    assert json.loads(_only_job(fleet).requirements)["sandbox"] == "container"


def test_fix_enqueue_lands_on_fleet(fleet, repo) -> None:
    result = runner.invoke(app, ["fix", "123", "--enqueue"])
    assert result.exit_code == 0, result.output
    job = _only_job(fleet)
    assert (job.kind, job.scope) == ("fix", "123")
    assert json.loads(job.requirements)["origin"] == ORIGIN


def test_origin_less_repo_still_enqueues_without_origin(fleet, tmp_path, monkeypatch) -> None:
    bare = tmp_path / "plain"
    bare.mkdir()
    monkeypatch.chdir(bare)
    result = runner.invoke(app, ["fix", "5", "--enqueue"])
    assert result.exit_code == 0, result.output
    assert json.loads(_only_job(fleet).requirements) == {"repo": "plain", "harness": "claude"}


def test_local_enqueue_is_unchanged_without_a_fleet(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    result = runner.invoke(app, ["fix", "5", "--enqueue"])
    assert result.exit_code == 0, result.output
    job = _only_job(QueueStore(tmp_path / "queue.db"))
    assert job.requirements is None


# --- AC2: pin / pool recorded ------------------------------------------------


@pytest.mark.parametrize("pin", [["--host", "home-lab"], ["--host=home-lab"]])
def test_host_pin_recorded(fleet, repo, pin) -> None:
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", *pin])
    assert result.exit_code == 0, result.output
    job = _only_job(fleet)
    assert job.host == "home-lab"
    assert "home-lab" in result.output
    assert not any(a.startswith("--host") or a == "home-lab" for a in json.loads(job.options))


@pytest.mark.parametrize("pool", [["--pool", "claude-shared"], ["--pool=claude-shared"]])
def test_pool_recorded_for_fix(fleet, repo, pool) -> None:
    result = runner.invoke(app, ["fix", "123", "--enqueue", *pool])
    assert result.exit_code == 0, result.output
    job = _only_job(fleet)
    assert job.pool == "claude-shared"
    assert "claude-shared" in result.output
    assert not any(a.startswith("--pool") or a == "claude-shared" for a in json.loads(job.options))


def test_forge_host_flag_stays_a_forge_flag(fleet, repo) -> None:
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", "--host=gitlab"])
    assert result.exit_code == 0, result.output
    job = _only_job(fleet)
    assert job.host is None
    assert "--host=gitlab" in json.loads(job.options)


def test_unknown_host_lists_known_workers(fleet, repo, monkeypatch) -> None:
    monkeypatch.setattr(QueueClient, "known_workers", lambda self: {"m3max": "macbook-pro-m3-max", "lab": "home-lab"})
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", "--host", "nowhere"])
    assert result.exit_code == 2
    assert "nowhere" in result.output
    assert "m3max" in result.output and "home-lab" in result.output
    assert fleet.list_jobs() == []


def test_known_host_is_accepted(fleet, repo, monkeypatch) -> None:
    monkeypatch.setattr(QueueClient, "known_workers", lambda self: {"lab": "home-lab"})
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", "--host=home-lab"])
    assert result.exit_code == 0, result.output
    assert _only_job(fleet).host == "home-lab"


def test_empty_registry_rejects_any_pin(fleet, repo, monkeypatch) -> None:
    monkeypatch.setattr(QueueClient, "known_workers", lambda self: {})
    result = runner.invoke(app, ["fix", "1", "--enqueue", "--host", "home-lab"])
    assert result.exit_code == 2
    assert "no workers" in result.output.lower()


def test_service_without_registry_accepts_the_pin(fleet, repo) -> None:
    # The live service has no /workers route yet (35.2-001): nothing to validate against.
    result = runner.invoke(app, ["fix", "1", "--enqueue", "--host", "home-lab"])
    assert result.exit_code == 0, result.output
    assert _only_job(fleet).host == "home-lab"


# --- flag validation ---------------------------------------------------------


@pytest.mark.parametrize("verb,target", [("build", "12.4-005"), ("fix", "123")])
@pytest.mark.parametrize("flags", [["--pool", "claude-shared"], ["--pool=x"], ["--host", "home-lab"], ["--host=home-lab"]])
def test_fleet_flags_rejected_without_enqueue(fleet, repo, verb, target, flags) -> None:
    result = runner.invoke(app, [verb, target, *flags])
    assert result.exit_code == 2
    assert "--enqueue" in result.output
    assert fleet.list_jobs() == []


@pytest.mark.parametrize("flags", [["--pool"], ["--host"], ["--pool="], ["--host="]])
def test_fleet_flag_needs_a_value(fleet, repo, flags) -> None:
    result = runner.invoke(app, ["build", "12.4-005", "--enqueue", *flags])
    assert result.exit_code == 2
    assert "requires a value" in result.output
    assert fleet.list_jobs() == []


def test_pin_without_a_fleet_queue_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    result = runner.invoke(app, ["fix", "5", "--enqueue", "--pool", "claude-shared"])
    assert result.exit_code == 2
    assert "SDLC_QUEUE_URL" in result.output
    assert QueueStore(tmp_path / "queue.db").list_jobs() == []


def test_fleet_down_never_falls_back_to_local(tmp_path, monkeypatch, repo) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    result = runner.invoke(app, ["fix", "5", "--enqueue", "--host", "home-lab"])
    assert result.exit_code == 2
    assert not (tmp_path / "queue.db").exists()


# --- known_workers (client) --------------------------------------------------


class _Resp:
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self._raw = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_known_workers_maps_worker_to_host() -> None:
    body = {"workers": [{"worker": "m3max", "host": "mbp", "pools": []}, {"worker": "lab", "host": "home-lab"}]}
    client = QueueClient("http://q:1", opener=lambda req, timeout: _Resp(200, body))
    assert client.known_workers() == {"m3max": "mbp", "lab": "home-lab"}


def test_known_workers_is_none_when_the_route_is_missing(fleet) -> None:
    assert QueueClient(fleet.url, token=TOKEN).known_workers() is None


# --- AC3: list columns -------------------------------------------------------


def test_queue_list_shows_worker_host_pool_columns(fleet, repo) -> None:
    runner.invoke(app, ["fix", "123", "--enqueue", "--host", "home-lab", "--pool", "claude-shared"])
    fleet.claim_next(claimed_by="m3max", lease_seconds=60, host="home-lab", pools=["claude-shared"])
    result = runner.invoke(app, ["queue", "list"])
    assert result.exit_code == 0, result.output
    header, row = result.output.splitlines()[0:2]
    for column in ("WORKER", "HOST", "POOL"):
        assert column in header
    assert "m3max" in row and "home-lab" in row and "claude-shared" in row


def test_queue_list_dashes_empty_fleet_columns(fleet, repo) -> None:
    runner.invoke(app, ["fix", "123", "--enqueue"])
    row = runner.invoke(app, ["queue", "list"]).output.splitlines()[1]
    assert row.split().count("-") >= 3
