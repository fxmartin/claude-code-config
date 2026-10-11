# ABOUTME: Host-level run registry so one dashboard can discover every `sdlc build`.
# ABOUTME: Story 11.2-001 — atomic, concurrency-safe JSON cache; ledger stays authoritative.

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

__all__ = [
    "DASHBOARD_URL_ENV",
    "WORKER_ENV",
    "Registry",
    "RunRecord",
    "default_registry_path",
    "derive_state",
    "format_live_owner_refusal",
    "live_record",
    "normalize_dashboard_url",
    "pid_alive",
]

# Registry filename under the chosen state directory.
_REGISTRY_NAME = "registry.json"

# Names the fleet worker a run belongs to (Story 35.4-001). `sdlc queue run
# --worker NAME` hands it to every job it launches, so the run's registry
# record — and the fleet view the XPS dashboard builds from it — says whose it is.
WORKER_ENV = "SDLC_WORKER"

# The tailnet origin a worker's `sdlc dashboard --host <tailnet-ip>` answers on
# (Story 35.4-002), advertised in the run's fleet record so the XPS can read the
# worker's transcripts. A queue worker advertises it with `queue run --dashboard-url`;
# a bare `sdlc build` pushing to the fleet reads it from this variable.
DASHBOARD_URL_ENV = "SDLC_DASHBOARD_URL"


