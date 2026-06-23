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


def test_api_payloads_carry_durations(tmp_path: Path) -> None:
    """Both /api/status and /api/runs expose duration_seconds (Story 11.2-005)."""
    db = tmp_path / ".sdlc-state.db"
    _seed(db)  # one IN_PROGRESS story with a started build stage, one DONE story
    with _running(db) as base:
        _s, _c, status_body = _get(base + "/api/status")
        _s2, _c2, runs_body = _get(base + "/api/runs")
    snap = json.loads(status_body)
    assert "duration_seconds" in snap["run"]
    assert snap["run"]["duration_seconds"] >= 0  # in-progress run → elapsed-so-far
    assert all("duration_seconds" in s for s in snap["stories"])
    runs = json.loads(runs_body)
    assert all("duration_seconds" in r for r in runs)


def test_page_includes_duration_formatter_and_column(tmp_path: Path) -> None:
    """The served page ships the shared formatter and a per-story duration header."""
    db = tmp_path / ".sdlc-state.db"
    _seed(db)
    with _running(db) as base:
        _s, _c, body = _get(base + "/")
    text = body.decode("utf-8")
    assert "function humanDuration" in text   # shared formatter present
    assert "<th>duration</th>" in text         # per-story duration column header


def test_page_renders_timestamps_in_local_time(tmp_path: Path) -> None:
    """Timestamps route through fmtLocal so the viewer sees local time (Issue #77)."""
    db = tmp_path / ".sdlc-state.db"
    _seed(db)
    with _running(db) as base:
        _s, _c, body = _get(base + "/")
    text = body.decode("utf-8")
    # The helper that converts UTC ISO timestamps to the viewer's local zone.
    assert "function fmtLocal" in text
    assert "toLocaleString()" in text
    # fmtLocal must pin the zone to UTC for the SQLite CURRENT_TIMESTAMP shape
    # ("YYYY-MM-DD HH:MM:SS", no offset): new Date() parses a bare string as
    # *local*, so without this normalization events/activity would still read
    # UTC and Issue #77 would only be half-fixed. Append "Z" after swapping the
    # space for "T" when the value carries no explicit offset.
    assert '.replace(" ", "T")' in text
    assert '+ "Z"' in text
    # Each absolute-time display site routes through fmtLocal (not raw esc()).
    assert "esc(fmtLocal(r.started_at" in text   # run sidebar started_at
    assert "esc(fmtLocal(e.ts))" in text          # event log ts
    assert "esc(fmtLocal(a.ts))" in text          # activity ts
    # Raw UTC retained in title= so the viewer can inspect the exact value.
    assert "title='" in text and "esc(r.started_at" in text   # run sidebar tooltip
    assert "title='" in text and "esc(e.ts)" in text          # event tooltip
    assert "title='" in text and "esc(a.ts)" in text          # activity tooltip


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


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo/", "owner/repo"),
        (None, None),
        ("", None),
        ("singlepart", None),
    ],
)
def test_slug_from_url(url, expected) -> None:
    from sdlc.dashboard import _slug_from_url

    assert _slug_from_url(url) == expected


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


# --- in-dashboard transcript viewer (Story 11.2-010) -----------------------


def _seed_with_transcripts(db_path: Path, run_id: str | None = None) -> tuple[str, Path]:
    """Seed a story whose stages point at on-disk transcript files.

    Returns ``(run_id, logs_dir)``. Lays down build (DONE) + a bugfix attempt so
    the viewer must enumerate every attempt, plus a coverage stage whose
    ``output_path`` points at a file that does NOT exist (missing-transcript
    placeholder case).
    """
    ledger = Ledger(db_path)
    ledger.init()
    rid = run_id or ledger.run_create("all", "parallel")
    ledger.set_total(rid, 1)
    ledger.story_upsert(rid, "70.1-001", "70", "Viewer", "high", 3, "backend", "", None, "DONE")
    logs = Path(f"{db_path}.logs") / rid
    logs.mkdir(parents=True, exist_ok=True)
    build_log = logs / "70.1-001-build-1.log"
    build_log.write_text("BUILD TRANSCRIPT", encoding="utf-8")
    ledger.stage_start(rid, "70.1-001", "build", 1)
    ledger.stage_finish(rid, "70.1-001", "build", 1, "DONE", output_path=str(build_log.resolve()))
    bug_log = logs / "70.1-001-bugfix-1.log"
    bug_log.write_text("BUGFIX TRANSCRIPT", encoding="utf-8")
    ledger.stage_start(rid, "70.1-001", "bugfix", 1)
    ledger.stage_finish(rid, "70.1-001", "bugfix", 1, "DONE", output_path=str(bug_log.resolve()))
    # A coverage stage whose recorded path was never written to disk.
    missing = logs / "70.1-001-coverage-1.log"
    ledger.stage_start(rid, "70.1-001", "coverage", 1)
    ledger.stage_finish(rid, "70.1-001", "coverage", 1, "DONE", output_path=str(missing.resolve()))
    return rid, logs


def test_api_logs_lists_story_transcripts(tmp_path: Path) -> None:
    """``/api/logs?story=`` enumerates every stage attempt with inline content."""
    db = tmp_path / ".sdlc-state.db"
    rid, _logs = _seed_with_transcripts(db)
    with _running(db) as base:
        _s, ctype, body = _get(base + "/api/logs?story=70.1-001")
    assert "application/json" in ctype
    payload = json.loads(body)
    assert payload["run"] == rid and payload["story"] == "70.1-001"
    by_stage = {(t["stage"], t["attempt"]): t for t in payload["transcripts"]}
    # build + bugfix transcripts exist and carry their content inline.
    assert by_stage[("build", 1)]["exists"] is True
    assert by_stage[("build", 1)]["content"] == "BUILD TRANSCRIPT"
    assert by_stage[("bugfix", 1)]["exists"] is True
    assert by_stage[("bugfix", 1)]["content"] == "BUGFIX TRANSCRIPT"


