# ABOUTME: Host-level development queue — `sdlc build/fix --enqueue` records jobs here.
# ABOUTME: Stories 32.1-001/32.2-002. SQLite/WAL store for `sdlc queue list|add|cancel|
# ABOUTME: requeue|prioritise`, plus the approval park a scheduler re-polls.

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, Sequence

__all__ = [
    "CANCEL_REQUESTED_REASON",
    "CODEX_POOL",
    "DEFAULT_BUDGETS",
    "HEARTBEAT_SECONDS",
    "OFFLINE_AFTER_SECONDS",
    "PRIORITY_CLASSES",
    "UNSATISFIABLE_REASON_PREFIX",
    "JobBudget",
    "JobRecord",
    "QueuePause",
    "QueueBackend",
    "QueueError",
    "QueueStore",
    "WorkerRecord",
    "budget_breach",
    "budget_for",
    "default_priority",
    "default_queue_path",
    "fix_rounds_exhausted",
    "job_needs",
    "overlap_dependencies",
]

# Queue filename under the chosen state directory — a sibling of registry.json
# under the same host state dir (registry.py's default_registry_path()).
_QUEUE_NAME = "queue.db"

# What `queue list` shows beside a ``running`` job whose cancel is on its way to
# the worker (Story 35.4-003).
CANCEL_REQUESTED_REASON = "cancel requested"

# How long a contended writer waits out the WAL writer lock before erroring
# "database is locked". Mirrors the ledger's LEDGER_BUSY_TIMEOUT_MS
# (sdlc/build.py) — same discipline, smaller store.
QUEUE_BUSY_TIMEOUT_MS = 5000

_KINDS = {"build", "fix"}
_PRIORITIES = ("low", "normal", "high", "urgent")
# The public, ordered vocabulary (lowest class first). Story 32.3-001 AC1 talks
# about jobs being in a *higher* class than others, so the order has to be
# nameable outside this module — `PRIORITY_CLASSES.index(...)` is that name.
PRIORITY_CLASSES = _PRIORITIES
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
#
# Story 32.3-001 adds ``needs_attention`` — the park the queue's *own* budget
# breaker stamps when a job burns more fix rounds or wall-clock than its class
# allows. Deliberately distinct from ``blocked``: ``blocked`` means the run
# parked itself (rate limit, approval gate) or the scheduler refused to start it
# (stale controller); ``needs_attention`` means the job was running fine as far
# as the run knew and the *queue* stopped it. Two causes, two words, so
# `queue list` says which happened.
# Issue #709: every `blocked` reason the per-job version guard stamps starts
# with this marker, so `sdlc queue run --self-update` can requeue exactly its
# own guard parks after reinstalling — and nothing an operator must look at.
VERSION_GUARD_REASON_PREFIX = "controller version guard: "

_STATES = {
    "queued", "running", "done", "failed", "cancelled", "blocked", "parked",
    "needs_attention",
}

# The subset of :data:`_STATES` a job can finish in. ``running`` and
# ``queued`` are in-flight; ``cancelled`` is an operator action, not an
# outcome the scheduler reports.
_TERMINAL_STATES = {"done", "failed", "blocked", "needs_attention"}

# States an operator may retire at once. ``queued`` is the everyday case; ``blocked``
# is here because Story 32.1-002 introduced that park and it would otherwise be
# a dead end; ``parked`` is here because Story 32.2-002's approval wait must be
# abandonable when FX decides the change request is not going to be approved. A
# ``running`` job belongs to a live scheduler and its child, so cancelling it
# only *asks* that holder to stop (``cancel_requested``, Story 35.4-003); a
# ``done``/``failed`` job is history worth keeping and is not cancellable.
_CANCELLABLE_STATES = {"queued", "blocked", "parked", "needs_attention"}

# States an operator may re-arm with :meth:`QueueStore.requeue_job`. ``queued``
# is excluded because it is already armed, ``running`` because it is live.
_REQUEUEABLE_STATES = _STATES - {"running", "queued"}

# States in which a job is still "ahead" of an overlapping peer: it has not
# finished touching its files, so a job that shares one must wait. Every
# terminal (and ``cancelled``) releases the hold — see :meth:`QueueStore.overlap_holds`.
_PENDING_STATES = ("queued", "running")


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


# ---------------------------------------------------------------------------
# Policy: priority classes, per-class budgets, file-overlap serialisation.
# These are pure functions over queue rows plus each job's investigation output
# (Story 32.3-001) — no store, no scheduler, no clock, so they are testable on
# their own and reusable by any caller that has the same two inputs.
# ---------------------------------------------------------------------------


def default_priority(kind: str, labels: Iterable[str] = ()) -> str:
    """The class a job starts in, mirroring `sdlc fix all`'s dispatch order.

    `fix all` ranks its candidates *bugs, then enhancements, then the rest*
    (``fix_issue._category_rank``), and a repair is more urgent than new
    feature work. This maps that judgement onto the queue's four classes:

    ==================================  ==========
    job                                 class
    ==================================  ==========
    `fix` on an issue labelled ``bug``  ``urgent``
    any other `fix`                     ``high``
    `build`                             ``normal``
    ==================================  ==========

    So a `fix` job outranks a `build` job and a bug outranks an enhancement —
    AC1's two rules. The four classes are coarser than `fix all`'s three-way
    category rank, so an enhancement and an unlabelled fix share ``high``; that
    collapse is deliberate, since the queue's classes are also an *operator*
    vocabulary (`sdlc queue prioritise`) and splitting them further would make
    them harder to hold in the head than the ordering is worth.

    ``labels`` is optional because the enqueue path is deliberately offline —
    `sdlc fix --enqueue 42` must not call the forge — so the label half only
    applies where labels are already in hand (`sdlc queue add --label`).
    """
    if kind != "fix":
        return "normal"
    # Reuse `fix all`'s own predicates rather than re-deriving the vocabulary:
    # one definition of "is a bug", so the two orders cannot drift. Imported
    # lazily — fix_issue pulls in the whole pipeline, and queue.py is imported
    # by read-only callers (`sdlc doctor`, the dashboard) that must stay light.
    from sdlc.fix_issue import _is_bug

    return "urgent" if _is_bug(labels) else "high"


@dataclass(frozen=True)
class JobBudget:
    """One job's spend ceiling — the queue's runaway breaker (AC3).

    Two numbers, because a job runs away in two directions: it can thrash
    (``max_fix_rounds`` — how many ``bugfix`` stage attempts its run may burn)
    or it can hang (``wall_clock_seconds`` — how long one *launch* of the job
    may take). Either one exceeded parks the job ``needs_attention``.

    This is **not** the pipeline's own retry budget. `build.py`'s
    ``MAX_BUGFIX_ATTEMPTS`` bounds one *story's* bugfix loop from inside the
    run; this bounds the *job* from outside it, by reading the run's ledger.
    Two layers, two knobs: the inner one decides when a story gives up, the
    outer one decides when the host stops paying for the job at all.
    """

    max_fix_rounds: int
    wall_clock_seconds: int

    def to_dict(self) -> dict[str, int]:
        return {
            "max_fix_rounds": self.max_fix_rounds,
            "wall_clock_seconds": self.wall_clock_seconds,
        }

    def label(self) -> str:
        """A column-width summary for `sdlc queue list`, e.g. ``5r/4h``."""
        hours = self.wall_clock_seconds / 3600
        clock = f"{hours:g}h" if hours >= 1 else f"{self.wall_clock_seconds // 60}m"
        return f"{self.max_fix_rounds}r/{clock}"


# Per-class defaults. A more important job gets more rope, which is the whole
# point of having classes at all. The shape (a handful of fix rounds, a
# multi-hour job cap) follows Hyqs's tested figures scaled from its per-stage
# cap to a whole job: five rounds is the "thrashing, not progressing" line, and
# a job that has not finished in its class's hours has stopped being a job and
# started being a bill.
DEFAULT_BUDGETS: dict[str, JobBudget] = {
    "urgent": JobBudget(max_fix_rounds=8, wall_clock_seconds=8 * 3600),
    "high": JobBudget(max_fix_rounds=5, wall_clock_seconds=6 * 3600),
    "normal": JobBudget(max_fix_rounds=5, wall_clock_seconds=4 * 3600),
    "low": JobBudget(max_fix_rounds=3, wall_clock_seconds=2 * 3600),
}

