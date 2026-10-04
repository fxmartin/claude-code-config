# ABOUTME: Tests for remote run detail (Story 35.4-006) — the XPS dashboard relays a fleet run's own
# ABOUTME: /api/status and change token from its worker, falls back to the header, and names the forge.

from __future__ import annotations

import json
import plistlib
import threading
from pathlib import Path

import pytest
from test_fleet_transcripts import (
    TAILNET_URL,
    _dead_origin,
    _Fleet,
    _get,
    _record,
    _remote_row,
    _seed_worker_run,
    _serve,
)

from sdlc import dashboard
from sdlc.dashboard import (
    _change_token,
    _origin_forge,
    _WorkerStatusRelay,
    _WorkerUnreachable,
    make_server,
)
from sdlc.doctor import check_worker_dashboard
from sdlc.queue import QueueStore
from sdlc.queue_client import QueueClient, push_fleet_run
from sdlc.registry import Registry, RunRecord

ORIGIN = "http://gitlab.test/root/alpha.git"


@pytest.fixture
def xps(tmp_path: Path):
    """An XPS dashboard (empty local registry) fed by a real worker dashboard."""
    worker_root = tmp_path / "mac"
    worker_root.mkdir()
    rid, db, worker_registry = _seed_worker_run(worker_root)
    with _serve(worker_registry) as worker_url:
        fleet = _Fleet(_remote_row(rid, db, dashboard_url=worker_url, origin=ORIGIN))
        with _serve(Registry(tmp_path / "xps-registry.json"), fleet) as xps_url:
            yield xps_url, worker_url, rid, db, fleet


# --- /api/status is the worker's own ------------------------------------------------


def test_status_is_the_workers_snapshot_with_worker_and_origin(xps) -> None:
    xps_url, worker_url, rid, _db, _fleet = xps
    worker_snap = json.loads(_get(f"{worker_url}/api/status?run={rid}")[1])
    snap = json.loads(_get(f"{xps_url}/api/status?run={rid}")[1])

    assert snap["worker"] == "m3max"
    assert snap["origin"] == worker_url
    assert snap["run"]["worker"] == "m3max"
    assert "detail_unavailable" not in snap
    # What a local run shows: the stories with their stage attempts, the DAG, the counts.
    assert [s["story_id"] for s in snap["stories"]] == ["70.1-001", "70.1-002"]
    assert snap["stories"] == worker_snap["stories"]
    assert snap["stories"][0]["stages"]
    assert snap["counts"] == worker_snap["counts"]
    assert snap["dag"] == worker_snap["dag"]
    assert snap["run"]["id"] == rid


def test_a_local_runs_status_is_untouched_by_the_relay(tmp_path: Path) -> None:
    # Byte-identical for a run this host owns: no worker/origin/detail keys appear.
    rid, _db, registry = _seed_worker_run(tmp_path)
    with _serve(registry) as url:
        snap = json.loads(_get(f"{url}/api/status?run={rid}")[1])
    assert not {"worker", "origin", "detail_unavailable"} & snap.keys()
    assert "worker" not in snap["run"]


def test_a_local_run_wins_over_a_fleet_row_with_the_same_id(xps, tmp_path: Path) -> None:
    _xps_url, worker_url, rid, db, _fleet = xps
    # The XPS also has the run locally: the local ledger is the authority, no relay.
    local_registry = Registry(tmp_path / "both.json")
    local_registry.register(
        RunRecord(rid, str(db.parent), str(db), "all", 1, "IN_PROGRESS", "2026-10-02T10:00:00+00:00")
    )
    fleet = _Fleet(_remote_row(rid, db, dashboard_url=_dead_origin()))
    with _serve(local_registry, fleet) as url:
        snap = json.loads(_get(f"{url}/api/status?run={rid}")[1])
    assert "origin" not in snap and [s["story_id"] for s in snap["stories"]]


def test_an_unreachable_worker_falls_back_to_the_header_and_says_so(xps) -> None:
    xps_url, _w, rid, _db, fleet = xps
    fleet.rows[0]["dashboard_url"] = _dead_origin()
    snap = json.loads(_get(f"{xps_url}/api/status?run={rid}")[1])

    assert snap["detail_unavailable"] == "detail unavailable — m3max dashboard not reachable"
    assert "did not answer" in snap["detail_error"]
    assert snap["stories"] == []
    assert snap["run"]["mode"] == "remote"  # today's header-only snapshot
    assert snap["counts"]["total"] == 5 and snap["counts"]["done"] == 1  # what the worker last pushed


