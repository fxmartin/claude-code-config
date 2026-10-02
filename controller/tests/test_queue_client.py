# ABOUTME: Tests for QueueClient and the SDLC_QUEUE_URL switch (Story 35.1-002).
# ABOUTME: Factory selection, client vs a live in-process `serve`, fail-fast, doctor, file precedence.

from __future__ import annotations

import json
import socket
import subprocess
import threading
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

import sdlc.queue_client as qc
from sdlc.cli import app
from sdlc.queue import JobRecord, QueueError, QueueStore, default_queue_path
from sdlc.queue_client import (
    QueueClient,
    QueueConfigError,
    QueueRefused,
    QueueUnavailable,
    open_queue,
    resolve_queue_url,
)
from sdlc.queue_server import AccessPolicy, make_server

runner = CliRunner()
TOKEN = "s3cret-token"
# `shutdown()` blocks until `serve_forever` next polls its stop flag; the
# stdlib's 0.5s default idled every live-server test half a second.
_FAST_SHUTDOWN = {"poll_interval": 0.01}


class _Live:
    """A real queue service on an ephemeral loopback port (token-gated)."""

    def __init__(self, store: QueueStore) -> None:
        policy = AccessPolicy(token=TOKEN, networks=("127.0.0.0/8",))
        self.server = make_server(store, policy, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs=_FAST_SHUTDOWN, daemon=True)
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


@pytest.fixture
def client(live: _Live) -> QueueClient:
    return QueueClient(live.url, token=TOKEN)


