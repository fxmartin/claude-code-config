# ABOUTME: Local progress dashboard — a stdlib http.server view over the ledger.
# ABOUTME: Serves `/` (HTML), `/api/status` (snapshot), `/api/runs` (run history).
#
# This decouples progress display from any agent/turn loop: a build writes the
# SQLite ledger, this server reads it read-only, and the browser polls it. No web
# framework — Python stdlib only, to keep the controller's dependency footprint
# minimal. The ledger is per-repo, so the runs list is that repo's build history.

from __future__ import annotations

import html
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from sdlc import __version__, github_stats
from sdlc.build import _EMPTY_COUNTS, Ledger, _duration_seconds, status_snapshot
from sdlc.issue_host import GITHUB, detect_host
from sdlc.portfolio import portfolio_view
from sdlc.registry import Registry, RunRecord, derive_state

# scp-like remote: git@host:owner/sub/repo.git
_SCP_REMOTE = re.compile(r"^[\w.-]+@([\w.-]+):(.+?)(?:\.git)?/?$")
# url remote: ssh|https|http://[user[:pw]@]host[:port]/owner/sub/repo[.git]
_URL_REMOTE = re.compile(r"^(?:ssh|https?)://(?:[^@/]+@)?([\w.-]+)(?::\d+)?/(.+?)(?:\.git)?/?$")


def _web_url_from_remote(remote: str) -> str | None:
    """Turn a git ``origin`` URL into the forge project web base, or None.

    Handles ``git@host:owner/repo.git``, ``ssh://git@host[:port]/owner/repo.git``
    and ``https://[user[:token]@]host/owner/repo[.git]`` → ``https://host/owner/repo``.
    """
    remote = remote.strip()
    if not remote:
        return None
    for pattern in (_SCP_REMOTE, _URL_REMOTE):
        m = pattern.match(remote)
        if m:
            return f"https://{m.group(1)}/{m.group(2)}"
    return None


