# ABOUTME: Host-level development queue — `sdlc build/fix --enqueue` records jobs here.
# ABOUTME: Story 32.1-001. SQLite/WAL store for `sdlc queue list|add|cancel|prioritise`.

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

__all__ = [
    "JobRecord",
    "QueueError",
    "QueueStore",
    "default_queue_path",
]

# Queue filename under the chosen state directory — a sibling of registry.json
# under the same host state dir (registry.py's default_registry_path()).
_QUEUE_NAME = "queue.db"

# How long a contended writer waits out the WAL writer lock before erroring
# "database is locked". Mirrors the ledger's LEDGER_BUSY_TIMEOUT_MS
# (sdlc/build.py) — same discipline, smaller store.
QUEUE_BUSY_TIMEOUT_MS = 5000

_KINDS = {"build", "fix"}
_PRIORITIES = ("low", "normal", "high", "urgent")
_PRIORITY_RANK = {name: rank for rank, name in enumerate(_PRIORITIES)}
_STATES = {"queued", "running", "done", "failed", "cancelled"}


def default_queue_path() -> Path:
    """Resolve the host-level queue path, XDG-aware.

    Resolution order mirrors :func:`sdlc.registry.default_registry_path`,
    substituting its ``SDLC_REGISTRY_PATH`` precedent with ``SDLC_QUEUE_PATH``:

    1. ``SDLC_QUEUE_PATH`` — an explicit file path (used by tests and power users).
    2. ``XDG_STATE_HOME/sdlc/queue.db`` when ``XDG_STATE_HOME`` is set.
    3. ``~/.sdlc/queue.db`` — the registry's sibling.
    """
    explicit = os.environ.get("SDLC_QUEUE_PATH")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "sdlc" / _QUEUE_NAME
    return Path.home() / ".sdlc" / _QUEUE_NAME


class QueueError(Exception):
    """A queue operation was refused (unknown job, invalid state/priority)."""


@dataclass
class JobRecord:
    """One row of the ``jobs`` table."""

    id: int
    repo: str
    kind: str
    scope: str
    priority: str
    state: str
    claimed_by: str | None
    lease_until: str | None
    run_id: str | None
    options: str | None
    created_at: str
    updated_at: str
    reason: str | None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "repo": self.repo,
            "kind": self.kind,
            "scope": self.scope,
            "priority": self.priority,
            "state": self.state,
            "claimed_by": self.claimed_by,
            "lease_until": self.lease_until,
            "run_id": self.run_id,
            "options": self.options,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reason": self.reason,
        }


# Queue DDL. Deliberately mirrors the ledger's connect/init/migrate shape
# (sdlc/build.py's Ledger class) rather than sharing it: build.py's Ledger is
# entangled with the run state machine, and extracting a shared base at this
# story's scope would be a large, risky refactor for a five-point story. This
# is the documented debt from REVIEW.md item 5 — a later pass can lift both
# onto one shared helper once the ledger side is decoupled enough to move.
_SCHEMA_DDL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    scope       TEXT NOT NULL,
    priority    TEXT NOT NULL DEFAULT 'normal',
    state       TEXT NOT NULL DEFAULT 'queued',
    claimed_by  TEXT,
    lease_until TIMESTAMP,
    run_id      TEXT,
    options     TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reason      TEXT
);