@pytest.mark.parametrize("advertised", [None, "", "file:///etc/passwd"])
def test_a_worker_that_advertises_no_dashboard_falls_back_and_says_so(xps, advertised) -> None:
    xps_url, _w, rid, _db, fleet = xps
    fleet.rows[0]["dashboard_url"] = advertised
    snap = json.loads(_get(f"{xps_url}/api/status?run={rid}")[1])
    assert snap["detail_unavailable"] == "detail unavailable — m3max advertises no dashboard"
    assert snap["stories"] == []


def test_a_worker_with_no_record_of_the_run_is_not_an_empty_run(xps) -> None:
    xps_url, _w, rid, _db, fleet = xps
    fleet.rows[0]["run_id"] = "unknown-to-the-worker"
    snap = json.loads(_get(f"{xps_url}/api/status?run=unknown-to-the-worker")[1])
    assert "no record of this run" in snap["detail_unavailable"]
    assert snap["run"]["id"] == "unknown-to-the-worker"


@pytest.mark.parametrize(
    ("path", "relayed"),
    [
        ("/api/status?run=ghost", ["/api/status"]),
        ("/api/logs?run=ghost", ["/api/status"]),
        ("/api/logs?run=ghost&story=70.1-001", ["/api/logs"]),
    ],
)
def test_a_dashboard_its_own_fleet_row_names_relays_once_not_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, relayed: list[str]
) -> None:
    # A worker's dashboard sees the fleet too: a run it no longer holds (pruned) still
    # names that dashboard there. Each relayed request must not be relayed again.
    real = dashboard._fetch_worker_json
    fetched: list[str] = []

    def counting(origin: str, route: str, query: dict) -> dict:
        fetched.append(route)
        if len(fetched) > 5:  # bound a regression so it fails instead of exhausting threads
            raise _WorkerUnreachable("relay loop")
        return real(origin, route, query)

    monkeypatch.setattr(dashboard, "_fetch_worker_json", counting)
    fleet = _Fleet(_remote_row("ghost", tmp_path / ".sdlc-state.db"))
    with _serve(Registry(tmp_path / "registry.json"), fleet) as url:
        fleet.rows[0]["dashboard_url"] = url
        status, body = _get(f"{url}{path}")
    assert status == 200
    assert fetched == relayed
    if path.startswith("/api/status"):
        assert "no record of this run" in json.loads(body)["detail_unavailable"]


def test_the_page_says_why_there_are_no_stories_instead_of_no_stories_yet() -> None:
    page = dashboard._PAGE
    assert "d.detail_unavailable" in page
    assert page.index("d.detail_unavailable") < page.index("no stories yet…")
    # A relayed run's stage links go to the worker's own confined /log; local stay relative.
    assert 'let statOrigin = "";' in page
    assert "statOrigin = d.origin || \"\";" in page


# --- the relay's cache --------------------------------------------------------------


def test_the_relay_shares_one_fetch_per_ttl_and_remembers_a_failure() -> None:
    now = [0.0]
    calls: list[str] = []

    def fetch(origin: str, path: str, query: dict) -> dict:
        calls.append(origin)
        if origin == "http://down:1":
            raise _WorkerUnreachable("down")
        return {"run": {"id": query["run"]}}

    relay = _WorkerStatusRelay(ttl=1.0, failure_ttl=10.0, clock=lambda: now[0], fetch=fetch)
    relay.get("http://up:1", "r")
    relay.get("http://up:1", "r")
    assert calls == ["http://up:1"]
    now[0] = 1.5
    relay.get("http://up:1", "r")
    assert len(calls) == 2

    for _ in range(3):
        with pytest.raises(_WorkerUnreachable, match="down"):
            relay.get("http://down:1", "r")
    assert calls.count("http://down:1") == 1  # a blackholed worker costs one timeout per window
    now[0] = 12.0
    with pytest.raises(_WorkerUnreachable):
        relay.get("http://down:1", "r")
    assert calls.count("http://down:1") == 2


def test_the_relay_forgets_a_run_once_no_read_can_be_served_from_it() -> None:
    # Otherwise every remote run ever viewed keeps its payload for the dashboard's life.
    now = [0.0]
    relay = _WorkerStatusRelay(
        ttl=1.0, failure_ttl=10.0, clock=lambda: now[0],
        fetch=lambda origin, path, query: {"run": {"id": query["run"]}},
    )
    for i in range(50):
        relay.get("http://up:1", f"run-{i}")
        now[0] += 10.0
    assert list(relay._entries) == [("http://up:1", "run-49")]


