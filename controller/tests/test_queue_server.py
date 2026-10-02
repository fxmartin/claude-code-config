# ABOUTME: Tests for `sdlc queue serve` (Story 35.1-001) — every route against a real
# ABOUTME: QueueStore in tmp, the 403 identity gate, bind rules, and concurrent claims.

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from sdlc.cli import app
from sdlc.queue import QueueStore
from sdlc.queue_server import (
    TAILNET_NETWORKS,
    AccessPolicy,
    BindError,
    WhoisUnavailable,
    make_server,
    parse_bind,
    serve,
    tailscale_whois,
)

LOOPBACK = ("127.0.0.0/8",)


class _Running:
    """A queue server on an ephemeral loopback port, torn down by the fixture."""

    def __init__(self, store: QueueStore, policy: AccessPolicy) -> None:
        self.server = make_server(store, policy, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def call(self, method: str, path: str, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, (json.loads(raw) if raw else None)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=10)


@pytest.fixture
def store(tmp_path) -> QueueStore:
    s = QueueStore(tmp_path / "queue.db")
    s.init()
    return s


def _policy(**kwargs) -> AccessPolicy:
    kwargs.setdefault("allow", ["fx@example.com"])
    kwargs.setdefault("whois", lambda ip: "fx@example.com")
    kwargs.setdefault("networks", LOOPBACK)
    return AccessPolicy(**kwargs)


@pytest.fixture
def api(store):
    running = _Running(store, _policy())
    yield running
    running.stop()


def _add(api, **overrides):
    body = {"repo": "/r/a", "kind": "build", "scope": "epic-1"}
    body.update(overrides)
    status, job = api.call("POST", "/jobs", body)
    assert status == 201, job
    return job


# --- routes ------------------------------------------------------------------


def test_post_jobs_creates_job_and_returns_the_record_json(api, store) -> None:
    job = _add(
        api,
        priority="high",
        options=["--auto"],
        host="home-lab",
        pool="claude-max",
        requirements={"repo": "claude-code-config", "harness": "claude"},
    )
    stored = store.get_job(job["id"])
    assert stored is not None
    assert job == stored.to_dict()
    assert job["state"] == "queued"
    assert job["priority"] == "high"
    assert json.loads(job["options"]) == ["--auto"]
    assert job["host"] == "home-lab"
    assert job["pool"] == "claude-max"
    assert json.loads(job["requirements"]) == {
        "repo": "claude-code-config",
        "harness": "claude",
    }


def test_post_jobs_rejects_bad_input_with_400(api) -> None:
    assert api.call("POST", "/jobs", {"repo": "/r", "kind": "nope", "scope": "x"})[0] == 400
    assert api.call("POST", "/jobs", {"kind": "build", "scope": "x"})[0] == 400
    assert api.call(
        "POST", "/jobs", {"repo": "/r", "kind": "build", "scope": "x", "requirements": {"gpu": "y"}}
    )[0] == 400


def test_get_jobs_matches_queue_list_json_shape(api, store) -> None:
    _add(api, scope="epic-1")
    _add(api, scope="epic-2", priority="urgent")
    status, payload = api.call("GET", "/jobs")
    assert status == 200
    assert payload == {
        "pause": None,
        "jobs": [r.to_dict() for r in store.list_jobs()],
    }
    assert [j["scope"] for j in payload["jobs"]] == ["epic-2", "epic-1"]


def test_get_jobs_filters_by_repo(api) -> None:
    _add(api, repo="/r/a")
    _add(api, repo="/r/b")
    status, payload = api.call("GET", "/jobs?repo=/r/b")
    assert status == 200
    assert [j["repo"] for j in payload["jobs"]] == ["/r/b"]


def test_claim_renew_release_finish_lifecycle(api, store) -> None:
    job = _add(api)
    status, claimed = api.call("POST", "/jobs/claim", {"worker": "m3max", "lease_seconds": 60})
    assert status == 200
    assert claimed["id"] == job["id"]
    assert claimed["state"] == "running"
    assert claimed["claimed_by"] == "m3max"
    assert claimed["worker"] == "m3max"

    status, renewed = api.call(
        "POST", f"/jobs/{job['id']}/renew", {"worker": "m3max", "lease_seconds": 600}
    )
    assert status == 200
    assert renewed["lease_until"] > claimed["lease_until"]

    status, released = api.call(
        "POST", f"/jobs/{job['id']}/release", {"worker": "m3max", "reason": "shutdown"}
    )
    assert status == 200
    assert released["state"] == "queued"
    assert released["claimed_by"] is None
    assert released["worker"] is None
    assert released["reason"] == "shutdown"

    api.call("POST", "/jobs/claim", {"worker": "xps"})
    status, finished = api.call(
        "POST", f"/jobs/{job['id']}/finish", {"state": "done", "worker": "xps"}
    )
    assert status == 200
    assert finished["state"] == "done"
    assert finished["worker"] is None


def test_claim_with_nothing_queued_is_204(api) -> None:
    status, body = api.call("POST", "/jobs/claim", {"worker": "w"})
    assert status == 204
    assert body is None


def test_claim_requires_a_worker(api) -> None:
    _add(api)
    assert api.call("POST", "/jobs/claim", {})[0] == 400


def test_claim_respects_host_pin_and_pool(api) -> None:
    pinned = _add(api, repo="/r/a", host="home-lab")
    pooled = _add(api, repo="/r/b", pool="codex-shared")
    status, got = api.call("POST", "/jobs/claim", {"worker": "w", "host": "xps"})
    assert status == 204  # the pinned job is not ours; the pooled one is not our pool

    status, got = api.call("POST", "/jobs/claim", {"worker": "w", "host": "home-lab"})
    assert status == 200 and got["id"] == pinned["id"]

    status, got = api.call("POST", "/jobs/claim", {"worker": "w", "pools": ["codex-shared"]})
    assert status == 200 and got["id"] == pooled["id"]


def test_claim_is_held_while_the_queue_is_paused(api) -> None:
    _add(api)
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert api.call("POST", "/pause", {"until": until, "reason": "limit"})[0] == 200
    assert api.call("POST", "/jobs/claim", {"worker": "w"})[0] == 204
    assert api.call("DELETE", "/pause")[0] == 200
    assert api.call("POST", "/jobs/claim", {"worker": "w"})[0] == 200


def test_renew_and_release_by_a_non_holder_is_409(api) -> None:
    job = _add(api)
    api.call("POST", "/jobs/claim", {"worker": "m3max"})
    assert api.call("POST", f"/jobs/{job['id']}/renew", {"worker": "xps"})[0] == 409
    assert api.call("POST", f"/jobs/{job['id']}/release", {"worker": "xps"})[0] == 409
    assert api.call(
        "POST", f"/jobs/{job['id']}/finish", {"state": "done", "worker": "xps"}
    )[0] == 409


def test_finish_rejects_non_terminal_state(api) -> None:
    job = _add(api)
    api.call("POST", "/jobs/claim", {"worker": "w"})
    assert api.call("POST", f"/jobs/{job['id']}/finish", {"state": "queued"})[0] == 400


def test_cancel_requeue_prioritise(api) -> None:
    job = _add(api)
    status, cancelled = api.call("POST", f"/jobs/{job['id']}/cancel")
    assert status == 200 and cancelled["state"] == "cancelled"

    status, requeued = api.call("POST", f"/jobs/{job['id']}/requeue")
    assert status == 200 and requeued["state"] == "queued"

    status, bumped = api.call("POST", f"/jobs/{job['id']}/prioritise", {"priority": "urgent"})
    assert status == 200 and bumped["priority"] == "urgent"
    assert json.loads(bumped["budget"])["max_fix_rounds"] == 8


def test_state_refusals_are_409_and_bad_values_400(api) -> None:
    job = _add(api)
    assert api.call("POST", f"/jobs/{job['id']}/requeue")[0] == 409  # already queued
    api.call("POST", "/jobs/claim", {"worker": "w"})
    assert api.call("POST", f"/jobs/{job['id']}/cancel")[0] == 409  # running
    assert api.call("POST", f"/jobs/{job['id']}/prioritise", {"priority": "asap"})[0] == 400


def test_unknown_job_is_404(api) -> None:
    for verb in ("cancel", "requeue", "prioritise", "renew", "release", "finish"):
        status, _ = api.call("POST", f"/jobs/999/{verb}", {"worker": "w", "priority": "low", "state": "done"})
        assert status == 404, verb


def test_pause_and_resume_routes(api, store) -> None:
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    status, opened = api.call(
        "POST", "/pause", {"until": until, "reason": "max window", "run_id": "r1"}
    )
    assert status == 200
    assert opened["opened"] is True
    assert opened["pause"]["reason"] == "max window"
    assert store.dispatch_pause() is not None

    # A second signal inside the window is not a new announcement.
    status, again = api.call("POST", "/pause", {"until": until})
    assert status == 200 and again["opened"] is False

    status, listing = api.call("GET", "/jobs")
    assert listing["pause"]["reason"] == "max window"

    status, cleared = api.call("DELETE", "/pause")
    assert status == 200 and cleared == {"pause": None}
    assert store.dispatch_pause() is None


def test_pause_rejects_a_bad_timestamp(api) -> None:
    assert api.call("POST", "/pause", {"until": "tomorrow-ish"})[0] == 400
    assert api.call("POST", "/pause", {})[0] == 400


def test_unknown_route_404_malformed_and_oversized_bodies(api) -> None:
    assert api.call("GET", "/nope")[0] == 404
    req = urllib.request.Request(api.url + "/jobs", data=b"{not json", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 400
    big = urllib.request.Request(api.url + "/jobs", data=b"x" * (2 * 1024 * 1024), method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(big, timeout=10)
    assert exc.value.code == 413


# --- concurrency ---------------------------------------------------------------


def test_concurrent_claims_give_each_job_exactly_one_winner(api, store) -> None:
    ids = {_add(api, repo=f"/r/{n}")["id"] for n in range(6)}

    def claim(worker: str):
        return api.call("POST", "/jobs/claim", {"worker": worker})

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(claim, [f"w{n}" for n in range(12)]))

    won = [body["id"] for status, body in results if status == 200]
    assert sorted(won) == sorted(ids)  # every job claimed, none twice
    assert sum(1 for status, _ in results if status == 204) == 6
    assert {j.state for j in store.list_jobs()} == {"running"}


# --- identity / 403 -----------------------------------------------------------


def test_peer_outside_the_tailnet_is_403_and_logged(store, caplog) -> None:
    # Default networks are the tailnet's; a loopback test client is not in them.
    running = _Running(store, AccessPolicy(allow=["fx@example.com"], whois=lambda ip: "fx@example.com"))
    try:
        with caplog.at_level("WARNING", logger="sdlc.queue_server"):
            status, _ = running.call("GET", "/jobs")
        assert status == 403
        assert "127.0.0.1" in caplog.text and "refused" in caplog.text
    finally:
        running.stop()


def test_unknown_tailscale_identity_is_403(store) -> None:
    running = _Running(store, _policy(whois=lambda ip: "stranger@example.com"))
    try:
        assert running.call("GET", "/jobs")[0] == 403
        assert running.call("POST", "/jobs", {"repo": "/r", "kind": "build", "scope": "x"})[0] == 403
        assert store.list_jobs() == []
    finally:
        running.stop()


def test_whois_failure_fails_closed(store) -> None:
    def broken(ip):
        raise WhoisUnavailable("tailscale not installed")

    running = _Running(store, _policy(whois=broken))
    try:
        assert running.call("GET", "/jobs")[0] == 403
    finally:
        running.stop()


def test_token_is_the_fallback_when_whois_is_unavailable(store) -> None:
    def broken(ip):
        raise WhoisUnavailable("tailscale not installed")

    running = _Running(store, _policy(whois=broken, allow=[], token="s3cret"))
    try:
        assert running.call("GET", "/jobs")[0] == 403
        assert running.call("GET", "/jobs", headers={"Authorization": "Bearer wrong"})[0] == 403
        assert running.call("GET", "/jobs", headers={"Authorization": "Bearer s3cret"})[0] == 200
    finally:
        running.stop()


def test_token_does_not_bypass_the_network_gate(store) -> None:
    running = _Running(store, AccessPolicy(allow=[], token="s3cret", whois=lambda ip: None))
    try:
        assert running.call("GET", "/jobs", headers={"Authorization": "Bearer s3cret"})[0] == 403
    finally:
        running.stop()


def test_whois_result_is_cached_per_ip_for_a_minute() -> None:
    calls: list[str] = []
    clock = [1000.0]

    def whois(ip):
        calls.append(ip)
        return "fx@example.com"

    policy = AccessPolicy(
        allow=["fx@example.com"], whois=whois, networks=LOOPBACK, clock=lambda: clock[0]
    )
    assert policy.authorize("127.0.0.1", None).allowed
    assert policy.authorize("127.0.0.1", None).allowed
    assert calls == ["127.0.0.1"]
    clock[0] += 61
    assert policy.authorize("127.0.0.1", None).allowed
    assert calls == ["127.0.0.1", "127.0.0.1"]


def test_ipv4_mapped_ipv6_peer_is_judged_as_its_ipv4() -> None:
    policy = AccessPolicy(
        allow=["fx@example.com"], whois=lambda ip: "fx@example.com", networks=("100.64.0.0/10",)
    )
    assert policy.authorize("::ffff:100.101.102.103", None).allowed
    assert not policy.authorize("::ffff:8.8.8.8", None).allowed


def test_default_networks_are_the_tailnet_ranges() -> None:
    policy = AccessPolicy(allow=["a"], whois=lambda ip: "a")
    assert TAILNET_NETWORKS == ("100.64.0.0/10", "fd7a:115c:a1e0::/48")
    assert policy.authorize("100.100.1.1", None).allowed
    assert policy.authorize("fd7a:115c:a1e0::1", None).allowed
    assert not policy.authorize("192.168.1.5", None).allowed
    assert not policy.authorize("127.0.0.1", None).allowed


def test_policy_without_allowlist_or_token_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="allow"):
        AccessPolicy(allow=[], token=None)


def test_non_ascii_bearer_is_refused_not_raised() -> None:
    # hmac.compare_digest raises TypeError on non-ASCII str; the gate must answer.
    policy = AccessPolicy(token="s3cret", networks=LOOPBACK)
    assert not policy.authorize("127.0.0.1", "sécret").allowed
    assert policy.authorize("127.0.0.1", "s3cret").allowed


def test_non_ascii_token_is_refused_at_construction() -> None:
    # Header values arrive latin-1 decoded, so a non-ASCII secret could never match.
    with pytest.raises(ValueError, match="ASCII"):
        AccessPolicy(token="sécret", networks=LOOPBACK)


def test_non_ascii_authorization_header_is_403_and_logged(store, caplog) -> None:
    import http.client

    running = _Running(store, AccessPolicy(token="s3cret", networks=LOOPBACK))
    host, port = running.url.removeprefix("http://").split(":")
    try:
        conn = http.client.HTTPConnection(host, int(port), timeout=10)
        with caplog.at_level("WARNING", logger="sdlc.queue_server"):
            conn.putrequest("GET", "/jobs")
            conn.putheader("Authorization", "Bearer sécret".encode())
            conn.endheaders()
            assert conn.getresponse().status == 403
        conn.close()
        assert "refused" in caplog.text
    finally:
        running.stop()


def test_a_crashing_authorizer_fails_closed_with_403(store, caplog) -> None:
    def broken(ip):
        raise RuntimeError("whois exploded")

    running = _Running(store, _policy(whois=broken))
    try:
        with caplog.at_level("WARNING", logger="sdlc.queue_server"):
            assert running.call("GET", "/jobs")[0] == 403
        assert "refused" in caplog.text and "RuntimeError" in caplog.text
    finally:
        running.stop()


@pytest.mark.parametrize(
    "partial",
    [
        b"GET /jobs HTTP/1.1\r\nHost: x\r\n",  # headers never finish
        b"POST /jobs HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\n{}",  # body never finishes
    ],
)
def test_a_stalled_request_is_timed_out_not_held_forever(store, monkeypatch, partial) -> None:
    import socket

    from sdlc import queue_server

    assert queue_server._Handler.timeout is not None  # the shipped default bounds a handler
    monkeypatch.setattr(queue_server._Handler, "timeout", 0.2)
    running = _Running(store, _policy())
    host, port = running.url.removeprefix("http://").split(":")
    try:
        with socket.create_connection((host, int(port)), timeout=10) as sock:
            sock.sendall(partial)
            # The server hangs up on the stalled client: EOF, and no 500 answer.
            assert sock.recv(4096) == b""
    finally:
        running.stop()


# --- bind rules ----------------------------------------------------------------


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("100.101.102.103:8790", ("100.101.102.103", 8790)),
        ("[fd7a:115c:a1e0::1]:8790", ("fd7a:115c:a1e0::1", 8790)),
        ("127.0.0.1:0", ("127.0.0.1", 0)),
    ],
)
def test_parse_bind_accepts_tailnet_and_loopback(spec, expected) -> None:
    assert parse_bind(spec) == expected