def _dead_url() -> str:
    """A loopback URL nothing listens on (bind, learn the port, close)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


# --- URL resolution: env > file > none ---------------------------------------


def test_resolve_none_when_nothing_configured(tmp_path: Path) -> None:
    assert resolve_queue_url(cwd=tmp_path, home=tmp_path / "home") is None


def test_resolve_env_wins_over_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".sdlc-fleet.yaml").write_text("queue_url: http://file-host:8790\n")
    monkeypatch.setenv("SDLC_QUEUE_URL", "http://env-host:8790/")
    assert resolve_queue_url(cwd=tmp_path, home=home) == "http://env-host:8790"


def test_resolve_blank_env_falls_through_to_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".sdlc-queue.yaml").write_text("queue_url: http://file-host:8790\n")
    monkeypatch.setenv("SDLC_QUEUE_URL", "  ")
    assert resolve_queue_url(cwd=tmp_path, home=tmp_path / "home") == "http://file-host:8790"


def test_resolve_repo_file_wins_over_home_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".sdlc-fleet.yaml").write_text("queue_url: http://home-file:8790\n")
    (tmp_path / ".sdlc-queue.yaml").write_text("queue_url: http://repo-file:8790\n")
    assert resolve_queue_url(cwd=tmp_path, home=home) == "http://repo-file:8790"


def test_resolve_home_fleet_file_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".sdlc-fleet.yaml").write_text("queue_url: http://home-file:8790\n")
    assert resolve_queue_url(cwd=tmp_path, home=home) == "http://home-file:8790"


@pytest.mark.parametrize(
    "text",
    [
        "queue_url: [a, b]\n",
        "queue_url: ftp://host:8790\n",
        "queue_url: not-a-url\n",
        "- just\n- a list\n",
        "queue_url: [unclosed\n",
    ],
)
def test_resolve_malformed_file_is_a_config_error(tmp_path: Path, text: str) -> None:
    (tmp_path / ".sdlc-queue.yaml").write_text(text)
    with pytest.raises(QueueConfigError):
        resolve_queue_url(cwd=tmp_path, home=tmp_path / "home")


def test_resolve_file_without_the_key_means_none(tmp_path: Path) -> None:
    (tmp_path / ".sdlc-queue.yaml").write_text("other: 1\n")
    assert resolve_queue_url(cwd=tmp_path, home=tmp_path / "home") is None


# `http://[::1` makes urlsplit itself raise ValueError: it must still surface as
# the config error every caller catches, never a bare ValueError.
@pytest.mark.parametrize("url", ["home-lab:8790", "http://[::1"])
def test_resolve_malformed_env_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", url)
    with pytest.raises(QueueConfigError):
        resolve_queue_url()


# --- factory ------------------------------------------------------------------


def test_open_queue_without_url_is_the_local_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "q.db"))
    backend = open_queue()
    assert isinstance(backend, QueueStore)
    assert backend.db_path == default_queue_path() == tmp_path / "q.db"


def test_open_queue_with_url_is_a_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", "http://home-lab.tailnet.ts.net:8790")
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", "tok")
    backend = open_queue()
    assert isinstance(backend, QueueClient)
    assert backend.url == "http://home-lab.tailnet.ts.net:8790"


def test_open_queue_never_touches_the_network_or_local_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", _dead_url())
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "q.db"))
    open_queue()  # opening is lazy: no request, no queue.db
    assert not (tmp_path / "q.db").exists()


# --- client against a live service ---------------------------------------------


def test_client_add_and_list_round_trip(client: QueueClient, store: QueueStore) -> None:
    job_id = client.add_job(
        repo="/r/a",
        kind="build",
        scope="epic-1",
        priority="high",
        options_json=json.dumps(["--auto"]),
        labels=("bug",),
        host="home-lab",
        pool="claude-max",
        requirements_json=json.dumps({"repo": "x", "harness": "claude"}),
    )
    [listed] = client.list_jobs()
    assert isinstance(listed, JobRecord)
    assert listed.id == job_id and listed.priority == "high" and listed.state == "queued"
    assert json.loads(listed.options or "") == ["--auto"]
    assert listed.host == "home-lab" and listed.pool == "claude-max"
    assert json.loads(listed.requirements or "") == {"harness": "claude", "repo": "x"}
    # The service wrote its own store — the client holds no local state.
    assert store.get_job(job_id) is not None


def test_client_list_filters_by_repo_and_get_job(client: QueueClient) -> None:
    a = client.add_job(repo="/r/a", kind="build", scope="s")
    client.add_job(repo="/r/b", kind="fix", scope="9")
    assert [j.id for j in client.list_jobs("/r/a")] == [a]
    job = client.get_job(a)
    assert job is not None and job.repo == "/r/a"
    assert client.get_job(9999) is None


def test_client_add_job_invalid_kind_is_queue_error(client: QueueClient) -> None:
    with pytest.raises(QueueError, match="invalid kind"):
        client.add_job(repo="/r", kind="nope", scope="s")


def test_client_claim_renew_finish(client: QueueClient) -> None:
    job_id = client.add_job(repo="/r", kind="build", scope="s")
    claimed = client.claim_next(claimed_by="w1", lease_seconds=60)
    assert claimed is not None and claimed.id == job_id and claimed.state == "running"
    assert client.claim_next(claimed_by="w2", lease_seconds=60) is None  # nothing left
    assert client.renew_lease(job_id, claimed_by="w1", lease_seconds=120) is True
    assert client.renew_lease(job_id, claimed_by="w2", lease_seconds=120) is False
    assert client.finish_job(job_id, "done", claimed_by="w2") is False
    assert client.finish_job(job_id, "done", reason="ok", claimed_by="w1") is True
    job = client.get_job(job_id)
    assert job is not None and job.state == "done" and job.reason == "ok"


def test_client_finish_unknown_job_is_false_and_bad_state_raises(client: QueueClient) -> None:
    assert client.finish_job(4242, "done") is False
    job_id = client.add_job(repo="/r", kind="build", scope="s")
    with pytest.raises(QueueError, match="invalid terminal state"):
        client.finish_job(job_id, "running")


def test_client_release_claim_requeues_and_ignores_non_holder(client: QueueClient) -> None:
    job_id = client.add_job(repo="/r", kind="build", scope="s")
    client.claim_next(claimed_by="w1", lease_seconds=60)
    client.release_claim(job_id, claimed_by="intruder")  # silent, like the store
    assert client.get_job(job_id).state == "running"  # type: ignore[union-attr]
    client.release_claim(job_id, claimed_by="w1", reason="shutdown")
    assert client.get_job(job_id).state == "queued"  # type: ignore[union-attr]


def test_client_claim_honours_host_and_pools(client: QueueClient) -> None:
    client.add_job(repo="/r", kind="build", scope="s", host="home-lab", pool="max")
    assert client.claim_next(claimed_by="w", lease_seconds=60, host="other", pools=["max"]) is None
    assert client.claim_next(claimed_by="w", lease_seconds=60, host="home-lab", pools=[]) is None
    got = client.claim_next(claimed_by="w", lease_seconds=60, host="home-lab", pools=["max"])
    assert got is not None


def test_client_cancel_requeue_prioritise(client: QueueClient) -> None:
    job_id = client.add_job(repo="/r", kind="build", scope="s")
    client.prioritise_job(job_id, "urgent")
    assert client.get_job(job_id).priority == "urgent"  # type: ignore[union-attr]
    client.cancel_job(job_id)
    assert client.get_job(job_id).state == "cancelled"  # type: ignore[union-attr]
    client.requeue_job(job_id)
    assert client.get_job(job_id).state == "queued"  # type: ignore[union-attr]
    with pytest.raises(QueueError, match="invalid priority"):
        client.prioritise_job(job_id, "mega")


def test_client_unknown_job_and_refused_moves_are_queue_errors(client: QueueClient) -> None:
    with pytest.raises(QueueError, match="unknown job id"):
        client.cancel_job(4242)
    job_id = client.add_job(repo="/r", kind="build", scope="s")
    client.claim_next(claimed_by="w", lease_seconds=60)
    client.finish_job(job_id, "done", claimed_by="w")
    with pytest.raises(QueueError, match="cannot cancel"):
        client.cancel_job(job_id)


def test_client_cancel_of_a_running_job_flags_it_for_its_worker(client: QueueClient) -> None:
    job_id = client.add_job(repo="/r", kind="build", scope="s")
    client.claim_next(claimed_by="w", lease_seconds=60)
    client.cancel_job(job_id)
    job = client.get_job(job_id)
    assert job is not None and job.state == "running" and job.cancel_requested is True
    assert client.finish_job(job_id, "cancelled", claimed_by="w") is True
    assert client.get_job(job_id).state == "cancelled"  # type: ignore[union-attr]


def test_client_pause_lifecycle(client: QueueClient) -> None:
    assert client.dispatch_pause() is None
    until = datetime.now(timezone.utc) + timedelta(minutes=30)
    assert client.pause_dispatch(until=until, reason="rate limited", run_id="r1") is True
    pause = client.dispatch_pause()
    assert pause is not None and pause.reason == "rate limited" and pause.run_id == "r1"
    assert client.pause_dispatch(until=until, reason="again") is False  # already open
    client.add_job(repo="/r", kind="build", scope="s")
    assert client.claim_next(claimed_by="w", lease_seconds=60) is None  # held
    client.clear_pause()
    assert client.dispatch_pause() is None


def test_client_init_and_ensure_migrated_are_noops(client: QueueClient, store: QueueStore) -> None:
    client.init()
    client.ensure_migrated()
    assert store.list_jobs() == []


def test_client_health_reports_the_service_version(client: QueueClient) -> None:
    from sdlc import __version__

    assert client.health()["controller_version"] == __version__


def test_client_wrong_token_is_refused(live: _Live) -> None:
    with pytest.raises(QueueRefused) as info:
        QueueClient(live.url, token="wrong").list_jobs()
    assert live.url in str(info.value)


def test_client_without_token_is_refused(live: _Live) -> None:
    with pytest.raises(QueueRefused):
        QueueClient(live.url, token=None).list_jobs()


# --- unreachable service --------------------------------------------------------


def test_unreachable_names_the_url_and_the_error() -> None:
    url = _dead_url()
    with pytest.raises(QueueUnavailable) as info:
        QueueClient(url, token="t").list_jobs()
    message = str(info.value)
    assert url in message
    assert "refused" in message.lower() or "unreachable" in message.lower()


def test_one_retry_on_connection_error_then_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def _opener(req, timeout):
        nonlocal calls
        calls += 1
        assert timeout == 10
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    client = QueueClient("http://h:8790", token=None, opener=_opener)
    with pytest.raises(QueueUnavailable):
        client.add_job(repo="/r", kind="build", scope="s")
    assert calls == 2  # the attempt plus exactly one retry


def test_a_blip_is_absorbed_by_the_retry(live: _Live) -> None:
    real = QueueClient(live.url, token=TOKEN)
    failures = [urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))]
    original = real._opener

    def _flaky(req, timeout):
        if failures:
            raise failures.pop()
        return original(req, timeout)

    real._opener = _flaky
    assert real.list_jobs() == []


def test_a_reset_after_send_is_not_retried_for_writes() -> None:
    """The request may have landed; replaying `add` would enqueue twice."""
    calls = 0

    def _opener(req, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.URLError(ConnectionResetError(104, "reset by peer"))

    client = QueueClient("http://h:8790", token=None, opener=_opener)
    with pytest.raises(QueueUnavailable):
        client.add_job(repo="/r", kind="build", scope="s")
    assert calls == 1
    calls = 0
    with pytest.raises(QueueUnavailable):
        client.list_jobs()  # a read is safe to replay
    assert calls == 2


def test_server_error_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)  # type: ignore[arg-type]

    with pytest.raises(QueueUnavailable, match="503"):
        QueueClient("http://h:8790", opener=_opener).list_jobs()


# --- CLI: --enqueue fails fast, never falls back to local ------------------------


def _project(tmp_path: Path) -> Path:
    stories = tmp_path / "docs" / "stories"
    stories.mkdir(parents=True)
    (stories / "epic-99-sample.md").write_text(
        "# Epic 99\n\n##### Story 99.1-001: One\n**Priority**: P1\n**Points**: 1\n"
        "**Dependencies**: None.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_enqueue_with_unreachable_url_fails_fast_and_never_enqueues_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    local = tmp_path / "queue.db"
    url = _dead_url()
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(local))
    monkeypatch.setenv("SDLC_QUEUE_URL", url)

    result = runner.invoke(app, ["build", "epic-99", "--enqueue"])
    assert result.exit_code == 2, result.output
    assert url in result.output
    assert not local.exists()


def test_fix_enqueue_with_unreachable_url_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.setenv("SDLC_QUEUE_URL", _dead_url())
    result = runner.invoke(app, ["fix", "12", "--enqueue"])
    assert result.exit_code == 2, result.output
    assert not (tmp_path / "queue.db").exists()


def test_plain_build_never_opens_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_URL", _dead_url())

    def _boom():
        raise AssertionError("a plain build must not open the queue")

    monkeypatch.setattr(qc, "open_queue", _boom)
    result = runner.invoke(app, ["build", "epic-99", "--dry-run", "--skip-preflight"])
    assert "unreachable" not in result.output


def test_enqueue_with_live_url_lands_on_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: _Live, store: QueueStore
) -> None:
    _project(tmp_path)
    # A real clone always has an origin, so a real enqueue always sends it in
    # `requirements` (Story 35.2-002) — the service must take it, not 400 the job.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "http://gitlab.test/root/proj.git"],
        check=True,
    )
    monkeypatch.chdir(tmp_path)
    local = tmp_path / "queue.db"
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(local))
    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)

    result = runner.invoke(app, ["build", "epic-99", "--enqueue", "--auto"])
    assert result.exit_code == 0, result.output
    assert "queued" in result.output
    [job] = store.list_jobs()
    assert job.kind == "build" and job.scope == "epic-99"
    assert job.repo == str(tmp_path.resolve())
    assert json.loads(job.requirements or "{}")["origin"] == "http://gitlab.test/root/proj.git"
    assert not local.exists()


def test_queue_verbs_talk_to_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: _Live, store: QueueStore
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "local.db"))
    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)

    added = runner.invoke(app, ["queue", "add", "build", "epic-3", "--repo", str(tmp_path)])
    assert added.exit_code == 0, added.output
    [job] = store.list_jobs()

    listing = runner.invoke(app, ["queue", "list", "--json"])
    assert listing.exit_code == 0, listing.output
    payload = json.loads(listing.output)
    assert [j["id"] for j in payload["jobs"]] == [job.id] and payload["pause"] is None

    assert runner.invoke(app, ["queue", "prioritise", str(job.id), "urgent"]).exit_code == 0
    assert store.get_job(job.id).priority == "urgent"  # type: ignore[union-attr]
    assert runner.invoke(app, ["queue", "cancel", str(job.id)]).exit_code == 0
    assert store.get_job(job.id).state == "cancelled"  # type: ignore[union-attr]
    assert runner.invoke(app, ["queue", "requeue", str(job.id)]).exit_code == 0
    assert store.get_job(job.id).state == "queued"  # type: ignore[union-attr]
    assert not (tmp_path / "local.db").exists()


def test_queue_list_unreachable_is_one_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _dead_url()
    monkeypatch.setenv("SDLC_QUEUE_URL", url)
    result = runner.invoke(app, ["queue", "list"])
    assert result.exit_code == 2
    assert url in result.output and "Traceback" not in result.output


def test_queue_run_without_a_worker_name_refuses_a_remote_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Story 35.2-005: a fleet drain is a registered worker (its claim is matched
    # on what it registered); a bare `queue run` would claim by hand-picked id.
    monkeypatch.setenv("SDLC_QUEUE_URL", "http://h:8790")
    result = runner.invoke(app, ["queue", "run"])
    assert result.exit_code == 2
    assert "SDLC_QUEUE_URL" in result.output and "--worker" in result.output


def test_queue_run_as_a_worker_drains_the_fleet_queue_through_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: _Live
) -> None:
    from sdlc.scheduler import SchedulerResult

    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "local.db"))
    seen: dict = {}

    def fake_run_queue(backend, **kwargs):
        seen["store"] = backend
        seen.update(kwargs)
        return SchedulerResult()

    monkeypatch.setattr("sdlc.scheduler.run_queue", fake_run_queue)

    result = runner.invoke(app, ["queue", "run", "--worker", "m3max", "--pool", "claude-m3"])

    assert result.exit_code == 0, result.output
    assert isinstance(seen["store"], QueueClient) and seen["store"].url == live.url
    assert seen["identity"] == "m3max" and seen["config"].worker.name == "m3max"
    assert not (tmp_path / "local.db").exists()  # nothing touched the local store


def test_queue_run_against_an_unreachable_fleet_is_one_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _dead_url()
    monkeypatch.setenv("SDLC_QUEUE_URL", url)
    result = runner.invoke(app, ["queue", "run", "--worker", "m3max"])
    assert result.exit_code == 2
    assert url in result.output and "Traceback" not in result.output


@pytest.mark.parametrize("url", ["home-lab:8790", "http://[::1"])
def test_malformed_url_is_one_error_line(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("SDLC_QUEUE_URL", url)
    result = runner.invoke(app, ["queue", "list"])
    assert result.exit_code == 2 and "SDLC_QUEUE_URL" in result.output


# --- dashboard follows the factory ---------------------------------------------


def test_queue_view_shows_the_fleet_queue(
    monkeypatch: pytest.MonkeyPatch, live: _Live, store: QueueStore, tmp_path: Path
) -> None:
    from sdlc.dashboard import queue_view

    store.add_job(repo="/r/fleet", kind="fix", scope="7")
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "local.db"))
    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)
    view = queue_view()
    assert [j["repo"] for j in view["jobs"]] == ["/r/fleet"]
    assert view["pause"] is None


def test_queue_view_degrades_when_the_fleet_queue_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sdlc.dashboard import queue_view

    url = _dead_url()
    monkeypatch.setenv("SDLC_QUEUE_URL", url)
    view = queue_view()
    assert view["jobs"] == [] and view["pause"] is None
    assert url in view["error"]


# --- doctor --------------------------------------------------------------------


def test_doctor_fleet_finding_reports_reachability_identity_and_version(
    live: _Live,
) -> None:
    from sdlc import __version__
    from sdlc.doctor import check_fleet_queue

    finding = check_fleet_queue(QueueClient(live.url, token=TOKEN))
    assert finding.check == "fleet-queue" and finding.status == "CLEAN"
    assert live.url in finding.detail
    assert "reachable" in finding.detail
    assert "identity accepted" in finding.detail
    assert f"v{__version__}" in finding.detail


def test_doctor_fleet_finding_fails_when_unreachable() -> None:
    from sdlc.doctor import check_fleet_queue

    url = _dead_url()
    finding = check_fleet_queue(QueueClient(url, token=None))
    assert finding.status == "FAIL" and url in finding.detail and finding.remedy


def test_doctor_fleet_finding_fails_when_identity_refused(live: _Live) -> None:
    from sdlc.doctor import check_fleet_queue

    finding = check_fleet_queue(QueueClient(live.url, token="wrong"))
    assert finding.status == "FAIL"
    assert "reachable" in finding.detail and "identity" in finding.detail
    assert "SDLC_QUEUE_TOKEN" in finding.remedy


def test_run_doctor_adds_the_finding_only_when_a_url_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: _Live
) -> None:
    from sdlc.doctor import run_doctor

    kwargs = dict(
        repo_root=tmp_path,
        claude_dir=tmp_path / "claude",
        db_path=tmp_path / "ledger.db",
        queue_path=tmp_path / "queue.db",
        dep_probe=lambda name: True,
    )
    without = run_doctor(**kwargs)  # type: ignore[arg-type]
    assert "fleet-queue" not in {f.check for f in without.findings}

    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)
    with_url = run_doctor(**kwargs)  # type: ignore[arg-type]
    assert "fleet-queue" in {f.check for f in with_url.findings}


# --- workers (Story 35.2-001) -------------------------------------------------


def test_register_worker_round_trips_and_heartbeats_in_place(client: QueueClient) -> None:
    first = client.register_worker(
        "m3max",
        host="macbook-pro-m3-max",
        pools=["claude-m3", "codex-shared"],
        harnesses=["claude", "codex"],
        sandbox="podman",
        repos=["agentic-coding-monitor"],
        slots=2,
        slots_free=2,
    )
    beat = client.register_worker("m3max", host="macbook-pro-m3-max", slots=2, slots_free=1)

    assert first.pools == ["claude-m3", "codex-shared"] and first.sandbox == "podman"
    assert beat.registered_at == first.registered_at
    assert [(w.name, w.slots_free) for w in client.list_workers()] == [("m3max", 1)]


def test_register_worker_refused_input_is_a_request_error(client: QueueClient) -> None:
    with pytest.raises(qc.QueueRequestError) as caught:
        client.register_worker("m3max", host="h", slots=0)
    assert caught.value.status == 400


def test_a_registered_client_claim_is_capability_matched(client: QueueClient) -> None:
    client.register_worker("lab", host="home-lab", repos=["other"], slots=1)
    job_id = client.add_job(
        repo="/r/a", kind="build", scope="1", requirements_json='{"repo": "agentic-coding-monitor"}'
    )

    assert client.claim_next(claimed_by="lab", lease_seconds=90) is None
    client.register_worker("m3max", host="m3", repos=["agentic-coding-monitor"], slots=1)
    claimed = client.claim_next(claimed_by="m3max", lease_seconds=90)
    assert claimed is not None and claimed.id == job_id


def test_client_pool_pauses_are_independent_and_clear_by_pool(client: QueueClient) -> None:
    """Story 35.2-003: one window per pool, over the wire."""
    until = datetime.now(timezone.utc) + timedelta(minutes=30)
    assert client.pause_dispatch(until=until, reason="r", pool="claude-shared") is True
    assert client.pause_dispatch(until=until, reason="r", pool="codex-shared") is True
    assert client.pause_dispatch(until=until, reason="again", pool="claude-shared") is False
    assert client.dispatch_pause() is None  # nothing pool-less
    assert client.dispatch_pause("claude-shared").pool == "claude-shared"
    assert {p.pool for p in client.dispatch_pauses()} == {"claude-shared", "codex-shared"}

    client.clear_pause("claude-shared")  # `queue unpause --pool`, from any machine

    assert [p.pool for p in client.dispatch_pauses()] == ["codex-shared"]
    client.clear_pause()
    assert client.dispatch_pauses() == []


def test_client_claims_hold_only_the_paused_pool(client: QueueClient) -> None:
    until = datetime.now(timezone.utc) + timedelta(minutes=30)
    client.register_worker(
        "xps", host="xps", pools=["claude-shared"], harnesses=["claude"], repos=["r"],
        slots=1, slots_free=1,
    )
    client.add_job(repo="/r/r", kind="build", scope="s", pool="claude-shared")
    client.pause_dispatch(until=until, pool="claude-shared")
    assert client.claim_next(claimed_by="xps", lease_seconds=60) is None
    client.clear_pause("claude-shared")
    assert client.claim_next(claimed_by="xps", lease_seconds=60) is not None


def test_cancel_of_a_running_job_from_the_xps_signals_its_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: _Live, store: QueueStore
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "local.db"))
    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)
    job_id = store.add_job(repo=str(tmp_path), kind="build", scope="epic-3")
    store.claim_next(claimed_by="home-lab", lease_seconds=60)

    cancel = runner.invoke(app, ["queue", "cancel", str(job_id)])

    assert cancel.exit_code == 0, cancel.output
    assert "cancel requested" in cancel.output and "worker" in cancel.output
    assert store.get_job(job_id).cancel_requested is True  # type: ignore[union-attr]
    listing = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)
    assert listing["jobs"][0]["cancel_requested"] is True
    assert not (tmp_path / "local.db").exists()


def test_unpause_pool_from_the_xps_resumes_only_that_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: _Live, store: QueueStore
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "local.db"))
    monkeypatch.setenv("SDLC_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SDLC_QUEUE_URL", live.url)
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", TOKEN)
    until = datetime.now(timezone.utc) + timedelta(hours=1)
    for pool in ("claude-shared", "codex-shared"):
        store.pause_dispatch(until=until, pool=pool)

    result = runner.invoke(app, ["queue", "unpause", "--pool", "claude-shared"])

    assert result.exit_code == 0, result.output
    assert [p.pool for p in store.dispatch_pauses()] == ["codex-shared"]
    listing = json.loads(runner.invoke(app, ["queue", "list", "--json"]).output)
    assert [p["pool"] for p in listing["pauses"]] == ["codex-shared"]