# Host-wide overrides, in the ``SDLC_*`` env convention the rest of the
# controller uses (``SDLC_QUEUE_PATH``, ``SDLC_NOTIFY``). Blunt on purpose:
# they replace the value for *every* class, which is what a host that simply
# runs hotter or colder than the defaults actually wants. A malformed or
# non-positive value is ignored rather than honoured — disarming a breaker by
# typo is the one failure mode a breaker must not have.
#
# They are read at **stamp time** — `sdlc queue add`/`--enqueue` and `sdlc queue
# prioritise` — and never at drain time, because the budget is frozen onto the
# row (that freeze is what makes `queue list`'s ``5r/4h`` column honest). So
# setting them before `sdlc queue run` does nothing to jobs that are already
# enqueued; to change those, re-stamp them with `sdlc queue prioritise`. Every
# help string and doc that names these two says so.
_ENV_MAX_FIX_ROUNDS = "SDLC_QUEUE_MAX_FIX_ROUNDS"
_ENV_WALL_CLOCK_MINUTES = "SDLC_QUEUE_WALL_CLOCK_MINUTES"


def _positive_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def budget_for(priority: str) -> JobBudget:
    """The budget a job in class ``priority`` carries (AC4 — config, not constants).

    Falls back to the ``normal`` class for an unknown name so a hand-edited row
    can never leave a job *un*budgeted.
    """
    budget = DEFAULT_BUDGETS.get(priority, DEFAULT_BUDGETS["normal"])
    rounds = _positive_env(_ENV_MAX_FIX_ROUNDS)
    minutes = _positive_env(_ENV_WALL_CLOCK_MINUTES)
    if rounds is None and minutes is None:
        return budget
    return JobBudget(
        max_fix_rounds=rounds if rounds is not None else budget.max_fix_rounds,
        wall_clock_seconds=(
            minutes * 60 if minutes is not None else budget.wall_clock_seconds
        ),
    )


def fix_rounds_exhausted(budget: JobBudget, fix_rounds: int) -> bool:
    """Whether ``fix_rounds`` alone has exhausted ``budget`` — the *thrash* arm.

    Split out of :func:`budget_breach` because the caller needs the arm, not
    just the reason: a thrash park banks a fix-round baseline so the operator's
    `sdlc queue requeue` grants one more allowance, and a wall-clock park must
    not (it has not spent the rounds). One definition, so the reason string and
    the baseline decision can never disagree about what "thrashed" means.
    """
    return fix_rounds >= budget.max_fix_rounds


def budget_breach(
    budget: JobBudget, *, elapsed_seconds: float, fix_rounds: int
) -> str | None:
    """Why ``budget`` is exhausted, or ``None`` while it is not (AC3).

    Returns an operator-readable reason rather than a bool so the park recorded
    on the job says *which* ceiling was hit and by how much — "budget exceeded"
    on its own sends a human to read the ledger to find out what happened.

    ``fix_rounds`` is the rounds burned *since the job's fix-round baseline*
    (:meth:`QueueStore.record_fix_rounds_baseline`), not since the run opened —
    the caller subtracts. The two are the same number until the first thrash
    park, which is when the baseline starts to exist.
    """
    if fix_rounds_exhausted(budget, fix_rounds):
        return (
            f"budget exhausted: {fix_rounds} fix rounds "
            f"(cap {budget.max_fix_rounds})"
        )
    if elapsed_seconds > budget.wall_clock_seconds:
        return (
            f"budget exhausted: wall-clock {int(elapsed_seconds // 60)}m "
            f"(cap {budget.wall_clock_seconds // 60}m)"
        )
    return None


def overlap_dependencies(
    jobs: Sequence[tuple[int, str]],
    files_by_job: Mapping[int, set[str]],
) -> dict[int, list[int]]:
    """Serialisation edges between jobs in one repo whose files overlap (AC2).

    ``jobs`` is ``[(job_id, repo), …]`` — the queue rows — and ``files_by_job``
    is each job's investigated ``files_to_modify``. Returns
    ``{job_id: [predecessor_job_ids]}``: at most one edge per job, the chain
    predecessor, exactly as ``fix_issue.build_overlap_dependencies`` produces
    for one `fix all` batch.

    This *is* that function, applied once per repo instead of once per batch —
    the extension AC2 asks for. Grouping by repo first is the whole difference:
    two jobs in different checkouts cannot race a path, however identical their
    file lists look, so they must never be chained.

    The chain runs in ascending job id, i.e. oldest first, which is inherited
    from #436's "ascending issue number". Within an overlapping component that
    outranks the priority class — a component is a correctness constraint, and
    the classes order work *across* components. Nothing deadlocks: the head of
    every chain is always claimable.
    """
    from sdlc.fix_issue import build_overlap_dependencies

    by_repo: dict[str, dict[int, set[str]]] = defaultdict(dict)
    for job_id, repo in jobs:
        by_repo[repo][job_id] = set(files_by_job.get(job_id) or ())
    deps: dict[int, list[int]] = {}
    for repo_files in by_repo.values():
        deps.update(build_overlap_dependencies(repo_files))
    return deps


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
    # Story 32.3-001. ``budget`` is the class budget frozen at enqueue (JSON);
    # ``files`` is the job's investigated ``files_to_modify`` (JSON array),
    # which is what the repo-scoped overlap graph is built from;
    # ``fix_rounds_baseline`` is the run's cumulative bugfix-round count as of
    # the last thrash park, so the cap is measured from there rather than from
    # the run's birth. Zero (never parked) for every job until the breaker
    # fires, and for every row written before the column existed.
    budget: str | None = None
    files: str | None = None
    fix_rounds_baseline: int = 0
    # Story 35.1-001 (fleet execution). ``host`` pins the job to one machine and
    # ``pool`` to one subscription pool (both nullable = anywhere); ``requirements``
    # is JSON text — ``{repo, harness, sandbox}`` — a worker must satisfy;
    # ``worker`` is the fleet worker that holds the claim right now.
    host: str | None = None
    pool: str | None = None
    requirements: str | None = None
    worker: str | None = None
    # Story 35.4-003: an operator asked for this ``running`` job to stop. The
    # holder sees it on its next pass, kills the run's process group and
    # finishes the job ``cancelled``.
    cancel_requested: bool = False

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
            "budget": self.budget,
            "files": self.files,
            "fix_rounds_baseline": self.fix_rounds_baseline,
            "host": self.host,
            "pool": self.pool,
            "requirements": self.requirements,
            "worker": self.worker,
            "cancel_requested": self.cancel_requested,
        }

    def job_budget(self) -> JobBudget:
        """The budget recorded on this job, or its class default.

        Recorded rather than recomputed so an operator can see in `queue list`
        the ceiling the job is actually being held to; the class default is the
        fallback for a row written before this story or corrupted since, which
        must still leave the job *budgeted* rather than uncapped.
        """
        default = budget_for(self.priority)
        if not self.budget:
            return default
        try:
            data = json.loads(self.budget)
            return JobBudget(
                max_fix_rounds=int(data["max_fix_rounds"]),
                wall_clock_seconds=int(data["wall_clock_seconds"]),
            )
        except (ValueError, TypeError, KeyError):
            return default

    def files_to_modify(self) -> set[str]:
        """The job's investigated file set, or empty when it has none."""
        return _decode_files(self.files)


@dataclass
class QueuePause:
    """The queue's own state while it waits out one shared rate-limit window.

    Story 32.2-001. ``paused_until`` is the cached reset instant (ISO-8601 UTC);
    ``run_id``/``repo``/``source`` name whichever run *discovered* the window, so
    an operator can go read the authoritative story in that run's ledger.
    ``probed_at`` is the last live-API re-probe, throttling the probe across
    passes, restarts and peer schedulers. ``pool`` (Story 35.2-003) is the one
    subscription pool the window belongs to; ``None`` is the pool-less window of
    a local queue, which holds every claim.
    """

    paused_until: str
    paused_at: str
    reason: str | None = None
    run_id: str | None = None
    repo: str | None = None
    source: str | None = None
    probed_at: str | None = None
    pool: str | None = None

    def is_active(self, now: datetime | None = None) -> bool:
        """Whether dispatch is still held at ``now``.

        A ``paused_until`` that cannot be parsed (a hand-edited store) reads as
        *not paused*: an unreadable timestamp must never wedge the queue shut
        with no way to tell how long for.
        """
        try:
            until = datetime.fromisoformat(self.paused_until)
        except (TypeError, ValueError):
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return _at(now) < until

    def to_dict(self) -> dict:
        return {
            "paused_until": self.paused_until,
            "paused_at": self.paused_at,
            "reason": self.reason,
            "run_id": self.run_id,
            "repo": self.repo,
            "source": self.source,
            "probed_at": self.probed_at,
            "pool": self.pool,
        }


