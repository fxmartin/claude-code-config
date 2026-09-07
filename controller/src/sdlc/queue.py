# ABOUTME: Host-level development queue — `sdlc build/fix --enqueue` records jobs here.
# ABOUTME: Stories 32.1-001/32.2-002. SQLite/WAL store for `sdlc queue list|add|cancel|
# ABOUTME: requeue|prioritise`, plus the approval park a scheduler re-polls.

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
# Lifecycle states. Story 32.1-002 adds ``blocked`` — the parked terminal a
# scheduler stamps on a job it refused to execute (e.g. the repo's installed
# controller disagrees with its checkout, Story 15.1-004) rather than running
# it on stale code.
#
# Story 32.2-002 adds ``parked``, which is deliberately *not* terminal: a job
# whose run stopped ``AWAITING_APPROVAL`` is waiting on a human labelling its
# change request, and the queue re-polls that CR until it can resume the run
# itself. ``blocked`` needs an operator command to leave; ``parked`` leaves on
# its own the moment the forge says so.
_STATES = {"queued", "running", "done", "failed", "cancelled", "blocked", "parked"}

# The subset of :data:`_STATES` a job can finish in. ``running`` and
# ``queued`` are in-flight; ``cancelled`` is an operator action, not an
# outcome the scheduler reports.
_TERMINAL_STATES = {"done", "failed", "blocked"}

# States an operator may retire. ``queued`` is the everyday case; ``blocked``
# is here because Story 32.1-002 introduced that park and it would otherwise be
# a dead end; ``parked`` is here because Story 32.2-002's approval wait must be
# abandonable when FX decides the change request is not going to be approved. A
# ``running`` job belongs to a live scheduler and its child, and a
# ``done``/``failed`` job is history worth keeping — neither is cancellable.
_CANCELLABLE_STATES = {"queued", "blocked", "parked"}

# States an operator may re-arm with :meth:`QueueStore.requeue_job`. ``queued``
# is excluded because it is already armed, ``running`` because it is live.
_REQUEUEABLE_STATES = _STATES - {"running", "queued"}


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
    # Story 32.2-002. ``pr_number`` is the change request the run left open when
    # it parked ``AWAITING_APPROVAL``; ``poll_after`` is the earliest instant the
    # scheduler may read that CR again — the bound that keeps a night of polling
    # inside the host's API rate limits.
    pr_number: int | None = None
    poll_after: str | None = None

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
            "pr_number": self.pr_number,
            "poll_after": self.poll_after,
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
    reason      TEXT,
    pr_number   INTEGER,
    poll_after  TIMESTAMP
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
# ``_MIGRATIONS`` (sdlc/build.py). The fresh schema above already carries every
# column, so these only matter to a host queue.db written by an earlier version
# — it upgrades in place rather than being rebuilt.
_MIGRATIONS: list[tuple[int, str, str, list[tuple[str, str]], str | None]] = [
    (
        1,
        "approval_park",  # Story 32.2-002
        "jobs",
        [("pr_number", "INTEGER"), ("poll_after", "TIMESTAMP")],
        None,
    ),
]


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


def _at(now: datetime | None) -> datetime:
    """``now`` or the current UTC instant — the single clock seam for leases.

    Every lease comparison in this module goes through here so a test can drive
    expiry deterministically instead of sleeping out a real 90-second lease.
    """
    return now if now is not None else datetime.now(timezone.utc)