def test_api_logs_missing_transcript_placeholder(tmp_path: Path) -> None:
    """A stage whose recorded transcript is absent on disk → exists False, no
    content — the client renders a placeholder, never an error (200, not 500)."""
    db = tmp_path / ".sdlc-state.db"
    _seed_with_transcripts(db)
    with _running(db) as base:
        status, _c, body = _get(base + "/api/logs?story=70.1-001")
    assert status == 200
    cov = next(t for t in json.loads(body)["transcripts"] if t["stage"] == "coverage")
    assert cov["exists"] is False and cov["content"] == ""


def test_api_logs_empty_for_unknown_story(tmp_path: Path) -> None:
    """An unknown / story-less request returns an empty transcript list, not 404."""
    db = tmp_path / ".sdlc-state.db"
    _seed_with_transcripts(db)
    with _running(db) as base:
        s1, _c, b1 = _get(base + "/api/logs?story=does-not-exist")
        s2, _c, b2 = _get(base + "/api/logs")  # no story param
    assert s1 == 200 and json.loads(b1)["transcripts"] == []
    assert s2 == 200 and json.loads(b2)["transcripts"] == []


def test_api_logs_confines_to_logs_root(tmp_path: Path) -> None:
    """A ledger row whose ``output_path`` escapes the logs root is never read —
    the transcript reports ``exists: False`` rather than leaking an outside file."""
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    rid = ledger.run_create("all", "parallel")
    ledger.set_total(rid, 1)
    ledger.story_upsert(rid, "70.2-001", "70", "Escape", "high", 1, "backend", "", None, "DONE")
    outside = tmp_path / "secret.txt"  # NOT under <db>.logs
    outside.write_text("DO NOT SERVE", encoding="utf-8")
    ledger.stage_start(rid, "70.2-001", "build", 1)
    ledger.stage_finish(rid, "70.2-001", "build", 1, "DONE", output_path=str(outside.resolve()))
    with _running(db) as base:
        _s, _c, body = _get(base + "/api/logs?story=70.2-001")
    t = json.loads(body)["transcripts"][0]
    assert t["exists"] is False
    assert "DO NOT SERVE" not in t["content"]


def test_api_logs_registry_mode_per_run(tmp_path: Path) -> None:
    """In registry-discovery mode ``/api/logs`` resolves the selected run's own
    ledger + logs root, so transcripts never bleed across repos."""
    import os

    from sdlc.registry import Registry, RunRecord

    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    db_a = repo_a / ".sdlc-state.db"
    run_a, _logs = _seed_with_transcripts(db_a)
    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord(run_a, str(repo_a), str(db_a), "all", os.getpid(),
                  "DONE", "2026-01-01T00:00:00+00:00", total=1, completed=1)
    )
    with _running_registry(registry) as base:
        _s, _c, body = _get(base + "/api/logs?story=70.1-001&run=" + run_a)
    payload = json.loads(body)
    assert payload["run"] == run_a
    build = next(t for t in payload["transcripts"] if t["stage"] == "build")
    assert build["exists"] is True and build["content"] == "BUILD TRANSCRIPT"


def test_api_logs_unstarted_stage_has_no_path(tmp_path: Path) -> None:
    """A stage that started but never finished has a null ``output_path`` — the
    viewer reports it ``exists: False`` with empty content rather than trying to
    read a non-path, exactly like a missing transcript."""
    db = tmp_path / ".sdlc-state.db"
    ledger = Ledger(db)
    ledger.init()
    rid = ledger.run_create("all", "parallel")
    ledger.set_total(rid, 1)
    ledger.story_upsert(rid, "70.3-001", "70", "Unfinished", "high", 1, "backend", "", None, "IN_PROGRESS")
    ledger.stage_start(rid, "70.3-001", "build", 1)  # never finished → output_path NULL
    with _running(db) as base:
        status, _c, body = _get(base + "/api/logs?story=70.3-001")
    assert status == 200
    t = next(x for x in json.loads(body)["transcripts"] if x["stage"] == "build")
    assert t["path"] is None and t["exists"] is False and t["content"] == ""


def test_api_logs_unknown_run_in_registry_mode(tmp_path: Path) -> None:
    """In discovery mode a request naming a run the registry doesn't know about
    resolves to no ledger → an empty transcript list (HTTP 200), not an error."""
    registry, _run_a, _run_b, _ra, _rb = _two_repo_registry(tmp_path)
    with _running_registry(registry) as base:
        status, _c, body = _get(base + "/api/logs?story=AAA.1-001&run=no-such-run")
    assert status == 200
    payload = json.loads(body)
    assert payload["run"] is None and payload["transcripts"] == []


def test_api_logs_unreadable_ledger_returns_empty(monkeypatch, tmp_path: Path) -> None:
    """A ledger that raises while enumerating stages degrades to an empty list
    (HTTP 200) instead of surfacing a 500 to the viewer."""
    import sqlite3

    import sdlc.dashboard as dash

    db = tmp_path / ".sdlc-state.db"
    _seed_with_transcripts(db)

    def _raise(self, run_id):
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(dash.Ledger, "stage_breakdown", _raise)
    with _running(db) as base:
        status, _c, body = _get(base + "/api/logs?story=70.1-001")
    assert status == 200
    assert json.loads(body)["transcripts"] == []