def normalize_dashboard_url(url: str) -> str:
    """``url`` as a bare ``scheme://host[:port]`` origin, or ``ValueError``.

    The XPS dashboard fetches this origin on a pushed record's say-so, so it must
    be nothing but an http(s) origin: no credentials, path, query or fragment to
    steer the request anywhere but the worker's own ``/api/logs``.
    """
    text = (url or "").strip()
    try:
        parts = urlsplit(text)
        host, _port = parts.hostname, parts.port  # `.port` raises on a non-numeric port
    except ValueError as exc:
        raise ValueError(f"dashboard url {text!r} is malformed: {exc}") from exc
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError(f"dashboard url {text!r} must look like http://host:port")
    if parts.username is not None or parts.password is not None:
        raise ValueError("dashboard url must not carry credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"dashboard url {text!r} must be an origin, with no path or query")
    return f"{parts.scheme}://{parts.netloc}"


def default_registry_path() -> Path:
    """Resolve the host-level registry path, XDG-aware.

    Resolution order:
    1. ``SDLC_REGISTRY_PATH`` — an explicit file path (used by tests and power users).
    2. ``XDG_STATE_HOME/sdlc/registry.json`` when ``XDG_STATE_HOME`` is set.
    3. ``~/.sdlc/registry.json`` — the documented default.
    """
    explicit = os.environ.get("SDLC_REGISTRY_PATH")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "sdlc" / _REGISTRY_NAME
    return Path.home() / ".sdlc" / _REGISTRY_NAME


@dataclass
class RunRecord:
    """One run's registry entry — a discovery cache, not a source of truth.

    The per-repo ledger (``db``) remains authoritative for a run's detail; this
    record only carries what a dashboard needs to *find* and triage a run.
    """

    run_id: str
    repo: str  # absolute repo path
    db: str  # ledger DB path
    scope: str
    pid: int
    status: str  # last-written lifecycle status (IN_PROGRESS, DONE, FAILED, ...)
    started_at: str
    finished_at: str | None = None
    total: int | None = None
    completed: int | None = None
    # The fleet worker that owns the run (Story 35.4-001); None for a run that
    # never touched a fleet. A registry.json from before it simply lacks the key.
    worker: str | None = None
    # The worker's dashboard origin (Story 35.4-002); None when it advertises none.
    dashboard_url: str | None = None
    # The repo's git ``origin`` remote, credentials stripped (Story 35.4-006), so a
    # machine without the worker's checkout can still name the repo's forge.
    origin: str | None = None
    # Where the run is right now (Story 35.4-005): ``preflight`` | ``stories`` |
    # ``closing``; None for a finished run or one a registry from before this
    # field wrote. The fleet view shows it beside the worker.
    phase: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RunRecord":
        # Tolerate forward-compat extra keys by ignoring unknown fields.
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def pid_alive(pid: object) -> bool:
    """True when ``pid`` names a live process.

    ``os.kill(pid, 0)`` is the canonical liveness probe: it sends no signal but
    raises ``ProcessLookupError`` when the pid is gone. A ``PermissionError``
    means the process exists but is owned by another user — still alive.
    """
    # A malformed pid (None, list, unparseable string, …) names no live process.
    if not isinstance(pid, (int, str)):
        return False
    try:
        pid = int(pid)
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def derive_state(
    record: RunRecord, *, remote: bool = False, worker_online: bool | None = None
) -> str:
    """The run's *effective* state for display.

    A finished run keeps its recorded terminal status. An unfinished run whose
    pid is gone is reported ``DEAD`` (crashed) so it does not linger as
    "in progress" forever; otherwise the live status stands.

    A ``remote`` run (Story 35.4-001) ran on another machine, so its pid says
    nothing here: its worker's heartbeat is the liveness witness instead.
    ``worker_online`` is ``None`` when no heartbeat is known (the worker never
    registered), and the recorded status stands rather than a guessed ``DEAD``.
    """
    if record.finished_at:
        return record.status
    if remote:
        return "DEAD" if worker_online is False else record.status
    if not pid_alive(record.pid):
        return "DEAD"
    return record.status


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_live_owner_refusal(record: RunRecord) -> str:
    """The standard refusal text when a live owner blocks a fresh entry (issue #595).

    Shared by every caller of :meth:`Registry.find_live_owner` — `sdlc fix`,
    `sdlc resume`, and (issue #578) a future webhook trigger — so the message a
    second process sees reads identically regardless of which command tripped the
    guard: it names the owning pid and when it started, then gives the two ways
    out (watch it, or take it over once that pid is confirmed gone).
    """
    return (
        f"run {record.run_id} is already live (pid {record.pid}, "
        f"started {record.started_at})\n"
        "  → watch it:      sdlc status\n"
        f"  → take it over:  sdlc resume --run {record.run_id} --force   "
        "(only if that pid is gone)"
    )


def _live_counts(rec: RunRecord) -> tuple[int | None, int | None, str | None]:
    """Live ``(done, total, phase)`` from the run's own ledger, else the cached ones.

    The registry's counts are only written at registration and close-out, so an
    in-flight run would read ``0/N`` until it ends; the ledger is authoritative.
    The phase (Story 35.4-005) rides on the same ``list_runs`` row, so a
    heartbeat reads the ledger once.
    """
    # Lazy import: build.py imports this module.
    import sqlite3

    from sdlc.build import Ledger

    try:
        for r in Ledger(rec.db).list_runs():
            if r["id"] == rec.run_id:
                return r["done"], r["total"], r["phase"]
    except (OSError, sqlite3.Error):
        pass  # unreachable ledger → keep the registry's cached counts and phase
    return rec.completed, rec.total, rec.phase


def live_record(record: RunRecord) -> RunRecord:
    """``record`` with its counts and phase read live from the run's own ledger.

    What a worker pushes on each heartbeat (Story 35.4-001): the fleet view
    cannot reach the ledger, so the pushed counts and phase are all it has.
    """
    completed, total, phase = _live_counts(record)
    return replace(
        record, completed=completed, total=total,
        phase=None if record.finished_at else phase,
    )


class Registry:
    """Concurrency-safe accessor for the shared run registry file.

    Two ``sdlc build`` processes may register at once, so every read-modify-write
    runs under an exclusive ``flock`` on a sidecar lock file and commits via an
    atomic ``os.replace``. A missing or corrupt file degrades to an empty list —
    a damaged cache must never break run discovery.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_registry_path()

    # --- locking + atomic IO ------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read_raw(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
            return []
        if not isinstance(data, list):
            return []
        # Keep only dict rows: a non-dict element (string/number/null) would
        # crash the upsert/finish writers' ``row.get(...)`` — a junk cache must
        # never break a build or discovery.
        return [row for row in data if isinstance(row, dict)]

    def _write_raw(self, rows: list[dict]) -> None:
        # Atomic replace: write a sibling temp file, fsync, then rename over the
        # target so a concurrent reader never sees a half-written file.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def _mutate(self, fn) -> None:
        with self._locked():
            rows = self._read_raw()
            rows = fn(rows)
            self._write_raw(rows)

    # --- writers ------------------------------------------------------------

    def register(self, record: RunRecord) -> None:
        """Insert or replace the entry for ``record.run_id`` (upsert by id)."""
        if not record.started_at:
            record.started_at = _now_iso()

        def _upsert(rows: list[dict]) -> list[dict]:
            kept = [r for r in rows if r.get("run_id") != record.run_id]
            kept.append(record.to_dict())
            return kept

        self._mutate(_upsert)

    def mark_finished(
        self, run_id: str, status: str, *, completed: int | None = None
    ) -> None:
        """Stamp a run terminal: set ``status``, ``finished_at`` (and ``completed``).

        A no-op when ``run_id`` is unknown — the caller's exit path stays
        best-effort and never fails a build over a missing cache entry.
        """

        def _finish(rows: list[dict]) -> list[dict]:
            for row in rows:
                if row.get("run_id") == run_id:
                    row["status"] = status
                    row["finished_at"] = _now_iso()
                    row["phase"] = None  # a finished run is in no phase
                    if completed is not None:
                        row["completed"] = completed
            return rows

        self._mutate(_finish)

    def set_phase(self, run_id: str, phase: str | None) -> None:
        """Record which phase ``run_id`` is in (Story 35.4-005); a no-op if unknown."""

        def _set(rows: list[dict]) -> list[dict]:
            for row in rows:
                if row.get("run_id") == run_id:
                    row["phase"] = phase
            return rows

        self._mutate(_set)

    def prune(self, *, include_finished: bool = False) -> int:
        """Drop dead (crashed) entries; optionally also drop finished ones.

        Returns the number removed.
        """
        removed = 0

        def _prune(rows: list[dict]) -> list[dict]:
            nonlocal removed
            kept: list[dict] = []
            for row in rows:
                try:
                    rec = RunRecord.from_dict(row)
                except (TypeError, ValueError):
                    # An unparseable row (missing required keys, bad types) is
                    # junk that could never render — drop it on prune.
                    removed += 1
                    continue
                state = derive_state(rec)
                drop = state == "DEAD" or (include_finished and row.get("finished_at"))
                if drop:
                    removed += 1
                else:
                    kept.append(row)
            return kept

        self._mutate(_prune)
        return removed

    # --- readers ------------------------------------------------------------

    def records(self) -> list[RunRecord]:
        """Every parseable registry entry as a :class:`RunRecord`.

        Empty when the file is absent/corrupt; individual rows that fail to
        parse (missing required keys, wrong types) are skipped rather than
        crashing discovery — the registry is a best-effort cache.
        """
        records: list[RunRecord] = []
        for row in self._read_raw():
            try:
                records.append(RunRecord.from_dict(row))
            except (TypeError, ValueError):
                continue
        return records

    def find_live_owner(
        self,
        *,
        run_id: str | None = None,
        repo: str | None = None,
        scope: str | None = None,
        exclude_pid: int | None = None,
    ) -> RunRecord | None:
        """The live record already owning ``run_id`` or ``(repo, scope)``, if any.

        A shared concurrency-guard primitive (issue #595, and issue #578's later
        webhook trigger): a caller about to open or re-enter a run probes here
        first so two processes never drive the same run at once. Match by
        ``run_id`` when one exists already (the resume case); otherwise by the
        ``(repo, scope)`` pair (the fresh-entry case, e.g. `sdlc fix <issue>` —
        no run_id exists yet, but a live run already working that same repo+scope
        would still collide). ``repo`` must be pre-resolved the same way
        :func:`Registry.register`'s caller resolves it (``str(Path(...).resolve())``)
        or it will never match.

        "Live" means unfinished with a pid that still answers — the same test
        :func:`derive_state` uses to avoid reporting ``DEAD`` as "in progress",
        applied here as a gate rather than a display label. ``exclude_pid`` skips
        a record plainly owned by the caller's own process (a process re-entering
        a run it itself already holds is not a collision); it is not assumed to be
        the common case, so callers pass their own pid explicitly rather than it
        being implicit.

        Returns ``None`` when nothing live matches — including when the only
        match is DEAD (its pid is gone) or already finished, both of which are
        reclaimable, not a collision.
        """
        if run_id is None and (repo is None or scope is None):
            raise ValueError("find_live_owner requires run_id, or both repo and scope")
        for record in self.records():
            if exclude_pid is not None and record.pid == exclude_pid:
                continue
            if run_id is not None:
                if record.run_id != run_id:
                    continue
            elif record.repo != repo or record.scope != scope:
                continue
            if record.finished_at or not pid_alive(record.pid):
                continue
            return record
        return None

    def view(self) -> list[dict]:
        """Records as dicts annotated with the derived effective ``state``."""
        rows = []
        for rec in self.records():
            row = rec.to_dict()
            row["state"] = derive_state(rec)
            if not rec.finished_at:
                # The live phase too (Story 35.4-005): the cached one is never
                # `closing`, and `/api/runs` shows the ledger's.
                row["completed"], row["total"], row["phase"] = _live_counts(rec)
            rows.append(row)
        return rows