CREATE TABLE IF NOT EXISTS _migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_repo  ON jobs(repo);
"""

# Schema migrations applied after the base DDL, in the same
# ``(version, name, table, columns, create_sql)`` shape as the ledger's
# ``_MIGRATIONS`` (sdlc/build.py) — empty today (the fresh schema above covers
# every column this story needs); future columns (e.g. a claim lease renewal
# field) land here so an existing host queue.db upgrades in place.
_MIGRATIONS: list[tuple[int, str, str, list[tuple[str, str]], str | None]] = []


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending schema migrations on an open connection (idempotent).

    Mirrors ``sdlc.build._apply_migrations``; see the module docstring above
    the DDL for why this is a mirror rather than a shared helper.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    applied = {r[0] for r in conn.execute("SELECT version FROM _migrations").fetchall()}
    for version, name, table, columns, create_sql in _MIGRATIONS:
        if version in applied:
            continue
        if create_sql:
            conn.executescript(create_sql)
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if existing:
            for col, col_type in columns:
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        conn.execute(
            "INSERT OR IGNORE INTO _migrations(version, name) VALUES (?, ?)",
            (version, name),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueueStore:
    """Durable host-level job queue, backed by SQLite/WAL.

    One store serves every repo on the host: `sdlc build --enqueue`/`sdlc fix
    --enqueue` record a job here instead of running, and `sdlc queue
    list|add|cancel|prioritise` manage it. The per-repo ledger stays the sole
    truth for a run once one starts — this store only tracks the job's own
    lifecycle plus the `run_id` link (Story 32.1-001 AC5).
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=QUEUE_BUSY_TIMEOUT_MS / 1000)
        conn.execute(f"PRAGMA busy_timeout = {QUEUE_BUSY_TIMEOUT_MS};")
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self) -> None:
        """Create the queue schema if absent, then apply migrations (idempotent)."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA_DDL)
            _apply_migrations(conn)

    def ensure_migrated(self) -> None:
        """Bring a *pre-existing* queue up to the current schema (idempotent).

        Unlike :meth:`init`, this never creates the store: when the file is
        absent it is a no-op, so a read verb (`sdlc queue list`, `sdlc doctor`)
        launched on a host that has never enqueued anything leaves no spurious
        empty queue.db behind.
        """
        if not self.db_path.exists():
            return
        with self._connect() as conn:
            conn.execute("PRAGMA busy_timeout = 2000;")
            conn.execute("BEGIN IMMEDIATE;")
            _apply_migrations(conn)

    # --- writers --------------------------------------------------------

    def add_job(
        self,
        *,
        repo: str,
        kind: str,
        scope: str,
        priority: str = "normal",
        options_json: str | None = None,
    ) -> int:
        """Insert a fresh ``queued`` job and return its id.

        ``repo`` must already be resolved the way the registry resolves it
        (``str(Path(...).resolve())``) — this store does not re-resolve it.
        """
        if kind not in _KINDS:
            raise QueueError(
                f"invalid kind: {kind!r} (expected one of {sorted(_KINDS)})"
            )
        if priority not in _PRIORITY_RANK:
            raise QueueError(
                f"invalid priority: {priority!r} (expected one of {list(_PRIORITIES)})"
            )
        now = _now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO jobs(repo, kind, scope, priority, state, options, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
                (repo, kind, scope, priority, options_json, now, now),
            )
            assert cur.lastrowid is not None  # INSERT always assigns a rowid
            return int(cur.lastrowid)

    def cancel_job(self, job_id: int) -> None:
        """Mark a ``queued`` job ``cancelled``; refuse on any other state."""
        job = self.get_job(job_id)
        if job is None:
            raise QueueError(f"unknown job id: {job_id}")
        if job.state != "queued":
            raise QueueError(
                f"cannot cancel job {job_id}: state is {job.state} "
                "(only a queued job can be cancelled)"
            )
        self._set_state(job_id, "cancelled")

    def prioritise_job(self, job_id: int, priority_class: str) -> None:
        """Reorder a job by changing its priority class."""
        if priority_class not in _PRIORITY_RANK:
            raise QueueError(
                f"invalid priority: {priority_class!r} "
                f"(expected one of {list(_PRIORITIES)})"
            )
        job = self.get_job(job_id)
        if job is None:
            raise QueueError(f"unknown job id: {job_id}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET priority = ?, updated_at = ? WHERE id = ?",
                (priority_class, _now_iso(), job_id),
            )

    def _set_state(self, job_id: int, state: str) -> None:
        """Stamp a job's lifecycle state (internal — the claim/lease verbs a
        later story adds will grow their own public entry points; today's
        callers are :meth:`cancel_job` and the test suite seeding a
        ``running`` fixture)."""
        if state not in _STATES:
            raise QueueError(f"invalid state: {state!r} (expected one of {sorted(_STATES)})")
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE id = ?",
                (state, _now_iso(), job_id),
            )

    # --- readers ----------------------------------------------------------

    def get_job(self, job_id: int) -> JobRecord | None:
        if not self.db_path.exists():
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_jobs(self, repo: str | None = None) -> list[JobRecord]:
        """Every job on the host, newest-priority-first then FIFO within a class.

        ``repo`` filters to one repo (already resolved the way
        :meth:`add_job`'s caller resolves it); ``None`` lists across every repo
        on the host, matching :meth:`sdlc.registry.Registry.records`'s
        cross-repo posture. A missing store (nothing ever enqueued on this
        host) degrades to an empty list rather than conjuring an empty
        ``queue.db`` — the same read-never-creates contract as
        :meth:`ensure_migrated`.
        """
        if not self.db_path.exists():
            return []
        query = (
            "SELECT * FROM jobs"
            + (" WHERE repo = ?" if repo is not None else "")
            + " ORDER BY CASE priority "
            + " ".join(f"WHEN '{name}' THEN {rank}" for name, rank in _PRIORITY_RANK.items())
            + " ELSE 1 END DESC, created_at ASC, id ASC"
        )
        params = (repo,) if repo is not None else ()
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_record(row) for row in rows]

    def counts_by_state(self) -> dict[str, int]:
        """Job counts grouped by lifecycle state, for `sdlc doctor`."""
        if not self.db_path.exists():
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"
            ).fetchall()
        return {row["state"]: row["n"] for row in rows}


def _row_to_record(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        id=row["id"],
        repo=row["repo"],
        kind=row["kind"],
        scope=row["scope"],
        priority=row["priority"],
        state=row["state"],
        claimed_by=row["claimed_by"],
        lease_until=row["lease_until"],
        run_id=row["run_id"],
        options=row["options"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        reason=row["reason"],
    )