def test_resolve_run_db_no_source_returns_none() -> None:
    """``_resolve_run_db`` with neither a registry nor a single ``db_path``
    yields ``(None, None)`` — the idle-dashboard guard for the logs viewer."""
    from types import SimpleNamespace

    from sdlc.dashboard import _Handler

    handler = _Handler.__new__(_Handler)
    handler.server = SimpleNamespace(registry=None, db_path=None)
    assert handler._resolve_run_db(None) == (None, None)


def test_page_has_session_viewer() -> None:
    """The page ships an in-dashboard session viewer: a per-story 'view session'
    control, a modal, the /api/logs fetch, and a graceful stream-json renderer —
    while preserving the new-tab /log link as a fallback (Story 11.2-010)."""
    from sdlc.dashboard import _PAGE

    assert "view-session" in _PAGE  # per-story control
    assert "sessionModal" in _PAGE  # in-dashboard modal (no leaving the page)
    assert "function openSession(" in _PAGE
    assert "/api/logs" in _PAGE  # fetches the stage→content list
    assert "renderTranscriptContent" in _PAGE  # plain-text + stream-json degrade
    assert "target='_blank'" in _PAGE  # new-tab /log fallback preserved


def test_page_session_viewer_guards_stale_fetches() -> None:
    """Opening a second story (or reselecting a run) before the first /api/logs
    resolves must not paint the slower, stale response — the viewer claims a
    monotonic token per open and drops a superseded/closed response."""
    from sdlc.dashboard import _PAGE

    assert "let sessionReq = 0;" in _PAGE  # monotonic open token
    assert "++sessionReq" in _PAGE  # each open claims the latest token
    assert "if(myReq !== sessionReq) return;" in _PAGE  # stale response is dropped
    # Closing invalidates any in-flight fetch so a late reply can't repaint.
    assert "sessionReq++; document.getElementById(\"sessionModal\").hidden = true;" in _PAGE
    # Switching runs dismisses the (run-bound) modal and invalidates its in-flight
    # fetch, so the old run's reply can never paint into the now-different run.
    assert "if(next !== sel) closeSession();" in _PAGE


# --- live story status labels (Story 11.2-009) -----------------------------


def test_page_maps_in_progress_to_started_label() -> None:
    """The dashboard renders an IN_PROGRESS story as STARTED (not TODO / not the
    raw status), via a label map the badge function reads (Story 11.2-009)."""
    from sdlc.dashboard import _PAGE

    # A label map exists and remaps IN_PROGRESS to the human-facing STARTED.
    assert "STARTED" in _PAGE
    assert '"IN_PROGRESS": "STARTED"' in _PAGE
    # The badge renders the mapped label, not the raw status text, while keeping
    # the raw status as the CSS class (so colors/styling are unchanged).
    assert "function statusLabel(" in _PAGE
    assert "statusLabel(s)" in _PAGE


def test_page_keeps_distinct_status_labels() -> None:
    """BLOCKED / NEEDS_ATTENTION / SKIPPED keep their own distinct labels — only
    IN_PROGRESS is remapped, so no real state is collapsed away (Story 11.2-009)."""
    from sdlc.dashboard import _PAGE

    # Pull out the LABELS object literal and confirm it remaps nothing else.
    start = _PAGE.index("const LABELS = {")
    end = _PAGE.index("}", start)
    labels_src = _PAGE[start:end]
    for status in ("BLOCKED", "NEEDS_ATTENTION", "SKIPPED", "TODO", "DONE", "FAILED"):
        assert status not in labels_src
    # They still each have a distinct badge style (their own colour).
    for status in (".BLOCKED", ".NEEDS_ATTENTION", ".SKIPPED", ".TODO"):
        assert status in _PAGE


def test_page_has_started_badge_style() -> None:
    """A .STARTED badge style exists alongside .IN_PROGRESS (Story 11.2-009)."""
    from sdlc.dashboard import _PAGE

    assert ".STARTED" in _PAGE


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


def test_change_token_unreadable_ledger_returns_zero(tmp_path: Path) -> None:
    """A db file that exists but is not a valid SQLite database degrades to "0"
    rather than crashing the SSE poll loop (the ``except sqlite3.Error`` path)."""
    db = tmp_path / ".sdlc-state.db"
    db.write_bytes(b"this is not a sqlite database")  # exists, but corrupt
    assert Ledger(db).change_token() == "0"


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


# --- live per-run detail: sub-stage activity (Story 11.2-004) ---------------


def test_page_renders_substage_activity() -> None:
    """The detail view binds each story's latest sub-stage activity (11.1-002)."""
    from sdlc.dashboard import _PAGE

    # The render reads the per-story `activity` the snapshot attaches and emits a
    # dedicated sub-stage row helper that the story rows append.
    assert "s.activity" in _PAGE
    assert "activityRow" in _PAGE
    assert "substage" in _PAGE  # styled sub-stage row class


def test_page_substage_render_degrades_without_data() -> None:
    """Older runs / captured-mode fallback carry no activity → no sub-stage row."""
    from sdlc.dashboard import _PAGE

    # A guard that returns empty when activity is absent keeps the detail view
    # stage-level for runs that never streamed sub-stage events.
    assert 'if(!a) return ""' in _PAGE


def _seed_substage(db_path: Path) -> str:
    """Seed a run whose in-flight story has a latest sub-stage progress event."""
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("11.2-004", "serial")
    ledger.set_total(run_id, 1)
    ledger.story_upsert(
        run_id, "s1", "epic-11", "Detail story", "Should", 3,
        "backend", "feature/s1", None, "IN_PROGRESS",
    )
    ledger.stage_start(run_id, "s1", "build", 1)
    ledger.progress_log(run_id, "s1", "build", "file_changed", "editing cli.py")
    return run_id