# Queue DDL. Deliberately mirrors the ledger's connect/init/migrate shape
# (sdlc/build.py's Ledger class) rather than sharing it: build.py's Ledger is
# entangled with the run state machine, and extracting a shared base at this
# story's scope would be a large, risky refactor for a five-point story. This
# is the documented debt from REVIEW.md item 5 — a later pass can lift both
# onto one shared helper once the ledger side is decoupled enough to move.
#
# One rate-limit window for the whole queue (Story 32.2-001). The Max plan is a
# *host* resource, so the pause is a property of the queue, not of N parked
# jobs: a single row (``CHECK (id = 1)`` makes "single" a schema fact rather than
# a convention) holding when dispatch may resume and what discovered the window.
# Rate-limit truth still lives in the run's ledger — this only *caches* the
# reset, so every repo waits the window out once instead of rediscovering it.
_QUEUE_STATE_DDL = """
CREATE TABLE IF NOT EXISTS queue_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    paused_until TIMESTAMP,
    reason       TEXT,
    run_id       TEXT,
    repo         TEXT,
    source       TEXT,
    paused_at    TIMESTAMP,
    probed_at    TIMESTAMP
);
"""

# Story 35.2-003: one rate-limit window per subscription pool. ``queue_state``'s
# single row could only say "the host is paused"; with two Claude subscriptions and
# a Codex one, a park must hold only the pool that hit it. ``pool`` is the key, with
# '' standing for the pool-less window of a local queue (the degenerate one-pool
# case of Story 32.2-001), so it stays a plain primary key rather than a nullable
# one SQLite would let repeat.
_QUEUE_PAUSES_DDL = """
CREATE TABLE IF NOT EXISTS queue_pauses (
    pool         TEXT PRIMARY KEY,
    paused_until TIMESTAMP,
    reason       TEXT,
    run_id       TEXT,
    repo         TEXT,
    source       TEXT,
    paused_at    TIMESTAMP,
    probed_at    TIMESTAMP
);
"""

# A queue.db written before pools carries its window in ``queue_state``: move it
# across as the pool-less pause, so an upgrade mid-window keeps waiting it out.
_QUEUE_PAUSES_BACKFILL = """
INSERT OR IGNORE INTO queue_pauses(pool, paused_until, reason, run_id, repo, source,
                                   paused_at, probed_at)
    SELECT '', paused_until, reason, run_id, repo, source, paused_at, probed_at
    FROM queue_state WHERE id = 1;
DELETE FROM queue_state;
"""

# Story 32.2-003: the audit trail of operator-declared clears. ``queue_state``
# is a single row deleted on resume, so without this the fact that an operator
# lifted a pause (and what it was holding) would leave no trace at all.
_PAUSE_CLEARS_DDL = """
CREATE TABLE IF NOT EXISTS queue_pause_clears (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cleared_at   TIMESTAMP NOT NULL,
    reason       TEXT NOT NULL,
    paused_until TIMESTAMP,
    runs_cleared INTEGER NOT NULL DEFAULT 0
);
"""

# Story 35.2-001: the worker registry. One row per fleet worker, rewritten by
# every heartbeat. The list columns are JSON text so a capability can grow
# without a migration; ``last_heartbeat`` is what "online" is judged from.
_WORKERS_DDL = """
CREATE TABLE IF NOT EXISTS workers (
    name           TEXT PRIMARY KEY,
    host           TEXT NOT NULL,
    pools          TEXT NOT NULL DEFAULT '[]',
    harnesses      TEXT NOT NULL DEFAULT '[]',
    sandbox        TEXT,
    repos          TEXT NOT NULL DEFAULT '[]',
    slots          INTEGER NOT NULL DEFAULT 1,
    slots_free     INTEGER NOT NULL DEFAULT 1,
    registered_at  TIMESTAMP NOT NULL,
    last_heartbeat TIMESTAMP NOT NULL
);
"""

_SCHEMA_DDL = (
    """
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
    poll_after  TIMESTAMP,
    budget      TEXT,
    files       TEXT,
    fix_rounds_baseline INTEGER,
    host        TEXT,
    pool        TEXT,
    requirements TEXT,
    worker      TEXT,
    cancel_requested INTEGER
);

CREATE TABLE IF NOT EXISTS _migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_repo  ON jobs(repo);
"""
    + _QUEUE_STATE_DDL
    + _PAUSE_CLEARS_DDL
    + _WORKERS_DDL
    + _QUEUE_PAUSES_DDL
)

