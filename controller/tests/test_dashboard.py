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
def _running(db_path: Path, *, sse_poll: float | None = None, sse_heartbeat: float | None = None):
    server = make_server(db_path, host="127.0.0.1", port=0)
    if sse_poll is not None:
        server.sse_poll_interval = sse_poll  # type: ignore[attr-defined]
    if sse_heartbeat is not None:
        server.sse_heartbeat_interval = sse_heartbeat  # type: ignore[attr-defined]
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


def test_brand_bar_shows_version(tmp_path: Path) -> None:
    """The served page renders the controller version in the brand bar."""
    from sdlc import __version__

    db = tmp_path / ".sdlc-state.db"
    with _running(db) as base:
        _s, _c, body = _get(base + "/")
    text = body.decode("utf-8")
    assert f"v{__version__}" in text         # version rendered next to the app name
    assert "__SDLC_VERSION__" not in text    # placeholder fully substituted


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


# --- per-stage pipeline detail + /log endpoint -----------------------------


def test_api_status_has_stage_breakdown(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed(db)  # 34.5-003: build IN_PROGRESS, nothing past it
    with _running(db) as base:
        _s, _c, body = _get(base + "/api/status")
    by_id = {s["story_id"]: s for s in json.loads(body)["stories"]}
    story = by_id["34.5-003"]
    names = [st["name"] for st in story["stages"]]
    assert names == ["build", "coverage", "review", "merge"]
    by_name = {st["name"]: st["status"] for st in story["stages"]}
    assert by_name["build"] == "IN_PROGRESS"
    assert by_name["coverage"] == "PENDING"
    assert by_name["review"] == "PENDING" and by_name["merge"] == "PENDING"
    assert "bugfix_attempts" in story


def test_log_endpoint_serves_within_root_and_confines(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed(db)
    logs = tmp_path / ".sdlc-state.db.logs" / "run"
    logs.mkdir(parents=True)
    transcript = logs / "34.5-003-build-1.log"
    transcript.write_text("AGENT TRANSCRIPT HERE", encoding="utf-8")
    outside = tmp_path / "secret.txt"  # under tmp_path but NOT under the logs root
    outside.write_text("do not serve", encoding="utf-8")

    import urllib.parse

    with _running(db) as base:
        s1, _c, body = _get(base + "/log?path=" + urllib.parse.quote(str(transcript.resolve())))
        assert s1 == 200 and b"AGENT TRANSCRIPT HERE" in body
        try:
            _get(base + "/log?path=" + urllib.parse.quote(str(outside.resolve())))
            raise AssertionError("expected 404 for a path outside the logs root")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404


def test_page_has_stage_columns() -> None:
    from sdlc.dashboard import _PAGE

    for header in ("build", "QA", "review", "merge"):
        assert f"<th>{header}</th>" in _PAGE


# --- multi-run registry overview (Story 11.2-002) --------------------------


@contextmanager
def _running_registry(registry):
    """Run the dashboard in registry-discovery mode (no single ``--db``)."""
    server = make_server(db_path=None, host="127.0.0.1", port=0, registry=registry)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _seed_run(db_path: Path, scope: str, story_id: str, status: str) -> str:
    """A self-contained ledger with one run carrying a single story."""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create(scope, "parallel")
    ledger.set_total(run_id, 1)
    ledger.story_upsert(run_id, story_id, scope, "Story", "high", 1, "backend", "", None, status)
    return run_id


def _two_repo_registry(tmp_path: Path):
    """A registry pointing at two distinct per-repo ledgers (own stories)."""
    import os

    from sdlc.registry import Registry, RunRecord

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    db_a = repo_a / ".sdlc-state.db"
    db_b = repo_b / ".sdlc-state.db"
    run_a = _seed_run(db_a, "epic-aaa", "AAA.1-001", "IN_PROGRESS")
    run_b = _seed_run(db_b, "epic-bbb", "BBB.1-001", "DONE")

    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord(run_a, str(repo_a), str(db_a), "epic-aaa", os.getpid(),
                  "IN_PROGRESS", "2026-01-01T00:00:00+00:00", total=1, completed=0)
    )
    registry.register(
        RunRecord(run_b, str(repo_b), str(db_b), "epic-bbb", os.getpid(),
                  "DONE", "2026-01-02T00:00:00+00:00", finished_at="2026-01-02T01:00:00+00:00",
                  total=1, completed=1)
    )
    return registry, run_a, run_b, str(repo_a), str(repo_b)


def test_api_runs_lists_registry_in_discovery_mode(tmp_path: Path) -> None:
    registry, run_a, run_b, repo_a, repo_b = _two_repo_registry(tmp_path)
    with _running_registry(registry) as base:
        status, ctype, body = _get(base + "/api/runs")
    assert status == 200 and "application/json" in ctype
    rows = json.loads(body)
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {run_a, run_b}
    # repo, scope, status, and live done/total (read from each run's own ledger).
    assert by_id[run_a]["repo"] == repo_a and by_id[run_a]["scope"] == "epic-aaa"
    assert by_id[run_a]["total"] == 1 and by_id[run_a]["done"] == 0
    assert by_id[run_b]["done"] == 1 and by_id[run_b]["status"] == "DONE"
    # newest started_at first.
    assert [r["id"] for r in rows] == [run_b, run_a]


def test_api_status_per_run_isolation(tmp_path: Path) -> None:
    """Selecting a run resolves that run's own ledger — no cross-run bleed."""
    registry, run_a, run_b, _ra, _rb = _two_repo_registry(tmp_path)
    with _running_registry(registry) as base:
        _s, _c, a = _get(base + "/api/status?run=" + run_a)
        _s, _c, b = _get(base + "/api/status?run=" + run_b)
    pa, pb = json.loads(a), json.loads(b)
    assert pa["run"]["id"] == run_a
    assert {s["story_id"] for s in pa["stories"]} == {"AAA.1-001"}
    assert pb["run"]["id"] == run_b
    assert {s["story_id"] for s in pb["stories"]} == {"BBB.1-001"}


def test_api_status_discovery_defaults_to_newest(tmp_path: Path) -> None:
    registry, _ra, run_b, _x, _y = _two_repo_registry(tmp_path)
    with _running_registry(registry) as base:
        _s, _c, latest = _get(base + "/api/status")
        _s, _c, bogus = _get(base + "/api/status?run=does-not-exist")
    assert json.loads(latest)["run"]["id"] == run_b  # newest started_at
    assert json.loads(bogus)["run"] is None           # unknown run → null


def test_api_runs_empty_registry(tmp_path: Path) -> None:
    from sdlc.registry import Registry

    registry = Registry(tmp_path / "registry.json")  # never written
    with _running_registry(registry) as base:
        _s, _c, runs = _get(base + "/api/runs")
        _s, _c, status = _get(base + "/api/status")
    assert json.loads(runs) == []
    assert json.loads(status)["run"] is None


def test_db_mode_preserves_single_run_browser(tmp_path: Path) -> None:
    """Passing a ``--db`` keeps the existing single-repo run browser behaviour."""
    db = tmp_path / ".sdlc-state.db"
    old, new = _seed_two(db)
    server = make_server(db, host="127.0.0.1", port=0)
    assert server.registry is None  # single-db mode, not registry discovery
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        _s, _c, body = _get(base + "/api/runs")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert [r["id"] for r in json.loads(body)] == [new, old]


def test_page_renders_run_repo() -> None:
    from sdlc.dashboard import _PAGE

    assert "r.repo" in _PAGE  # overview shows which repo each run belongs to


def test_log_link_carries_selected_run() -> None:
    """Log links must pass the selected run so registry mode confines correctly."""
    from sdlc.dashboard import _PAGE

    # stageCell builds the /log href; it must append the selected run id so a
    # non-newest run's transcript resolves against *that* run's logs root.
    assert "&run=" in _PAGE


def test_log_endpoint_registry_mode_resolves_selected_run(tmp_path: Path) -> None:
    """A non-newest run's transcript serves only when its run id is supplied."""
    import urllib.parse

    registry, run_a, _run_b, repo_a, _repo_b = _two_repo_registry(tmp_path)
    # run_a is the *older* run (2026-01-01); run_b is newest. Put a transcript
    # under run_a's own logs root.
    logs = Path(repo_a) / ".sdlc-state.db.logs" / "run"
    logs.mkdir(parents=True)
    transcript = logs / "AAA.1-001-build-1.log"
    transcript.write_text("RUN A TRANSCRIPT", encoding="utf-8")
    quoted = urllib.parse.quote(str(transcript.resolve()))

    with _running_registry(registry) as base:
        # With the selected run id → resolves run_a's logs root, serves it.
        s_ok, _c, body = _get(base + "/log?path=" + quoted + "&run=" + run_a)
        assert s_ok == 200 and b"RUN A TRANSCRIPT" in body
        # Without it → falls back to the newest run (run_b) whose logs root does
        # not contain this file → 404 (correct per-run confinement).
        try:
            _get(base + "/log?path=" + quoted)
            raise AssertionError("expected 404 when the run id is omitted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404


# --- live auto-refresh transport / SSE (Story 11.2-003) --------------------


def test_change_token_advances_on_event(tmp_path: Path) -> None:
    """Ledger.change_token is "0" for a missing ledger and moves with events."""
    db = tmp_path / ".sdlc-state.db"
    assert Ledger(db).change_token() == "0"  # no DB yet
    run_id = _seed(db)  # _seed logs one "run started" event
    first = Ledger(db).change_token()
    assert first != "0"
    Ledger(db).event_log(run_id, "", "info", "controller", "something happened")
    assert Ledger(db).change_token() != first  # changes on new activity


def test_change_token_advances_on_non_event_write(tmp_path: Path) -> None:
    """In-place writes the dashboard renders but that emit no event still move
    the token — otherwise the SSE stream would miss stage/story/PR/usage updates."""
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed(db)
    ledger = Ledger(db)
    base = ledger.change_token()

    # A stage transition (no event row) must change the token.
    ledger.stage_finish(run_id, "34.5-003", "build", 1, "DONE")
    after_stage = ledger.change_token()
    assert after_stage != base

    # A per-stage usage update (no event row) must change it again.
    ledger.stage_set_usage(
        run_id, "34.5-003", "build", 1, session_id="s", input_tokens=10,
        output_tokens=20, cache_read_tokens=0, cache_creation_tokens=0, cost_usd=0.01,
    )
    after_usage = ledger.change_token()
    assert after_usage != after_stage

    # A story status/PR change (no event row) must change it again.
    ledger.set_story_status(run_id, "34.5-003", "DONE")
    ledger.set_story_pr(run_id, "34.5-003", 99)
    assert ledger.change_token() != after_usage


def test_change_token_helper_single_db(tmp_path: Path) -> None:
    """_change_token reflects the ledger's max event id in single-db mode."""
    from sdlc.dashboard import _change_token

    db = tmp_path / ".sdlc-state.db"
    run_id = _seed(db)
    server = make_server(db, host="127.0.0.1", port=0)
    try:
        before = _change_token(server)
        Ledger(db).event_log(run_id, "", "info", "controller", "tick")
        after = _change_token(server)
        assert before != after  # token moves when the ledger changes
    finally:
        server.server_close()


def test_change_token_helper_registry(tmp_path: Path) -> None:
    """In registry mode the token covers every run's ledger (cross-repo)."""
    from sdlc.dashboard import _change_token

    registry, run_a, _run_b, repo_a, _repo_b = _two_repo_registry(tmp_path)
    server = make_server(db_path=None, host="127.0.0.1", port=0, registry=registry)
    try:
        before = _change_token(server)
        # Advance run_a's own ledger; the combined token must change.
        Ledger(Path(repo_a) / ".sdlc-state.db").event_log(
            run_a, "", "info", "controller", "tick"
        )
        after = _change_token(server)
        assert before != after
    finally:
        server.server_close()


def _read_sse_records(resp, *, want_changes: int = 0, want_text: str = "", deadline: float = 4.0):
    """Read an open SSE response, returning (change_tokens, raw_text).

    Stops once ``want_changes`` change-event ``data:`` tokens are collected and
    (when given) ``want_text`` appears in the raw stream, or the deadline
    elapses. Only ``change`` events carry ``data:`` lines (heartbeats are bare
    comment lines), so counting ``data:`` lines counts change pushes.
    """
    import socket
    import time as _t

    end = _t.monotonic() + deadline
    raw = ""
    tokens: list[str] = []
    while _t.monotonic() < end:
        if len(tokens) >= want_changes and (not want_text or want_text in raw):
            break
        try:
            line = resp.readline()
        except (TimeoutError, socket.timeout, OSError):
            break  # idle past the socket timeout — return what we have
        if not line:
            break
        s = line.decode("utf-8", "replace")
        raw += s
        if s.startswith("data:"):
            tokens.append(s[len("data:"):].strip())
    return tokens, raw


def test_stream_content_type_and_initial_change(tmp_path: Path) -> None:
    """/api/stream is an event-stream that pushes an initial change on connect."""
    db = tmp_path / ".sdlc-state.db"
    _seed(db)
    with _running(db, sse_poll=0.05, sse_heartbeat=5.0) as base:
        resp = urllib.request.urlopen(base + "/api/stream", timeout=3.0)  # noqa: S310
        try:
            ctype = resp.headers.get("Content-Type", "")
            tokens, raw = _read_sse_records(resp, want_changes=1, deadline=3.0)
        finally:
            resp.close()
    assert "text/event-stream" in ctype
    assert "retry:" in raw          # client reconnect hint
    assert len(tokens) >= 1         # an initial change so the page renders at once


def test_stream_pushes_change_on_new_event(tmp_path: Path) -> None:
    """A ledger write produces a fresh change token over the open stream."""
    db = tmp_path / ".sdlc-state.db"
    run_id = _seed(db)
    with _running(db, sse_poll=0.05, sse_heartbeat=5.0) as base:
        resp = urllib.request.urlopen(base + "/api/stream", timeout=3.0)  # noqa: S310
        try:
            first, _raw = _read_sse_records(resp, want_changes=1, deadline=3.0)
            Ledger(db).event_log(run_id, "", "info", "controller", "new activity")
            nxt, _raw2 = _read_sse_records(resp, want_changes=1, deadline=3.0)
        finally:
            resp.close()
    assert first and nxt          # an initial push, then one for the new write
    assert nxt[0] != first[0]     # token advanced after the write


def test_stream_heartbeat_when_idle(tmp_path: Path) -> None:
    """With no ledger changes the stream stays quiet but emits heartbeats."""
    db = tmp_path / ".sdlc-state.db"
    _seed(db)
    with _running(db, sse_poll=0.05, sse_heartbeat=0.1) as base:
        resp = urllib.request.urlopen(base + "/api/stream", timeout=3.0)  # noqa: S310
        try:
            _tokens, raw = _read_sse_records(resp, want_changes=1, want_text=": heartbeat", deadline=3.0)
        finally:
            resp.close()
    assert ": heartbeat" in raw  # idle keep-alive comment (ignored by EventSource)


def test_page_uses_eventsource_transport() -> None:
    from sdlc.dashboard import _PAGE

    assert "EventSource" in _PAGE     # server push, not just polling
    assert "/api/stream" in _PAGE     # the SSE endpoint the client subscribes to