def test_api_status_surfaces_substage_activity(tmp_path: Path) -> None:
    """The dashboard /api/status payload carries the per-story sub-stage activity
    the detail view renders live, with stage/kind/message."""
    db = tmp_path / ".sdlc-state.db"
    _seed_substage(db)
    with _running(db) as base:
        _s, _c, body = _get(base + "/api/status")
    story = next(s for s in json.loads(body)["stories"] if s["story_id"] == "s1")
    assert story["activity"]["stage"] == "build"
    assert story["activity"]["kind"] == "file_changed"
    assert story["activity"]["message"] == "editing cli.py"


def test_api_status_activity_null_without_progress(tmp_path: Path) -> None:
    """A run with no streamed progress yields activity=None so the view degrades."""
    db = tmp_path / ".sdlc-state.db"
    _seed(db)  # no progress_log events
    with _running(db) as base:
        _s, _c, body = _get(base + "/api/status")
    for story in json.loads(body)["stories"]:
        assert story["activity"] is None


# --- project-url / project-name resolution ---------------------------------


def test_git_project_url_resolves_origin(tmp_path: Path) -> None:
    """A real repo with an ``origin`` remote resolves to its forge web base."""
    import subprocess

    from sdlc.dashboard import git_project_url

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "git@github.com:owner/repo.git"],
        check=True,
    )
    assert git_project_url(tmp_path) == "https://github.com/owner/repo"


def test_git_project_url_handles_missing_git(monkeypatch) -> None:
    """When git is absent / errors, git_project_url returns None (plain-text PRs)."""
    import sdlc.dashboard as dash

    def _raise(*_a, **_k):
        raise OSError("git not found")

    monkeypatch.setattr(dash.subprocess, "run", _raise)
    assert dash.git_project_url("/nope") is None


def test_project_name_from_url() -> None:
    """``owner/repo`` is taken from the URL when one is known."""
    from sdlc.dashboard import _project_name

    assert _project_name("https://github.com/owner/repo", Path("/x/.sdlc-state.db")) == "owner/repo"


# --- registry fallback / change-token branches -----------------------------


def test_registry_runs_view_keeps_cached_counts_on_error(monkeypatch, tmp_path: Path) -> None:
    """An unreachable per-run ledger falls back to the registry's cached counts."""
    import os
    import sqlite3

    import sdlc.dashboard as dash
    from sdlc.registry import Registry, RunRecord

    def _raise(self):
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(dash.Ledger, "list_runs", _raise)
    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord("rid", str(tmp_path), str(tmp_path / ".sdlc-state.db"), "epic-x",
                  os.getpid(), "IN_PROGRESS", "2026-01-01T00:00:00+00:00", total=7, completed=3)
    )
    rows = dash._registry_runs_view(registry)
    assert rows[0]["done"] == 3 and rows[0]["total"] == 7


def test_change_token_no_source_returns_zero() -> None:
    """Neither a registry nor a db_path → an idle dashboard stays quiet."""
    from types import SimpleNamespace

    from sdlc.dashboard import _change_token

    assert _change_token(SimpleNamespace(registry=None, db_path=None)) == "0"


def test_change_token_single_db_unreadable_returns_zero(monkeypatch, tmp_path: Path) -> None:
    """A raising ledger in single-db mode degrades the token to ``"0"``."""
    import sqlite3
    from types import SimpleNamespace

    import sdlc.dashboard as dash

    def _raise(self):
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(dash.Ledger, "change_token", _raise)
    srv = SimpleNamespace(registry=None, db_path=tmp_path / ".sdlc-state.db")
    assert dash._change_token(srv) == "0"


def test_change_token_registry_unreadable_contributes_zero(monkeypatch, tmp_path: Path) -> None:
    """A raising per-run ledger contributes ``0`` rather than breaking the stream."""
    import os
    import sqlite3
    from types import SimpleNamespace

    import sdlc.dashboard as dash
    from sdlc.registry import Registry, RunRecord

    def _raise(self):
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(dash.Ledger, "change_token", _raise)
    registry = Registry(tmp_path / "registry.json")
    registry.register(
        RunRecord("rid", str(tmp_path), str(tmp_path / ".sdlc-state.db"), "epic-x",
                  os.getpid(), "IN_PROGRESS", "2026-01-01T00:00:00+00:00", total=1, completed=0)
    )
    srv = SimpleNamespace(registry=registry, db_path=None)
    tok = dash._change_token(srv)
    assert tok.startswith("rid:") and tok.endswith(":0")


# --- lifecycle helpers: lsof, stop_dashboard error paths -------------------


def test_pids_on_port_handles_missing_lsof(monkeypatch) -> None:
    """No lsof → an empty PID list rather than an exception."""
    import sdlc.dashboard as dash

    def _raise(*_a, **_k):
        raise OSError("no lsof")

    monkeypatch.setattr(dash.subprocess, "run", _raise)
    assert dash._pids_on_port(12345) == []


def test_stop_dashboard_ignores_unreadable_pidfile(monkeypatch) -> None:
    """A garbage PID file is ignored (ValueError swallowed)."""
    import sdlc.dashboard as dash

    monkeypatch.setattr(dash, "_pids_on_port", lambda port: [])
    host, port = "127.0.0.1", 8801
    pf = dash._pidfile(host, port)
    pf.write_text("not-a-pid")
    try:
        assert dash.stop_dashboard(host, port) == 0
    finally:
        pf.unlink(missing_ok=True)


