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

from sdlc import __version__
from sdlc.build import _EMPTY_COUNTS, Ledger, _duration_seconds, status_snapshot
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


# --- multi-run registry discovery (Story 11.2-002) -------------------------
# In discovery mode the dashboard has no single ledger: it reads the host-level
# registry to find every `sdlc build` across repos, and resolves each run's own
# ledger on demand. The per-repo ledger stays authoritative for run detail.


def _registry_runs_view(registry: Registry) -> list[dict]:
    """Normalize the host-level registry into the runs-browser row shape.

    Each row carries the cross-repo discovery fields (``repo``, ``scope``) plus
    the derived effective ``status`` and live ``done``/``total`` read from that
    run's own ledger when reachable — falling back to the registry's cached
    counts when a ledger is missing/corrupt (the registry is best-effort).
    Newest run first.
    """
    rows: list[dict] = []
    for rec in registry.records():
        done, total = rec.completed, rec.total
        try:
            for r in Ledger(rec.db).list_runs():
                if r["id"] == rec.run_id:
                    done, total = r["done"], r["total"]
                    break
        except (OSError, sqlite3.Error):
            pass  # unreachable ledger → keep the registry's cached counts
        rows.append(
            {
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
        )
    rows.sort(key=lambda r: (r["started_at"] or ""), reverse=True)
    return rows


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
  .IN_PROGRESS { background: #e4ecfd; color: var(--blue); }
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
  .substage > td { border-bottom: 1px solid var(--surface); padding-top: 0; color: var(--sub); }
  .substage .kind { color: var(--blue); margin-right: 4px; }
  .events { margin-top: 16px; }
  .events div { padding: 2px 0; border-bottom: 1px solid var(--crust); font-size: 13px; }
  .lvl-error { color: var(--red); } .lvl-warn { color: var(--peach); } .lvl-success { color: var(--green); }
  #updated { float: right; font-size: 12px; }
  code { color: var(--text); }
  a { color: var(--blue); text-decoration: none; } a:hover { text-decoration: underline; }
  @media (max-width: 760px) {
    .wrap { flex-direction: column; }
    .side { width: auto; border-right: none; border-bottom: 1px solid var(--surface); }
  }
</style>
</head>
<body>
  <header class="topbar">
    <span class="brand">Autonomous <span class="tld">SDLC</span><span class="ver">__SDLC_VERSION__</span></span>
    <span id="repo" class="muted"></span>
  </header>
  <div class="wrap">
    <div class="side"><h2>Runs</h2><div id="runs"></div></div>
    <div class="main">
      <div id="updated" class="muted">connecting…</div>
      <div id="head" class="muted"></div>
      <div class="bar"><span id="bar" style="width:0%"></span></div>
      <div class="chips" id="chips"></div>
      <div id="stories"></div>
      <div class="events" id="events"></div>
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
function badge(s){return "<span class='badge "+esc(s)+"'>"+esc(s)+"</span>";}
function localTime(s){
  if(!s) return "";
  const d = new Date(String(s).replace(" ","T") + "Z");
  return isNaN(d) ? String(s) : d.toLocaleString();
}
function humanTokens(n){
  if(n==null) return "—";
  if(n>=1e6) return (n/1e6).toFixed(n>=1e7?0:1)+"M";
  if(n>=1e3) return (n/1e3).toFixed(n>=1e4?0:1)+"k";
  return String(n);
}
function usd(n){ return n==null ? "" : "$"+Number(n).toFixed(n<1?3:2); }
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
  // Span every column but the leading story cell so the activity reads as a
  // continuation of its story row. Update the colspan if columns change.
  return "<tr class='substage'><td></td>"
    + "<td colspan='8' class='small'><span class='kind'>"+glyph+"</span>"
    + stage + esc(a.message)
    + (a.ts ? " <span class='muted'>"+esc(a.ts)+"</span>" : "") + "</td></tr>";
}

async function tick(){
  try{
    const q = sel ? ("?run=" + encodeURIComponent(sel)) : "";
    const [runsR, statR] = await Promise.all([
      fetch("/api/runs",{cache:"no-store"}),
      fetch("/api/status"+q,{cache:"no-store"}),
    ]);
    renderRuns(await runsR.json());
    renderMain(await statR.json());
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
    return "<div class='run "+(sel===r.id?"active":"")+"' data-run='"+esc(r.id)+"'>"
      + badge(r.status) + " <code>" + esc(r.id.slice(0,8)) + "</code>"
      + repo
      + "<div class='muted small'>" + sub + "</div>"
      + "<div class='muted small'>" + esc(localTime(r.started_at)) + "</div></div>";
  }).join("");
  document.getElementById("runs").innerHTML = html;
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
  document.getElementById("head").innerHTML =
    "run <code>"+esc(run.id.slice(0,8))+"</code> &middot; "+badge(run.status)
    + " &middot; scope=<code>"+esc(run.scope)+"</code> &middot; "+esc(run.mode) + durLine + cfgline + usageLine;
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
    .map(k => "<span class='chip'><b class='"+k+"'>"+(c[k.toLowerCase()]||0)+"</b> "+k.toLowerCase().replace("_"," ")+"</span>")
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
    return "<tr><td><code>"+esc(s.story_id)+"</code></td>"
    + "<td>"+badge(s.status)+bug+"</td>"
    + stageCells
    + "<td>"+pr+"</td>"
    + "<td class='muted small'>"+tok+"</td>"
    + "<td class='muted small'>"+humanDuration(s.duration_seconds)+"</td></tr>"
    + activityRow(s);
  }).join("");
  document.getElementById("stories").innerHTML = rows
    ? "<table><tr><th>story</th><th>status</th><th>build</th><th>QA</th>"
      + "<th>review</th><th>merge</th><th>PR</th><th>tokens</th><th>duration</th></tr>"+rows+"</table>"
    : "<p class='muted'>no stories yet…</p>";
  document.getElementById("events").innerHTML = (d.events||[]).slice().reverse().map(e =>
    "<div><span class='muted'>"+esc(localTime(e.ts))+"</span> <span class='lvl-"+esc(e.level)+"'>"+esc(e.level)+"</span> "+esc(e.message)+"</div>"
  ).join("");
}

document.getElementById("runs").addEventListener("click", e => {
  const el = e.target.closest(".run");
  if(!el) return;
  sel = el.dataset.run || null;  // "" → Live
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
                self._json(self._registry_status(run))
            else:
                snap = status_snapshot(Ledger(self.server.db_path), run)
                snap["pr_base"] = self.server.project_url
                snap["project"] = {
                    "name": self.server.project_name,
                    "url": self.server.project_url,
                }
                self._json(snap)
        elif path == "/api/runs":
            if self.server.registry is not None:
                self._json(_registry_runs_view(self.server.registry))
            else:
                self._json(Ledger(self.server.db_path).list_runs())
        elif path == "/api/stream":
            self._serve_stream()
        elif path == "/log":
            self._serve_log(query.get("path", [""])[0], run)
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

    def _serve_log(self, requested: str, run_id: str | None = None) -> None:
        """Serve a transcript file, but only one resolving inside the logs root.

        In registry-discovery mode the logs root is the selected run's own
        ``<db>.logs`` directory, keeping the path confinement per-run.
        """
        if self.server.registry is not None:
            rec = self._resolve_run(run_id)
            root = Path(f"{rec.db}.logs").resolve() if rec is not None else None
        else:
            root = self.server.logs_root
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