# --- the SSE change token -----------------------------------------------------------


def test_the_change_token_follows_the_selected_remote_runs_worker(xps) -> None:
    from sdlc.build import Ledger

    _xps_url, worker_url, rid, db, fleet = xps
    server = make_server(
        db_path=None, host="127.0.0.1", port=0, registry=Registry(db.parent / "xps.json")
    )
    server.fleet = fleet  # type: ignore[attr-defined]
    server.worker_status = _WorkerStatusRelay(ttl=0.0)  # type: ignore[attr-defined]
    try:
        before = _change_token(server, rid)
        assert before == _change_token(server, rid)  # quiet while nothing moves
        Ledger(db).event_log(rid, "", "info", "controller", "worker-side activity")
        after = _change_token(server, rid)
        assert after != before  # an event on the worker moves the XPS's token
        assert f"detail:{rid}:" in after
        # Without a selected run the token is the fleet's pushed progress only.
        assert "detail:" not in _change_token(server)
        # An unreachable worker is a token value, never an exception or a stall.
        fleet.rows[0]["dashboard_url"] = _dead_origin()
        assert _change_token(server, rid).endswith(":unreachable")
    finally:
        server.server_close()


def test_the_stream_url_carries_the_selected_run() -> None:
    page = dashboard._PAGE
    assert '"/api/stream" + (sel ? "?run=" + encodeURIComponent(sel) : "")' in page
    assert "if(changed) connectStream();" in page


# --- the forge panel ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("http://gitlab.test/root/alpha.git", ("root/alpha", "gitlab", "http://gitlab.test")),
        ("https://oauth@gitlab.corp.io:8443/g/alpha.git", ("g/alpha", "gitlab", "https://gitlab.corp.io:8443")),
        ("git@gitlab.test:root/alpha.git", ("root/alpha", "gitlab", "https://gitlab.test")),
        ("git@github.com:fxmartin/alpha.git", ("fxmartin/alpha", "github", None)),
        ("https://gitlab.com/fxmartin/alpha", ("fxmartin/alpha", "gitlab", None)),
        ("https://ghe.corp.io/o/r.git", ("o/r", "github", "https://ghe.corp.io")),
        ("", (None, "github", None)),
        ("/Users/fxmartin/Work/alpha", (None, "github", None)),
        # The remote pattern takes any digits; an impossible port is unresolvable, not a crash.
        ("http://gitlab.test:99999/root/alpha.git", (None, "github", None)),
    ],
)
def test_the_forge_is_resolved_from_the_origin_not_a_path(origin: str, expected) -> None:
    assert _origin_forge(origin) == expected


