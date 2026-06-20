# ABOUTME: Host-level run registry so one dashboard can discover every `sdlc build`.
# ABOUTME: Story 11.2-001 — atomic, concurrency-safe JSON cache; ledger stays authoritative.

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

__all__ = [
    "Registry",
    "RunRecord",
    "default_registry_path",
    "derive_state",
    "pid_alive",
]

# Registry filename under the chosen state directory.
_REGISTRY_NAME = "registry.json"


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


def derive_state(record: RunRecord) -> str:
    """The run's *effective* state for display.

    A finished run keeps its recorded terminal status. An unfinished run whose
    pid is gone is reported ``DEAD`` (crashed) so it does not linger as
    "in progress" forever; otherwise the live status stands.
    """
    if record.finished_at:
        return record.status
    if not pid_alive(record.pid):
        return "DEAD"
    return record.status


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                    if completed is not None:
                        row["completed"] = completed
            return rows

        self._mutate(_finish)

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

    def view(self) -> list[dict]:
        """Records as dicts annotated with the derived effective ``state``."""
        rows = []
        for rec in self.records():
            row = rec.to_dict()
            row["state"] = derive_state(rec)
            rows.append(row)
        return rows