@pytest.mark.parametrize(
    "spec",
    ["0.0.0.0:8790", "[::]:8790", "192.168.1.5:8790", "8.8.8.8:8790", "home-lab:8790",
     "100.101.102.103", "100.101.102.103:notaport", ":8790", "100.101.102.103:99999"],
)
def test_parse_bind_refuses_everything_else(spec) -> None:
    with pytest.raises(BindError):
        parse_bind(spec)


def test_make_server_itself_refuses_a_wildcard_bind(store) -> None:
    with pytest.raises(BindError):
        make_server(store, _policy(), "0.0.0.0", 0)


# --- migration -----------------------------------------------------------------


def test_fleet_migration_adds_columns_to_an_old_queue_and_is_idempotent(tmp_path) -> None:
    db = tmp_path / "queue.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, kind TEXT NOT NULL,
            scope TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal',
            state TEXT NOT NULL DEFAULT 'queued', claimed_by TEXT, lease_until TIMESTAMP,
            run_id TEXT, options TEXT, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, reason TEXT
        );
        INSERT INTO jobs(repo, kind, scope) VALUES ('/old', 'build', 'epic-9');
        """
    )
    conn.commit()
    conn.close()

    store = QueueStore(db)
    store.ensure_migrated()
    store.ensure_migrated()
    store.init()

    conn = sqlite3.connect(db)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        assert {"host", "pool", "requirements", "worker"} <= cols
        applied = [r[0] for r in conn.execute("SELECT name FROM _migrations")]
        assert applied.count("fleet_job_columns") == 1
    finally:
        conn.close()
    (old,) = store.list_jobs()
    assert (old.host, old.pool, old.requirements, old.worker) == (None, None, None, None)


# --- CLI -----------------------------------------------------------------------


def test_serve_help_states_the_api_bind_rules_and_identity_model() -> None:
    result = CliRunner().invoke(app, ["queue", "serve", "--help"])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    for needle in ("POST /jobs/claim", "DELETE /pause", "never binds 0.0.0.0",
                   "tailscale whois", "SDLC_QUEUE_TOKEN", "--allow", "403"):
        assert needle in text, needle


def test_serve_refuses_a_wildcard_bind_before_starting(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    result = CliRunner().invoke(app, ["queue", "serve", "--bind", "0.0.0.0:8790", "--allow", "a@b"])
    assert result.exit_code == 2
    assert "0.0.0.0" in result.output


def test_serve_refuses_to_start_without_an_allowlist_or_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.delenv("SDLC_QUEUE_TOKEN", raising=False)
    monkeypatch.delenv("SDLC_QUEUE_ALLOW", raising=False)
    result = CliRunner().invoke(app, ["queue", "serve", "--bind", "127.0.0.1:0"])
    assert result.exit_code == 2
    assert "allow" in result.output.lower()


def _capture_serve(monkeypatch) -> dict:
    captured: dict = {}

    def fake_serve(store, policy, host, port) -> None:
        captured.update(policy=policy, host=host, port=port)

    monkeypatch.setattr("sdlc.queue_server.serve", fake_serve)
    return captured


def test_serve_on_loopback_admits_loopback_peers_by_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", "s3cret")
    monkeypatch.delenv("SDLC_QUEUE_ALLOW", raising=False)
    captured = _capture_serve(monkeypatch)
    result = CliRunner().invoke(app, ["queue", "serve", "--bind", "127.0.0.1:0"])
    assert result.exit_code == 0, result.output
    policy = captured["policy"]
    assert policy.authorize("127.0.0.1", "s3cret").allowed
    assert not policy.authorize("127.0.0.1", "wrong").allowed
    assert not policy.authorize("100.64.0.1", "s3cret").allowed


def test_serve_on_a_tailnet_address_admits_only_tailnet_peers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", "s3cret")
    captured = _capture_serve(monkeypatch)
    result = CliRunner().invoke(app, ["queue", "serve", "--bind", "100.101.102.103:8790"])
    assert result.exit_code == 0, result.output
    policy = captured["policy"]
    assert policy.authorize("100.64.0.1", "s3cret").allowed
    assert not policy.authorize("127.0.0.1", "s3cret").allowed


def test_serve_reports_a_bind_failure_without_a_traceback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SDLC_QUEUE_PATH", str(tmp_path / "queue.db"))
    monkeypatch.setenv("SDLC_QUEUE_TOKEN", "s3cret")

    def in_use(*_a, **_k) -> None:
        raise OSError(98, "Address already in use")

    monkeypatch.setattr("sdlc.queue_server.serve", in_use)
    result = CliRunner().invoke(app, ["queue", "serve", "--bind", "127.0.0.1:8790"])
    assert result.exit_code == 2
    assert "Address already in use" in result.output
    assert "127.0.0.1:8790" in result.output
    assert not isinstance(result.exception, OSError)


# --- coverage gaps: whois, serve(), HTTP edge cases ------------------------------


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr="") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _fake_run(monkeypatch, result) -> None:
    def run(*_a, **_k):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("sdlc.queue_server.subprocess.run", run)


def test_tailscale_whois_returns_the_login_name(monkeypatch) -> None:
    payload = json.dumps({"UserProfile": {"LoginName": "fx@example.com"}})
    _fake_run(monkeypatch, _Proc(stdout=payload))
    assert tailscale_whois("100.64.0.1") == "fx@example.com"


@pytest.mark.parametrize("stdout", ["not json", "{}", '{"UserProfile": null}', '{"UserProfile": {"LoginName": ""}}'])
def test_tailscale_whois_without_a_login_is_none(monkeypatch, stdout) -> None:
    _fake_run(monkeypatch, _Proc(stdout=stdout))
    assert tailscale_whois("100.64.0.1") is None


@pytest.mark.parametrize("failure", [OSError("no tailscale"), _Proc(returncode=1, stderr="down")])
def test_tailscale_whois_cli_failure_is_unavailable(monkeypatch, failure) -> None:
    _fake_run(monkeypatch, failure)
    with pytest.raises(WhoisUnavailable):
        tailscale_whois("100.64.0.1")


def test_policy_refuses_unparseable_peer_and_missing_identity(store) -> None:
    assert not _policy().authorize("not-an-ip", None).allowed
    assert not _policy(whois=lambda ip: None).authorize("127.0.0.1", None).allowed
    token_only = AccessPolicy(token="s3cret", networks=LOOPBACK)
    assert not token_only.authorize("127.0.0.1", "wrong").allowed
    assert token_only.authorize("127.0.0.1", "s3cret").allowed


def test_bearer_token_header_admits_through_the_http_layer(store) -> None:
    running = _Running(store, AccessPolicy(token="s3cret", networks=LOOPBACK))
    try:
        assert running.call("GET", "/jobs")[0] == 403
        assert running.call("GET", "/jobs", headers={"Authorization": "Bearer s3cret"})[0] == 200
    finally:
        running.stop()


def test_wrong_method_is_405_and_bad_job_id_is_404(api) -> None:
    assert api.call("GET", "/jobs/1/cancel")[0] == 405
    assert api.call("GET", "/pause")[0] == 405
    assert api.call("POST", "/jobs/abc/cancel")[0] == 404


@pytest.mark.parametrize(
    "body",
    [
        {"repo": "r", "kind": "k", "scope": "s", "requirements": {"nope": "x"}},
        {"repo": "r", "kind": "k", "scope": "s", "options": "notalist"},
        {"repo": "r", "kind": "k", "scope": "s", "host": ""},
    ],
)
def test_add_job_validates_optional_fields(api, body) -> None:
    assert api.call("POST", "/jobs", body)[0] == 400


@pytest.mark.parametrize("lease", [0, -5, True, "60", 10**9])
def test_claim_rejects_bad_lease(api, lease) -> None:
    assert api.call("POST", "/jobs/claim", {"worker": "w", "lease_seconds": lease})[0] == 400


def test_bad_content_length_is_400(api) -> None:
    import http.client

    host, port = api.url.removeprefix("http://").split(":")
    for value in ("abc", "-1"):
        conn = http.client.HTTPConnection(host, int(port), timeout=10)
        conn.putrequest("POST", "/jobs")
        conn.putheader("Content-Length", value)
        conn.endheaders()
        assert conn.getresponse().status == 400
        conn.close()


def test_json_body_that_is_not_an_object_is_400(api) -> None:
    req = urllib.request.Request(api.url + "/jobs", data=b"[1]", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 400


def test_handler_bug_is_500_and_locked_store_is_503(api, store, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("bug")

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "list_jobs", boom)
    assert api.call("GET", "/jobs")[0] == 500
    monkeypatch.setattr(store, "list_jobs", locked)
    assert api.call("GET", "/jobs")[0] == 503


def test_serve_runs_until_interrupted_and_closes_the_server(store, monkeypatch) -> None:
    calls: list[str] = []

    class _Fake:
        server_address = ("127.0.0.1", 1234)

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            calls.append("closed")

    monkeypatch.setattr("sdlc.queue_server.make_server", lambda *a, **k: _Fake())
    serve(store, _policy(), "127.0.0.1", 0)
    assert calls == ["closed"]
