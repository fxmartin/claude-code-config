# ABOUTME: Tests for the local progress dashboard server (stdlib http.server).
# ABOUTME: Starts make_server on an ephemeral port in a thread; fetches via urllib.

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from sdlc.build import Ledger
from sdlc.dashboard import make_server


def _seed(db_path: Path) -> str:
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("all", "parallel")
    ledger.set_total(run_id, 2)
    ledger.event_log(run_id, "", "info", "controller", "run started")
    ledger.story_upsert(run_id, "34.5-003", "34", "Build it", "high", 3, "backend", "", None, "TODO")
    ledger.stage_start(run_id, "34.5-003", "build", 1)
    ledger.set_story_status(run_id, "34.5-003", "IN_PROGRESS")
    ledger.story_upsert(run_id, "34.6-001", "34", "Wire it", "med", 2, "backend", "", None, "DONE")
    ledger.set_story_pr(run_id, "34.6-001", 42)
    return run_id


@contextmanager
def _running(db_path: Path):
    server = make_server(db_path, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - localhost test
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def test_api_status_returns_snapshot_json(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed(db)
    with _running(db) as base:
        status, ctype, body = _get(base + "/api/status")
    assert status == 200
    assert "application/json" in ctype
    payload = json.loads(body)
    assert payload["run"]["id"] == run_id
    assert payload["counts"]["done"] == 1
    assert {s["story_id"] for s in payload["stories"]} == {"34.5-003", "34.6-001"}


def test_root_serves_html(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed(db)
    with _running(db) as base:
        status, ctype, body = _get(base + "/")
    assert status == 200
    assert "text/html" in ctype
    text = body.decode("utf-8")
    assert "sdlc build" in text and "/api/status" in text


def test_unknown_path_404(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed(db)
    with _running(db) as base:
        try:
            _get(base + "/nope")
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404


def test_api_status_no_run(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"  # never created
    with _running(db) as base:
        status, _ctype, body = _get(base + "/api/status")
    assert status == 200
    assert json.loads(body)["run"] is None


# --- clickable PR links ----------------------------------------------------


@pytest.mark.parametrize(
    "remote,expected",
    [
        (
            "git@github.com:fxmartin/claude-code-config.git",
            "https://github.com/fxmartin/claude-code-config",
        ),
        (
            "https://github.com/sales/rosetta.git",
            "https://github.com/sales/rosetta",
        ),
        ("ssh://git@host:2222/group/sub/repo.git", "https://host/group/sub/repo"),
        ("https://user:tok@host/g/r", "https://host/g/r"),
        ("not a url", None),
        ("", None),
    ],
)
def test_web_url_from_remote(remote: str, expected) -> None:
    from sdlc.dashboard import _web_url_from_remote

    assert _web_url_from_remote(remote) == expected


def test_page_has_pr_link_template() -> None:
    from sdlc.dashboard import _PAGE

    assert "/pull/" in _PAGE


def test_api_status_includes_pr_base(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed(db)
    server = make_server(db, host="127.0.0.1", port=0)
    server.project_url = "https://github.com/g/r"  # override resolved value
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _s, _c, body = _get(f"http://127.0.0.1:{server.server_address[1]}/api/status")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert json.loads(body)["pr_base"] == "https://github.com/g/r"


# --- runs browser ----------------------------------------------------------


def _seed_two(db_path: Path) -> tuple[str, str]:
    """Two runs: an older fully-done run, then a newer in-progress run."""
    ledger = Ledger(db_path)
    ledger.init()
    old = ledger.run_create("epic-33", "parallel")
    ledger.set_total(old, 1)
    ledger.story_upsert(old, "33.1-001", "33", "Old", "high", 1, "backend", "", None, "DONE")
    ledger.run_update_status(old, "DONE")
    new = ledger.run_create("34.5-003", "parallel")
    ledger.set_total(new, 2)
    ledger.story_upsert(new, "34.5-003", "34", "New", "high", 3, "backend", "", None, "IN_PROGRESS")
    ledger.story_upsert(new, "34.6-001", "34", "New2", "high", 2, "backend", "", None, "DONE")
    return old, new


def test_list_runs_newest_first_with_counts(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    old, new = _seed_two(db)
    runs = Ledger(db).list_runs()
    assert [r["id"] for r in runs] == [new, old]  # newest first
    by_id = {r["id"]: r for r in runs}
    assert by_id[new]["total"] == 2 and by_id[new]["done"] == 1
    assert by_id[old]["status"] == "DONE" and by_id[old]["done"] == 1
    assert Ledger(tmp_path / "missing.db").list_runs() == []


def test_api_runs_endpoint(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    old, new = _seed_two(db)
    with _running(db) as base:
        status, ctype, body = _get(base + "/api/runs")
    assert status == 200 and "application/json" in ctype
    ids = [r["id"] for r in json.loads(body)]
    assert ids == [new, old]


def test_api_status_run_param_selects_run(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    old, new = _seed_two(db)
    with _running(db) as base:
        _s, _c, latest = _get(base + "/api/status")
        _s, _c, picked = _get(base + "/api/status?run=" + old)
        _s, _c, bogus = _get(base + "/api/status?run=does-not-exist")
    assert json.loads(latest)["run"]["id"] == new       # no param → latest
    assert json.loads(picked)["run"]["id"] == old        # ?run= selects it
    assert json.loads(bogus)["run"] is None              # missing run → null


def test_api_status_includes_project(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed(db)
    with _running(db) as base:
        _s, _c, body = _get(base + "/api/status")
    project = json.loads(body)["project"]
    assert "name" in project and "url" in project      # name falls back to repo dir


def test_page_has_runs_and_latte_theme() -> None:
    from sdlc.dashboard import _PAGE

    assert "/api/runs" in _PAGE       # the runs browser polls it
    assert "#eff1f5" in _PAGE         # Catppuccin Latte base colour
    assert "Autonomous SDLC" in _PAGE  # product title/brand
    assert "<title>Autonomous SDLC</title>" in _PAGE


# --- stop / restart lifecycle ----------------------------------------------


def test_pidfile_is_per_host_port() -> None:
    from sdlc.dashboard import _pidfile

    a = _pidfile("127.0.0.1", 8787)
    b = _pidfile("127.0.0.1", 8788)
    assert a != b
    assert "8787" in a.name and a.suffix == ".pid"


def test_stop_dashboard_idle_returns_zero() -> None:
    from sdlc.dashboard import stop_dashboard

    # An unlikely, unused port with no PID file → nothing to stop.
    assert stop_dashboard("127.0.0.1", 8798) == 0


def test_stop_dashboard_kills_recorded_pid() -> None:
    import subprocess
    import sys

    from sdlc.dashboard import _pidfile, stop_dashboard

    host, port = "127.0.0.1", 8799  # unused port; we drive the PID file directly
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    _pidfile(host, port).write_text(str(proc.pid))
    try:
        stopped = stop_dashboard(host, port)
        assert stopped >= 1
        assert proc.wait(timeout=5) is not None       # SIGTERM ended it
        assert not _pidfile(host, port).exists()       # PID file cleaned up
    finally:
        if proc.poll() is None:
            proc.kill()