# Schema migrations applied after the base DDL, in the same
# ``(version, name, table, columns, create_sql)`` shape as the ledger's
# ``_MIGRATIONS`` (sdlc/build.py). The fresh schema above already carries every
# column, so these only matter to a host queue.db written by an earlier version
# — it upgrades in place rather than being rebuilt. Versions are append-only:
# 1 is Story 32.2-002's, 2 and 3 are Story 32.3-001's, 4 is Story 32.2-001's, and
# future columns (e.g. a claim lease renewal field) take the next number so a host
# queue.db that has already applied the earlier ones does not re-run them.
_MIGRATIONS: list[tuple[int, str, str, list[tuple[str, str]], str | None]] = [
    (
        1,
        "approval_park",  # Story 32.2-002
        "jobs",
        [("pr_number", "INTEGER"), ("poll_after", "TIMESTAMP")],
        None,
    ),
    # Story 32.3-001: the per-class budget frozen on the job, and the
    # investigated file set the repo-scoped overlap graph is built from. Both
    # are nullable, so a pre-existing host queue.db upgrades in place and its
    # older rows simply fall back to their class default / no files.
    (
        2,
        "job_budget_and_files",
        "jobs",
        [("budget", "TEXT"), ("files", "TEXT")],
        None,
    ),
    # Story 32.3-001: the fix rounds already burned when the breaker last parked
    # the job. Its own column rather than a key inside ``budget`` because the
    # two are different kinds of fact — ``budget`` is policy copied from the
    # class (and re-stamped wholesale by `prioritise`), this is consumption
    # measured off the run's ledger, and a class change must not erase it.
    # Nullable, read as 0: a row from before this migration has banked nothing.
    (
        3,
        "job_fix_rounds_baseline",
        "jobs",
        [("fix_rounds_baseline", "INTEGER")],
        None,
    ),
    # Story 32.2-001: the host pause table, for a queue.db written before it
    # existed. The fresh schema above already carries it, so on a new store the
    # ``CREATE TABLE IF NOT EXISTS`` is a no-op and only the bookkeeping row is
    # written.
    (4, "queue_state_pause", "queue_state", [], _QUEUE_STATE_DDL),
    # Story 32.2-003: the operator-clear audit table.
    (5, "queue_pause_clears", "queue_pause_clears", [], _PAUSE_CLEARS_DDL),
    # Story 35.1-001: fleet columns — the host pin, the subscription pool, the
    # requirements a worker must meet, and which worker holds the claim. All
    # nullable, so an older queue.db upgrades in place and its rows read as
    # "run anywhere, held by nobody".
    (
        6,
        "fleet_job_columns",
        "jobs",
        [
            ("host", "TEXT"),
            ("pool", "TEXT"),
            ("requirements", "TEXT"),
            ("worker", "TEXT"),
        ],
        None,
    ),
    # Story 35.2-001: the worker registry table, for a queue.db written before it.
    (7, "fleet_workers", "workers", [], _WORKERS_DDL),
    # Story 35.2-003: per-pool pause windows, replacing ``queue_state``'s one row.
    (8, "queue_pauses", "queue_pauses", [], _QUEUE_PAUSES_DDL + _QUEUE_PAUSES_BACKFILL),
    # Story 35.4-003: a cancel aimed at a running job, for its worker to act on.
    (9, "job_cancel_requested", "jobs", [("cancel_requested", "INTEGER")], None),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending schema migrations on an open connection (idempotent).

    Mirrors ``sdlc.build._apply_migrations``; see the module docstring above
    the DDL for why this is a mirror rather than a shared helper. The
    idempotency check is name-aware, not version-only (Issue #621) — see
    ``sdlc.build._apply_migrations`` for why.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    applied = {
        r[0]: r[1] for r in conn.execute("SELECT version, name FROM _migrations").fetchall()
    }
    for version, name, table, columns, create_sql in _MIGRATIONS:
        if applied.get(version) == name:
            continue
        if create_sql:
            conn.executescript(create_sql)
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if existing:
            for col, col_type in columns:
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        conn.execute(
            "INSERT OR REPLACE INTO _migrations(version, name) VALUES (?, ?)",
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


# ---------------------------------------------------------------------------
# Fleet workers (Story 35.2-001): what a worker advertises, and the pure rules
# that decide whether it may run a job. No store, no clock — `claim_next`, the
# scheduler and the "why is this job stuck" stamp all ask the same two questions
# of the same functions, so they cannot drift apart.
# ---------------------------------------------------------------------------

# A worker heartbeats this often; silent for three beats it is offline and the
# leases it held become reclaimable.
HEARTBEAT_SECONDS = 30
OFFLINE_AFTER_SECONDS = 3 * HEARTBEAT_SECONDS

# The pool every worker that runs a `codex` stage must declare: Codex is one
# shared subscription, not a per-machine one.
CODEX_POOL = "codex-shared"

# Every reason this module stamps on a queued job starts with this, which is how
# it recognises (and clears) its own and leaves anyone else's alone.
UNSATISFIABLE_REASON_PREFIX = "no eligible worker"

# `requirements.sandbox` is a string: these read as "not needed" / "any runtime";
# anything else names the runtime the worker must have (e.g. ``podman``).
_SANDBOX_NOT_NEEDED = frozenset({"", "0", "false", "no", "none", "off"})
_SANDBOX_ANY = frozenset({"1", "true", "yes", "on", "any", "container", "sandbox"})


@dataclass(frozen=True)
class WorkerRecord:
    """One row of the ``workers`` table: a worker's last advertised capabilities."""

    name: str
    host: str
    registered_at: str
    last_heartbeat: str
    pools: list[str] = dataclasses.field(default_factory=list)
    harnesses: list[str] = dataclasses.field(default_factory=list)
    sandbox: str | None = None
    repos: list[str] = dataclasses.field(default_factory=list)
    slots: int = 1
    slots_free: int = 1

    def is_online(self, now: datetime | None = None) -> bool:
        """Heard from within :data:`OFFLINE_AFTER_SECONDS` (three missed beats)."""
        try:
            beat = datetime.fromisoformat(self.last_heartbeat)
        except ValueError:
            return False
        if beat.tzinfo is None:
            beat = beat.replace(tzinfo=timezone.utc)
        return (_at(now) - beat).total_seconds() <= OFFLINE_AFTER_SECONDS

    def to_dict(self, now: datetime | None = None) -> dict:
        return {**dataclasses.asdict(self), "online": self.is_online(now)}


_Check = Callable[[WorkerRecord], bool]
_Need = tuple[str, _Check]


def _csv(value: str | None) -> list[str]:
    """``"claude, codex"`` → ``["claude", "codex"]`` (ordered, de-duplicated)."""
    seen: dict[str, None] = {}
    for part in (value or "").split(","):
        if part.strip():
            seen[part.strip()] = None
    return list(seen)


def _job_requirements(job: JobRecord) -> dict[str, str]:
    if not job.requirements:
        return {}
    try:
        data = json.loads(job.requirements)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if value is not None}


def _on_host(host: str) -> _Check:
    return lambda worker: worker.host == host


def _has_repo(repo: str) -> _Check:
    return lambda worker: repo in worker.repos


def _has_harness(harness: str) -> _Check:
    return lambda worker: harness in worker.harnesses


def _in_pool(pool: str) -> _Check:
    return lambda worker: pool in worker.pools


def _has_sandbox(runtime: str | None) -> _Check:
    """Any container runtime (``None``), or exactly the named one."""
    if runtime is None:
        return lambda worker: bool(worker.sandbox)
    return lambda worker: (worker.sandbox or "").lower() == runtime


def job_needs(job: JobRecord) -> list[_Need]:
    """What a worker must have to run ``job``, as ``(label, satisfied-by)`` pairs.

    The labels are what `sdlc queue list` prints when nobody can run the job, so
    they read as needs: ``repo X``, ``harness codex``, ``sandbox``, ``host H``,
    ``pool P``. A job that routes any stage to ``codex`` (``harness`` may list
    several, ``claude,codex``) also needs the :data:`CODEX_POOL`.
    """
    needs: list[_Need] = []
    if job.host:
        needs.append((f"host {job.host}", _on_host(job.host)))
    requirements = _job_requirements(job)
    if requirements.get("repo"):
        needs.append((f"repo {requirements['repo']}", _has_repo(requirements["repo"])))
    harnesses = _csv(requirements.get("harness"))
    for harness in harnesses:
        needs.append((f"harness {harness}", _has_harness(harness)))
    sandbox = requirements.get("sandbox", "").strip().lower()
    if sandbox not in _SANDBOX_NOT_NEEDED:
        if sandbox in _SANDBOX_ANY:
            needs.append(("sandbox", _has_sandbox(None)))
        else:
            needs.append((f"sandbox {sandbox}", _has_sandbox(sandbox)))
    pools = ([job.pool] if job.pool else []) + ([CODEX_POOL] if "codex" in harnesses else [])
    for pool in dict.fromkeys(pools):
        needs.append((f"pool {pool}", _in_pool(pool)))
    return needs


def job_pools(job: JobRecord, worker_pools: Iterable[str] = ()) -> list[str]:
    """The subscription pools ``job`` would spend from when run by that worker.

    Story 35.2-003: the pool of each harness its routed stages use. A Claude
    stage spends the job's pinned ``pool``, else the worker's declared Claude
    pool (its first pool that is not :data:`CODEX_POOL`); a ``codex`` stage
    spends :data:`CODEX_POOL`. A job that names no harness is a Claude job. The
    answer is empty for a worker with no Claude pool, so such a job is held only
    by a pool-less (local) pause.
    """
    harnesses = _csv(_job_requirements(job).get("harness"))
    pools: list[str] = [job.pool] if job.pool else []
    if not job.pool and (not harnesses or "claude" in harnesses):
        pools += [p for p in worker_pools if p != CODEX_POOL][:1]
    if "codex" in harnesses:
        pools.append(CODEX_POOL)
    return list(dict.fromkeys(pools))


def _may_run(needs: Sequence[_Need], worker: WorkerRecord) -> bool:
    return all(satisfied(worker) for _, satisfied in needs)


def _unsatisfiable_reason(job: JobRecord, online: Sequence[WorkerRecord]) -> str | None:
    """``no eligible worker (needs …)`` when no online worker may run ``job``."""
    needs = job_needs(job)
    if not needs or any(_may_run(needs, worker) for worker in online):
        return None
    # Name what nobody has; when every need is met somewhere but never all at
    # once, the whole list is the honest answer.
    missing = [label for label, ok in needs if not any(ok(w) for w in online)]
    return f"{UNSATISFIABLE_REASON_PREFIX} (needs {', '.join(missing or [l for l, _ in needs])})"


class QueueBackend(Protocol):
    """What a queue consumer needs from "the queue" — local SQLite or the fleet.

    Story 35.1-002. The verbs the `sdlc queue serve` API exposes, no more:
    :class:`QueueStore` (a file on this host) and
    :class:`sdlc.queue_client.QueueClient` (the same verbs over HTTP) both
    satisfy it, and :func:`sdlc.queue_client.open_queue` picks one. The
    scheduler's internals (parks, polls, overlap holds) are not here: they have no
    service route, so `sdlc queue run` stays on a :class:`QueueStore`.
    """

    def init(self) -> None: ...

    def ensure_migrated(self) -> None: ...

    def add_job(
        self,
        *,
        repo: str,
        kind: str,
        scope: str,
        priority: str | None = None,
        options_json: str | None = None,
        labels: Iterable[str] = (),
        host: str | None = None,
        pool: str | None = None,
        requirements_json: str | None = None,
    ) -> int: ...

    def get_job(self, job_id: int) -> JobRecord | None: ...

    def list_jobs(self, repo: str | None = None) -> list[JobRecord]: ...

    def claim_next(
        self,
        *,
        claimed_by: str,
        lease_seconds: int,
        host: str | None = None,
        pools: Iterable[str] | None = None,
    ) -> JobRecord | None: ...

    def renew_lease(self, job_id: int, *, claimed_by: str, lease_seconds: int) -> bool: ...

    def release_claim(
        self, job_id: int, *, claimed_by: str, reason: str | None = None
    ) -> None: ...

    def finish_job(
        self,
        job_id: int,
        state: str,
        *,
        reason: str | None = None,
        claimed_by: str | None = None,
    ) -> bool: ...

    def cancel_job(self, job_id: int) -> None: ...

    def requeue_job(self, job_id: int) -> None: ...

    def prioritise_job(self, job_id: int, priority_class: str) -> None: ...

    def pause_dispatch(
        self,
        *,
        until: datetime,
        reason: str | None = None,
        run_id: str | None = None,
        repo: str | None = None,
        source: str | None = None,
        pool: str | None = None,
    ) -> bool: ...

    def dispatch_pause(self, pool: str | None = None) -> QueuePause | None: ...

    def dispatch_pauses(self) -> list[QueuePause]: ...

    def clear_pause(self, pool: str | None = None) -> None: ...

    def register_worker(
        self,
        name: str,
        *,
        host: str,
        pools: Iterable[str] = (),
        harnesses: Iterable[str] = (),
        sandbox: str | None = None,
        repos: Iterable[str] = (),
        slots: int = 1,
        slots_free: int | None = None,
    ) -> WorkerRecord: ...

    def list_workers(self) -> list[WorkerRecord]: ...


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
        priority: str | None = None,
        options_json: str | None = None,
        labels: Iterable[str] = (),
        host: str | None = None,
        pool: str | None = None,
        requirements_json: str | None = None,
    ) -> int:
        """Insert a fresh ``queued`` job and return its id.

        ``repo`` must already be resolved the way the registry resolves it
        (``str(Path(...).resolve())``) — this store does not re-resolve it.

        ``priority`` omitted derives the class from ``kind``/``labels`` via
        :func:`default_priority`, so an unattended enqueue lands in the same
        order `fix all` would have chosen; passing one explicitly is the
        operator override and is never second-guessed. Either way the class's
        budget (:func:`budget_for`) is frozen onto the row — recorded, not
        recomputed, so `queue list` shows the ceiling the job is held to.
        """
        if kind not in _KINDS:
            raise QueueError(
                f"invalid kind: {kind!r} (expected one of {sorted(_KINDS)})"
            )
        if priority is None:
            priority = default_priority(kind, labels)
        if priority not in _PRIORITY_RANK:
            raise QueueError(
                f"invalid priority: {priority!r} (expected one of {list(_PRIORITIES)})"
            )
        now = _now_iso()
        budget = json.dumps(budget_for(priority).to_dict())
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO jobs(repo, kind, scope, priority, state, options, "
                "created_at, updated_at, budget, host, pool, requirements) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo, kind, scope, priority, options_json, now, now, budget,
                    host, pool, requirements_json,
                ),
            )
            assert cur.lastrowid is not None  # INSERT always assigns a rowid
            return int(cur.lastrowid)

    def cancel_job(self, job_id: int) -> None:
        """Retire a job that is not live; ask the holder of a ``running`` one to stop.

        ``queued`` is the everyday case. A *parked* job (``blocked`` — Story
        32.1-002's version-check terminal) is accepted too: it is not live, so
        cancelling it is safe, and without this it would sit in `queue list`
        forever with no way out. A ``running`` job still belongs to a scheduler
        and its child process, so it is only *flagged* (Story 35.4-003): the
        holder sees ``cancel_requested`` on its next pass, kills the run's
        process group and finishes the job ``cancelled``. A ``running`` job
        nobody holds has no one to read the flag, so it is retired outright.
        """
        job = self.get_job(job_id)
        if job is None:
            raise QueueError(f"unknown job id: {job_id}")
        if job.state == "running":
            with self._connect() as conn:
                # The holder is re-checked in the UPDATE itself: a scheduler that
                # reclaimed the job since the read above now drives a live run,
                # and retiring the row under it would orphan that run.
                retired = conn.execute(
                    "UPDATE jobs SET state = 'cancelled', cancel_requested = 0, updated_at = ? "
                    "WHERE id = ? AND state = 'running' AND claimed_by IS NULL",
                    (_now_iso(), job_id),
                ).rowcount
                if not retired:
                    # Only the holder may stop a live run, so this just leaves it a note.
                    conn.execute(
                        "UPDATE jobs SET cancel_requested = 1, reason = ?, updated_at = ? "
                        "WHERE id = ? AND state = 'running'",
                        (CANCEL_REQUESTED_REASON, _now_iso(), job_id),
                    )
            return
        if job.state not in _CANCELLABLE_STATES:
            raise QueueError(
                f"cannot cancel job {job_id}: state is {job.state} "
                f"(cancellable: {', '.join(sorted(_CANCELLABLE_STATES | {'running'}))})"
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
          picks each story up at the stage it stopped in. If the scheduler then
          finds the run terminal (every story ``BLOCKED``/``FAILED``, nothing
          resumable) it calls :meth:`restart_fresh` instead: a new run for the
          same scope (#716).

        Refuses a ``running`` job (a live scheduler owns it — stop that
        scheduler instead) and a ``queued`` one (already armed; silently
        succeeding would hide a mistyped id). ``options``, ``priority``,
        ``kind`` and ``scope`` are untouched; only lifecycle fields move.

        Budgets are untouched here too, and deliberately so: the wall clock is
        measured from each *launch*, so the resumed job gets a fresh one for
        free, and the fix-round allowance was already re-based by the breaker
        when it parked the job (:meth:`record_fix_rounds_baseline`). Requeue
        therefore needs no ledger of its own — which is what keeps this store
        ignorant of runs.
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
        # The requeue *is* the operator's newer word, so any cancel flag left on
        # the row is stale — kept, it would re-cancel the job on its next pass.
        with self._connect() as conn:
            if job.run_id:
                conn.execute(
                    "UPDATE jobs SET state = 'running', claimed_by = NULL, worker = NULL, "
                    "lease_until = ?, reason = NULL, cancel_requested = 0, updated_at = ? "
                    "WHERE id = ?",
                    (moment, moment, job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET state = 'queued', claimed_by = NULL, worker = NULL, "
                    "lease_until = NULL, reason = NULL, cancel_requested = 0, updated_at = ? "
                    "WHERE id = ?",
                    (moment, job_id),
                )

    def restart_fresh(self, job_id: int, *, reason: str, now: datetime | None = None) -> None:
        """Turn a reclaimable job back into a clean ``queued`` launch (#716).

        For a job whose run is terminal (every story ``BLOCKED``/``FAILED``):
        `sdlc resume` has nothing to pick up, so the scope must start a *new*
        run that re-reads its inputs. Clears ``run_id`` and the lease;
        ``options``, ``priority``, ``kind`` and ``scope`` are untouched.
        """
        moment = _at(now).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET state = 'queued', run_id = NULL, claimed_by = NULL, worker = NULL, "
                "lease_until = NULL, reason = ?, updated_at = ? "
                "WHERE id = ? AND state = 'running'",
                (reason, moment, job_id),
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
        # The budget belongs to the class, so moving classes moves the budget
        # (AC3/AC4). Leaving the old one behind would make `prioritise` a
        # half-move: a job promoted to `urgent` would still be capped as `low`.
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET priority = ?, budget = ?, updated_at = ? WHERE id = ?",
                (
                    priority_class,
                    json.dumps(budget_for(priority_class).to_dict()),
                    _now_iso(),
                    job_id,
                ),
            )

    def record_files(self, job_id: int, paths: Iterable[str]) -> None:
        """Record a job's investigated ``files_to_modify`` (Story 32.3-001 AC2).

        The write side of the repo-scoped overlap graph: once a job's
        investigation says which files it will touch, an overlapping peer in the
        same repo must wait for it (:meth:`overlap_holds`). Its production
        caller is the scheduler (``_Scheduler._record_plan_files``), which reads
        the plan from the run's own ledger. Today per-repo exclusivity already
        serialises everything in one checkout, so this narrows nothing yet.

        Note the horizon this buys, and the one it does not. A footprint is only
        knowable *after* a job has run far enough to freeze an investigation, so
        the graph can hold a job whose **recorded** footprint overlaps a pending
        peer — a requeued job held on the strength of what it touched last time —
        and can never hold two never-run jobs whose investigations would collide.
        Story 32.1-003, which lets two non-overlapping jobs share a checkout,
        therefore needs a start-time check of its own (investigate before claim,
        or re-check once the plan lands) *on top of* this graph, not just this
        graph. See :meth:`overlap_holds`.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET files = ?, updated_at = ? WHERE id = ?",
                (json.dumps(sorted({str(p) for p in paths})), _now_iso(), job_id),
            )

    def record_fix_rounds_baseline(self, job_id: int, rounds: int) -> None:
        """Bank the fix rounds burned so far, so the cap counts from here.

        Written by the scheduler's breaker at the moment it parks a job for
        thrashing (``_Scheduler._enforce_budgets``), and only for that arm — a
        wall-clock park has not spent the rounds, so banking them there would
        hand the job a second allowance it never earned.

        This is what makes `sdlc queue requeue` a real exit from a fix-round
        park. ``ledger_fix_rounds`` counts every ``bugfix`` row the *run* has
        ever recorded, and a requeue re-enters that same run, so without a
        baseline the resumed job is over its cap on its first poll and is
        stopped again having done nothing. With it, the operator's decision to
        keep paying buys exactly one more class budget — the breaker still
        fires, one allowance later, which is the point of it being a breaker.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET fix_rounds_baseline = ?, updated_at = ? WHERE id = ?",
                (int(rounds), _now_iso(), job_id),
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


    # --- dispatch pauses, one window per pool (Stories 32.2-001, 35.2-003) --

    def pause_dispatch(
        self,
        *,
        until: datetime,
        reason: str | None = None,
        run_id: str | None = None,
        repo: str | None = None,
        source: str | None = None,
        pool: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Hold dispatch for ``pool`` until ``until``. ``True`` when *this* call opened it.

        One window per subscription pool: a subscription is exhausted once, so
        the queue records the reset once and every scheduler reads it. ``pool``
        omitted is the pool-less window of a local queue, which holds every
        claim — the single-pause behaviour of Story 32.2-001. The return value is
        what keeps the announcement singular — the caller notifies only on
        ``True``, so a second job hitting the same wall inside the window is
        silent rather than one notify per repo.

        An already-open window is *extended* to the later reset and never
        shortened: a second signal carrying a longer wait is new information,
        while one carrying a shorter one would resume dispatch early into a
        still-closed window. The discovering run keeps its attribution either
        way — it is the one whose ledger holds the authoritative story.
        """
        moment = _at(now)
        existing = self.dispatch_pause(pool)
        active = existing is not None and existing.is_active(moment)
        if active:
            assert existing is not None  # narrowed by `active`
            current = datetime.fromisoformat(existing.paused_until)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            if until > current:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE queue_pauses SET paused_until = ? WHERE pool = ?",
                        (until.isoformat(), pool or ""),
                    )
            return False
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO queue_pauses(pool, paused_until, reason, run_id, repo, "
                "source, paused_at, probed_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(pool) DO UPDATE SET paused_until = excluded.paused_until, "
                "reason = excluded.reason, run_id = excluded.run_id, "
                "repo = excluded.repo, source = excluded.source, "
                "paused_at = excluded.paused_at, probed_at = NULL",
                (
                    pool or "", until.isoformat(), reason, run_id, repo, source,
                    moment.isoformat(),
                ),
            )
        return True

    def clear_pause(self, pool: str | None = None) -> None:
        """Resume dispatch (idempotent): one ``pool``'s window, or every window.

        Note the asymmetry with :meth:`dispatch_pause`, where ``None`` names the
        pool-less window: a clear that names no pool is the operator's "resume
        everything", and the only clear a local queue ever needs.
        """
        if not self.db_path.exists():
            return
        with self._connect() as conn:
            try:
                if pool is None:
                    conn.execute("DELETE FROM queue_pauses")
                else:
                    conn.execute("DELETE FROM queue_pauses WHERE pool = ?", (pool,))
            except sqlite3.OperationalError:
                return  # a queue.db from before the table — nothing recorded

    def record_pause_clear(
        self,
        *,
        paused_until: str | None,
        runs_cleared: int,
        reason: str = "operator",
        now: datetime | None = None,
    ) -> None:
        """Audit one operator-declared clear (Story 32.2-003): when, why, what."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO queue_pause_clears(cleared_at, reason, paused_until, "
                "runs_cleared) VALUES (?, ?, ?, ?)",
                (_at(now).isoformat(), reason, paused_until, runs_cleared),
            )

    def pause_clears(self) -> list[dict]:
        """Every recorded operator clear, oldest first."""
        if not self.db_path.exists():
            return []
        with self._connect() as conn:
            try:
                rows = conn.execute(
                    "SELECT cleared_at, reason, paused_until, runs_cleared "
                    "FROM queue_pause_clears ORDER BY id"
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]

    def mark_pause_probed(
        self, pool: str | None = None, *, now: datetime | None = None
    ) -> None:
        """Stamp ``pool``'s last live-API re-probe, so the throttle survives a restart."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE queue_pauses SET probed_at = ? WHERE pool = ?",
                (_at(now).isoformat(), pool or ""),
            )

    def dispatch_pause(self, pool: str | None = None) -> QueuePause | None:
        """``pool``'s recorded window (``None`` = the pool-less one), or ``None``.

        Deliberately *raw*: an elapsed window is still returned, because the
        difference between "was paused, the window just reopened" (announce a
        resume, clear the row) and "never paused" (say nothing) is exactly what
        keeps the resume notification singular. Callers ask
        :meth:`QueuePause.is_active` for the live question.
        """
        return next((p for p in self.dispatch_pauses() if p.pool == pool), None)

    def dispatch_pauses(self) -> list[QueuePause]:
        """Every recorded window, one per pool, oldest pause first (raw, see above)."""
        if not self.db_path.exists():
            return []
        with self._connect() as conn:
            try:
                rows = conn.execute(
                    "SELECT * FROM queue_pauses ORDER BY paused_at, pool"
                ).fetchall()
            except sqlite3.OperationalError:
                # A queue.db from before this table that no writer has migrated
                # yet — not paused, and a read must never create the table.
                return []
        return [
            QueuePause(
                paused_until=row["paused_until"],
                paused_at=row["paused_at"],
                reason=row["reason"],
                run_id=row["run_id"],
                repo=row["repo"],
                source=row["source"],
                probed_at=row["probed_at"],
                pool=row["pool"] or None,
            )
            for row in rows
        ]

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

        Jobs held back by a file overlap with an unfinished peer in the same
        repo (Story 32.3-001 AC2, :meth:`overlap_holds`) are filtered out here
        too, for the same reason and in the same place: a candidate the
        scheduler must not start is not a candidate.
        """
        if not self.db_path.exists():
            return []
        cutoff = _at(now).isoformat()
        any_busy = busy_repos or frozenset()
        fix_busy = fix_busy_repos or frozenset()
        held = self.overlap_holds()
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
            if not _blocked(record) and record.id not in held
        ]

    def overlap_holds(self) -> dict[int, int]:
        """``{held_job_id: predecessor_job_id}`` for file-overlapping peers (AC2).

        Builds the repo-scoped overlap graph (:func:`overlap_dependencies`) over
        every job that is still *pending* — ``queued`` or ``running`` — and
        reports each job whose chain predecessor has not finished with it yet.
        Restricting the graph to pending jobs is what makes the hold *release*:
        a predecessor that reaches any terminal (or is cancelled) drops out of
        the graph, so its successor becomes claimable on the very next pass with
        no extra bookkeeping to get wrong.

        A job with no recorded footprint is a singleton and is never held: the
        guarantee is over *recorded* file sets, not over the files two never-run
        jobs would turn out to want. :meth:`record_files` spells out what that
        leaves for Story 32.1-003 to add.
        """
        if not self.db_path.exists():
            return {}
        with self._connect() as conn:
            # Two literal placeholders rather than a generated IN-list: the SQL
            # stays static (nothing to inject into) and the pair below is the
            # one thing to keep in step with :data:`_PENDING_STATES`.
            queued, running = _PENDING_STATES
            rows = conn.execute(
                "SELECT id, repo, files FROM jobs WHERE state IN (?, ?)",
                (queued, running),
            ).fetchall()
        pending = {row["id"] for row in rows}
        jobs = [(row["id"], row["repo"]) for row in rows]
        files_by_job = {
            row["id"]: _decode_files(_optional_column(row, "files")) for row in rows
        }
        if not any(files_by_job.values()):
            # The common case by far — nothing has been investigated yet, so
            # there is no graph to build. Skip the (lazy, heavy) fix_issue
            # import entirely rather than pay for an all-singleton answer.
            return {}
        holds: dict[int, int] = {}
        for job_id, deps in overlap_dependencies(jobs, files_by_job).items():
            blocking = [dep for dep in deps if dep in pending]
            if blocking:
                holds[job_id] = blocking[0]
        return holds

    def claim_job(
        self,
        job_id: int,
        *,
        claimed_by: str,
        lease_seconds: int,
        now: datetime | None = None,
        worker: str | None = None,
    ) -> JobRecord | None:
        """Take a ``queued`` job under a lease; ``None`` when someone beat us.

        ``worker`` (Story 35.1-001) records which fleet worker holds the claim;
        a local scheduler leaves it ``None``.

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
                "reason = NULL, updated_at = ?, worker = ? "
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
                    worker,
                    job_id,
                    moment.isoformat(),
                ),
            )
            if cur.rowcount != 1:
                return None
        return self.get_job(job_id)

    def claim_next(
        self,
        *,
        claimed_by: str,
        lease_seconds: int,
        host: str | None = None,
        pools: Iterable[str] | None = None,
        now: datetime | None = None,
    ) -> JobRecord | None:
        """Claim the best job this worker may run; ``None`` when there is none.

        The fleet's one claim verb (Story 35.1-001): :meth:`peek_claimable` in
        dispatch order, filtered to what ``host``/``pools`` may run, then the
        guarded :meth:`claim_job` UPDATE — which stays the sole arbiter, so two
        workers racing for one row still produce exactly one winner and the
        loser simply tries the next candidate. A held dispatch window
        (:meth:`dispatch_pause`) is the queue's, not any one worker's: a
        pool-less window yields nothing, and a pool's window yields none of the
        jobs that would spend from that pool (:func:`job_pools`) — the claimer's
        other pools keep flowing.

        A job pinned to a ``host`` goes only to a worker on that host; a job in
        a ``pool`` only to a worker serving it. An unpinned/unpooled job goes
        anywhere. A claimer that has registered (:meth:`register_worker`, Story
        35.2-001) is matched on its advertised repos, harnesses, sandbox, host
        and pools instead, and the least-loaded eligible worker gets the job;
        an unregistered claimer keeps the host/pool-only rules above.
        """
        moment = _at(now)
        self.expire_offline_leases(now=moment)
        self.stamp_unsatisfiable(now=moment)
        paused = {p.pool for p in self.dispatch_pauses() if p.is_active(moment)}
        if None in paused:
            return None  # the pool-less (local) window holds every claim
        served = set(pools or ())
        candidates = self.peek_claimable(
            busy_repos=self.running_repos(),
            fix_busy_repos=self.running_repos(kind="fix"),
            now=now,
        )
        registered = self.get_worker(claimed_by)
        if paused:
            # A pool's window holds only the jobs that would spend from it
            # (Story 35.2-003); the same worker keeps claiming from the others.
            worker_pools = registered.pools if registered is not None else list(pools or ())
            candidates = [
                job for job in candidates if paused.isdisjoint(job_pools(job, worker_pools))
            ]
        if registered is not None:
            # A registered worker is matched on what it advertised (Story
            # 35.2-001), not on what this call says: the registry is the truth.
            candidates = self.claimable_for_worker(claimed_by, candidates, now=moment)
        else:
            candidates = [
                job
                for job in candidates
                if (job.host is None or job.host == host)
                and (job.pool is None or job.pool in served)
            ]
        for job in candidates:
            claimed = self.claim_job(
                job.id,
                claimed_by=claimed_by,
                lease_seconds=lease_seconds,
                now=now,
                worker=claimed_by,
            )
            if claimed is not None:
                if registered is not None:
                    self._take_slot(claimed_by)
                return claimed
        return None

    # --- the worker registry (Story 35.2-001) ------------------------------

    def register_worker(
        self,
        name: str,
        *,
        host: str,
        pools: Iterable[str] = (),
        harnesses: Iterable[str] = (),
        sandbox: str | None = None,
        repos: Iterable[str] = (),
        slots: int = 1,
        slots_free: int | None = None,
        now: datetime | None = None,
    ) -> WorkerRecord:
        """Record (or refresh) a worker's capabilities — registration *is* the heartbeat.

        One verb for both because a heartbeat should carry the current picture
        anyway (a repo cloned since the last beat, a slot freed): the first call
        creates the row, every later one rewrites it and keeps ``registered_at``.
        Each call also sweeps the fleet — leases held by workers that have gone
        silent are made reclaimable, and queued jobs are re-judged for whether
        anyone can run them — so those states move on the beat, not on a timer.
        """
        name = name.strip()
        host = host.strip()
        if not name:
            raise QueueError("worker name must not be blank")
        if not host:
            raise QueueError("worker host must not be blank")
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
            raise QueueError(f"worker slots must be a positive integer, got {slots!r}")
        free = slots if slots_free is None else max(0, min(int(slots_free), slots))
        moment = _at(now).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO workers(name, host, pools, harnesses, sandbox, repos, slots, "
                "slots_free, registered_at, last_heartbeat) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET host = excluded.host, pools = excluded.pools, "
                "harnesses = excluded.harnesses, sandbox = excluded.sandbox, "
                "repos = excluded.repos, slots = excluded.slots, "
                "slots_free = excluded.slots_free, last_heartbeat = excluded.last_heartbeat",
                (
                    name,
                    host,
                    json.dumps(list(pools)),
                    json.dumps(list(harnesses)),
                    sandbox or None,
                    json.dumps(list(repos)),
                    slots,
                    free,
                    moment,
                    moment,
                ),
            )
        self.expire_offline_leases(now=now)
        self.stamp_unsatisfiable(now=now)
        record = self.get_worker(name)
        assert record is not None  # just written
        return record

    def get_worker(self, name: str) -> WorkerRecord | None:
        return next((w for w in self.list_workers() if w.name == name), None)

    def list_workers(self) -> list[WorkerRecord]:
        """Every registered worker, by name. A queue.db without the table lists none."""
        if not self.db_path.exists():
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM workers ORDER BY name").fetchall()
        except sqlite3.OperationalError:  # no `workers` table yet: nothing has registered
            return []
        return [_row_to_worker(row) for row in rows]

    def _take_slot(self, name: str) -> None:
        """Count a fresh claim against the worker until its next heartbeat says otherwise."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE workers SET slots_free = MAX(0, slots_free - 1) WHERE name = ?", (name,)
            )

    def expire_offline_leases(self, *, now: datetime | None = None) -> int:
        """Make the leases of silent workers reclaimable; how many jobs that freed.

        A worker that misses three heartbeats is presumed gone, so its running
        jobs' leases are cut to now — :meth:`expired_running_jobs` then offers
        them to whoever drives the queue, which still checks the run's own pid
        before resuming, so a worker that merely lost its network loses nothing.
        """
        moment = _at(now)
        offline = [w.name for w in self.list_workers() if not w.is_online(moment)]
        if not offline:
            return 0
        marks = ", ".join("?" for _ in offline)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET lease_until = ?, updated_at = ? "
                f"WHERE state = 'running' AND worker IN ({marks}) "
                "AND (lease_until IS NULL OR lease_until > ?)",
                (moment.isoformat(), moment.isoformat(), *offline, moment.isoformat()),
            )
            return cur.rowcount

    def stamp_unsatisfiable(self, *, now: datetime | None = None) -> int:
        """Say, on each queued job nobody can run, what it is missing; how many changed.

        Judged against the *online* workers only, and only once any worker has
        ever registered — a host that never ran the fleet keeps its queue
        untouched. A job that becomes runnable has its stamp removed; any other
        reason on the job (``repo busy``, an overlap hold) is left as found.
        """
        workers = self.list_workers()
        if not workers:
            return 0
        moment = _at(now)
        online = [w for w in workers if w.is_online(moment)]
        changed = 0
        for job in (j for j in self.list_jobs() if j.state == "queued"):
            reason = _unsatisfiable_reason(job, online)
            if reason is not None and job.reason != reason:
                self.set_reason(job.id, reason)
                changed += 1
            elif (
                reason is None
                and job.reason is not None
                and job.reason.startswith(UNSATISFIABLE_REASON_PREFIX)
            ):
                self.set_reason(job.id, None)
                changed += 1
        return changed

    def claimable_for_worker(
        self,
        name: str,
        candidates: Iterable[JobRecord],
        *,
        slots_free: int | None = None,
        now: datetime | None = None,
    ) -> list[JobRecord]:
        """The candidates (dispatch order kept) worker ``name`` may take right now.

        A job is kept when the worker meets every need (:func:`job_needs`) *and*
        no other online worker that also meets them has strictly more free slots
        — least loaded first, with a tie left to whoever asks first. A worker
        with no free slot keeps nothing. ``slots_free`` overrides the last
        heartbeat's figure for a caller (the scheduler) that knows better.
        """
        candidates = list(candidates)
        workers = self.list_workers()
        me = next((w for w in workers if w.name == name), None)
        if me is None:
            return candidates
        if slots_free is not None:
            me = dataclasses.replace(me, slots_free=slots_free)
        if me.slots_free <= 0:
            return []
        moment = _at(now)
        rivals = [
            w
            for w in workers
            if w.name != name and w.is_online(moment) and w.slots_free > me.slots_free
        ]
        kept: list[JobRecord] = []
        for job in candidates:
            needs = job_needs(job)
            if _may_run(needs, me) and not any(_may_run(needs, rival) for rival in rivals):
                kept.append(job)
        return kept


    def reclaim_job(
        self,
        job_id: int,
        *,
        claimed_by: str,
        lease_seconds: int,
        now: datetime | None = None,
        worker: str | None = None,
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
                "UPDATE jobs SET claimed_by = ?, lease_until = ?, updated_at = ?, "
                "worker = ? WHERE id = ? AND state = 'running' "
                "AND (lease_until IS NULL OR lease_until < ?)",
                (
                    claimed_by,
                    (moment + timedelta(seconds=lease_seconds)).isoformat(),
                    moment.isoformat(),
                    worker,
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
            if job.cancel_requested:
                # The operator already said stop: handing the job back would
                # have the next scheduler resume (or restart) what was cancelled.
                # The release's own reason (an interrupt, a rate-limit wait)
                # would read as if the job were coming back, so it is not kept.
                conn.execute(
                    "UPDATE jobs SET state = 'cancelled', claimed_by = NULL, worker = NULL, "
                    "lease_until = NULL, cancel_requested = 0, reason = ?, updated_at = ? "
                    "WHERE id = ? AND claimed_by = ?",
                    ("cancelled by operator", moment, job_id, claimed_by),
                )
            elif job.run_id:
                conn.execute(
                    "UPDATE jobs SET claimed_by = NULL, worker = NULL, lease_until = ?, reason = ?, "
                    "updated_at = ? WHERE id = ? AND claimed_by = ?",
                    (moment, reason, moment, job_id, claimed_by),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET state = 'queued', claimed_by = NULL, worker = NULL, "
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

    def finish_job(
        self,
        job_id: int,
        state: str,
        *,
        reason: str | None = None,
        claimed_by: str | None = None,
    ) -> bool:
        """Stamp a job terminal and drop its lease; ``False`` when nothing matched.

        The four terminals are ``done``, ``failed``, ``blocked`` (the run parked
        itself, or the scheduler refused to start it) and ``needs_attention``
        (the queue's own budget breaker stopped it); ``cancelled`` is also
        accepted, for a holder that has stopped a job whose cancel was requested.

        ``claimed_by`` (Story 35.1-001) stamps the job only while that holder
        still owns the claim, checked in the UPDATE itself — a check read first
        could be overtaken by another process's reclaim.
        """
        finishable = _TERMINAL_STATES | {"cancelled"}  # the holder's ack of a cancel request
        if state not in finishable:
            raise QueueError(
                f"invalid terminal state: {state!r} "
                f"(expected one of {sorted(finishable)})"
            )
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET state = ?, claimed_by = NULL, worker = NULL, lease_until = NULL, "
                "cancel_requested = 0, "
                "reason = ?, updated_at = ? WHERE id = ? AND (? IS NULL OR claimed_by = ?)",
                (state, reason, _now_iso(), job_id, claimed_by, claimed_by),
            )
            return cur.rowcount == 1

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
    ) -> bool:
        """Park a job on ``pr_number`` pending a human's approval.

        ``AWAITING_APPROVAL`` is terminal for a *run* — ``build.py`` stops there
        deliberately, because the bugfix loop cannot self-approve — but it is not
        terminal for the *job*. Parking is the queue outliving the run: the job
        keeps its ``run_id`` (so the resume that follows is a resume, never a
        restart), records the CR to watch, and drops its claim and lease so its
        repo and its agent slot go straight back to the pool.

        A job whose cancel was requested (Story 35.4-003) is finished
        ``cancelled`` instead, and ``False`` returned: parking would hand it back
        for an approval to resume — the very thing the operator stopped — the
        same call :meth:`release_claim` makes. Its ``pr_number`` is still kept.
        So is a job already ``cancelled``: a peer drain retired it while its
        holder stalled, and that holder's late park must not bring it back.

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
            # One transaction: a cancel landing mid-park is either honoured here
            # or refused by cancel_job's ``state = 'running'`` guard — never left
            # on a parked row for a take-back or a requeue to act on later.
            cancelled = conn.execute(
                "UPDATE jobs SET state = 'cancelled', claimed_by = NULL, worker = NULL, "
                "lease_until = NULL, cancel_requested = 0, pr_number = ?, reason = ?, "
                "updated_at = ? WHERE id = ? AND (cancel_requested = 1 OR state = 'cancelled')",
                (pr_number, "cancelled by operator", _now_iso(), job_id),
            ).rowcount
            if cancelled:
                return False
            conn.execute(
                "UPDATE jobs SET state = 'parked', claimed_by = NULL, worker = NULL, "
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
        return True

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
        worker: str | None = None,
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
                "poll_after = NULL, reason = NULL, cancel_requested = 0, updated_at = ?, "
                "worker = ? WHERE id = ? AND state = 'parked' "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM jobs AS busy "
                "  WHERE busy.repo = jobs.repo AND busy.state = 'running'"
                ")",
                (
                    claimed_by,
                    (moment + timedelta(seconds=lease_seconds)).isoformat(),
                    moment.isoformat(),
                    worker,
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
        budget=_optional_column(row, "budget"),
        files=_optional_column(row, "files"),
        fix_rounds_baseline=int(_optional_column(row, "fix_rounds_baseline") or 0),
        host=_optional_column(row, "host"),
        pool=_optional_column(row, "pool"),
        requirements=_optional_column(row, "requirements"),
        worker=_optional_column(row, "worker"),
        cancel_requested=bool(_optional_column(row, "cancel_requested")),
    )


def _row_to_worker(row: sqlite3.Row) -> WorkerRecord:
    def _names(column: str) -> list[str]:
        try:
            parsed = json.loads(row[column] or "[]")
        except ValueError:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []

    return WorkerRecord(
        name=row["name"],
        host=row["host"],
        registered_at=row["registered_at"],
        last_heartbeat=row["last_heartbeat"],
        pools=_names("pools"),
        harnesses=_names("harnesses"),
        sandbox=row["sandbox"],
        repos=_names("repos"),
        slots=int(row["slots"]),
        slots_free=int(row["slots_free"]),
    )


def _decode_files(raw: str | None) -> set[str]:
    """A ``files`` column's JSON array as a set; junk/absent reads as empty."""
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return set()
    if not isinstance(parsed, list):
        return set()
    return {str(item) for item in parsed}


def _optional_column(row: sqlite3.Row, name: str) -> Any:
    """``row[name]`` when the column exists, else ``None``.

    A read verb never migrates (:meth:`QueueStore.ensure_migrated` is explicit),
    so `sdlc queue list` can legitimately meet a pre-32.3-001 queue.db that has
    no ``budget``/``files``/``fix_rounds_baseline`` columns. Missing reads as
    absent, not as a crash.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return None
