# ABOUTME: Local progress dashboard — a stdlib http.server view over the ledger.
# ABOUTME: Serves `/` (HTML), `/api/status` (snapshot), `/api/runs` (run history).
#
# This decouples progress display from any agent/turn loop: a build writes the
# SQLite ledger, this server reads it read-only, and the browser polls it. No web
# framework — Python stdlib only, to keep the controller's dependency footprint
# minimal. The ledger is per-repo, so the runs list is that repo's build history.

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from sdlc.build import Ledger, status_snapshot

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
    --peach:#fe640b; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
         background: var(--base); color: var(--text); }
  .topbar { display: flex; align-items: center; justify-content: space-between;
            gap: 16px; flex-wrap: wrap; padding: 12px 24px; background: var(--mantle);
            border-bottom: 1px solid var(--surface); }
  .brand { font-weight: 700; font-size: 15px; letter-spacing: .02em; }
  .brand .tld { color: var(--blue); }
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
  .SKIPPED { background: var(--surface); color: var(--sub); }
  .TODO { background: var(--crust); color: var(--overlay); }
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
    <span class="brand">Autonomous <span class="tld">SDLC</span></span>
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
const ORDER = ["DONE","IN_PROGRESS","FAILED","BLOCKED","NEEDS_ATTENTION","SKIPPED","TODO"];
let sel = null;  // null = Live (latest)
function esc(s){return String(s==null?"":s).replace(/[&<>'"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));}
function badge(s){return "<span class='badge "+esc(s)+"'>"+esc(s)+"</span>";}

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
      + (r.failed ? " &middot; " + esc(r.failed) + " failed" : "");
    return "<div class='run "+(sel===r.id?"active":"")+"' data-run='"+esc(r.id)+"'>"
      + badge(r.status) + " <code>" + esc(r.id.slice(0,8)) + "</code>"
      + "<div class='muted small'>" + sub + "</div>"
      + "<div class='muted small'>" + esc(r.started_at||"") + "</div></div>";
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
  document.getElementById("head").innerHTML =
    "run <code>"+esc(run.id.slice(0,8))+"</code> &middot; "+badge(run.status)
    + " &middot; scope=<code>"+esc(run.scope)+"</code> &middot; "+esc(run.mode);
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
    return "<tr><td><code>"+esc(s.story_id)+"</code></td>"
    + "<td>"+badge(s.status)+"</td>"
    + "<td>"+esc(s.current_stage||"-")+"</td>"
    + "<td>"+pr+"</td></tr>";
  }).join("");
  document.getElementById("stories").innerHTML = rows
    ? "<table><tr><th>story</th><th>status</th><th>stage</th><th>PR</th></tr>"+rows+"</table>"
    : "<p class='muted'>no stories yet…</p>";
  document.getElementById("events").innerHTML = (d.events||[]).slice().reverse().map(e =>
    "<div><span class='muted'>"+esc(e.ts)+"</span> <span class='lvl-"+esc(e.level)+"'>"+esc(e.level)+"</span> "+esc(e.message)+"</div>"
  ).join("");
}

document.getElementById("runs").addEventListener("click", e => {
  const el = e.target.closest(".run");
  if(!el) return;
  sel = el.dataset.run || null;  // "" → Live
  tick();
});
tick(); setInterval(tick, 2500);
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
        ledger = Ledger(self.server.db_path)
        if path == "/":
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path in ("/api/status", "/status.json"):
            run = parse_qs(parts.query).get("run", [None])[0] or self.server.run_id
            snap = status_snapshot(ledger, run)
            snap["pr_base"] = self.server.project_url
            snap["project"] = {"name": self.server.project_name, "url": self.server.project_url}
            self._json(snap)
        elif path == "/api/runs":
            self._json(ledger.list_runs())
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


def make_server(
    db_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8787,
    run_id: str | None = None,
) -> ThreadingHTTPServer:
    """Build (but do not start) the dashboard server bound to ``host:port``."""
    server = ThreadingHTTPServer((host, port), _Handler)
    db_path = Path(db_path)
    server.db_path = db_path  # type: ignore[attr-defined]
    server.run_id = run_id  # type: ignore[attr-defined]
    # Resolve the project's GitHub web base + a repo label once (from the repo
    # holding the ledger): used for PR links and the header. None when not a git repo.
    server.project_url = git_project_url(db_path.parent)  # type: ignore[attr-defined]
    server.project_name = _project_name(server.project_url, db_path)  # type: ignore[attr-defined]
    return server


def serve(
    db_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8787,
    run_id: str | None = None,
    open_browser: bool = False,
) -> None:
    """Run the dashboard until interrupted. Prints the URL; optionally opens it."""
    server = make_server(db_path, host, port, run_id)
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

    print(f"sdlc dashboard → {url}  (ledger: {db_path}; Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ndashboard stopped.")
    finally:
        server.server_close()
        pf.unlink(missing_ok=True)