def git_project_url(root: str | Path) -> str | None:
    """Resolve the project's GitHub web base from ``git remote get-url origin``.

    Returns None when ``root`` is not a git repo / has no origin / git is absent
    — the dashboard then renders PR numbers as plain text.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return _web_url_from_remote(out.stdout)


def _project_name(project_url: str | None, db_path: Path) -> str:
    """Repo label for the header: ``owner/repo`` from the URL, else the repo dir."""
    if project_url:
        return "/".join(project_url.rstrip("/").split("/")[-2:])
    return db_path.resolve().parent.name


def _slug_from_url(project_url: str | None) -> str | None:
    """``owner/repo`` GitHub slug from a forge web base, or None."""
    if not project_url:
        return None
    parts = project_url.rstrip("/").split("/")
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:])


def repo_slug(root: str | Path) -> str | None:
    """Derive the run's ``owner/repo`` GitHub slug from its git remote.

    Reuses :func:`git_project_url` (and thus the shared remote regexes), so the
    slug resolution matches the PR-link/header resolution exactly. Returns None
    when ``root`` is not a git repo / has no origin — the GitHub badge/panel then
    degrades to the "unavailable" state.
    """
    return _slug_from_url(git_project_url(root))


def repo_host(root: str | Path) -> str:
    """Detect the run's code host (``github``/``gitlab``) from its git remote.

    Story 23.7-001: the repo-health surface fetches via the host's CLI, so it
    must know which forge a run targets. Defaults to GitHub when the host can't
    be determined (no remote, or an unrecognised host), so a GitHub repo — and
    any ambiguous remote — behaves exactly as before this story.
    """
    return detect_host(root) or GITHUB


# --- multi-run registry discovery (Story 11.2-002) -------------------------
# In discovery mode the dashboard has no single ledger: it reads the host-level
# registry to find every `sdlc build` across repos, and resolves each run's own
# ledger on demand. The per-repo ledger stays authoritative for run detail.


def _registry_runs_view(
    registry: Registry, github: "github_stats.GitHubStatsCache | None" = None
) -> list[dict]:
    """Normalize the host-level registry into the runs-browser row shape.

    Each row carries the cross-repo discovery fields (``repo``, ``scope``) plus
    the derived effective ``status`` and live ``done``/``total`` read from that
    run's own ledger when reachable — falling back to the registry's cached
    counts when a ledger is missing/corrupt (the registry is best-effort).
    Newest run first.

    When a ``github`` cache is supplied, each row gains a compact ``github``
    summary (issues/PRs/CI) for its repo. The repo→slug resolution and the cache
    read are **deduped per repo** within one view, so N runs in one repo cost a
    single slug lookup and a single (cached) fetch — never one per run.
    """
    rows: list[dict] = []
    gh_by_repo: dict[str, dict] = {}
    for rec in registry.records():
        done, total = rec.completed, rec.total
        try:
            for r in Ledger(rec.db).list_runs():
                if r["id"] == rec.run_id:
                    done, total = r["done"], r["total"]
                    break
        except (OSError, sqlite3.Error):
            pass  # unreachable ledger → keep the registry's cached counts
        row = {
            "id": rec.run_id,
            "repo": rec.repo,
            "scope": rec.scope,
            "status": derive_state(rec),
            "started_at": rec.started_at,
            "finished_at": rec.finished_at,
            "duration_seconds": _duration_seconds(rec.started_at, rec.finished_at),
            "done": done,
            "total": total,
        }
        if github is not None:
            if rec.repo not in gh_by_repo:
                gh_by_repo[rec.repo] = github.get(repo_slug(rec.repo), repo_host(rec.repo))
            row["github"] = gh_by_repo[rec.repo]
        rows.append(row)
    rows.sort(key=lambda r: (r["started_at"] or ""), reverse=True)
    return rows


# --- wave-column dependency DAG (Story 11.2-008) ---------------------------
# The per-run detail renders a DAG where each column is a cohort wave (left→right
# = execution order), each node a story, and each edge a story→dependency link.
# The layout is fully constrained by the wave index recorded in 11.2-007, so no
# graph-layout library is needed: this helper turns the story rows into the
# column/row/edge structure the client paints with inline SVG + HTML.


def dag_layout(stories: list[dict]) -> dict:
    """Group ``stories`` into wave columns with intra-queue dependency edges.

    Returns ``{"available", "waves", "edges"}`` where ``waves`` is an
    execution-ordered list of ``{"index", "stories": [story_id, …]}`` (stories
    sharing a wave run in parallel, kept in input order = order-within-wave) and
    ``edges`` is a list of ``{"from", "to"}`` story-id pairs, one per dependency
    that is itself in the queue. Only in-queue edges are emitted, matching
    ``compute_cohorts`` semantics — a dependency on an already-merged
    out-of-queue story is not a node, so it produces no dangling edge.

    Degrades gracefully: when no story carries a wave (an older ledger or a run
    scheduled before 11.2-007 persisted waves), ``available`` is False and the
    client falls back to the flat story list. A rare story with a NULL wave in
    an otherwise-waved run is coerced to wave 0 so no node is dropped.
    """
    present = {s["story_id"] for s in stories if s.get("story_id")}
    if not any(s.get("wave") is not None for s in stories):
        return {"available": False, "waves": [], "edges": []}
    by_wave: dict[int, list[str]] = {}
    for s in stories:
        wave = s.get("wave")
        by_wave.setdefault(int(wave) if wave is not None else 0, []).append(s["story_id"])
    waves = [{"index": w, "stories": by_wave[w]} for w in sorted(by_wave)]
    edges = [
        {"from": dep, "to": s["story_id"]}
        for s in stories
        for dep in (s.get("dependencies") or [])
        if dep in present
    ]
    return {"available": True, "waves": waves, "edges": edges}


# --- live transport: change detection (Story 11.2-003) ---------------------
# The SSE endpoint pushes a delta only when the ledger actually moves. We avoid
# diffing rows by collapsing all activity into a cheap token; when it changes,
# the client refetches the snapshot it already knows how to render (idempotent),
# which keeps the transport simple and free of duplicate-row bugs on reconnect.


def _change_token(server) -> str:
    """A cheap token of ledger activity used by the SSE stream to detect change.

    Single-db mode: the ledger's max event id (as a string). Registry-discovery
    mode: a digest of every run's id, derived status, and its own ledger's max
    event id — so the stream also fires when a run is added/removed or changes
    state across repos. An unreachable ledger contributes 0 rather than breaking
    the stream (the registry is best-effort). Returns ``"0"`` when there is no
    source yet, so an idle dashboard simply stays quiet.
    """
    registry = getattr(server, "registry", None)
    if registry is not None:
        parts: list[str] = []
        for rec in registry.records():
            try:
                tok = Ledger(rec.db).change_token()
            except (OSError, sqlite3.Error):
                tok = "0"
            parts.append(f"{rec.run_id}:{derive_state(rec)}:{tok}")
        return "|".join(parts)
    db_path = getattr(server, "db_path", None)
    if db_path is None:
        return "0"
    try:
        return Ledger(db_path).change_token()
    except (OSError, sqlite3.Error):
        return "0"


# Live transport tuning (overridable per-server, mostly for tests). The poll
# interval bounds time-to-push (~1s ⇒ well under the 2s target); the heartbeat
# keeps the connection (and any intermediary) alive while idle without traffic.
_SSE_POLL_INTERVAL = 1.0
_SSE_HEARTBEAT_INTERVAL = 15.0


# --- lifecycle: stop/restart -----------------------------------------------
# A dashboard is detached when launched in the background, so we record its PID
# (keyed by host:port) and provide --stop/--restart. lsof is a fallback so we
# can also stop a dashboard started by an older version (no PID file).


def _pidfile(host: str, port: int) -> Path:
    return Path(tempfile.gettempdir()) / f"sdlc-dashboard-{host}-{port}.pid"


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _pids_on_port(port: int) -> list[int]:
    """Listening PIDs on ``port`` via lsof; empty when lsof is unavailable."""
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


def stop_dashboard(host: str = "127.0.0.1", port: int = 8787) -> int:
    """Stop any dashboard on ``host:port``. Returns how many processes were signalled.

    Sends SIGTERM to the recorded PID and to any listener lsof finds on the port,
    removes the PID file, and waits (briefly) for the port to free.
    """
    pids: set[int] = set(_pids_on_port(port))
    pf = _pidfile(host, port)
    if pf.exists():
        try:
            pids.add(int(pf.read_text().strip()))
        except (ValueError, OSError):
            pass

    stopped = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            stopped += 1
        except (ProcessLookupError, PermissionError):
            pass
    pf.unlink(missing_ok=True)

    for _ in range(20):  # up to ~2s for the port to free
        if not _port_in_use(host, port):
            break
        time.sleep(0.1)
    return stopped


# Self-contained page: inline CSS + JS, no CDN/external assets (offline-safe).
# Catppuccin Latte palette. Polls /api/runs + /api/status every 2.5s.
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Autonomous SDLC</title>
<style>
  :root { color-scheme: light;
    --base:#eff1f5; --mantle:#e6e9ef; --crust:#dce0e8; --text:#4c4f69; --sub:#6c6f85;
    --surface:#ccd0da; --overlay:#9ca0b0; --blue:#1e66f5; --green:#40a02b; --red:#d20f39;
    --peach:#fe640b; --mauve:#8839ef; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
         background: var(--base); color: var(--text); }
  .topbar { display: flex; align-items: center; justify-content: space-between;
            gap: 16px; flex-wrap: wrap; padding: 12px 24px; background: var(--mantle);
            border-bottom: 1px solid var(--surface); }
  .brand { font-weight: 700; font-size: 15px; letter-spacing: .02em; }
  .brand .tld { color: var(--blue); }
  .brand .ver { color: var(--sub); font-weight: 500; font-size: 12px; margin-left: 4px; }
  .wrap { display: flex; min-height: 100vh; }
  .side { width: 264px; flex: none; background: var(--mantle);
          border-right: 1px solid var(--surface); padding: 16px; overflow: auto; }
  .side h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .05em;
             color: var(--sub); margin: 0 0 10px; }
  .run { padding: 8px 10px; border-radius: 8px; cursor: pointer; margin-bottom: 6px; }
  .run:hover { background: var(--crust); }
  .run.active { background: var(--surface); }
  /* Story 19.2-001: an active (building) run carries a blue left-accent border
     and a pulsing live dot, so in-progress runs are recognizable at a glance
     without reading the badge. This is a separate visual channel from .active
     (selected-for-viewing, a background fill) — the two mean different things
     (building vs currently-viewed) and a run can read as both at once. The 7px
     left padding offsets the 3px border so card text stays aligned. */
  .run--live { border-left: 3px solid var(--blue); padding-left: 7px; }
  .run--live::before { content: "\\25cf"; color: var(--blue); margin-right: 6px;
                       animation: run-pulse 1.4s ease-in-out infinite; }
  @keyframes run-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .2; } }
  @media (prefers-reduced-motion: reduce) {
    .run--live::before { animation: none; }
  }
  .main { flex: 1; padding: 24px; overflow: auto; }
  h1 { font-size: 15px; margin: 0 0 4px; font-weight: 600; }
  .muted { color: var(--sub); } .small { font-size: 12px; }
  .bar { height: 10px; background: var(--surface); border-radius: 6px; overflow: hidden; margin: 12px 0; }
  .bar > span { display: block; height: 100%; background: var(--green); transition: width .4s; }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
  .chip { padding: 2px 10px; border-radius: 12px; background: var(--mantle);
          border: 1px solid var(--surface); font-size: 12px; }
  table { border-collapse: collapse; width: 100%; margin-top: 12px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--surface); }
  th { color: var(--sub); font-weight: 600; font-size: 12px; }
  .badge { padding: 1px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }
  .DONE { background: #e6f4ea; color: var(--green); }
  /* Story 11.2-009: IN_PROGRESS reads as STARTED in the UI; both selectors share
     one style so the colour is identical whether the class is the raw status or
     the display label. */
  .IN_PROGRESS, .STARTED { background: #e4ecfd; color: var(--blue); }
  .FAILED { background: #fbe3e8; color: var(--red); }
  .BLOCKED { background: #fdeede; color: var(--peach); }
  .NEEDS_ATTENTION { background: #fdeede; color: var(--peach); }
  .AWAITING_APPROVAL { background: #e9e7fd; color: var(--mauve); }
  /* Story 14.1-003: a rate-limit pause is waiting for time, not human attention
     — its own distinct (yellow) badge, never styled like NEEDS_ATTENTION. */
  .RATE_LIMITED { background: #fdf6e3; color: #df8e1d; }
  .SKIPPED { background: var(--surface); color: var(--sub); }
  .TODO { background: var(--crust); color: var(--overlay); }
  .PENDING { background: var(--crust); color: var(--overlay); }
  /* Story 11.2-014: the activity line is the third row of a story's stacked
     block — no inner border below it; the next block's title-row border-top is
     the separator (see .story-title below). */
  .substage > td { border-bottom: none; padding-top: 0; color: var(--sub); }
  /* Story 11.2-014: each story is a stacked block of three rows — a full-width
     title row, a step-columns row, then the activity line. A border-top on the
     title row sets one block apart from the next; the inner borders between the
     three rows are removed so the block reads as a single unit. */
  .story-title > td { border-top: 1px solid var(--surface); border-bottom: none;
                      padding-bottom: 2px; }
  .story-stages > td { border-bottom: none; padding-top: 2px; padding-bottom: 2px; }
  .substage .kind { color: var(--blue); margin-right: 4px; }
  /* Story 11.2-011: stable-height live regions. #head (run summary), the
     per-story .substage activity line, and #updated all re-render on every SSE
     tick (11.2-003 / 11.2-004) and their content can occupy 1–3 lines. Reserving
     each region's max height keeps a full innerHTML swap from changing its box
     height, so the elements below it never reflow — the page does not jump and a
     scrolled view does not shift under the cursor. #head reserves its 3-line max
     (run line + config + usage); #updated stays one line; the substage activity
     clamps to a single ellipsized line (full text on hover) instead of growing. */
  #head { min-height: 4.5em; }
  #updated { min-height: 1.5em; }
  /* Clamp the substage activity to a single line. `white-space: nowrap` +
     text-overflow can't be used here: in an auto-layout table a nowrap cell
     grows the column (and scrolls .main sideways) instead of ellipsizing. The
     line-clamp idiom keeps normal wrapping — so the cell stays at the table
     width — then clips to one line with an ellipsis, giving a stable height. */
  .substage .act { display: -webkit-box; -webkit-box-orient: vertical;
                   -webkit-line-clamp: 1; line-clamp: 1; overflow: hidden; }
  .events { margin-top: 16px; }
  .events div { padding: 2px 0; border-bottom: 1px solid var(--crust); font-size: 13px; }
  .lvl-error { color: var(--red); } .lvl-warn { color: var(--peach); } .lvl-success { color: var(--green); }
  #updated { float: right; font-size: 12px; }
  code { color: var(--text); }
  a { color: var(--blue); text-decoration: none; } a:hover { text-decoration: underline; }
  /* Story 11.2-006: GitHub repo-health badge (overview row) + panel (detail). */
  .gh { display: inline-flex; gap: 8px; align-items: center; margin-top: 4px;
        font-size: 12px; color: var(--sub); }
  .gh .gh-sep { color: var(--overlay); }
  .gh.unavail { font-style: italic; }
  .ci-success { color: var(--green); } .ci-failure { color: var(--red); }
  .ci-in_progress { color: var(--blue); } .ci-cancelled { color: var(--peach); }
  .ghpanel { margin: 16px 0; padding: 12px 14px; background: var(--mantle);
             border: 1px solid var(--surface); border-radius: 8px; }
  .ghpanel h3 { margin: 0 0 8px; font-size: 13px; font-weight: 600; }
  .ghpanel.unavail { color: var(--sub); font-style: italic; }
  /* Story 11.2-008: wave-column dependency DAG. Columns = cohort waves, nodes =
     stories, edges = SVG connectors. position:relative anchors the absolute edge
     overlay; nodes flow as normal columns so no per-pixel layout maths leak in. */
  .dagwrap { margin: 16px 0; padding: 12px 14px; background: var(--mantle);
             border: 1px solid var(--surface); border-radius: 8px; }
  .dagwrap h3 { margin: 0 0 10px; font-size: 13px; font-weight: 600; }
  .dag-cols { position: relative; display: flex; gap: 36px; align-items: flex-start; }
  .dag-edges { position: absolute; inset: 0; width: 100%; height: 100%;
               pointer-events: none; overflow: visible; }
  .dag-edges path { fill: none; stroke: var(--overlay); stroke-width: 1.5; opacity: 0.7; }
  .dag-col { display: flex; flex-direction: column; gap: 12px; min-width: 150px; z-index: 1; }
  .dag-col .wave-h { font-size: 11px; color: var(--sub); font-weight: 600; }
  .dag-node { padding: 6px 8px; background: var(--base); border: 1px solid var(--surface);
              border-radius: 6px; font-size: 12px; }
  .dag-node .nid { font-family: monospace; color: var(--text); }
  .dag-node .ntitle { color: var(--sub); display: block; margin: 2px 0 4px;
                      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  /* Story 11.2-010: in-dashboard transcript viewer. A "view session" control
     per story opens a modal listing that story's stage transcripts and renders
     each inline — no leaving the page. The new-tab /log link stays as fallback. */
  .view-session { cursor: pointer; font-size: 11px; color: var(--blue); margin-left: 6px; white-space: nowrap; }
  /* Story 11.2-012 / 11.2-014: the human-readable story title beside its ID.
     The title now owns a full-width row (.story-title), so the 22em ellipsis
     truncation is dropped — it is shown in full and may wrap freely. The title
     is static per render, so wrapping never reflows on an SSE tick (11.2-011).
     A null/empty title renders nothing (degrades to just the ID). */
  .stitle { color: var(--sub); }
  .modal { position: fixed; inset: 0; z-index: 50; background: rgba(76,79,105,.45);
           display: flex; align-items: center; justify-content: center; padding: 24px; }
  .modal[hidden] { display: none; }
  .modal-card { background: var(--base); border: 1px solid var(--surface);
                border-radius: 10px; width: min(920px, 94vw); max-height: 86vh;
                display: flex; flex-direction: column; overflow: hidden; }
  .modal-head { display: flex; align-items: center; justify-content: space-between;
                gap: 12px; padding: 12px 16px; background: var(--mantle);
                border-bottom: 1px solid var(--surface); }
  .modal-head h3 { margin: 0; font-size: 13px; font-weight: 600; }
  .modal-close { background: none; border: none; font-size: 22px; line-height: 1;
                 cursor: pointer; color: var(--sub); padding: 0 4px; }
  .modal-close:hover { color: var(--text); }
  .modal-body { padding: 12px 16px; overflow: auto; }
  .transcript { margin-bottom: 12px; }
  .transcript > summary { cursor: pointer; font-weight: 600; padding: 4px 0; }
  .transcript .tlink { font-size: 11px; margin: 4px 0; }
  .transcript pre { background: var(--mantle); border: 1px solid var(--surface);
                    border-radius: 6px; padding: 10px; margin: 4px 0 0; overflow: auto;
                    max-height: 50vh; white-space: pre-wrap; word-break: break-word;
                    font-size: 12px; }
  .transcript .empty { color: var(--sub); font-style: italic; }
  /* Story 22.6-001: top-bar view switch between the per-run Builds view and the
     all-epics Portfolio panel. The two views are sibling containers toggled with
     [hidden]; the buttons reuse the chip look with an active fill. */
  .views { display: inline-flex; gap: 6px; }
  .vbtn { font: inherit; font-size: 12px; cursor: pointer; padding: 3px 12px;
          border-radius: 12px; border: 1px solid var(--surface);
          background: var(--mantle); color: var(--sub); }
  .vbtn.active { background: var(--surface); color: var(--text); font-weight: 600; }
  /* Story 22.6-001: the portfolio panel — one section per epic, each a table of
     its stories (status · harness · owner · title) with a per-epic harness
     roll-up in the heading. Rendered from the local inventory cache, offline. */
  .portfolio { padding: 24px; }
  /* The view switch sets [hidden] on whichever container is inactive. `.wrap`
     carries an explicit display:flex, which would beat the UA [hidden] rule at
     equal specificity — so the toggle needs author [hidden] rules on both. */
  .wrap[hidden] { display: none; }
  .portfolio[hidden] { display: none; }
  .portfolio .pf-head { display: flex; align-items: center; justify-content: space-between;
                        gap: 12px; margin-bottom: 8px; }
  .portfolio .epic { margin: 0 0 22px; }
  .portfolio .epic h3 { font-size: 14px; margin: 0 0 6px; font-weight: 600; }
  /* Fixed column widths so every epic's table aligns with the next — without
     table-layout:fixed each per-epic table auto-sizes to its own content and the
     columns drift right epic after epic (Story 22.6-001). The title column has no
     width, so it absorbs the remaining space and ellipsises when long. */
  .portfolio table { table-layout: fixed; }
  .portfolio th:nth-child(1), .portfolio td:nth-child(1) { width: 7rem; }
  .portfolio th:nth-child(2), .portfolio td:nth-child(2) { width: 9rem; }
  .portfolio th:nth-child(3), .portfolio td:nth-child(3) { width: 9rem; }
  .portfolio th:nth-child(4), .portfolio td:nth-child(4) { width: 8rem; }
  .portfolio td.stitle { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .pf-refresh { font: inherit; font-size: 12px; cursor: pointer; padding: 3px 12px;
                border-radius: 12px; border: 1px solid var(--surface);
                background: var(--mantle); color: var(--sub); }
  .pf-refresh:hover { background: var(--crust); }
  /* The harness badge (Claude / Codex / qwen) — a neutral pill distinct from the
     coloured status badges so the two channels never read as the same thing. */
  .hbadge { padding: 1px 8px; border-radius: 10px; font-size: 12px; font-weight: 600;
            background: var(--mantle); border: 1px solid var(--surface); color: var(--sub); }
  @media (max-width: 760px) {
    .wrap { flex-direction: column; }
    .side { width: auto; border-right: none; border-bottom: 1px solid var(--surface); }
  }
</style>
</head>
<body>
  <header class="topbar">
    <span class="brand">Autonomous <span class="tld">SDLC</span><span class="ver">__SDLC_VERSION__</span></span>
    <nav class="views">
      <button id="viewBuilds" class="vbtn active">Builds</button>
      <button id="viewPortfolio" class="vbtn">Portfolio</button>
    </nav>
    <span id="repo" class="muted"></span>
  </header>
  <div class="wrap" id="buildsView">
    <div class="side"><h2>Runs</h2><div id="runs"></div></div>
    <div class="main">
      <div id="updated" class="muted">connecting…</div>
      <div id="head" class="muted"></div>
      <div class="bar"><span id="bar" style="width:0%"></span></div>
      <div class="chips" id="chips"></div>
      <div id="github"></div>
      <div id="dag"></div>
      <div id="stories"></div>
      <div class="events" id="events"></div>
    </div>
  </div>
  <section class="portfolio" id="portfolioView" hidden>
    <div class="pf-head">
      <h1>Portfolio <span class="muted small">every epic &amp; story &middot; status &middot; owner</span></h1>
      <button id="pfRefresh" class="pf-refresh" title="re-read the local inventory cache">refresh</button>
    </div>
    <div id="portfolioBody"><p class="muted">loading&hellip;</p></div>
  </section>
  <div id="sessionModal" class="modal" hidden>
    <div class="modal-card">
      <div class="modal-head"><h3 id="sessionTitle"></h3>
        <button id="sessionClose" class="modal-close" aria-label="close">&times;</button></div>
      <div id="sessionBody" class="modal-body"></div>
    </div>
  </div>
<script>
const ORDER = ["DONE","IN_PROGRESS","RATE_LIMITED","FAILED","BLOCKED","NEEDS_ATTENTION","AWAITING_APPROVAL","SKIPPED","TODO"];
let sel = null;  // null = Live (latest)
// Live run-duration ticker (Story 11.2-005): when a run is in-progress, count
// up locally from the server-computed elapsed (runtimeBase) captured at fetch
// (runtimeAnchor). null disables the ticker (finished run / no timestamps).
let runtimeBase = null, runtimeAnchor = null;
function esc(s){return String(s==null?"":s).replace(/[&<>'"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));}
// Story 11.2-009: display labels decouple the rendered text from the ledger
// status vocabulary. A story marked IN_PROGRESS the moment its first stage starts
// (controller; see _run_story) reads as STARTED in the UI, so the status column
// tracks real progress instead of sitting on TODO for the whole build. Only
// IN_PROGRESS is remapped — BLOCKED / NEEDS_ATTENTION / SKIPPED / RATE_LIMITED /
// AWAITING_APPROVAL keep their own distinct labels so no real state is hidden.
// The CSS class stays the raw status, so colours/styling are unchanged.
const LABELS = {"IN_PROGRESS": "STARTED"};
function statusLabel(s){return LABELS[s] || s;}
function badge(s){return "<span class='badge "+esc(s)+"'>"+esc(statusLabel(s))+"</span>";}
function humanTokens(n){
  if(n==null) return "—";
  if(n>=1e6) return (n/1e6).toFixed(n>=1e7?0:1)+"M";
  if(n>=1e3) return (n/1e3).toFixed(n>=1e4?0:1)+"k";
  return String(n);
}
function usd(n){ return n==null ? "" : "$"+Number(n).toFixed(n<1?3:2); }
// Repo health (Story 11.2-006 GitHub + 23.7-001 GitLab). The badge/panel read a
// backend-cached per-repo summary; the client never drives `gh`/`glab`. Both
// degrade to a muted "<forge> unavailable" state when the summary is absent or
// not available, with host-appropriate wording (GitLab MRs vs GitHub PRs).
const CI_GLYPH = {success:"\\u2713", failure:"\\u2717", in_progress:"\\u25f7", cancelled:"\\u2298"};
function ciGlyph(s){
  const g = CI_GLYPH[s] || "\\u2014";
  return "<span class='ci-"+esc(s||"none")+"' title='CI "+esc(s||"unknown")+"'>"+g+"</span>";
}
function ghCount(n){ return n==null ? "\\u2014" : esc(n); }
// Forge-appropriate wording: GitLab opens Merge Requests, GitHub Pull Requests.
function forgeName(g){ return (g && g.host === "gitlab") ? "GitLab" : "GitHub"; }
function crNoun(g){ return (g && g.host === "gitlab") ? "MRs" : "PRs"; }
// Compact per-row badge: open issues, open MRs/PRs, latest default-branch CI.
function ghBadge(g){
  if(!g || !g.available)
    return "<div class='gh unavail' title='"+forgeName(g)+" unavailable'>"+forgeName(g)+" \\u2014</div>";
  return "<div class='gh' title='open issues \\u00b7 open "+crNoun(g)+" \\u00b7 default-branch CI'>"
    + "<span>\\u26a0 "+ghCount(g.issues_open)+"</span>"
    + "<span class='gh-sep'>|</span><span>\\u21c4 "+ghCount(g.prs_open)+"</span>"
    + "<span class='gh-sep'>|</span>"+ciGlyph(g.ci_status)+"</div>";
}
// Full panel for the selected run's repo: issues + MRs/PRs open/closed and CI.
function renderGithub(g){
  const el = document.getElementById("github");
  if(!el) return;
  if(!g || !g.available){ el.innerHTML =
    "<div class='ghpanel unavail'>"+forgeName(g)+" unavailable</div>"; return; }
  const ciAge = g.ci_created_at
    ? " <span class='muted' title='"+esc(g.ci_created_at)+"'>"+esc(fmtLocal(g.ci_created_at))+"</span>"
    : "";
  const ciBranch = g.ci_branch ? " on <code>"+esc(g.ci_branch)+"</code>" : "";
  el.innerHTML = "<div class='ghpanel'><h3>"+forgeName(g)+" \\u00b7 <code>"+esc(g.slug||"")+"</code></h3>"
    + "<div class='chips'>"
    + "<span class='chip'>issues <b>"+ghCount(g.issues_open)+"</b> open \\u00b7 "+ghCount(g.issues_closed)+" closed</span>"
    + "<span class='chip'>"+crNoun(g)+" <b>"+ghCount(g.prs_open)+"</b> open \\u00b7 "+ghCount(g.prs_closed)+" closed</span>"
    + "</div>"
    + "<div class='small'>CI "+ciGlyph(g.ci_status)+" "+esc(g.ci_status||"\\u2014")+ciBranch+ciAge+"</div>"
    + "</div>";
}
// Render a UTC ledger timestamp in the viewer's local timezone (Issue #77).
// Ledger timestamps arrive in two UTC shapes: SQLite CURRENT_TIMESTAMP
// ("YYYY-MM-DD HH:MM:SS", space-separated, no zone suffix) and registry
// ISO-8601 ("…T…+00:00"). new Date() parses a bare space/T string as *local*,
// not UTC, so we must pin the zone: swap the space for "T" and append "Z" when
// the string carries no explicit offset. Strings that already have an offset
// (the registry shape) are passed through untouched. Non-parseable input is
// shown verbatim. Stored/emitted timestamps stay UTC; only display adapts.
function fmtLocal(iso){
  if(!iso) return "";
  let s = String(iso);
  // Has an explicit zone (Z or ±HH:MM)? Trust it; otherwise treat as UTC.
  if(!/[zZ]$|[+-]\\d{2}:?\\d{2}$/.test(s)){
    s = s.replace(" ", "T") + "Z";
  }
  const d = new Date(s);
  if(isNaN(d)) return String(iso);
  return d.toLocaleString();
}
// Shared duration formatter (Story 11.2-005): seconds → e.g. "4m 12s",
// "1h 03m", "8s". Guards null/negative/NaN so a cell never shows a bad value.
function humanDuration(s){
  if(s==null || !isFinite(s) || s<0) return "—";
  s = Math.floor(s);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  if(h>0) return h+"h "+String(m).padStart(2,"0")+"m";
  if(m>0) return m+"m "+String(sec).padStart(2,"0")+"s";
  return sec+"s";
}
function stageCell(st){
  let title = (st.attempt?("attempt "+st.attempt):"") + (st.failure_category?(" · "+esc(st.failure_category)):"");
  if(st.tokens!=null) title += " · "+humanTokens(st.tokens)+" tok"+(st.cost_usd!=null?(" · "+usd(st.cost_usd)):"");
  const inner = title ? "<span title='"+esc(title)+"'>"+badge(st.status)+"</span>" : badge(st.status);
  // Carry the selected run so registry mode confines /log to that run's logs
  // root (a non-newest run's transcript lives under its own <db>.logs).
  const runQ = sel ? "&run=" + encodeURIComponent(sel) : "";
  return st.output_path
    ? "<td><a href='/log?path="+encodeURIComponent(st.output_path)+runQ+"' target='_blank' rel='noopener'>"+inner+"</a></td>"
    : "<td>"+inner+"</td>";
}

// Live sub-stage activity (Story 11.2-004): the latest progress milestone the
// agent emitted for a story (11.1-002), shown as a second row under the story.
// A small glyph hints the kind; absent activity (older runs / captured-mode
// fallback) returns "" so the detail view degrades to the stage-level pipeline.
const KIND_GLYPH = {agent_started:"▸", tool_use:"⚙", file_changed:"✎", test_run:"✓", message:"💬"};
function activityRow(s){
  const a = s.activity;
  if(!a) return "";  // no streamed sub-stage data → stay stage-level
  const glyph = KIND_GLYPH[a.kind] || "▸";
  const stage = a.stage ? "<code>"+esc(a.stage)+"</code> " : "";
  // Story 11.2-014: span the full 8-column width as the third row of the story's
  // stacked block (the leading story column is gone). The colspan is hard-coupled
  // to the header column count — keep it in sync with renderMain's <th> row.
  // Story 11.2-011: the content rides in a `.act` element clamped to one
  // ellipsized line (full message on hover) so a refresh/SSE tick never grows
  // this row from 1 to 2–3 lines and reflows the table below it.
  return "<tr class='substage'>"
    + "<td colspan='8' class='small'><div class='act' title='"+esc(a.message)+"'><span class='kind'>"+glyph+"</span>"
    + stage + esc(a.message)
    + (a.ts ? " <span class='muted' title='"+esc(a.ts)+"'>"+esc(fmtLocal(a.ts))+"</span>" : "") + "</div></td></tr>";
}

async function tick(){
  try{
    const q = sel ? ("?run=" + encodeURIComponent(sel)) : "";
    const [runsR, statR, ghR] = await Promise.all([
      fetch("/api/runs",{cache:"no-store"}),
      fetch("/api/status"+q,{cache:"no-store"}),
      fetch("/api/github"+q,{cache:"no-store"}),
    ]);
    renderRuns(await runsR.json());
    renderMain(await statR.json());
    renderGithub(await ghR.json());
    document.getElementById("updated").textContent = "updated " + new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById("updated").textContent = "reconnecting…";
  }
}

function renderRuns(runs){
  let html = "<div class='run "+(sel===null?"active":"")+"' data-run=''>"
    + "<b>● Live</b> <span class='muted small'>(latest)</span></div>";
  html += (runs||[]).map(r => {
    const sub = esc(r.scope) + " &middot; " + esc(r.done) + "/" + esc(r.total)
      + (r.failed ? " &middot; " + esc(r.failed) + " failed" : "")
      + (r.duration_seconds!=null ? " &middot; " + humanDuration(r.duration_seconds) : "")
      + (r.total_tokens!=null ? " &middot; " + humanTokens(r.total_tokens) + " tok" : "")
      + (r.total_cost_usd!=null ? " &middot; " + usd(r.total_cost_usd) : "");
    const repo = r.repo
      ? "<div class='muted small'>📁 " + esc(String(r.repo).split(/[\\\\/]/).pop()) + "</div>"
      : "";
    const gh = ("github" in r) ? ghBadge(r.github) : "";
    // Story 19.2-001: tag active (building) runs with run--live so they stand
    // out from terminal runs. run status is IN_PROGRESS; STARTED is included so
    // the marker survives any display-status pass. This is orthogonal to active
    // (selected-for-viewing), so a selected building run carries both classes.
    const live = (r.status==="IN_PROGRESS"||r.status==="STARTED") ? " run--live" : "";
    return "<div class='run "+(sel===r.id?"active":"")+live+"' data-run='"+esc(r.id)+"'>"
      + badge(r.status) + " <code>" + esc(r.id.slice(0,8)) + "</code>"
      + repo
      + gh
      + "<div class='muted small'>" + sub + "</div>"
      + "<div class='muted small' title='" + esc(r.started_at||"") + "'>" + esc(fmtLocal(r.started_at||"")) + "</div></div>";
  }).join("");
  document.getElementById("runs").innerHTML = html;
}

// Wave-column dependency DAG (Story 11.2-008). The server (dag_layout) groups
// stories into waves (columns, left→right = execution order) and lists the
// in-queue dependency edges; this paints columns of HTML nodes and overlays the
// edges as inline SVG connectors — no external graph library. It re-renders on
// every tick, so node status tracks the run live (11.2-003) or static at load.
// Degrades to nothing (the flat story table remains) when wave data is absent.
function renderDag(d){
  const el = document.getElementById("dag");
  if(!el) return;
  const dag = d.dag;
  if(!dag || !dag.available || !(dag.waves||[]).length){ el.innerHTML = ""; return; }
  const byId = {};
  (d.stories||[]).forEach(s => { byId[s.story_id] = s; });
  const cols = dag.waves.map(w => {
    const nodes = w.stories.map(id => {
      const s = byId[id] || {story_id:id, status:"TODO", title:""};
      return "<div class='dag-node' id='dagn-"+esc(id)+"'>"
        + "<span class='nid'>"+esc(id)+"</span> "+badge(s.status||"TODO")
        + "<span class='ntitle' title='"+esc(s.title||"")+"'>"+esc(s.title||"")+"</span></div>";
    }).join("");
    return "<div class='dag-col'><div class='wave-h'>Wave "+(w.index+1)
      + " \\u2014 runs in parallel</div>"+nodes+"</div>";
  }).join("");
  el.innerHTML = "<div class='dagwrap'><h3>Dependency DAG \\u00b7 waves left\\u2192right</h3>"
    + "<div class='dag-cols' id='dagcols'><svg class='dag-edges' id='dagedges'></svg>"
    + cols + "</div></div>";
  drawDagEdges(dag.edges||[]);
}
// Edges connect each dependency (upstream node, right edge) to its dependent
// (downstream node, left edge) as a smooth SVG cubic curve. Coordinates are
// taken from the laid-out nodes' offsets relative to the positioned .dag-cols,
// so the connectors track the flexbox columns without hard-coded geometry.
function drawDagEdges(edges){
  const svg = document.getElementById("dagedges");
  if(!svg) return;
  svg.innerHTML = edges.map(e => {
    const a = document.getElementById("dagn-"+e.from);
    const b = document.getElementById("dagn-"+e.to);
    if(!a || !b) return "";
    const x1 = a.offsetLeft + a.offsetWidth, y1 = a.offsetTop + a.offsetHeight/2;
    const x2 = b.offsetLeft, y2 = b.offsetTop + b.offsetHeight/2;
    const mx = (x1 + x2) / 2;
    return "<path d='M"+x1+" "+y1+" C"+mx+" "+y1+" "+mx+" "+y2+" "+x2+" "+y2+"'></path>";
  }).join("");
}

function renderMain(d){
  const run = d.run, c = d.counts || {}, p = d.project || {};
  document.getElementById("repo").innerHTML = p.name
    ? "📦 " + (p.url ? "<a href='"+esc(p.url)+"' target='_blank' rel='noopener'>"+esc(p.name)+"</a>" : esc(p.name))
    : "";
  document.title = p.name ? p.name + " · Autonomous SDLC" : "Autonomous SDLC";
  if(!run){
    document.getElementById("head").textContent = "no build run yet — start one with `sdlc build …`";
    document.getElementById("bar").style.width = "0%";
    document.getElementById("chips").innerHTML = "";
    document.getElementById("stories").innerHTML = "";
    document.getElementById("dag").innerHTML = "";
    document.getElementById("events").innerHTML = "";
    return;
  }
  const cfg = run.config || {};
  let cfgline = "";
  if(Object.keys(cfg).length){
    const qa = cfg.skip_coverage ? "QA gate: off" : ("QA gate: on ("+esc(cfg.coverage_threshold)+"%)");
    cfgline = "<div class='muted small'>preflight: "+esc(cfg.preflight||"?")+" &middot; "+qa
      + (cfg.rebuild ? " &middot; rebuild" : "")
      + (cfg.limit ? (" &middot; limit "+esc(cfg.limit)) : "") + "</div>";
  }
  // Story 28.4-001: the routing config that *governed* this run, read from the
  // run row's frozen snapshot. Off is called out loudly (it means every stage
  // billed at the CLI default); on shows the profile and the effective per-stage
  // map after overrides, so which map ran is never guesswork after the fact.
  const rt = run.routing || {};
  const rtOff = !rt.profile || rt.profile === "off" || rt.profile === "none";
  let rtline = "";
  if(Object.keys(rt).length){
    if(rtOff){
      rtline = "<div class='muted small'><b>MODEL ROUTING OFF</b> — CLI default model on every stage</div>";
    } else {
      const map = Object.keys(rt.stage_models||{}).sort()
        .map(s => esc(s)+"="+esc(rt.stage_models[s])).join(" ");
      rtline = "<div class='muted small'>model routing: <code>"+esc(rt.profile)+"</code>"
        + " &middot; "+map
        + " &middot; → "+esc(rt.escalation_model)
        + (rt.predicted_tokens_threshold!=null
            ? (" on high-risk / predicted tokens &ge; "+esc(rt.predicted_tokens_threshold)
               +" / rework &ge; "+esc(rt.rework_threshold)
               +" (fallback points &ge; "+esc(rt.points_threshold)+")")
            : (" on high-risk or points &ge; "+esc(rt.points_threshold)))
        + "</div>";
    }
  }
  cfgline += rtline;
  const u = run.usage;
  const usageLine = u
    ? "<div class='muted small'>tokens "+humanTokens(u.total_tokens)
      + " (in "+humanTokens(u.input)+" &middot; out "+humanTokens(u.output)
      + " &middot; cache "+humanTokens((u.cache_read||0)+(u.cache_creation||0))+")"
      + (u.cost_usd!=null ? " &middot; "+usd(u.cost_usd) : "") + "</div>"
    : "";
  const running = run.status === "IN_PROGRESS";
  // Run total duration (Story 11.2-005): "took" once finished, "elapsed" while
  // running. The label carries an id so the ticker can refresh it in place
  // (live ticking) without re-rendering the whole head between transport pushes.
  const durLine = run.duration_seconds!=null
    ? " &middot; "+(running?"elapsed":"took")+" <span id='runtime'>"+esc(humanDuration(run.duration_seconds))+"</span>"
    : "";
  // Rate-limit stall time (Story 27.3-004): shown apart from the duration so
  // quota backoff never reads as agent runtime. Silent when the run never stalled.
  const stallLine = run.stall_seconds
    ? " &middot; <span title='time waited on rate limits — not agent runtime'>stalled "+esc(humanDuration(run.stall_seconds))+"</span>"
    : "";
  document.getElementById("head").innerHTML =
    "run <code>"+esc(run.id.slice(0,8))+"</code> &middot; "+badge(run.status)
    + " &middot; scope=<code>"+esc(run.scope)+"</code> &middot; "+esc(run.mode) + durLine + stallLine + cfgline + usageLine;
  // Anchor the local ticker: while running, count up from the server-computed
  // elapsed at fetch using the browser clock, so the value advances smoothly
  // even when the ledger (and the SSE transport) is momentarily quiet.
  if(running && run.duration_seconds!=null){
    runtimeBase = run.duration_seconds; runtimeAnchor = Date.now();
  } else {
    runtimeBase = null; runtimeAnchor = null;
  }
  const total = c.total||0, done = c.done||0;
  document.getElementById("bar").style.width = (total? Math.round(100*done/total):0) + "%";
  document.getElementById("chips").innerHTML = ORDER
    .filter(k => (c[k.toLowerCase()]||0) > 0 || k==="DONE")
    .map(k => "<span class='chip'><b class='"+k+"'>"+(c[k.toLowerCase()]||0)+"</b> "+statusLabel(k).toLowerCase().replace("_"," ")+"</span>")
    .join("");
  const prBase = d.pr_base;
  const rows = (d.stories||[]).map(s => {
    let pr = "-";
    if(s.pr_number){
      pr = prBase
        ? "<a href='"+esc(prBase)+"/pull/"+esc(s.pr_number)+"' target='_blank' rel='noopener'>#"+esc(s.pr_number)+"</a>"
        : "#"+esc(s.pr_number);
    }
    const stageCells = (s.stages||[]).map(stageCell).join("");
    const bug = s.bugfix_attempts > 0
      ? " <span class='badge BLOCKED' title='bugfix retries'>🔧×"+esc(s.bugfix_attempts)+"</span>"
      : "";
    const tok = s.tokens!=null
      ? humanTokens(s.tokens)+(s.cost_usd!=null?(" "+usd(s.cost_usd)):"")
      : "—";
    // Story 11.2-014: each story is a stacked block of three rows in the single
    // table — (1) a full-width title row `id · title  view session` shown in
    // full (no ellipsis: the title is static per render, so wrapping never
    // reflows on an SSE tick), (2) a step-columns row aligned under the headers,
    // and (3) the existing live activity line spanning the full width below.
    // The `story` header column is dropped → 8 columns; the title and activity
    // rows span them with colspan='8' (hard-coupled to the column count — update
    // both this and the header together). Supersedes the 11.2-012 truncation.
    // Story 11.2-012: a null/empty title degrades to just the ID.
    const stitle = s.title
      ? " <span class='sep muted'>\\u00b7</span> <span class='stitle' title='"+esc(s.title)+"'>"+esc(s.title)+"</span>"
      : "";
    return "<tr class='story-title'><td colspan='8'><code>"+esc(s.story_id)+"</code>"+stitle
    + "<a class='view-session' data-story='"+esc(s.story_id)+"' title='read this story\\u2019s agent transcripts here'>view session</a></td></tr>"
    + "<tr class='story-stages'><td>"+badge(s.status)+bug+"</td>"
    + stageCells
    + "<td>"+pr+"</td>"
    + "<td class='muted small'>"+tok+"</td>"
    + "<td class='muted small'>"+humanDuration(s.duration_seconds)
    + (s.stall_seconds ? " <span title='time waited on rate limits — not agent runtime'>(stalled "+humanDuration(s.stall_seconds)+")</span>" : "")
    + "</td></tr>"
    + activityRow(s);
  }).join("");
  document.getElementById("stories").innerHTML = rows
    ? "<table><tr><th>status</th><th>build</th><th>QA</th>"
      + "<th>review</th><th>merge</th><th>PR</th><th>tokens</th><th>duration</th></tr>"+rows+"</table>"
    : "<p class='muted'>no stories yet…</p>";
  renderDag(d);
  document.getElementById("events").innerHTML = (d.events||[]).slice().reverse().map(e =>
    "<div><span class='muted' title='"+esc(e.ts)+"'>"+esc(fmtLocal(e.ts))+"</span> <span class='lvl-"+esc(e.level)+"'>"+esc(e.level)+"</span> "+esc(e.message)+"</div>"
  ).join("");
}

document.getElementById("runs").addEventListener("click", e => {
  const el = e.target.closest(".run");
  if(!el) return;
  const next = el.dataset.run || null;  // "" → Live
  // A transcript modal is bound to the run that was selected when it opened, so
  // a run switch must dismiss it and invalidate its in-flight /api/logs fetch
  // (closeSession bumps the token) — otherwise the old run's reply could paint
  // into a modal that now belongs to a different run. closeSession is hoisted.
  if(next !== sel) closeSession();
  sel = next;
  tick();
});

// Live transport: subscribe to the server's SSE stream and refetch on each
// pushed "change". EventSource reconnects on its own, and tick() re-renders the
// whole snapshot, so a dropped connection resumes without duplicated rows. If
// EventSource is unavailable, fall back to gentle polling.
function connectStream(){
  if(!("EventSource" in window)){ setInterval(tick, 2500); return; }
  const es = new EventSource("/api/stream");
  es.addEventListener("change", tick);
  es.addEventListener("error", () => {
    document.getElementById("updated").textContent = "reconnecting…";
  });
}
// Advance the in-progress run's elapsed once a second between transport pushes.
function tickRuntime(){
  if(runtimeBase==null || runtimeAnchor==null) return;
  const el = document.getElementById("runtime");
  if(!el) return;
  el.textContent = humanDuration(runtimeBase + (Date.now()-runtimeAnchor)/1000);
}
setInterval(tickRuntime, 1000);

// GitHub repo health (Story 11.2-006) is time-based, not ledger-driven: the SSE
// stream only pushes on ledger movement, so a quiet/finished run would never
// refresh its GitHub badges/panel. Re-tick on the backend cache's ~60s cadence
// (independent of SSE) so the data refreshes without a full reload — tick()
// reads the cached summary and never itself drives `gh`.
const GH_REFRESH_INTERVAL = 30000;
setInterval(tick, GH_REFRESH_INTERVAL);

// In-dashboard transcript viewer (Story 11.2-010). A per-story "view session"
// control opens a modal that lists the story's stage transcripts (build,
// coverage, review, merge, and any bugfix retries) and renders each inline, so
// FX reads what each `claude -p` session did without hunting for .log files or
// leaving the page. Content comes from /api/logs (the same path-confined logs
// root as /log); the new-tab /log link is preserved per transcript as fallback.
function logHref(path){
  return "/log?path=" + encodeURIComponent(path)
    + (sel ? "&run=" + encodeURIComponent(sel) : "");
}
// Guard against out-of-order fetches: clicking a second story (or reselecting a
// run) before the first /api/logs resolves must not paint the slower, stale
// response into the modal. Each open captures a monotonic token; a response
// repaints only while it is still the latest, and closing invalidates any
// in-flight fetch so a late reply never reopens/repopulates a dismissed modal.
let sessionReq = 0;
// Plain-text transcripts render verbatim (readably). Once 11.1-001 streaming
// lands and logs become stream-json (one JSON object per line), the viewer
// degrades gracefully: each event collapses to a compact "type: message" line,
// and any line that is not valid JSON falls back to its raw text — so a mixed
// or future format never breaks the view.
function renderTranscriptContent(text){
  const raw = String(text==null?"":text);
  const lines = raw.split("\\n").filter(l => l.trim().length);
  const looksJsonl = lines.length>0 && lines.every(l => {
    const c = l.trim()[0]; return c==="{" || c==="[";
  });
  if(!looksJsonl) return esc(raw);
  return lines.map(l => {
    try{
      const ev = JSON.parse(l);
      const type = ev.type || ev.event || ev.kind || "event";
      const msg = ev.message || ev.text || ev.content;
      const body = msg==null ? "" : (typeof msg==="string" ? msg : JSON.stringify(msg));
      return esc(type) + (body ? ": " + esc(body) : "");
    }catch(_){ return esc(l); }  // not JSON → show the raw line, never break
  }).join("\\n");
}
function renderTranscripts(d){
  const ts = (d && d.transcripts) || [];
  if(!ts.length)
    return "<p class='empty'>No transcripts for this story yet \\u2014 it has not started, "
      + "or no stage has written a session log.</p>";
  return ts.map((t, i) => {
    const head = "<code>"+esc(t.stage||"?")+"</code>"
      + (t.attempt ? " <span class='muted small'>attempt "+esc(t.attempt)+"</span>" : "")
      + " " + badge(t.status||"PENDING");
    const link = t.path
      ? "<div class='tlink'><a href='"+logHref(t.path)+"' target='_blank' rel='noopener'>open in new tab</a></div>"
      : "";
    const inner = t.exists
      ? link + "<pre>"+renderTranscriptContent(t.content)+"</pre>"
      : "<p class='empty'>No transcript on disk for this stage.</p>" + link;
    return "<details class='transcript'"+(i===0?" open":"")+">"
      + "<summary>"+head+"</summary>"+inner+"</details>";
  }).join("");
}
async function openSession(storyId){
  const myReq = ++sessionReq;  // claim the latest-open token for this click
  const modal = document.getElementById("sessionModal");
  document.getElementById("sessionTitle").textContent = "Session transcripts \\u00b7 " + storyId;
  const bodyEl = document.getElementById("sessionBody");
  bodyEl.innerHTML = "<p class='muted'>loading\\u2026</p>";
  modal.hidden = false;
  try{
    const q = "?story=" + encodeURIComponent(storyId)
      + (sel ? "&run=" + encodeURIComponent(sel) : "");
    const r = await fetch("/api/logs" + q, {cache:"no-store"});
    const d = await r.json();
    if(myReq !== sessionReq) return;  // superseded / closed → drop this response
    bodyEl.innerHTML = renderTranscripts(d);
  }catch(e){
    if(myReq !== sessionReq) return;
    bodyEl.innerHTML = "<p class='empty'>Could not load transcripts.</p>";
  }
}
// Bumping the token invalidates any in-flight fetch so a late reply can't paint
// into the dismissed (or next) modal.
function closeSession(){ sessionReq++; document.getElementById("sessionModal").hidden = true; }
// Delegated: the stories table re-renders every tick, so bind on its container.
document.getElementById("stories").addEventListener("click", e => {
  const el = e.target.closest(".view-session");
  if(!el) return;
  e.preventDefault();
  openSession(el.dataset.story);
});
document.getElementById("sessionClose").addEventListener("click", closeSession);
document.getElementById("sessionModal").addEventListener("click", e => {
  if(e.target.id === "sessionModal") closeSession();  // click the backdrop to close
});
document.addEventListener("keydown", e => { if(e.key === "Escape") closeSession(); });

// All-epics portfolio panel (Story 22.6-001). A top-bar switch toggles between
// the per-run Builds view and this Portfolio view. The panel renders from the
// local inventory cache (/api/portfolio) with no host call, so it works offline;
// the refresh button (and switching to the view) just re-reads that cache.
const HARNESS_GLYPH = {claude:"\\u25c6", codex:"\\u25c8", qwen:"\\u25c7"};
function harnessBadge(h){
  const g = HARNESS_GLYPH[String(h||"").toLowerCase()] || "\\u25c7";
  return "<span class='hbadge' title='harness'>"+g+" "+esc(h)+"</span>";
}
function renderPortfolio(d){
  const el = document.getElementById("portfolioBody");
  if(!el) return;
  if(!d || !d.available || !(d.epics||[]).length){
    el.innerHTML = "<p class='muted'>No stories in the inventory yet \\u2014 run "
      + "<code>sdlc issues init</code> (or <code>sync</code>) to populate the portfolio.</p>";
    return;
  }
  el.innerHTML = d.epics.map(ep => {
    const roll = (ep.harness_rollup||[]).map(x => esc(x.count)+" on "+harnessBadge(x.harness)).join(" \\u00b7 ");
    const rows = ep.stories.map(s => {
      const owner = s.owner ? esc(s.owner) : "<span class='muted'>\\u2014</span>";
      const human = s.human_status ? " "+badge(String(s.human_status).toUpperCase()) : "";
      return "<tr><td><code>"+esc(s.story_id)+"</code></td>"
        + "<td>"+badge(s.status)+human+"</td>"
        + "<td>"+harnessBadge(s.harness)+"</td>"
        + "<td>"+owner+"</td>"
        + "<td class='stitle'>"+esc(s.title||"")+"</td></tr>";
    }).join("");
    return "<section class='epic'><h3>Epic-"+esc(ep.epic)
      + " <span class='muted small'>("+esc(ep.count)+" "+(ep.count===1?"story":"stories")
      + (roll ? " \\u00b7 "+roll : "")+")</span></h3>"
      + "<table><tr><th>id</th><th>status</th><th>harness</th><th>owner</th><th>title</th></tr>"
      + rows + "</table></section>";
  }).join("");
}
async function refreshPortfolio(){
  const el = document.getElementById("portfolioBody");
  try{
    const q = sel ? ("?run=" + encodeURIComponent(sel)) : "";
    const r = await fetch("/api/portfolio"+q, {cache:"no-store"});
    renderPortfolio(await r.json());
  }catch(e){
    if(el) el.innerHTML = "<p class='muted'>could not load the portfolio.</p>";
  }
}
function showView(name){
  const builds = name !== "portfolio";
  document.getElementById("buildsView").hidden = !builds;
  document.getElementById("portfolioView").hidden = builds;
  document.getElementById("viewBuilds").classList.toggle("active", builds);
  document.getElementById("viewPortfolio").classList.toggle("active", !builds);
  if(!builds) refreshPortfolio();  // re-read the cache each time the view opens
}
document.getElementById("viewBuilds").addEventListener("click", () => showView("builds"));
document.getElementById("viewPortfolio").addEventListener("click", () => showView("portfolio"));
document.getElementById("pfRefresh").addEventListener("click", refreshPortfolio);

tick();          // immediate first paint
connectStream(); // then live updates (the stream's initial change repaints too)
</script>
</body>
</html>"""