def test_the_forge_panel_of_a_remote_run_uses_the_fleet_records_origin(tmp_path: Path) -> None:
    seen: list[tuple] = []

    class _Cache:
        def get(self, slug, host="github", instance_url=None):
            seen.append((slug, host, instance_url))
            return {"available": True, "slug": slug, "host": host}

    # The repo path is the worker's: nothing under it exists on this machine.
    gone = Path("/Users/nobody/Work/alpha/.sdlc-state.db")
    server = make_server(
        db_path=None, host="127.0.0.1", port=0, registry=Registry(tmp_path / "xps.json")
    )
    server.fleet = _Fleet(_remote_row("run-1", gone, origin=ORIGIN))  # type: ignore[attr-defined]
    server.github_cache = _Cache()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/github?run=run-1"
        payload = json.loads(_get(url)[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert seen == [("root/alpha", "gitlab", "http://gitlab.test")]
    assert payload["slug"] == "root/alpha"


def test_a_remote_run_without_an_origin_degrades_to_unavailable(xps) -> None:
    xps_url, _w, rid, _db, fleet = xps
    fleet.rows[0]["origin"] = None
    payload = json.loads(_get(f"{xps_url}/api/github?run={rid}")[1])
    assert payload.get("available") is False


# --- the origin travels with the fleet record ---------------------------------------


def test_the_store_keeps_the_origin_on_the_fleet_row(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "q.db")
    store.init()
    store.put_fleet_run(_record(origin=ORIGIN))
    assert store.list_fleet_runs()[0]["origin"] == ORIGIN


def test_a_push_that_could_not_read_the_origin_keeps_the_known_one(tmp_path: Path) -> None:
    # The finish push is the run's last: a failed `git remote get-url` there must not
    # blank the forge panel of a finished remote run for good.
    store = QueueStore(tmp_path / "q.db")
    store.init()
    store.put_fleet_run(_record(origin=ORIGIN))
    store.put_fleet_run(
        _record(origin=None, status="DONE", finished_at="2026-10-02T11:00:00+00:00")
    )
    (row,) = store.list_fleet_runs()
    assert row["status"] == "DONE" and row["origin"] == ORIGIN


def test_a_push_carries_the_repos_origin_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pushed: list[RunRecord] = []
    monkeypatch.setenv("SDLC_QUEUE_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(QueueClient, "put_fleet_run", lambda self, record: pushed.append(record))
    monkeypatch.setattr(
        "sdlc.issue_host._remote_url", lambda root: "http://oauth2:glpat-SECRET@gitlab.test/root/alpha.git"
    )
    push_fleet_run(_record(dashboard_url=None))
    assert pushed[0].origin == "http://gitlab.test/root/alpha.git"
    assert "SECRET" not in json.dumps(pushed[0].to_dict())


def test_a_repo_with_no_origin_pushes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    pushed: list[RunRecord] = []
    monkeypatch.setenv("SDLC_QUEUE_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(QueueClient, "put_fleet_run", lambda self, record: pushed.append(record))
    monkeypatch.setattr("sdlc.issue_host._remote_url", lambda root: None)
    push_fleet_run(_record(dashboard_url=None))
    assert pushed[0].origin is None


def test_a_registry_json_written_before_origin_still_loads() -> None:
    legacy = {k: v for k, v in _record().to_dict().items() if k != "origin"}
    assert RunRecord.from_dict(legacy).origin is None


# --- doctor on the worker ------------------------------------------------------------


def _worker_plist(tmp_path: Path, args: list[str], env: dict | None = None) -> Path:
    path = tmp_path / "com.fxmartin.sdlc-worker.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.fxmartin.sdlc-worker",
                "ProgramArguments": ["sdlc", "queue", "run", "--worker", "m3max", *args],
                "EnvironmentVariables": env or {},
            }
        )
    )
    return path


def test_doctor_is_silent_where_no_worker_agent_is_installed(tmp_path: Path) -> None:
    assert check_worker_dashboard(agent_path=tmp_path / "absent.plist") is None


def test_doctor_is_clean_when_the_advertised_dashboard_answers(tmp_path: Path) -> None:
    probed: list[str] = []
    finding = check_worker_dashboard(
        agent_path=_worker_plist(tmp_path, ["--dashboard-url", TAILNET_URL + "/"]),
        probe=lambda origin: probed.append(origin),
    )
    assert finding is not None and finding.status == "CLEAN"
    assert probed == [TAILNET_URL]


def test_doctor_reads_the_equals_form_and_the_environment_too(tmp_path: Path) -> None:
    for plist in (
        _worker_plist(tmp_path, [f"--dashboard-url={TAILNET_URL}"]),
        _worker_plist(tmp_path, [], {"SDLC_DASHBOARD_URL": TAILNET_URL}),
    ):
        finding = check_worker_dashboard(agent_path=plist, probe=lambda _o: None)
        assert finding is not None and finding.status == "CLEAN"


def test_doctor_warns_when_the_advertised_dashboard_does_not_answer(tmp_path: Path) -> None:
    finding = check_worker_dashboard(
        agent_path=_worker_plist(tmp_path, ["--dashboard-url", TAILNET_URL]),
        probe=lambda _o: "Connection refused",
    )
    assert finding is not None and finding.status == "WARN"
    assert TAILNET_URL in finding.detail and "Connection refused" in finding.detail
    assert "sdlc-dashboard" in finding.remedy


def test_doctor_probes_a_real_listener_and_a_dead_port(tmp_path: Path) -> None:
    with _serve(Registry(tmp_path / "r.json")) as live:
        up = check_worker_dashboard(agent_path=_worker_plist(tmp_path, ["--dashboard-url", live]))
    down = check_worker_dashboard(
        agent_path=_worker_plist(tmp_path, ["--dashboard-url", _dead_origin()])
    )
    assert up is not None and up.status == "CLEAN"
    assert down is not None and down.status == "WARN"


def test_doctor_warns_when_the_worker_advertises_nothing_usable(tmp_path: Path) -> None:
    for args in ([], ["--dashboard-url", "m3max:8787"]):
        finding = check_worker_dashboard(
            agent_path=_worker_plist(tmp_path, args), probe=lambda _o: None
        )
        assert finding is not None and finding.status == "WARN"