class QueueStore:
    """Durable host-level job queue, backed by SQLite/WAL.

    One store serves every repo on the host: `sdlc build --enqueue`/`sdlc fix
    --enqueue` record a job here instead of running, and `sdlc queue
    list|add|cancel|prioritise` manage it. The per-repo ledger stays the sole
    truth for a run once one starts — this store only tracks the job's own
    lifecycle plus the `run_id` link (Story 32.1-001 AC5).

    Story 32.2-002 adds the approval park on top of that lifecycle: a job whose
    run stopped ``AWAITING_APPROVAL`` records the change request it is waiting
    on (``pr_number``) and when it may next be read (``poll_after``), so the
    wait outlives both the run and the scheduler that started it.
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
        """Retire a job that is not live; refuse a ``running`` one.

        ``queued`` is the everyday case. A *parked* job (``blocked`` — Story
        32.1-002's version-check terminal) is accepted too: it is not live, so
        cancelling it is safe, and without this it would sit in `queue list`
        forever with no way out. A ``running`` job still belongs to a scheduler
        and its child process, so it is refused — stop the scheduler instead.
        """
        job = self.get_job(job_id)
        if job is None:
            raise QueueError(f"unknown job id: {job_id}")
        if job.state not in _CANCELLABLE_STATES:
            raise QueueError(
                f"cannot cancel job {job_id}: state is {job.state} "
                f"(cancellable: {', '.join(sorted(_CANCELLABLE_STATES))})"
            )
        self._set_state(job_id, "cancelled")

    def requeue_job(self, job_id: int, *, now: datetime | None = None) -> None:
        """Re-arm a finished-or-parked job for the next drain, options intact.

        The exit from ``blocked`` (Story 32.1-002). The version check parks a
        job carrying a remedy — "reinstall the controller" — and an operator who
        follows that remedy needs the job to *run*, not to be re-created by hand
        from options `queue list` does not even display. Also serves a `failed`
        job worth one more attempt, and un-does a `cancelled`.

        Two shapes, the same ones :meth:`release_claim` draws, and for the same
        reason — a job that already opened a run must be **resumed, never
        restarted**:

        * **No ``run_id``** — nothing ran (the version-check park is always
          this shape), so the job goes back to ``queued`` for a clean start.
        * **A run exists** — the job returns to ``running`` with an already
          expired lease, so :meth:`expired_running_jobs` surfaces it to the next
          `sdlc queue run`, which re-enters through `sdlc resume --run <id>` and
          picks each story up at the stage it stopped in.

        Refuses a ``running`` job (a live scheduler owns it — stop that
        scheduler instead) and a ``queued`` one (already armed; silently
        succeeding would hide a mistyped id). ``options``, ``priority``,
        ``kind`` and ``scope`` are untouched; only lifecycle fields move.
        """
        job = self.get_job(job_id)
        if job is None:
            raise QueueError(f"unknown job id: {job_id}")
        if job.state not in _REQUEUEABLE_STATES:
            raise QueueError(
                f"cannot requeue job {job_id}: state is {job.state} "
                f"(requeueable: {', '.join(sorted(_REQUEUEABLE_STATES))})"
            )
        moment = _at(now).isoformat()
        with self._connect() as conn:
            if job.run_id:
                conn.execute(
                    "UPDATE jobs SET state = 'running', claimed_by = NULL, "
                    "lease_until = ?, reason = NULL, updated_at = ? WHERE id = ?",
                    (moment, moment, job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET state = 'queued', claimed_by = NULL, "
                    "lease_until = NULL, reason = NULL, updated_at = ? WHERE id = ?",
                    (moment, job_id),
                )

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


    # --- claims + leases (Story 32.1-002) ---------------------------------

    def peek_claimable(
        self,
        *,
        busy_repos: "set[str] | frozenset[str] | None" = None,
        fix_busy_repos: "set[str] | frozenset[str] | None" = None,
        now: datetime | None = None,
    ) -> list[JobRecord]:
        """Claimable jobs in dispatch order (highest priority, then FIFO).

        A read-only *candidate* list, deliberately separate from
        :meth:`claim_job`: the scheduler must weigh a job's agent-slot cost
        before it takes the job, and SQL cannot compute that cost. The claim
        itself stays a single guarded UPDATE, so losing a race here costs one
        retry, never a double-run.

        Per-repo exclusivity (Story 32.1-002 AC2, relaxed by 32.1-003): a
        ``fix`` job needs the repo root to itself, so it is excluded whenever
        the repo has *any* live job (``busy_repos``); a ``build`` job only
        needs the repo free of a live ``fix`` job (``fix_busy_repos``) — two
        ``build`` jobs may now overlap in one repo, since neither writes to
        the shared checkout mid-run any more. Both sets are filtered in
        Python rather than SQL because they are small (one entry per running
        job) and an ``IN`` clause built from caller strings is not worth the
        injection surface.
        """
        if not self.db_path.exists():
            return []
        cutoff = _at(now).isoformat()
        any_busy = busy_repos or frozenset()
        fix_busy = fix_busy_repos or frozenset()
        query = (
            "SELECT * FROM jobs WHERE state = 'queued' "
            "AND (lease_until IS NULL OR lease_until < ?) "
            "ORDER BY CASE priority "
            + " ".join(f"WHEN '{name}' THEN {rank}" for name, rank in _PRIORITY_RANK.items())
            + " ELSE 1 END DESC, created_at ASC, id ASC"
        )
        with self._connect() as conn:
            rows = conn.execute(query, (cutoff,)).fetchall()

        def _blocked(record: JobRecord) -> bool:
            if record.repo in fix_busy:
                return True
            return record.kind == "fix" and record.repo in any_busy

        return [
            record
            for record in (_row_to_record(row) for row in rows)
            if not _blocked(record)
        ]

    def claim_job(
        self,
        job_id: int,
        *,
        claimed_by: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> JobRecord | None:
        """Take a ``queued`` job under a lease; ``None`` when someone beat us.

        The single guarded UPDATE the story specifies — ``WHERE state='queued'
        AND (lease_until IS NULL OR lease_until < now)``. SQLite's write lock
        makes it atomic, so two schedulers racing for the same row produce
        exactly one winner.

        The guard also carries **per-repo exclusivity** (AC2, relaxed for
        build/build by Story 32.1-003), not just the row conditions.
        :meth:`peek_claimable` filters busy repos in Python, which is a read:
        two schedulers that both peek before either claims would each see one
        repo idle and each take a *different* queued job in it — two UPDATEs
        on distinct rows, so both succeed and the repo ends up with two runs
        that should have been exclusive. Re-asserting the rule inside the
        claim closes that window, and costs nothing in the single-scheduler
        case because the candidate has already passed the same test. It never
        blocks the row being claimed (that one is still ``queued``, not
        ``running``).

        The exclusivity predicate: a live job in the same repo blocks this
        claim only when *either* side is ``fix`` — a ``fix`` job always needs
        the repo root to itself, and a live ``fix`` job always excludes a new
        claim of any kind. Two ``build`` jobs never block each other any more
        (32.1-003 moved their status markers off the shared checkout).
        """
        moment = _at(now)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET state = 'running', claimed_by = ?, lease_until = ?, "
                "reason = NULL, updated_at = ? "
                "WHERE id = ? AND state = 'queued' "
                "AND (lease_until IS NULL OR lease_until < ?) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM jobs AS busy "
                "  WHERE busy.repo = jobs.repo AND busy.state = 'running' "
                "  AND (busy.kind = 'fix' OR jobs.kind = 'fix')"
                ")",
                (
                    claimed_by,
                    (moment + timedelta(seconds=lease_seconds)).isoformat(),
                    moment.isoformat(),
                    job_id,
                    moment.isoformat(),
                ),
            )
            if cur.rowcount != 1:
                return None
        return self.get_job(job_id)

    def reclaim_job(
        self,
        job_id: int,
        *,
        claimed_by: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> JobRecord | None:
        """Take over a ``running`` job whose lease lapsed; ``None`` if it did not.

        The killed-scheduler path (AC3). Same atomicity as :meth:`claim_job`,
        with ``state='running'`` and a *mandatory* expired lease as the guard —
        a job still under a live lease is never stolen. Liveness of the run's
        own pid is the caller's check (the registry owns that truth), not this
        store's.
        """
        moment = _at(now)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET claimed_by = ?, lease_until = ?, updated_at = ? "
                "WHERE id = ? AND state = 'running' "
                "AND (lease_until IS NULL OR lease_until < ?)",
                (
                    claimed_by,
                    (moment + timedelta(seconds=lease_seconds)).isoformat(),
                    moment.isoformat(),
                    job_id,
                    moment.isoformat(),
                ),
            )
            if cur.rowcount != 1:
                return None
        return self.get_job(job_id)

    def renew_lease(
        self,
        job_id: int,
        *,
        claimed_by: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        """Extend our own lease. ``False`` when we no longer hold the job.

        A lost renewal is not fatal — it means another scheduler already
        reclaimed the job (our process was presumed dead) — but the caller
        should stop treating the job as its own.
        """
        moment = _at(now)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET lease_until = ?, updated_at = ? "
                "WHERE id = ? AND state = 'running' AND claimed_by = ?",
                (
                    (moment + timedelta(seconds=lease_seconds)).isoformat(),
                    moment.isoformat(),
                    job_id,
                    claimed_by,
                ),
            )
            return cur.rowcount == 1

    def release_claim(
        self,
        job_id: int,
        *,
        claimed_by: str,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Give up a claim on shutdown (Ctrl-C) without losing the job.

        Two shapes, because "release" means different things either side of a
        started run:

        * **No ``run_id`` yet** — nothing was executed, so the job goes back to
          ``queued`` and the next scheduler starts it cleanly.
        * **A run exists** — the run is half-done and must be *resumed*, never
          restarted, so the job stays ``running`` and only its lease is expired
          (``lease_until = now``). :meth:`expired_running_jobs` then surfaces
          it to the next `sdlc queue run`, which re-enters via `sdlc resume`.
        """
        job = self.get_job(job_id)
        if job is None or job.claimed_by != claimed_by:
            return
        moment = _at(now).isoformat()
        with self._connect() as conn:
            if job.run_id:
                conn.execute(
                    "UPDATE jobs SET claimed_by = NULL, lease_until = ?, reason = ?, "
                    "updated_at = ? WHERE id = ? AND claimed_by = ?",
                    (moment, reason, moment, job_id, claimed_by),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET state = 'queued', claimed_by = NULL, "
                    "lease_until = NULL, reason = ?, updated_at = ? "
                    "WHERE id = ? AND claimed_by = ?",
                    (reason, moment, job_id, claimed_by),
                )

    def attach_run(self, job_id: int, run_id: str) -> None:
        """Link the job to the run its subprocess opened (Story 32.1-001 AC5)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET run_id = ?, updated_at = ? WHERE id = ?",
                (run_id, _now_iso(), job_id),
            )

    def finish_job(self, job_id: int, state: str, *, reason: str | None = None) -> None:
        """Stamp a job terminal (``done``/``failed``/``blocked``) and drop its lease."""
        if state not in _TERMINAL_STATES:
            raise QueueError(
                f"invalid terminal state: {state!r} "
                f"(expected one of {sorted(_TERMINAL_STATES)})"
            )
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET state = ?, claimed_by = NULL, lease_until = NULL, "
                "reason = ?, updated_at = ? WHERE id = ?",
                (state, reason, _now_iso(), job_id),
            )

    def set_reason(self, job_id: int, reason: str | None) -> None:
        """Record why a job is not progressing (e.g. ``repo busy``) without moving it."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET reason = ?, updated_at = ? WHERE id = ?",
                (reason, _now_iso(), job_id),
            )

    # --- the approval park (Story 32.2-002) -------------------------------

    def park_job(
        self,
        job_id: int,
        *,
        pr_number: int,
        reason: str,
        poll_after: datetime | None,
    ) -> None:
        """Park a job on ``pr_number`` pending a human's approval.

        ``AWAITING_APPROVAL`` is terminal for a *run* — ``build.py`` stops there
        deliberately, because the bugfix loop cannot self-approve — but it is not
        terminal for the *job*. Parking is the queue outliving the run: the job
        keeps its ``run_id`` (so the resume that follows is a resume, never a
        restart), records the CR to watch, and drops its claim and lease so its
        repo and its agent slot go straight back to the pool.

        Refuses a job with no ``run_id``: without a run there is nothing for an
        approval to release, and a job that never started belongs in ``queued``.
        """
        job = self.get_job(job_id)
        if job is None:
            raise QueueError(f"unknown job id: {job_id}")
        if not job.run_id:
            raise QueueError(
                f"cannot park job {job_id}: it opened no run to resume later"
            )
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET state = 'parked', claimed_by = NULL, "
                "lease_until = NULL, pr_number = ?, poll_after = ?, reason = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    pr_number,
                    poll_after.isoformat() if poll_after is not None else None,
                    reason,
                    _now_iso(),
                    job_id,
                ),
            )

    def schedule_poll(self, job_id: int, poll_after: datetime | None) -> None:
        """Set the earliest instant this job's change request may be read again.

        The rate-limit bound (AC4). Every poll — whether it learned something or
        the host was simply unreachable — pushes this out by the configured
        interval, so a `--follow` scheduler ticking every two seconds still
        makes one API read per job per interval.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET poll_after = ?, updated_at = ? WHERE id = ?",
                (
                    poll_after.isoformat() if poll_after is not None else None,
                    _now_iso(),
                    job_id,
                ),
            )

    def take_parked_job(
        self,
        job_id: int,
        *,
        claimed_by: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> JobRecord | None:
        """Take a ``parked`` job back under a lease; ``None`` when we may not.

        The approval landed (or the CR was merged by hand) and the queue is
        about to drive the job again — as a `sdlc resume` or a `sdlc reconcile`,
        both of which are real work in the repo, so the job returns to
        ``running`` and re-occupies its slot.

        Guarded exactly like :meth:`claim_job`, and for the same two reasons: a
        single atomic UPDATE means two schedulers polling the same CR produce
        one winner, and the ``NOT EXISTS`` predicate re-asserts per-repo
        exclusivity at the moment of the take rather than at the moment of the
        read. ``pr_number`` is deliberately kept — it is the record of which CR
        this job's work landed on — while ``poll_after`` is cleared, since a job
        that is running is no longer being polled.
        """
        moment = _at(now)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET state = 'running', claimed_by = ?, lease_until = ?, "
                "poll_after = NULL, reason = NULL, updated_at = ? "
                "WHERE id = ? AND state = 'parked' "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM jobs AS busy "
                "  WHERE busy.repo = jobs.repo AND busy.state = 'running'"
                ")",
                (
                    claimed_by,
                    (moment + timedelta(seconds=lease_seconds)).isoformat(),
                    moment.isoformat(),
                    job_id,
                ),
            )
            if cur.rowcount != 1:
                return None
        return self.get_job(job_id)

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

    def expired_running_jobs(self, *, now: datetime | None = None) -> list[JobRecord]:
        """``running`` jobs whose lease lapsed — reclaim candidates (AC3).

        A lapsed lease only means *no scheduler is renewing this job*. Whether
        the run itself is still alive is a separate question the caller answers
        against the registry's pid liveness; a job whose pid still answers is
        left alone.
        """
        if not self.db_path.exists():
            return []
        cutoff = _at(now).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state = 'running' "
                "AND (lease_until IS NULL OR lease_until < ?) "
                "ORDER BY id ASC",
                (cutoff,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def due_parked_jobs(self, *, now: datetime | None = None) -> list[JobRecord]:
        """``parked`` jobs whose change request is due for another read (AC1).

        Filtered in SQL on ``poll_after`` so a scheduler that loops every two
        seconds does no work — and issues no API call — for a job it polled four
        minutes ago. A ``NULL`` ``poll_after`` is due immediately: that is a job
        parked by an older controller (or one whose poll was explicitly reset),
        and stalling it forever would be the worse failure.

        Restricted to ``parked`` by the same predicate that makes polling stop
        at a terminal state: a job that has been cancelled, resumed or failed is
        simply not in this list any more.
        """
        if not self.db_path.exists():
            return []
        cutoff = _at(now).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state = 'parked' "
                "AND (poll_after IS NULL OR poll_after <= ?) "
                "ORDER BY id ASC",
                (cutoff,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def running_repos(self, *, kind: str | None = None) -> set[str]:
        """Repo paths with a ``running`` job — the per-repo exclusivity set (AC2).

        Read from the store rather than from one scheduler's in-memory state so
        two `sdlc queue run` processes on the same host still never put two
        runs in one repo. ``kind`` narrows to running jobs of that kind only —
        the scheduler uses ``kind="fix"`` (Story 32.1-003) to compute the
        ``fix_busy_repos`` half of :meth:`peek_claimable`'s exclusivity check.
        """
        if not self.db_path.exists():
            return set()
        query = "SELECT DISTINCT repo FROM jobs WHERE state = 'running'"
        params: tuple[str, ...] = ()
        if kind is not None:
            query += " AND kind = ?"
            params = (kind,)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return {row["repo"] for row in rows}

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
    # Story 32.2-002's columns are read defensively: the read verbs (`sdlc queue
    # list`, the dashboard's queue panel) deliberately never migrate the store,
    # so a queue.db written before this story still has to render. A missing
    # column reads as "unknown", not as a crash.
    keys = row.keys()
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
        pr_number=row["pr_number"] if "pr_number" in keys else None,
        poll_after=row["poll_after"] if "poll_after" in keys else None,
    )