class _Handler(BaseHTTPRequestHandler):
    """Serves the dashboard page, the JSON snapshot, and the run history. Reads
    the ledger per request, so it always reflects live state. ``?run=<id>`` on
    ``/api/status`` overrides the server default (None ⇒ latest run)."""

    def log_message(self, *args) -> None:  # noqa: D401 - silence default logging
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload) -> None:
        self._send(200, json.dumps(payload, default=str).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)
        run = query.get("run", [None])[0] or self.server.run_id
        if path == "/":
            # Inject the controller version into the brand bar (constant per
            # process; escaped though it comes from trusted package metadata).
            page = _PAGE.replace("__SDLC_VERSION__", html.escape(f"v{__version__}"))
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif path in ("/api/status", "/status.json"):
            if self.server.registry is not None:
                snap = self._registry_status(run)
            else:
                snap = status_snapshot(Ledger(self.server.db_path), run)
                snap["pr_base"] = self.server.project_url
                snap["project"] = {
                    "name": self.server.project_name,
                    "url": self.server.project_url,
                }
            # Story 11.2-008: the wave-column DAG layout is derived from the
            # run's stories (wave + deps recorded by 11.2-007) so the client can
            # paint columns/edges without a graph library.
            snap["dag"] = dag_layout(snap.get("stories", []))
            self._json(snap)
        elif path == "/api/runs":
            if self.server.registry is not None:
                self._json(_registry_runs_view(self.server.registry, self.server.github_cache))
            else:
                self._json(Ledger(self.server.db_path).list_runs())
        elif path == "/api/github":
            self._json(self._github_stats(run))
        elif path == "/api/portfolio":
            self._json(self._portfolio(run))
        elif path == "/api/stream":
            self._serve_stream()
        elif path == "/log":
            self._serve_log(query.get("path", [""])[0], run)
        elif path == "/api/logs":
            self._serve_logs(run, query.get("story", [""])[0])
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    # --- registry-discovery mode helpers -----------------------------------

    def _resolve_run(self, run_id: str | None) -> RunRecord | None:
        """The registry record for ``run_id``, or the newest run when unset.

        Returns None when the id is unknown or the registry is empty — the
        caller then renders "no run" rather than a hollow snapshot.
        """
        records = self.server.registry.records()
        if run_id:
            return next((r for r in records if r.run_id == run_id), None)
        if not records:
            return None
        return max(records, key=lambda r: (r.started_at or ""))

    def _registry_status(self, run_id: str | None) -> dict:
        """Status snapshot resolved through the registry to the run's own ledger.

        Per-run isolation: each run reads its registered ``db`` path, so two
        runs in two repos never bleed into one another's detail view.
        """
        rec = self._resolve_run(run_id)
        if rec is None:
            return {
                "db": None,
                "run": None,
                "counts": dict(_EMPTY_COUNTS),
                "stories": [],
                "events": [],
                "pr_base": None,
                "project": {"name": None, "url": None},
            }
        snap = status_snapshot(Ledger(rec.db), rec.run_id)
        project_url = git_project_url(Path(rec.db).parent)
        snap["pr_base"] = project_url
        snap["project"] = {"name": _project_name(project_url, Path(rec.db)), "url": project_url}
        return snap

    # --- GitHub repo health (Story 11.2-006) -------------------------------

    def _github_stats(self, run_id: str | None) -> dict:
        """Repo health for the selected run's repo, served from the cache.

        Registry mode resolves the repo via the run's registry record; single
        ``--db`` mode resolves it from the ledger's parent directory. The forge
        (``github``/``gitlab``) is detected from that repo's remote so a GitLab
        project fetches GitLab health. The read goes through the per-(host,slug)
        TTL cache, so it never drives ``gh``/``glab`` on the request path and
        degrades to the muted "unavailable" sentinel when the run / repo / CLI
        cannot be resolved.
        """
        cache = self.server.github_cache
        if self.server.registry is not None:
            rec = self._resolve_run(run_id)
            if rec is None:
                return github_stats.unavailable(None, "no-run")
            root: str | Path = rec.repo
        else:
            db_path = self.server.db_path
            if db_path is None:
                return github_stats.unavailable(None, "no-run")
            root = Path(db_path).parent
        return cache.get(repo_slug(root), repo_host(root))

    # --- all-epics portfolio panel (Story 22.6-001) ------------------------

    def _portfolio(self, run_id: str | None) -> dict:
        """All-epics/all-stories portfolio, built from the local inventory cache.

        Offline by design: it reads the per-repo ``story_inventory`` cache (the
        sync populates it) and never calls the host, so the panel renders even
        with no network. Registry-discovery mode resolves the selected run's own
        ledger (per-repo inventory); single-``--db`` mode reads the server's
        ledger directly — both independent of whether a build run exists. An
        absent ledger / inventory yields the empty portfolio so the client shows
        its "run sync first" state rather than an error.
        """
        _, db_path = self._resolve_run_db(run_id)
        if db_path is None:
            return portfolio_view([])
        try:
            rows = Ledger(db_path).inventory_rows()
        except (OSError, sqlite3.Error):
            rows = []
        return portfolio_view(rows)

    # --- live auto-refresh transport (Story 11.2-003) ----------------------

    def _sse_write(self, text: str) -> bool:
        """Write one SSE chunk; return False once the client has gone away."""
        try:
            self.wfile.write(text.encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _serve_stream(self) -> None:
        """Server-Sent Events: push a ``change`` event when the ledger advances.

        Polls the cheap change token (:func:`_change_token`) on a short interval
        and emits an SSE ``change`` event carrying the new token whenever it
        moves; the browser then refetches ``/api/runs`` + ``/api/status`` (the
        same idempotent render it already does), so a reconnect never duplicates
        rows. When idle it emits only a heartbeat comment, keeping CPU and
        traffic negligible. Runs in the handler's own thread
        (``ThreadingHTTPServer``), so multiple browser tabs each get an
        independent stream; the loop exits as soon as the client disconnects.
        """
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            # Defeat proxy/response buffering so events arrive as they are sent.
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        poll = getattr(self.server, "sse_poll_interval", _SSE_POLL_INTERVAL)
        heartbeat = getattr(self.server, "sse_heartbeat_interval", _SSE_HEARTBEAT_INTERVAL)
        # Tell the browser's EventSource how soon to retry if the stream drops.
        if not self._sse_write(f"retry: {max(1, int(poll * 1000))}\n\n"):
            return

        last_token: str | None = None
        idle = 0.0
        while not getattr(self.server, "_sse_stop", False):
            token = _change_token(self.server)
            if token != last_token:
                last_token = token
                if not self._sse_write(f"event: change\ndata: {token}\n\n"):
                    return
                idle = 0.0
            else:
                idle += poll
                if idle >= heartbeat:
                    if not self._sse_write(": heartbeat\n\n"):
                        return
                    idle = 0.0
            time.sleep(poll)

    def _logs_root(self, run_id: str | None) -> Path | None:
        """The logs root that confines transcript serving for ``run_id``.

        In registry-discovery mode this is the selected run's own ``<db>.logs``
        directory (per-run confinement); in single-``--db`` mode it is the
        server's one logs root. None when no run resolves.
        """
        if self.server.registry is not None:
            rec = self._resolve_run(run_id)
            return Path(f"{rec.db}.logs").resolve() if rec is not None else None
        return self.server.logs_root

    def _serve_log(self, requested: str, run_id: str | None = None) -> None:
        """Serve a transcript file, but only one resolving inside the logs root.

        In registry-discovery mode the logs root is the selected run's own
        ``<db>.logs`` directory, keeping the path confinement per-run.
        """
        root = self._logs_root(run_id)
        if root is None:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            target = Path(requested).resolve()
            target.relative_to(root)  # raises ValueError if outside the root
            body = target.read_bytes()
        except (ValueError, OSError):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send(200, body, "text/plain; charset=utf-8")

    # --- in-dashboard transcript viewer (Story 11.2-010) -------------------

    @staticmethod
    def _read_confined(root: Path, requested: str | None) -> str | None:
        """Read a transcript only when it resolves inside ``root``; else None.

        Shares the path-traversal guard with :meth:`_serve_log`: a recorded
        ``output_path`` that escapes the logs root (or cannot be read) yields
        None, so the viewer reports the transcript as missing rather than ever
        leaking a file outside the logs tree.
        """
        if not requested:
            return None
        try:
            target = Path(requested).resolve()
            target.relative_to(root)  # raises ValueError if outside the root
            return target.read_text(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            return None

    def _resolve_run_db(self, run_id: str | None) -> tuple[str | None, Path | None]:
        """``(run_id, db_path)`` for the selected run, resolving 'latest' in
        single-``--db`` mode and the registry record in discovery mode."""
        if self.server.registry is not None:
            rec = self._resolve_run(run_id)
            if rec is None:
                return None, None
            return rec.run_id, Path(rec.db)
        db_path = self.server.db_path
        if db_path is None:
            return None, None
        rid = run_id or Ledger(db_path).latest_run_id()
        return rid, db_path

    def _serve_logs(self, run_id: str | None, story_id: str) -> None:
        """List a story's stage transcripts (path + inline content) as JSON.

        Enumerates every stage attempt the ledger recorded for the story —
        build, coverage, review, merge, and any bugfix retries — and folds in
        each transcript's content read through the same path-confined guard as
        ``/log``. A stage whose transcript is absent on disk (not started yet or
        the file was never written) reports ``exists: False`` with empty content
        so the client shows a placeholder, never an error. An unknown / blank
        story, or an unreachable ledger, returns an empty list (HTTP 200).
        """
        rid, db_path = self._resolve_run_db(run_id)
        root = self._logs_root(run_id)
        payload: dict = {"run": rid, "story": story_id, "transcripts": []}
        if not story_id or rid is None or db_path is None or root is None:
            self._json(payload)
            return
        try:
            attempts = Ledger(db_path).stage_breakdown(rid).get(story_id, [])
        except (OSError, sqlite3.Error):
            self._json(payload)
            return
        transcripts = []
        for a in attempts:
            content = self._read_confined(root, a.get("output_path"))
            transcripts.append(
                {
                    "stage": a.get("name"),
                    "attempt": a.get("attempt"),
                    "status": a.get("status"),
                    "path": a.get("output_path"),
                    "exists": content is not None,
                    "content": content or "",
                }
            )
        payload["transcripts"] = transcripts
        self._json(payload)


def _migrate_registry_ledgers(registry: Registry) -> None:
    """Apply pending migrations to every discovered run's ledger (best-effort).

    Each registry record points at its own per-run ledger; the dashboard reads
    them read-only, so a ledger predating a migration would crash a request with
    "no such column". Migrate each up front. A missing/corrupt/unreachable ledger
    is skipped (the registry is best-effort) — and ``ensure_migrated`` is itself a
    no-op when the DB file is absent, so no record materialises a spurious DB.
    """
    seen: set[str] = set()
    for rec in registry.records():
        if rec.db in seen:
            continue
        seen.add(rec.db)
        try:
            Ledger(rec.db).ensure_migrated()
        except (OSError, sqlite3.Error):
            pass  # unreachable/corrupt ledger → leave it; reads already tolerate this


def make_server(
    db_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    run_id: str | None = None,
    registry: Registry | None = None,
) -> ThreadingHTTPServer:
    """Build (but do not start) the dashboard server bound to ``host:port``.

    With a ``db_path`` the server runs in single-repo mode (the historical
    behaviour): one ledger, one runs browser. Without one it runs in
    registry-discovery mode — it reads the host-level registry to list every
    run across repos and resolves each run's own ledger on demand.
    """
    server = ThreadingHTTPServer((host, port), _Handler)
    server.run_id = run_id  # type: ignore[attr-defined]
    # Shared GitHub-health cache: per-repo-slug, short TTL, refreshed off the
    # request path so a slow/failing `gh` never blocks the ledger-driven view.
    server.github_cache = github_stats.GitHubStatsCache()  # type: ignore[attr-defined]
    if db_path is None:
        # Registry-discovery mode: no single ledger; project/logs resolve per run.
        reg = registry if registry is not None else Registry()
        # Migrate every discovered run's own ledger up front, before the server
        # reads any of them read-only (_registry_runs_view / _registry_status /
        # _change_token) — a stale per-run DB would otherwise crash a request
        # with "no such column", exactly as a single --db ledger would.
        _migrate_registry_ledgers(reg)
        server.registry = reg  # type: ignore[attr-defined]
        server.db_path = None  # type: ignore[attr-defined]
        server.project_url = None  # type: ignore[attr-defined]
        server.project_name = None  # type: ignore[attr-defined]
        server.logs_root = None  # type: ignore[attr-defined]
        return server
    server.registry = None  # type: ignore[attr-defined]
    db_path = Path(db_path)
    # Apply any pending migrations on a pre-existing ledger before the dashboard
    # starts serving read-only snapshots, so a stale DB never crashes a request
    # with "no such column". No-op when the DB does not yet exist.
    Ledger(db_path).ensure_migrated()
    server.db_path = db_path  # type: ignore[attr-defined]
    # Resolve the project's GitHub web base + a repo label once (from the repo
    # holding the ledger): used for PR links and the header. None when not a git repo.
    server.project_url = git_project_url(db_path.parent)  # type: ignore[attr-defined]
    server.project_name = _project_name(server.project_url, db_path)  # type: ignore[attr-defined]
    # Transcript files live under "<db>.logs"; the /log endpoint will only serve
    # files resolving inside this root (no path traversal).
    server.logs_root = Path(f"{db_path}.logs").resolve()  # type: ignore[attr-defined]
    return server


def serve(
    db_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    run_id: str | None = None,
    open_browser: bool = False,
    registry: Registry | None = None,
) -> None:
    """Run the dashboard until interrupted. Prints the URL; optionally opens it.

    With no ``db_path`` it serves in registry-discovery mode, listing runs from
    the host-level registry across repos.
    """
    server = make_server(db_path, host, port, run_id, registry)
    url = f"http://{host}:{port}"
    pf = _pidfile(host, port)
    pf.write_text(str(os.getpid()))

    # Treat SIGTERM (what `--stop`/`--restart` send) like Ctrl-C so the finally
    # block runs and the PID file is cleaned. Only valid in the main thread.
    def _graceful(*_):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _graceful)
    except ValueError:
        pass  # not the main thread (e.g. under test) — skip

    source = f"ledger: {db_path}" if db_path is not None else "registry discovery"
    print(f"sdlc dashboard → {url}  ({source}; Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ndashboard stopped.")
    finally:
        server.server_close()
        pf.unlink(missing_ok=True)