def test_stop_dashboard_handles_dead_pid(monkeypatch) -> None:
    """Signalling a PID that no longer exists is swallowed (not counted)."""
    import sdlc.dashboard as dash

    monkeypatch.setattr(dash, "_pids_on_port", lambda port: [])

    def _kill(_pid, _sig):
        raise ProcessLookupError

    monkeypatch.setattr(dash.os, "kill", _kill)
    host, port = "127.0.0.1", 8802
    pf = dash._pidfile(host, port)
    pf.write_text("2147483646")
    try:
        assert dash.stop_dashboard(host, port) == 0
    finally:
        pf.unlink(missing_ok=True)


def test_stop_dashboard_waits_for_port_to_free(monkeypatch) -> None:
    """stop_dashboard polls until the port frees, sleeping between checks."""
    import sdlc.dashboard as dash

    calls = {"n": 0}

    def _in_use(_host, _port):
        calls["n"] += 1
        return calls["n"] == 1  # busy on the first probe, free thereafter

    monkeypatch.setattr(dash, "_port_in_use", _in_use)
    monkeypatch.setattr(dash, "_pids_on_port", lambda port: [])
    monkeypatch.setattr(dash.time, "sleep", lambda _s: None)
    host, port = "127.0.0.1", 8803
    dash._pidfile(host, port).unlink(missing_ok=True)
    assert dash.stop_dashboard(host, port) == 0
    assert calls["n"] >= 2  # looped at least once, exercising the wait


# --- favicon + log-root-None branch ----------------------------------------


def test_favicon_returns_no_content(tmp_path: Path) -> None:
    db = tmp_path / ".sdlc-state.db"
    _seed(db)
    with _running(db) as base:
        status, _ctype, body = _get(base + "/favicon.ico")
    assert status == 204 and body == b""


def test_log_endpoint_404_when_no_run_resolvable(tmp_path: Path) -> None:
    """In discovery mode with no runs, /log has no logs root → 404."""
    from sdlc.registry import Registry

    registry = Registry(tmp_path / "registry.json")  # empty
    with _running_registry(registry) as base:
        try:
            _get(base + "/log?path=/whatever")
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404


# --- serve() lifecycle -----------------------------------------------------


