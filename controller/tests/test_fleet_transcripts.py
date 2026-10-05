# ABOUTME: Tests for remote transcripts (Story 35.4-002) — a fleet run's record carries its worker's
# ABOUTME: dashboard URL, and the XPS dashboard reads that worker's /api/logs for "view session".

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdlc.build import Ledger
from sdlc.dashboard import _PAGE, make_server
from sdlc.queue import QueueStore
from sdlc.queue_client import QueueClient, push_fleet_run
from sdlc.queue_worker import detect_worker_profile
from sdlc.registry import (
    DASHBOARD_URL_ENV,
    Registry,
    RunRecord,
    normalize_dashboard_url,
)

_FAST_SHUTDOWN = {"poll_interval": 0.01}
TAILNET_URL = "http://m3max.tail1234.ts.net:8787"


def _record(run_id: str = "run-1", **overrides) -> RunRecord:
    fields = {
        "run_id": run_id,
        "repo": "/Users/fx/Work/alpha",
        "db": "/Users/fx/Work/alpha/.sdlc-state.db",
        "scope": "epic-1",
        "pid": 4242,
        "status": "IN_PROGRESS",
        "started_at": "2026-10-02T10:00:00+00:00",
        "total": 5,
        "completed": 1,
        "worker": "m3max",
        "dashboard_url": TAILNET_URL,
    }
    fields.update(overrides)
    return RunRecord(**fields)


# --- the record carries the dashboard URL ------------------------------------------


def test_run_record_carries_the_dashboard_url_and_defaults_to_none() -> None:
    bare = RunRecord(
        run_id="r", repo="/r", db="/r/db", scope="s", pid=1, status="IN_PROGRESS",
        started_at="2026-10-02T10:00:00+00:00",
    )
    assert bare.dashboard_url is None
    assert RunRecord.from_dict(_record().to_dict()).dashboard_url == TAILNET_URL
    # A registry.json written before this story has no `dashboard_url` key at all.
    legacy = {k: v for k, v in _record().to_dict().items() if k != "dashboard_url"}
    assert RunRecord.from_dict(legacy).dashboard_url is None


@pytest.mark.parametrize(
    ("given", "origin"),
    [
        (TAILNET_URL, TAILNET_URL),
        (TAILNET_URL + "/", TAILNET_URL),
        ("  http://100.64.0.7:8787  ", "http://100.64.0.7:8787"),
        ("https://m3max.example.ts.net", "https://m3max.example.ts.net"),
    ],
)
def test_a_dashboard_url_is_normalised_to_its_origin(given: str, origin: str) -> None:
    assert normalize_dashboard_url(given) == origin


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "m3max:8787",  # no scheme
        "file:///etc/passwd",
        "ftp://m3max:8787",
        "http://",
        "http://user:pw@m3max:8787",  # credentials do not belong in a record
        "http://m3max:8787/some/path",
        "http://m3max:8787/?q=1",
        "http://m3max:8787/#frag",
        "http://m3max:notaport",
    ],
)
def test_a_dashboard_url_that_is_not_a_plain_origin_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_dashboard_url(bad)


@pytest.fixture
def store(tmp_path: Path) -> QueueStore:
    s = QueueStore(tmp_path / "service-queue.db")
    s.init()
    return s


def test_the_store_keeps_the_dashboard_url_on_the_fleet_row(store: QueueStore) -> None:
    store.put_fleet_run(_record())
    (row,) = store.list_fleet_runs()
    assert row["dashboard_url"] == TAILNET_URL
    store.put_fleet_run(_record(dashboard_url=None))
    assert store.list_fleet_runs()[0]["dashboard_url"] is None