def test_serve_runs_until_interrupt(tmp_path: Path, monkeypatch) -> None:
    """serve() writes a PID file, opens the browser, and cleans up on Ctrl-C."""
    from http.server import ThreadingHTTPServer

    import sdlc.dashboard as dash

    db = tmp_path / ".sdlc-state.db"
    _seed(db)
    opened: list[str] = []
    monkeypatch.setattr(dash.webbrowser, "open", lambda url: opened.append(url))

    def _interrupt(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", _interrupt)

    dash.serve(db, host="127.0.0.1", port=0, open_browser=True)

    assert opened  # browser was opened
    assert not dash._pidfile("127.0.0.1", 0).exists()  # PID file cleaned up


def test_serve_sigterm_handler_raises_keyboard_interrupt(tmp_path: Path, monkeypatch) -> None:
    """The SIGTERM handler serve() installs re-raises as KeyboardInterrupt.

    `--stop`/`--restart` send SIGTERM; serve() maps it onto KeyboardInterrupt so
    the same finally-block cleanup (PID file removal) runs as for Ctrl-C.
    """
    import signal as signal_mod
    from http.server import ThreadingHTTPServer

    import sdlc.dashboard as dash

    db = tmp_path / ".sdlc-state.db"
    _seed(db)

    captured: dict[int, object] = {}

    def _record(signum, handler):
        captured[signum] = handler

    monkeypatch.setattr(dash.signal, "signal", _record)

    def _interrupt(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", _interrupt)

    dash.serve(db, host="127.0.0.1", port=0, open_browser=False)

    handler = captured.get(signal_mod.SIGTERM)
    assert callable(handler)
    with pytest.raises(KeyboardInterrupt):
        handler(signal_mod.SIGTERM, None)


def test_serve_skips_signal_off_main_thread(tmp_path: Path, monkeypatch) -> None:
    """Off the main thread, signal registration is skipped without error."""
    from http.server import ThreadingHTTPServer

    import sdlc.dashboard as dash

    db = tmp_path / ".sdlc-state.db"
    _seed(db)

    def _interrupt(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", _interrupt)
    errors: list[BaseException] = []

    def _run():
        try:
            dash.serve(db, host="127.0.0.1", port=0, open_browser=False)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors  # ValueError from signal.signal off-main-thread is swallowed


# --- SSE write-failure paths (client disconnects mid-stream) ----------------


class _FailingWFile:
    """A wfile whose writes succeed until ``fail_after`` calls, then break."""

    def __init__(self, fail_after: int) -> None:
        self.n = 0
        self.fail_after = fail_after

    def write(self, _b) -> None:
        self.n += 1
        if self.n > self.fail_after:
            raise BrokenPipeError("client gone")

    def flush(self) -> None:
        pass


def _stream_handler(fail_after: int):
    """A bare _Handler wired to a failing wfile, ready to call _serve_stream."""
    from types import SimpleNamespace

    from sdlc.dashboard import _Handler

    h = _Handler.__new__(_Handler)
    h.wfile = _FailingWFile(fail_after)
    h.requestline = "GET /api/stream HTTP/1.1"
    h.request_version = "HTTP/1.1"
    h.server = SimpleNamespace(
        registry=None, db_path=None, sse_poll_interval=0.01,
        sse_heartbeat_interval=0.05, _sse_stop=False,
    )
    return h


def test_serve_stream_returns_when_headers_fail() -> None:
    """A client that drops before headers flush ends the stream cleanly."""
    h = _stream_handler(fail_after=0)  # the header flush is the first write → fails
    h._serve_stream()  # must return, not raise


def test_serve_stream_returns_when_retry_hint_fails() -> None:
    """A drop right after headers (on the retry hint) ends the stream cleanly."""
    h = _stream_handler(fail_after=1)  # headers ok, retry-hint write fails
    h._serve_stream()


def test_serve_stream_returns_when_change_push_fails() -> None:
    """A drop on the first change push ends the stream cleanly."""
    h = _stream_handler(fail_after=2)  # headers + retry ok, change push fails
    h._serve_stream()


def test_migrate_registry_ledgers_dedups_and_tolerates_bad_ledgers(tmp_path: Path) -> None:
    """Each distinct DB is migrated once; unreachable/corrupt ledgers are skipped.

    Two records sharing one DB exercise the dedup ``continue``; a record pointing
    at a directory (which exists but cannot be opened as SQLite) exercises the
    best-effort ``except`` so a bad ledger never crashes the dashboard.
    """
    import os

    from sdlc.dashboard import _migrate_registry_ledgers
    from sdlc.registry import Registry, RunRecord

    repo = tmp_path / "repo"
    repo.mkdir()
    db = repo / ".sdlc-state.db"
    run_id = _seed_run(db, "epic-aaa", "AAA.1-001", "IN_PROGRESS")

    # A path that exists but is not a SQLite file → open raises sqlite3.Error.
    bad_db = tmp_path / "not-a-db"
    bad_db.mkdir()

    registry = Registry(tmp_path / "registry.json")
    started = "2026-01-01T00:00:00+00:00"
    registry.register(
        RunRecord(run_id, str(repo), str(db), "epic-aaa", os.getpid(),
                  "IN_PROGRESS", started, total=1, completed=0)
    )
    # Second record on the SAME db → the dedup branch (seen) skips re-migration.
    registry.register(
        RunRecord("dup-run", str(repo), str(db), "epic-aaa", os.getpid(),
                  "IN_PROGRESS", started, total=1, completed=0)
    )
    # Record on an unopenable db → the except branch leaves it untouched.
    registry.register(
        RunRecord("bad-run", str(tmp_path / "broken"), str(bad_db), "epic-bbb",
                  os.getpid(), "IN_PROGRESS", started, total=1, completed=0)
    )

    # Must complete without raising despite the bad ledger.
    _migrate_registry_ledgers(registry)


# --- GitHub repo health (Story 11.2-006) -----------------------------------


def _git_repo_with_origin(path: Path, remote: str) -> None:
    """Init a git repo at ``path`` with an ``origin`` remote (for slug tests)."""
    import subprocess

    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", remote], check=True
    )


def test_repo_slug_from_origin(tmp_path: Path) -> None:
    """The ``owner/repo`` slug is derived from the run's git remote."""
    from sdlc.dashboard import repo_slug

    _git_repo_with_origin(tmp_path, "git@github.com:fxmartin/claude-code-config.git")
    assert repo_slug(tmp_path) == "fxmartin/claude-code-config"


def test_repo_slug_none_without_remote(tmp_path: Path) -> None:
    """No git remote → no slug (the badge then degrades to unavailable)."""
    from sdlc.dashboard import repo_slug

    assert repo_slug(tmp_path) is None


class _StubCache:
    """A GitHub cache double: records slug lookups, returns canned stats."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def get(self, slug):
        self.calls.append(slug)
        if not slug:
            return {"available": False, "slug": None, "reason": "no-remote"}
        return {"available": True, "slug": slug, "issues_open": 4, "prs_open": 1,
                "ci_status": "success", "ci_branch": "main", "ci_created_at": "t"}


@contextmanager
def _running_registry_gh(registry, cache):
    """Registry-mode dashboard with an injected GitHub stats cache."""
    server = make_server(db_path=None, host="127.0.0.1", port=0, registry=registry)
    server.github_cache = cache  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_api_github_registry_mode_resolves_repo(tmp_path: Path) -> None:
    """``/api/github?run=`` resolves the selected run's repo slug + full stats."""
    registry, run_a, _run_b, repo_a, _repo_b = _two_repo_registry(tmp_path)
    _git_repo_with_origin(Path(repo_a), "git@github.com:acme/repo-a.git")
    cache = _StubCache()
    with _running_registry_gh(registry, cache) as base:
        _s, ctype, body = _get(base + "/api/github?run=" + run_a)
    assert "application/json" in ctype
    payload = json.loads(body)
    assert payload["available"] is True
    assert payload["slug"] == "acme/repo-a"
    assert cache.calls == ["acme/repo-a"]


def test_api_github_unknown_run_is_unavailable(tmp_path: Path) -> None:
    registry, _ra, _rb, _x, _y = _two_repo_registry(tmp_path)
    cache = _StubCache()
    with _running_registry_gh(registry, cache) as base:
        _s, _c, body = _get(base + "/api/github?run=does-not-exist")
    assert json.loads(body)["available"] is False


def test_overview_rows_carry_github_deduped_per_repo(tmp_path: Path) -> None:
    """Two runs in one repo share a single per-repo fetch (deduped by slug)."""
    import os

    import sdlc.dashboard as dash
    from sdlc.registry import Registry, RunRecord

    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo_with_origin(repo, "git@github.com:acme/shared.git")
    db = repo / ".sdlc-state.db"
    run1 = _seed_run(db, "epic-x", "X.1-001", "IN_PROGRESS")
    # A second registered run pointing at the SAME repo/ledger.
    Ledger(db).story_upsert(run1, "X.1-002", "epic-x", "Two", "med", 1, "backend", "", None, "TODO")
    run2 = Ledger(db).run_create("epic-x", "parallel")

    registry = Registry(tmp_path / "registry.json")
    for rid, started in ((run1, "2026-01-01T00:00:00+00:00"), (run2, "2026-01-02T00:00:00+00:00")):
        registry.register(
            RunRecord(rid, str(repo), str(db), "epic-x", os.getpid(),
                      "IN_PROGRESS", started, total=1, completed=0)
        )

    cache = _StubCache()
    rows = dash._registry_runs_view(registry, cache)
    assert len(rows) == 2
    # Both rows carry the same repo's stats…
    assert all(r["github"]["slug"] == "acme/shared" for r in rows)
    # …but the slug was resolved once and the cache hit once per repo, not per run.
    assert cache.calls == ["acme/shared"]


def test_registry_runs_view_without_github_omits_field(tmp_path: Path) -> None:
    """Backwards-compatible: no cache passed → rows have no ``github`` key."""
    import sdlc.dashboard as dash

    registry, _a, _b, _x, _y = _two_repo_registry(tmp_path)
    rows = dash._registry_runs_view(registry)
    assert rows and all("github" not in r for r in rows)


def test_api_github_single_db_mode(tmp_path: Path) -> None:
    """Single-``--db`` mode resolves the slug from the ledger's parent repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo_with_origin(repo, "git@github.com:acme/solo.git")
    db = repo / ".sdlc-state.db"
    _seed(db)
    server = make_server(db, host="127.0.0.1", port=0)
    cache = _StubCache()
    server.github_cache = cache  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        _s, _c, body = _get(base + "/api/github")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert json.loads(body)["slug"] == "acme/solo"
    assert cache.calls == ["acme/solo"]


def test_github_stats_single_db_mode_without_db_path_is_no_run() -> None:
    """Single-db mode with no ledger path degrades to the ``no-run`` sentinel."""
    from types import SimpleNamespace

    from sdlc.dashboard import _Handler

    h = _Handler.__new__(_Handler)
    h.server = SimpleNamespace(registry=None, db_path=None, github_cache=_StubCache())
    result = h._github_stats(None)
    assert result["available"] is False
    assert result["reason"] == "no-run"


def test_make_server_creates_github_cache_both_modes(tmp_path: Path) -> None:
    """A real GitHubStatsCache is attached in both single-db and registry modes."""
    from sdlc.github_stats import GitHubStatsCache

    db = tmp_path / ".sdlc-state.db"
    _seed(db)
    single = make_server(db, host="127.0.0.1", port=0)
    assert isinstance(single.github_cache, GitHubStatsCache)
    single.server_close()

    from sdlc.registry import Registry

    reg = Registry(tmp_path / "registry.json")
    disco = make_server(db_path=None, host="127.0.0.1", port=0, registry=reg)
    assert isinstance(disco.github_cache, GitHubStatsCache)
    disco.server_close()


def test_page_has_github_panel_and_badge_hooks() -> None:
    """The page ships the GitHub render hooks (panel container + fetch route)."""
    from sdlc.dashboard import _PAGE

    assert "/api/github" in _PAGE
    assert 'id="github"' in _PAGE


def test_page_refreshes_github_on_a_steady_cadence() -> None:
    """GitHub data is time-based, so the page must re-tick independent of SSE.

    The SSE stream only fires on ledger movement; without a steady poll a quiet
    run's GitHub badges/panel would never hit the ~60 s backend-cache refresh.
    """
    from sdlc.dashboard import _PAGE

    assert "GH_REFRESH_INTERVAL" in _PAGE
    assert "setInterval(tick, GH_REFRESH_INTERVAL)" in _PAGE


# --- Story 11.2-008: wave-column dependency DAG ----------------------------


def _seed_waves(db_path: Path) -> str:
    """A three-wave queue: A (w0) → {B,C} (w1) → D (w2).

    B and C share wave 1 (run in parallel), both depend on A; D depends on B.
    Mirrors how ``run_build`` records ``compute_cohorts`` output via
    :meth:`Ledger.set_story_wave`.
    """
    ledger = Ledger(db_path)
    ledger.init()
    run_id = ledger.run_create("all", "parallel")
    ledger.set_total(run_id, 4)
    for sid, title, status in (
        ("1.1-001", "Foundation", "DONE"),
        ("1.1-002", "Left branch", "IN_PROGRESS"),
        ("1.1-003", "Right branch", "TODO"),
        ("1.1-004", "Join", "TODO"),
    ):
        ledger.story_upsert(run_id, sid, "1", title, "high", 2, "backend", "", None, status)
    ledger.set_story_wave(run_id, "1.1-001", 0, [])
    ledger.set_story_wave(run_id, "1.1-002", 1, ["1.1-001"])
    ledger.set_story_wave(run_id, "1.1-003", 1, ["1.1-001"])
    ledger.set_story_wave(run_id, "1.1-004", 2, ["1.1-002"])
    return run_id


def test_dag_layout_assigns_waves_to_columns_in_order() -> None:
    """Each wave is a column, left→right = execution order; rows preserve input order."""
    from sdlc.dashboard import dag_layout

    stories = [
        {"story_id": "a", "wave": 0, "dependencies": []},
        {"story_id": "b", "wave": 1, "dependencies": ["a"]},
        {"story_id": "c", "wave": 1, "dependencies": ["a"]},
        {"story_id": "d", "wave": 2, "dependencies": ["b"]},
    ]
    dag = dag_layout(stories)
    assert dag["available"] is True
    assert [w["index"] for w in dag["waves"]] == [0, 1, 2]
    assert dag["waves"][0]["stories"] == ["a"]
    assert dag["waves"][1]["stories"] == ["b", "c"]  # same wave, input order
    assert dag["waves"][2]["stories"] == ["d"]


def test_dag_layout_derives_edges_from_in_queue_deps_only() -> None:
    """Edges connect a dependency (upstream) to its dependent; out-of-queue deps drop."""
    from sdlc.dashboard import dag_layout

    stories = [
        {"story_id": "a", "wave": 0, "dependencies": []},
        {"story_id": "b", "wave": 1, "dependencies": ["a", "merged-elsewhere"]},
    ]
    dag = dag_layout(stories)
    assert {"from": "a", "to": "b"} in dag["edges"]
    # The out-of-queue dependency is not a node, so no dangling edge is emitted.
    assert all(e["from"] != "merged-elsewhere" for e in dag["edges"])
    assert len(dag["edges"]) == 1


def test_dag_layout_degrades_when_no_wave_data() -> None:
    """Older ledger / unpersisted run: no wave anywhere → not available (flat fallback)."""
    from sdlc.dashboard import dag_layout

    stories = [
        {"story_id": "a", "wave": None, "dependencies": []},
        {"story_id": "b", "wave": None, "dependencies": []},
    ]
    dag = dag_layout(stories)
    assert dag["available"] is False
    assert dag["waves"] == []
    assert dag["edges"] == []


def test_dag_layout_sequential_run_is_one_story_per_wave() -> None:
    """A --sequential run records ascending waves: each story its own column."""
    from sdlc.dashboard import dag_layout

    stories = [
        {"story_id": "a", "wave": 0, "dependencies": []},
        {"story_id": "b", "wave": 1, "dependencies": ["a"]},
        {"story_id": "c", "wave": 2, "dependencies": ["b"]},
    ]
    dag = dag_layout(stories)
    assert dag["available"] is True
    assert [w["stories"] for w in dag["waves"]] == [["a"], ["b"], ["c"]]


def test_dag_layout_handles_empty_stories() -> None:
    from sdlc.dashboard import dag_layout

    dag = dag_layout([])
    assert dag == {"available": False, "waves": [], "edges": []}


def test_api_status_includes_dag(tmp_path: Path) -> None:
    """The /api/status payload exposes the wave-column DAG layout for the run."""
    db = tmp_path / "ledger.db"
    run_id = _seed_waves(db)
    with _running(db) as base:
        status, _, body = _get(f"{base}/api/status?run={run_id}")
    assert status == 200
    dag = json.loads(body)["dag"]
    assert dag["available"] is True
    assert [w["index"] for w in dag["waves"]] == [0, 1, 2]
    assert dag["waves"][1]["stories"] == ["1.1-002", "1.1-003"]
    assert {"from": "1.1-001", "to": "1.1-002"} in dag["edges"]
    assert {"from": "1.1-002", "to": "1.1-004"} in dag["edges"]


def test_api_status_dag_degrades_without_wave_data(tmp_path: Path) -> None:
    """A run with no recorded waves still serves a dag block (unavailable, no error)."""
    db = tmp_path / "ledger.db"
    run_id = _seed(db)  # seeds stories but never calls set_story_wave
    with _running(db) as base:
        status, _, body = _get(f"{base}/api/status?run={run_id}")
    assert status == 200
    assert json.loads(body)["dag"]["available"] is False


def test_page_renders_wave_dag() -> None:
    """The page ships the DAG render hooks: container, renderer, SVG edges, no graph lib."""
    from sdlc.dashboard import _PAGE

    assert 'id="dag"' in _PAGE
    assert "function renderDag(" in _PAGE
    assert "renderDag(d)" in _PAGE              # invoked from the main render path
    assert "d.dag" in _PAGE                      # reads the layout from the payload
    assert "runs in parallel" in _PAGE          # wave header copy
    assert "<svg" in _PAGE                       # inline SVG edges, no external lib
    assert "<path" in _PAGE                      # edges as SVG connectors


# --- stable-height live regions (Story 11.2-011) ---------------------------


def test_page_reserves_stable_height_for_live_regions() -> None:
    """The auto-updating regions reserve a stable height so a content swap that
    toggles between 1, 2 and 3 lines never reflows the elements below them — the
    page does not jump and a scrolled view does not shift (Story 11.2-011)."""
    from sdlc.dashboard import _PAGE

    # #head (run summary: run line + config line + usage line) reserves space
    # for its 3-line maximum, so a 1↔2↔3-line swap keeps its box height stable.
    assert "#head { min-height:" in _PAGE
    # The #updated clock line keeps a stable single-line height across ticks.
    assert "#updated { min-height:" in _PAGE
    # The per-story sub-stage activity line is clamped to a single line so a long
    # milestone is clipped rather than growing the row. The line-clamp idiom is
    # used (not nowrap + text-overflow), because in an auto-layout table a nowrap
    # cell widens the column instead of ellipsizing — verified live with a long
    # activity message (the row stays one line and the table does not overflow).
    assert ".substage .act {" in _PAGE
    assert "-webkit-line-clamp: 1" in _PAGE
    assert "overflow: hidden" in _PAGE


def test_activity_row_wraps_content_for_clamping() -> None:
    """activityRow wraps its content in the clamped `.act` element (with the full
    message available on hover) so the substage line's height stays stable across
    SSE ticks instead of wrapping to 2–3 lines (Story 11.2-011)."""
    from sdlc.dashboard import _PAGE

    assert "<div class='act'" in _PAGE  # clamped wrapper around the activity content
    # The full (un-clamped) message is exposed on hover, matching the no-reflow
    # truncation pattern used elsewhere on the dashboard.
    assert "title='\"+esc(a.message)+\"'" in _PAGE