def test_a_queue_db_written_before_dashboard_urls_upgrades_in_place(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = QueueStore(path)
    old.init()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE fleet_runs")
        conn.execute(
            "CREATE TABLE fleet_runs (run_id TEXT PRIMARY KEY, worker TEXT NOT NULL, "
            "repo TEXT NOT NULL, db TEXT NOT NULL, scope TEXT NOT NULL, pid INTEGER NOT NULL, "
            "status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, total INTEGER, "
            "completed INTEGER, updated_at TIMESTAMP NOT NULL)"
        )
        conn.execute(
            "DELETE FROM _migrations WHERE name IN ('fleet_run_dashboard_url', 'fleet_run_origin')"
        )
    old.ensure_migrated()
    old.put_fleet_run(_record())
    assert old.list_fleet_runs()[0]["dashboard_url"] == TAILNET_URL


# --- the /runs route + the client push ----------------------------------------------


@contextmanager
def _queue_service(store: QueueStore):
    from sdlc.queue_server import AccessPolicy
    from sdlc.queue_server import make_server as make_queue_server

    policy = AccessPolicy(token="s3cret", networks=("127.0.0.0/8",))
    server = make_queue_server(store, policy, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs=_FAST_SHUTDOWN, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_put_runs_carries_the_dashboard_url_through_the_service(store: QueueStore) -> None:
    with _queue_service(store) as url:
        client = QueueClient(url, token="s3cret")
        client.put_fleet_run(_record())
        assert client.list_fleet_runs()[0]["dashboard_url"] == TAILNET_URL


def test_put_runs_refuses_a_malformed_dashboard_url_with_400(store: QueueStore) -> None:
    with _queue_service(store) as url:
        body = _record(dashboard_url="file:///etc/passwd").to_dict()
        req = urllib.request.Request(
            url + "/runs", data=json.dumps(body).encode(), method="PUT",
            headers={"Content-Type": "application/json", "Authorization": "Bearer s3cret"},
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=10)  # noqa: S310 - localhost test
        assert err.value.code == 400
    assert store.list_fleet_runs() == []


def test_a_bare_build_pushes_the_dashboard_url_its_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pushed: list[RunRecord] = []
    monkeypatch.setenv("SDLC_QUEUE_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(QueueClient, "put_fleet_run", lambda self, record: pushed.append(record))
    monkeypatch.setenv(DASHBOARD_URL_ENV, TAILNET_URL + "/")
    push_fleet_run(_record(dashboard_url=None))
    monkeypatch.delenv(DASHBOARD_URL_ENV)
    push_fleet_run(_record("run-2", dashboard_url=None))
    assert [r.dashboard_url for r in pushed] == [TAILNET_URL, None]


def test_a_malformed_dashboard_url_in_the_environment_never_costs_the_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pushed: list[RunRecord] = []
    monkeypatch.setenv("SDLC_QUEUE_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(QueueClient, "put_fleet_run", lambda self, record: pushed.append(record))
    monkeypatch.setenv(DASHBOARD_URL_ENV, "not-a-url")
    push_fleet_run(_record(dashboard_url=None))
    assert [r.dashboard_url for r in pushed] == [None]


# --- the worker advertises it --------------------------------------------------------


def test_a_worker_profile_normalises_its_dashboard_url(tmp_path: Path) -> None:
    profile = detect_worker_profile(
        "m3max", work_dir=tmp_path, probe=lambda _b: False, runtime=lambda: "podman",
        dashboard_url=TAILNET_URL + "/",
    )
    assert profile.dashboard_url == TAILNET_URL
    bare = detect_worker_profile(
        "m3max", work_dir=tmp_path, probe=lambda _b: False, runtime=lambda: "podman"
    )
    assert bare.dashboard_url is None
    with pytest.raises(ValueError):
        detect_worker_profile(
            "m3max", work_dir=tmp_path, probe=lambda _b: False, runtime=lambda: "podman",
            dashboard_url="m3max:8787",
        )


def test_queue_run_refuses_a_dashboard_url_without_a_worker() -> None:
    from sdlc.cli import app

    result = CliRunner().invoke(app, ["queue", "run", "--dashboard-url", TAILNET_URL])
    assert result.exit_code == 2
    assert "--worker" in result.output


def test_queue_run_refuses_a_malformed_dashboard_url() -> None:
    from sdlc.cli import app

    result = CliRunner().invoke(
        app, ["queue", "run", "--worker", "m3max", "--dashboard-url", "m3max:8787"]
    )
    assert result.exit_code == 2
    assert "dashboard" in result.output.lower()


# --- the XPS dashboard reads the worker's /api/logs ---------------------------------


class _Fleet:
    """A stand-in for the dashboard's fleet view: fixed rows, no queue service."""

    def __init__(self, *rows: dict) -> None:
        self.rows = list(rows)

    def snapshot(self) -> dict:
        return {"configured": True, "available": True, "error": None, "runs": self.rows}

    def status(self) -> dict:
        return {"configured": True, "available": True, "error": None}

    def invalidate(self) -> None:
        pass


@contextmanager
def _serve(registry: Registry, fleet: _Fleet | None = None):
    server = make_server(db_path=None, host="127.0.0.1", port=0, registry=registry)
    if fleet is not None:
        server.fleet = fleet  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, kwargs=_FAST_SHUTDOWN, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - localhost test
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _dead_origin() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _seed_worker_run(root: Path) -> tuple[str, Path, Registry]:
    """The Mac's side: a ledger with two stories' transcripts, registered in its own registry."""
    repo = root / "alpha"
    repo.mkdir()
    db = repo / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    rid = ledger.run_create("all", "parallel")
    ledger.set_total(rid, 2)
    logs = Path(f"{db}.logs") / rid
    logs.mkdir(parents=True)
    for story, text in (("70.1-001", "BUILD ONE"), ("70.1-002", "BUILD TWO")):
        ledger.story_upsert(rid, story, "70", story, "high", 3, "backend", "", None, "IN_PROGRESS")
        log = logs / f"{story}-build-1.log"
        log.write_text(text, encoding="utf-8")
        ledger.stage_start(rid, story, "build", 1)
        ledger.stage_finish(rid, story, "build", 1, "DONE", output_path=str(log.resolve()))
    registry = Registry(root / "worker-registry.json")
    registry.register(
        RunRecord(rid, str(repo), str(db), "all", os.getpid(), "IN_PROGRESS",
                  "2026-10-02T10:00:00+00:00", total=2, completed=1)
    )
    return rid, db, registry


def _remote_row(rid: str, db: Path, **overrides) -> dict:
    row = {
        **_record(rid, repo=str(db.parent), db=str(db)).to_dict(),
        "updated_at": "2026-10-02T10:05:00+00:00",
        "worker_online": True,
    }
    row.update(overrides)
    return row


@pytest.fixture
def xps(tmp_path: Path):
    """An XPS dashboard (empty local registry) and the worker dashboard its fleet row names."""
    worker_root = tmp_path / "mac"
    worker_root.mkdir()
    rid, db, worker_registry = _seed_worker_run(worker_root)
    xps_registry = Registry(tmp_path / "xps-registry.json")
    with _serve(worker_registry) as worker_url:
        fleet = _Fleet(_remote_row(rid, db, dashboard_url=worker_url))
        with _serve(xps_registry, fleet) as xps_url:
            yield xps_url, worker_url, rid, db, fleet


def test_view_session_loads_the_transcripts_from_the_workers_dashboard(xps) -> None:
    xps_url, worker_url, rid, _db, _fleet = xps
    status, body = _get(f"{xps_url}/api/logs?story=70.1-001&run={rid}")
    assert status == 200
    payload = json.loads(body)
    assert payload["worker"] == "m3max"
    assert payload["origin"] == worker_url
    assert payload["run"] == rid and payload["story"] == "70.1-001"
    assert "error" not in payload
    (build,) = payload["transcripts"]
    assert (build["stage"], build["attempt"], build["exists"]) == ("build", 1, True)
    assert build["content"] == "BUILD ONE"


def test_without_a_story_it_gathers_every_story_the_worker_has_for_the_run(xps) -> None:
    xps_url, _w, rid, _db, _fleet = xps
    _status, body = _get(f"{xps_url}/api/logs?run={rid}")
    payload = json.loads(body)
    assert {(t["story"], t["content"]) for t in payload["transcripts"]} == {
        ("70.1-001", "BUILD ONE"),
        ("70.1-002", "BUILD TWO"),
    }


def test_a_workers_dashboard_that_is_down_is_reported_with_its_log_root(xps) -> None:
    xps_url, _w, rid, db, fleet = xps
    fleet.rows[0]["dashboard_url"] = _dead_origin()
    status, body = _get(f"{xps_url}/api/logs?story=70.1-001&run={rid}")
    assert status == 200  # a down worker is an answer, not a server error
    payload = json.loads(body)
    assert payload["transcripts"] == []
    assert payload["worker"] == "m3max"
    assert "did not answer" in payload["error"]
    assert payload["logs_root"] == f"{db}.logs"


@pytest.mark.parametrize("advertised", [None, "", "file:///etc/passwd", "http://u:p@h:1"])
def test_a_run_with_no_usable_dashboard_url_says_so_and_still_offers_the_log_root(
    xps, advertised
) -> None:
    xps_url, _w, rid, db, fleet = xps
    fleet.rows[0]["dashboard_url"] = advertised
    payload = json.loads(_get(f"{xps_url}/api/logs?story=70.1-001&run={rid}")[1])
    assert payload["transcripts"] == []
    assert payload["origin"] is None
    assert "dashboard" in payload["error"]
    assert payload["logs_root"] == f"{db}.logs"


def test_a_worker_that_answers_with_junk_is_unreachable_not_a_crash(xps, tmp_path: Path) -> None:
    xps_url, _w, rid, _db, fleet = xps
    junk = socket.socket()
    junk.bind(("127.0.0.1", 0))
    junk.listen(1)

    def serve_junk() -> None:
        conn, _ = junk.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\nnot json")
        conn.close()

    threading.Thread(target=serve_junk, daemon=True).start()
    fleet.rows[0]["dashboard_url"] = f"http://127.0.0.1:{junk.getsockname()[1]}"
    try:
        payload = json.loads(_get(f"{xps_url}/api/logs?story=70.1-001&run={rid}")[1])
    finally:
        junk.close()
    assert payload["transcripts"] == []
    assert "error" in payload


def test_the_workers_confinement_is_unchanged_a_path_outside_its_logs_root_is_missing(
    tmp_path: Path,
) -> None:
    worker_root = tmp_path / "mac"
    worker_root.mkdir()
    rid, db, worker_registry = _seed_worker_run(worker_root)
    secret = tmp_path / "secret.txt"
    secret.write_text("DO NOT SERVE", encoding="utf-8")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE stages SET output_path = ? WHERE story_id = '70.1-001'", (str(secret),))
    with _serve(worker_registry) as worker_url:
        fleet = _Fleet(_remote_row(rid, db, dashboard_url=worker_url))
        with _serve(Registry(tmp_path / "xps.json"), fleet) as xps_url:
            raw = _get(f"{xps_url}/api/logs?story=70.1-001&run={rid}")[1]
    (transcript,) = json.loads(raw)["transcripts"]
    assert transcript["exists"] is False and transcript["content"] == ""
    assert b"DO NOT SERVE" not in raw


def test_the_xps_never_reads_its_own_disk_for_a_remote_runs_log(xps, tmp_path: Path) -> None:
    xps_url, _w, rid, _db, _fleet = xps
    local = tmp_path / "xps-local.log"
    local.write_text("XPS FILE", encoding="utf-8")
    status, _body = _get(f"{xps_url}/log?path={local}&run={rid}")
    assert status == 404


def test_the_xps_forwards_only_the_story_and_run_it_was_asked_for(xps) -> None:
    xps_url, _w, rid, _db, _fleet = xps
    story = "70.1-001%26run%3Dother%23x"  # an '&run=' smuggled into the story id
    payload = json.loads(_get(f"{xps_url}/api/logs?story={story}&run={rid}")[1])
    assert payload["transcripts"] == []
    assert payload["run"] == rid


def test_a_local_run_is_still_served_from_the_local_ledger(tmp_path: Path) -> None:
    rid, db, registry = _seed_worker_run(tmp_path)
    fleet = _Fleet(_remote_row(rid, db, dashboard_url=_dead_origin()))
    with _serve(registry, fleet) as base:
        payload = json.loads(_get(f"{base}/api/logs?story=70.1-001&run={rid}")[1])
    assert "worker" not in payload
    assert payload["transcripts"][0]["content"] == "BUILD ONE"


# --- the modal's string contract -----------------------------------------------------


def test_the_modal_shows_which_worker_served_the_transcripts() -> None:
    fn = _PAGE[_PAGE.index("function renderTranscripts("):]
    fn = fn[: fn.index("\n}\n")]
    assert "served by" in fn
    assert "d.worker" in fn and "d.origin" in fn


def test_the_modal_says_the_workers_dashboard_is_down_and_offers_the_log_root() -> None:
    fn = _PAGE[_PAGE.index("function renderTranscripts("):]
    fn = fn[: fn.index("\n}\n")]
    assert "d.error" in fn
    assert "d.logs_root" in fn
    assert "log root" in fn


def test_a_remote_run_header_offers_a_run_level_view_session() -> None:
    head = _PAGE[_PAGE.index('document.getElementById("head").innerHTML'):]
    head = head[: head.index(";\n")]
    assert 'run.mode === "remote"' in head
    assert "view-session" in head
    assert 'document.getElementById("head").addEventListener("click"' in _PAGE


def test_the_modal_still_fetches_the_dashboards_own_api_logs() -> None:
    # The XPS dashboard fetches the worker server-side, so the page keeps one origin.
    fn = _PAGE[_PAGE.index("async function openSession("):]
    assert 'fetch("/api/logs" + q' in fn[: fn.index("\n}\n")]


@contextmanager
def _raw_worker(response: bytes):
    """A one-shot worker that answers any request with fixed raw HTTP bytes."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def answer() -> None:
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(response)
        conn.close()

    threading.Thread(target=answer, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.getsockname()[1]}"
    finally:
        srv.close()


def _ok(body: bytes) -> bytes:
    return b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n" + body


def test_a_worker_answering_a_json_non_object_is_unreachable() -> None:
    from sdlc.dashboard import _fetch_worker_json, _WorkerUnreachable

    with _raw_worker(_ok(b"[1, 2]")) as origin:
        with pytest.raises(_WorkerUnreachable, match="not a dashboard response"):
            _fetch_worker_json(origin, "/api/logs", {"run": "r"})


def test_a_worker_answer_over_the_size_cap_is_refused(monkeypatch) -> None:
    from sdlc import dashboard
    from sdlc.dashboard import _fetch_worker_json, _WorkerUnreachable

    monkeypatch.setattr(dashboard, "_WORKER_LOGS_MAX_BYTES", 8)
    with _raw_worker(_ok(b'{"transcripts": []}')) as origin:
        with pytest.raises(_WorkerUnreachable, match="more than a transcript viewer"):
            _fetch_worker_json(origin, "/api/logs", {"run": "r"})


def test_a_worker_redirect_is_an_error_not_a_hop_to_another_host() -> None:
    from sdlc.dashboard import _fetch_worker_json, _WorkerUnreachable

    redirect = b"HTTP/1.0 302 Found\r\nLocation: http://169.254.169.254/\r\n\r\n"
    with _raw_worker(redirect) as origin:
        with pytest.raises(_WorkerUnreachable, match="did not answer"):
            _fetch_worker_json(origin, "/api/logs", {"run": "r"})
