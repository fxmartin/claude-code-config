# ABOUTME: Deterministic build-stories state machine ported from the skill (7.3-001).
# ABOUTME: Owns preflight, cohorts, agent dispatch, schema validation, ledger writes.

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Protocol

from sdlc.cohort import Story, compute_cohorts, truncate_queue
from sdlc.contracts import RESULT_END_MARKER, RESULT_START_MARKER, ContractError
from sdlc.dispatch import AgentDispatchError, AgentResult, dispatch_agent

# Maximum bugfix iterations per story before giving up — mirrors the skill's
# "max 2 bugfix iterations" rule (Step 5d2) so behaviour matches the playbook.
MAX_BUGFIX_ATTEMPTS = 2

# Canonical ledger DDL. Kept in sync with state/schema.sql (Epic-04). Embedded
# here so the controller can create a ledger even when installed standalone via
# `uv tool install` with no repo checkout in reach.
_SCHEMA_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    scope           TEXT,
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    mode            TEXT,
    total_stories   INTEGER DEFAULT 0,
    completed       INTEGER DEFAULT 0,
    failed          INTEGER DEFAULT 0,
    status          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stories (
    run_id          TEXT NOT NULL,
    story_id        TEXT NOT NULL,
    epic_id         TEXT,
    title           TEXT,
    priority        TEXT,
    points          INTEGER,
    agent_type      TEXT,
    branch          TEXT,
    pr_number       INTEGER,
    current_stage   TEXT,
    status          TEXT NOT NULL,
    PRIMARY KEY (run_id, story_id),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS stages (
    run_id              TEXT NOT NULL,
    story_id            TEXT NOT NULL,
    stage_name          TEXT NOT NULL,
    attempt             INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL,
    started_at          TIMESTAMP,
    finished_at         TIMESTAMP,
    failure_category    TEXT,
    output_path         TEXT,
    session_id          TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_creation_tokens INTEGER,
    cost_usd            REAL,
    PRIMARY KEY (run_id, story_id, stage_name, attempt),
    FOREIGN KEY (run_id, story_id) REFERENCES stories(run_id, story_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    story_id    TEXT,
    ts          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    level       TEXT NOT NULL,
    source      TEXT,
    message     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS _migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_stories_status  ON stories(status);
CREATE INDEX IF NOT EXISTS idx_stages_status   ON stages(status);
CREATE INDEX IF NOT EXISTS idx_runs_status     ON runs(status);
CREATE INDEX IF NOT EXISTS idx_events_run_ts   ON events(run_id, ts);
"""

_TERMINAL_RUN_STATES = {"DONE", "FAILED", "ABORTED", "NEEDS_ATTENTION"}

# Schema migrations applied by Ledger.init() after the base DDL. Each entry adds
# missing columns to an existing ledger idempotently (guarded by PRAGMA
# table_info, so a fresh DB created with the up-to-date DDL is a no-op) and is
# recorded in the _migrations table so it runs at most once per DB. Migration 1
# adds the per-stage token/cost columns to a pre-existing ledger without
# touching its rows (old stages keep NULL usage and render as "—").
_MIGRATIONS: list[tuple[int, str, str, list[tuple[str, str]]]] = [
    (
        1,
        "stage usage columns",
        "stages",
        [
            ("session_id", "TEXT"),
            ("input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("cache_read_tokens", "INTEGER"),
            ("cache_creation_tokens", "INTEGER"),
            ("cost_usd", "REAL"),
        ],
    ),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending schema migrations on an open connection (idempotent).

    Adds any missing columns via ``ALTER TABLE`` and records each applied
    version in ``_migrations``. Identifiers come from the internal ``_MIGRATIONS``
    table (never user input), so the f-string interpolation is safe — SQLite
    cannot parametrise column/table names.
    """
    applied = {r[0] for r in conn.execute("SELECT version FROM _migrations").fetchall()}
    for version, name, table, columns in _MIGRATIONS:
        if version in applied:
            continue
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, col_type in columns:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        conn.execute(
            "INSERT OR IGNORE INTO _migrations(version, name) VALUES (?, ?)",
            (version, name),
        )


# Boolean flags the build subcommand accepts. Kept identical to the skill's
# argument-hint so `sdlc build $ARGUMENTS` is a drop-in for `/build-stories`.
_BOOL_FLAGS = {
    "--dry-run": "dry_run",
    "--auto": "auto",
    "--skip-coverage": "skip_coverage",
    "--sequential": "sequential",
    "--skip-preflight": "skip_preflight",
    "--rebuild": "rebuild",
}


# ---------------------------------------------------------------------------
# Options + argument parsing
# ---------------------------------------------------------------------------

@dataclass
class BuildOptions:
    """Parsed `sdlc build` arguments — the same surface the skill exposes."""

    scope: str = "all"
    dry_run: bool = False
    auto: bool = False
    skip_coverage: bool = False
    limit: int = 0
    sequential: bool = False
    coverage_threshold: int = 90
    skip_preflight: bool = False
    rebuild: bool = False
    preflight_timeout: int = 600


def parse_build_args(args: Iterable[str]) -> BuildOptions:
    """Parse the `sdlc build` argument vector into :class:`BuildOptions`.

    Accepts the exact flags the skill documents:
    ``[scope] [--dry-run] [--auto] [--skip-coverage] [--limit=N]
    [--sequential] [--coverage-threshold=N] [--skip-preflight] [--rebuild]
    [--preflight-timeout=SEC]``. ``scope`` is ``all``, ``epic-NN``, an epic
    name, or a single story id ``X.Y-NNN`` (default ``all``). Unknown flags
    raise :class:`ValueError` so a typo never silently changes behaviour.
    """
    opts = BuildOptions()
    scope_set = False
    for arg in args:
        if arg in _BOOL_FLAGS:
            setattr(opts, _BOOL_FLAGS[arg], True)
        elif arg.startswith("--limit="):
            opts.limit = int(arg.split("=", 1)[1])
        elif arg.startswith("--coverage-threshold="):
            opts.coverage_threshold = int(arg.split("=", 1)[1])
        elif arg.startswith("--preflight-timeout="):
            opts.preflight_timeout = int(arg.split("=", 1)[1])
        elif arg.startswith("--"):
            raise ValueError(f"unknown flag: {arg}")
        elif not scope_set:
            opts.scope = arg
            scope_set = True
        else:
            raise ValueError(f"unexpected positional argument: {arg}")
    return opts


# ---------------------------------------------------------------------------
# Ledger — thin wrapper over the Epic-04 SQLite schema (stdlib sqlite3)
# ---------------------------------------------------------------------------

class Ledger:
    """Durable run state, backed by the Epic-04 SQLite schema.

    Single-writer by construction (the controller is the only writer). Every
    write enables foreign keys per-connection because SQLite does not inherit
    enforcement from the DB header — the same discipline `sdlc-state.sh` uses.
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Commit-or-rollback like sqlite3's own context manager, then *close* the
        # connection so a long run does not leak a file handle per write (the
        # bare ``with sqlite3.connect(...)`` form commits but never closes).
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self) -> None:
        """Create the ledger schema if absent, then apply migrations (idempotent)."""
        with self._connect() as conn:
            conn.executescript(_SCHEMA_DDL)
            _apply_migrations(conn)

    def run_create(self, scope: str, mode: str) -> str:
        """Insert a fresh run row and return its generated id."""
        run_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs(id, scope, mode, status, started_at) "
                "VALUES (?, ?, ?, 'IN_PROGRESS', CURRENT_TIMESTAMP)",
                (run_id, scope, mode),
            )
        return run_id

    def run_update_status(self, run_id: str, status: str) -> None:
        """Transition a run's status; terminal states stamp ``finished_at``."""
        with self._connect() as conn:
            if status in _TERMINAL_RUN_STATES:
                conn.execute(
                    "UPDATE runs SET status = ?, finished_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (status, run_id),
                )
            else:
                conn.execute(
                    "UPDATE runs SET status = ? WHERE id = ?", (status, run_id)
                )

    def run_update_counts(self, run_id: str, completed: int, failed: int) -> None:
        """Record the final completed/failed tallies on the run row."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET completed = ?, failed = ? WHERE id = ?",
                (completed, failed, run_id),
            )

    def set_total(self, run_id: str, total: int) -> None:
        """Record how many stories this run scheduled."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET total_stories = ? WHERE id = ?", (total, run_id)
            )

    def story_upsert(
        self,
        run_id: str,
        story_id: str,
        epic_id: str,
        title: str,
        priority: str,
        points: int | None,
        agent_type: str,
        branch: str,
        pr_number: int | None,
        status: str,
    ) -> None:
        """INSERT-or-patch a story row, preserving its stage history.

        Uses ``ON CONFLICT DO UPDATE`` (not ``INSERT OR REPLACE``) so the FK
        cascade never wipes per-attempt stage rows when a story transitions.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO stories
                  (run_id, story_id, epic_id, title, priority, points,
                   agent_type, branch, pr_number, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, story_id) DO UPDATE SET
                    epic_id    = excluded.epic_id,
                    title      = excluded.title,
                    priority   = excluded.priority,
                    points     = excluded.points,
                    agent_type = excluded.agent_type,
                    branch     = excluded.branch,
                    pr_number  = excluded.pr_number,
                    status     = excluded.status
                """,
                (
                    run_id,
                    story_id,
                    epic_id or None,
                    title or None,
                    priority or None,
                    points,
                    agent_type or None,
                    branch or None,
                    pr_number,
                    status,
                ),
            )

    def set_story_status(self, run_id: str, story_id: str, status: str) -> None:
        """Patch only the status column of an existing story row."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE stories SET status = ? WHERE run_id = ? AND story_id = ?",
                (status, run_id, story_id),
            )

    def set_story_pr(self, run_id: str, story_id: str, pr_number: int) -> None:
        """Record the PR number once a coverage/build agent creates it."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE stories SET pr_number = ? WHERE run_id = ? AND story_id = ?",
                (pr_number, run_id, story_id),
            )

    def stage_start(
        self, run_id: str, story_id: str, stage_name: str, attempt: int = 1
    ) -> None:
        """Append an IN_PROGRESS stage attempt row."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO stages "
                "(run_id, story_id, stage_name, attempt, status, started_at) "
                "VALUES (?, ?, ?, ?, 'IN_PROGRESS', CURRENT_TIMESTAMP)",
                (run_id, story_id, stage_name, attempt),
            )

    def stage_finish(
        self,
        run_id: str,
        story_id: str,
        stage_name: str,
        attempt: int,
        status: str,
        failure_category: str = "",
        output_path: str = "",
    ) -> None:
        """Transition a stage attempt to a terminal status."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE stages SET status = ?, finished_at = CURRENT_TIMESTAMP, "
                "failure_category = ?, output_path = ? "
                "WHERE run_id = ? AND story_id = ? AND stage_name = ? AND attempt = ?",
                (
                    status,
                    failure_category or None,
                    output_path or None,
                    run_id,
                    story_id,
                    stage_name,
                    attempt,
                ),
            )

    def stage_set_usage(
        self,
        run_id: str,
        story_id: str,
        stage_name: str,
        attempt: int,
        *,
        session_id: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None,
        cache_creation_tokens: int | None,
        cost_usd: float | None,
    ) -> None:
        """Record an agent's token/cost usage on a stage attempt row.

        Called after a dispatch returns the ``--output-format json`` envelope.
        Skipped by the caller when the agent emitted plain text and carried no
        usage, so old rows keep NULL usage and render as "—".
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE stages SET session_id = ?, input_tokens = ?, "
                "output_tokens = ?, cache_read_tokens = ?, "
                "cache_creation_tokens = ?, cost_usd = ? "
                "WHERE run_id = ? AND story_id = ? AND stage_name = ? AND attempt = ?",
                (
                    session_id, input_tokens, output_tokens, cache_read_tokens,
                    cache_creation_tokens, cost_usd,
                    run_id, story_id, stage_name, attempt,
                ),
            )

    def event_log(
        self, run_id: str, story_id: str, level: str, source: str, message: str
    ) -> None:
        """Append an audit event row (mirrors every cmux log call)."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events(run_id, story_id, level, source, message) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id or None, story_id or None, level, source or None, message),
            )

    # --- Read-only queries -------------------------------------------------
    # These power `sdlc status`. They open the ledger read-only with a
    # busy timeout so a poll issued *while the controller is writing* waits out
    # the brief writer lock instead of erroring. A missing DB file is reported
    # as "no run yet" (None / empty), never an exception — the status command
    # and the polling skill treat absence as "not started".

    @contextmanager
    def _connect_ro(self) -> Iterator[sqlite3.Connection]:
        """Open a read-only connection to the ledger (caller guarantees it exists)."""
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 2000;")
            yield conn
        finally:
            conn.close()

    def latest_run_id(self) -> str | None:
        """The most recently started run id, or None when there is no run / no DB."""
        if not self.db_path.exists():
            return None
        with self._connect_ro() as conn:
            row = conn.execute(
                "SELECT id FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return row["id"] if row else None

    def latest_resumable_run(self, scope: str | None = None) -> str | None:
        """The most recent IN_PROGRESS run id (optionally for ``scope``), or None.

        A clean build close-out stamps a terminal status, so a run still marked
        ``IN_PROGRESS`` is one that was interrupted before finishing — exactly
        what ``sdlc resume`` recovers. ``scope`` ``None``/``all`` matches any
        scope; a specific scope (``epic-99``, a story id) filters to that run.
        """
        if not self.db_path.exists():
            return None
        with self._connect_ro() as conn:
            if scope and scope.lower() != "all":
                row = conn.execute(
                    "SELECT id FROM runs WHERE status = 'IN_PROGRESS' AND scope = ? "
                    "ORDER BY started_at DESC, rowid DESC LIMIT 1",
                    (scope,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM runs WHERE status = 'IN_PROGRESS' "
                    "ORDER BY started_at DESC, rowid DESC LIMIT 1"
                ).fetchone()
        return row["id"] if row else None

    def state_rows(self, run_id: str) -> list[dict]:
        """Every persisted stage-machine row for ``run_id`` for `sdlc state`.

        Each row is ``{story_id, stage_name, status, attempt, branch,
        pr_number}`` in a stable, chronological order (by story, then start
        time) — a greppable dump of the state machine for debugging.
        """
        if not self.db_path.exists():
            return []
        with self._connect_ro() as conn:
            rows = conn.execute(
                """
                SELECT st.story_id, st.stage_name, st.status, st.attempt,
                       s.branch, s.pr_number
                FROM stages st
                JOIN stories s
                  ON st.run_id = s.run_id AND st.story_id = s.story_id
                WHERE st.run_id = ?
                ORDER BY st.story_id, st.started_at, st.rowid
                """,
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def run_row(self, run_id: str) -> dict | None:
        """The `runs` row for ``run_id`` as a dict, or None when absent."""
        if not self.db_path.exists():
            return None
        with self._connect_ro() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def story_rows(self, run_id: str) -> list[dict]:
        """Per-story progress for ``run_id``, newest-stage first.

        ``current_stage`` / ``stage_status`` are derived from the ``stages``
        table (the controller never populates ``stories.current_stage``): the
        latest stage attempt by start time wins.
        """
        if not self.db_path.exists():
            return []
        with self._connect_ro() as conn:
            rows = conn.execute(
                """
                SELECT
                    s.story_id, s.title, s.priority, s.status, s.pr_number,
                    (SELECT st.stage_name FROM stages st
                       WHERE st.run_id = s.run_id AND st.story_id = s.story_id
                       ORDER BY st.started_at DESC, st.rowid DESC LIMIT 1) AS current_stage,
                    (SELECT st.status FROM stages st
                       WHERE st.run_id = s.run_id AND st.story_id = s.story_id
                       ORDER BY st.started_at DESC, st.rowid DESC LIMIT 1) AS stage_status
                FROM stories s
                WHERE s.run_id = ?
                ORDER BY s.rowid
                """,
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, run_id: str, limit: int = 10) -> list[dict]:
        """The last ``limit`` human audit events for ``run_id``, oldest-first.

        The internal ``config`` marker event (run options) is excluded so it
        never clutters the human-facing event log.
        """
        if not self.db_path.exists():
            return []
        with self._connect_ro() as conn:
            rows = conn.execute(
                "SELECT ts, level, source, story_id, message FROM events "
                "WHERE run_id = ? AND (source IS NULL OR source != 'config') "
                "ORDER BY id DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def run_config(self, run_id: str) -> dict:
        """The run's options, recorded as a ``config`` event at start (or {})."""
        if not self.db_path.exists():
            return {}
        with self._connect_ro() as conn:
            row = conn.execute(
                "SELECT message FROM events WHERE run_id = ? AND source = 'config' "
                "ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return {}
        try:
            return json.loads(row["message"])
        except (ValueError, TypeError):
            return {}

    def stage_breakdown(self, run_id: str) -> dict[str, list[dict]]:
        """All stage attempts for ``run_id``, grouped by story id (chronological).

        Each entry is ``{name, attempt, status, started_at, finished_at,
        failure_category, output_path, session_id, input_tokens, output_tokens,
        cache_read_tokens, cache_creation_tokens, cost_usd, tokens}`` where
        ``tokens`` is the summed token count (None when no usage was recorded).
        Powers the dashboard's per-stage view and its token tooltips.
        """
        if not self.db_path.exists():
            return {}
        with self._connect_ro() as conn:
            # Tolerate a ledger created before token columns existed (read-only
            # viewers never migrate): select usage columns only when present.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(stages)").fetchall()}
            usage_sel = (
                ", session_id, input_tokens, output_tokens, cache_read_tokens, "
                "cache_creation_tokens, cost_usd"
                if "input_tokens" in cols else ""
            )
            rows = conn.execute(
                "SELECT story_id, stage_name AS name, attempt, status, started_at, "
                "finished_at, failure_category, output_path" + usage_sel +
                " FROM stages WHERE run_id = ? ORDER BY story_id, started_at, rowid",
                (run_id,),
            ).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            d = dict(r)
            d["tokens"] = _sum_tokens(d)
            out.setdefault(d.pop("story_id"), []).append(d)
        return out

    def list_runs(self, limit: int = 50) -> list[dict]:
        """The most recent ``limit`` runs (newest first) for the runs browser.

        Each entry is ``{id, scope, mode, status, started_at, finished_at,
        total, done, failed, total_tokens, total_cost_usd}``. The
        ``total/done/failed`` tallies are computed live from the per-story rows
        (a single grouped query), so an in-progress run shows accurate counts —
        the run row's stored ``completed/failed`` are 0 until close-out.
        ``total_tokens``/``total_cost_usd`` are None for runs with no recorded
        usage (those predating token capture). Capped at ``limit``.
        """
        if not self.db_path.exists():
            return []
        with self._connect_ro() as conn:
            runs = conn.execute(
                "SELECT id, scope, mode, status, started_at, finished_at, "
                "total_stories FROM runs ORDER BY started_at DESC, rowid DESC "
                "LIMIT ?",
                (limit,),
            ).fetchall()
            grouped = conn.execute(
                "SELECT run_id, status, COUNT(*) AS n FROM stories GROUP BY run_id, status"
            ).fetchall()
            # Token columns are absent on a ledger created before this feature;
            # skip the rollup so a read-only viewer never hits "no such column".
            stage_cols = {r[1] for r in conn.execute("PRAGMA table_info(stages)").fetchall()}
            usage = (
                conn.execute(
                    "SELECT run_id, "
                    "SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)"
                    "+COALESCE(cache_read_tokens,0)+COALESCE(cache_creation_tokens,0)) AS tok, "
                    "SUM(COALESCE(cost_usd,0)) AS cost, "
                    "COUNT(input_tokens) AS n_tok, COUNT(cost_usd) AS n_cost "
                    "FROM stages GROUP BY run_id"
                ).fetchall()
                if "input_tokens" in stage_cols else []
            )

        counts: dict[str, dict[str, int]] = {}
        for g in grouped:
            counts.setdefault(g["run_id"], {})[g["status"]] = g["n"]
        usage_by_run = {u["run_id"]: u for u in usage}

        out: list[dict] = []
        for r in runs:
            by_status = counts.get(r["id"], {})
            total = r["total_stories"] or sum(by_status.values())
            u = usage_by_run.get(r["id"])
            out.append(
                {
                    "id": r["id"],
                    "scope": r["scope"],
                    "mode": r["mode"],
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "finished_at": r["finished_at"],
                    "total": total,
                    "done": by_status.get("DONE", 0),
                    "failed": by_status.get("FAILED", 0),
                    "total_tokens": (u["tok"] if u and u["n_tok"] else None),
                    "total_cost_usd": (u["cost"] if u and u["n_cost"] else None),
                }
            )
        return out


# ---------------------------------------------------------------------------
# Progress snapshot — shared by `status --json` and the dashboard
# ---------------------------------------------------------------------------

_EMPTY_COUNTS = {
    "total": 0, "done": 0, "failed": 0, "blocked": 0,
    "in_progress": 0, "skipped": 0, "todo": 0, "needs_attention": 0,
}

# The four per-stage token components recorded from the agent usage envelope.
_TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
)


def _sum_tokens(row: dict) -> int | None:
    """Total tokens across the four usage components, or None when none recorded.

    None (not 0) signals "no usage data" so the dashboard renders "—" rather
    than a misleading zero for runs/stages that predate token capture.
    """
    values = [row.get(k) for k in _TOKEN_FIELDS]
    if all(v is None for v in values):
        return None
    return sum(v or 0 for v in values)


def _aggregate_run_usage(breakdown: dict[str, list[dict]]) -> dict | None:
    """Sum token/cost usage across every stage attempt of a run.

    Returns ``{input, output, cache_read, cache_creation, total_tokens,
    cost_usd}`` or None when no stage recorded any usage (a pre-capture run).
    """
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    key_map = {
        "input": "input_tokens", "output": "output_tokens",
        "cache_read": "cache_read_tokens", "cache_creation": "cache_creation_tokens",
    }
    cost = 0.0
    seen = False
    for attempts in breakdown.values():
        for a in attempts:
            for out_key, in_key in key_map.items():
                v = a.get(in_key)
                if v is not None:
                    totals[out_key] += v
                    seen = True
            c = a.get("cost_usd")
            if c is not None:
                cost += c
                seen = True
    if not seen:
        return None
    return {**totals, "total_tokens": sum(totals.values()), "cost_usd": cost}


def status_snapshot(ledger: Ledger, run_id: str | None = None) -> dict:
    """A read-only progress snapshot of ``run_id`` (default: the latest run).

    Returns the stable shape consumed by both ``sdlc status --json`` and
    the local dashboard: ``{db, run|None, counts, stories, events}``. Counts are
    derived from the per-story rows (not the run row's end-of-run tallies, which
    are 0 mid-run). When there is no run, ``run`` is None and counts are zero.
    """
    rid = run_id or ledger.latest_run_id()
    payload: dict = {
        "db": str(ledger.db_path),
        "run": None,
        "counts": dict(_EMPTY_COUNTS),
        "stories": [],
        "events": [],
    }
    if rid is None:
        return payload

    run_row = ledger.run_row(rid)
    if run_row is None:
        # An explicit run id that doesn't exist → report "no run", not a hollow one.
        return payload

    stories = ledger.story_rows(rid)
    events = ledger.recent_events(rid, limit=10)
    config = ledger.run_config(rid)
    breakdown = ledger.stage_breakdown(rid)

    def _count(value: str) -> int:
        return sum(1 for s in stories if s.get("status") == value)

    # Attach the per-stage pipeline to each story: the latest attempt of each
    # pipeline stage (build → coverage → review → merge), PENDING when not yet
    # started, SKIPPED for coverage when this run skipped the coverage gate.
    # Also fold in per-story token/cost totals across all of the story's attempts.
    skip_coverage = bool(config.get("skip_coverage"))
    for s in stories:
        attempts = breakdown.get(s["story_id"], [])
        latest: dict[str, dict] = {}
        for a in attempts:  # chronological, so the last write wins per stage
            latest[a["name"]] = a
        pipeline = []
        for name in _STAGES:
            row = latest.get(name)
            if row is not None:
                pipeline.append(row)
            elif name == "coverage" and skip_coverage:
                pipeline.append({"name": name, "status": "SKIPPED"})
            else:
                pipeline.append({"name": name, "status": "PENDING"})
        s["stages"] = pipeline
        s["bugfix_attempts"] = sum(1 for a in attempts if a["name"] == "bugfix")
        story_tok = [a.get("tokens") for a in attempts]
        s["tokens"] = (
            sum(t or 0 for t in story_tok) if any(t is not None for t in story_tok) else None
        )
        story_cost = [a.get("cost_usd") for a in attempts]
        s["cost_usd"] = (
            sum(c or 0 for c in story_cost) if any(c is not None for c in story_cost) else None
        )

    # Run-level usage rollup across every stage attempt of every story.
    run_usage = _aggregate_run_usage(breakdown)

    payload["run"] = {
        "id": rid,
        "scope": run_row.get("scope"),
        "mode": run_row.get("mode"),
        "status": run_row.get("status"),
        "started_at": run_row.get("started_at"),
        "finished_at": run_row.get("finished_at"),
        "config": config,
        "usage": run_usage,
    }
    payload["counts"] = {
        "total": run_row.get("total_stories") or len(stories),
        "done": _count("DONE"),
        "failed": _count("FAILED"),
        "blocked": _count("BLOCKED"),
        "in_progress": _count("IN_PROGRESS"),
        "skipped": _count("SKIPPED"),
        "todo": _count("TODO"),
        "needs_attention": _count("NEEDS_ATTENTION"),
    }
    payload["stories"] = stories
    payload["events"] = events
    return payload


# ---------------------------------------------------------------------------
# Dispatcher protocol + result
# ---------------------------------------------------------------------------

class Dispatcher(Protocol):
    """Callable seam the state machine uses to invoke an agent.

    The production implementation is :func:`sdlc.dispatch.dispatch_agent`; tests
    pass a fake that returns canned schema-valid responses. Keeping this a plain
    callable means the state machine never imports subprocess directly.
    """

    def __call__(
        self, agent_type: str, prompt: str, *, story: Story | None = ..., **kwargs
    ) -> AgentResult: ...


@dataclass
class BuildResult:
    """The terminal outcome of a build run."""

    completed: int = 0
    failed: int = 0
    skipped: int = 0
    blocked: int = 0
    needs_attention: int = 0
    planned: int = 0
    dry_run: bool = False
    preflight_failed: bool = False
    run_id: str | None = None
    story_status: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def detect_test_command(root: Path) -> list[str] | None:
    """Detect the project's preflight command, preferring its real quality gate.

    Order: the project's own gate first — ``scripts/quality-gate.sh`` or a
    ``gate`` Makefile target — because that is what the repo actually runs in CI
    and pre-push (e.g. ROSETTA's gate runs ``pytest -n 4``). Only if no gate
    exists do we fall back to a generic suite: ``package.json`` (npm test) →
    ``pyproject.toml`` (``uv run pytest``, with ``-n auto`` when pytest-xdist is
    present so it isn't a slow serial run) → ``Makefile`` (make test) → bats.
    Returns ``None`` when nothing is found.
    """
    gate = root / "scripts" / "quality-gate.sh"
    if gate.is_file():
        return ["bash", str(gate)]
    makefile = root / "Makefile"
    makefile_text = makefile.read_text(encoding="utf-8") if makefile.is_file() else ""
    if "gate:" in makefile_text:
        return ["make", "gate"]

    if (root / "package.json").is_file():
        return ["npm", "test"]
    if (root / "pyproject.toml").is_file():
        cmd = ["uv", "run", "pytest"]
        if _has_pytest_xdist(root):
            cmd += ["-n", "auto"]
        return cmd
    if "test:" in makefile_text:
        return ["make", "test"]
    if (root / "test").is_dir():
        return ["bats", "test/"]
    return None


def _has_pytest_xdist(root: Path) -> bool:
    """True when pytest-xdist appears in the project's deps/lock (enables -n auto)."""
    for name in ("pyproject.toml", "uv.lock", "requirements.txt"):
        path = root / name
        if path.is_file() and "pytest-xdist" in path.read_text(encoding="utf-8"):
            return True
    return False


def default_preflight(root: Path | None = None, timeout: int = 600) -> bool:
    """Run the detected preflight command and return True when it is green.

    Streams the command's output (no capture) so the user sees progress instead
    of a silent hang, and bounds it with ``timeout`` seconds — on expiry it
    prints a clear message and fails (use ``--skip-preflight`` to bypass).
    ``run_build`` accepts a ``preflight`` callable so tests inject a stub.
    """
    root = root or Path.cwd()
    cmd = detect_test_command(root)
    if cmd is None:
        # No suite to run — treat as a pass rather than blocking the build.
        return True
    print(f"preflight: {' '.join(cmd)} (timeout {timeout}s)", file=sys.stderr)
    try:
        completed = subprocess.run(cmd, cwd=root, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(
            f"PRE_FLIGHT_TIMEOUT: '{' '.join(cmd)}' exceeded {timeout}s — aborting. "
            "Raise --preflight-timeout=N or bypass with --skip-preflight.",
            file=sys.stderr,
        )
        return False
    return completed.returncode == 0


# ---------------------------------------------------------------------------
# Prompt rendering (kept terse — the agent reads the epic file itself)
# ---------------------------------------------------------------------------

def _result_wrapper(schema_filename: str) -> str:
    """The exact result-block wrapper every agent must emit (R10).

    Shown verbatim so the agent uses the sentinel markers rather than a markdown
    ```json fence. The controller now tolerates fences as a fallback, but the
    sentinels are unambiguous — this instruction reduces the drift at the source.
    """
    return (
        "End your reply with EXACTLY this wrapper — the literal marker lines, "
        "no markdown code fences (do not wrap it in ```json), and nothing after "
        "the closing marker:\n"
        + RESULT_START_MARKER
        + "\n{ ...the JSON object per controller/src/sdlc/schemas/"
        + schema_filename
        + " ... }\n"
        + RESULT_END_MARKER
    )


def render_build_prompt(story: Story, opts: BuildOptions) -> str:
    """Render the build-agent instructions for one story.

    Deliberately mirrors the skill's build-agent prompt: create the branch, read
    the epic, TDD, quality gates, commit, and emit the result block the
    controller validates.
    """
    push = (
        "6. Push and create PR; include the PR number in the result block."
        if opts.skip_coverage
        else "6. Commit locally; the coverage agent pushes and opens the PR."
    )
    return (
        f"You are building story {story.id}: {story.title}\n"
        f"Epic: {story.epic_name} (from {story.epic_file})\n"
        f"Priority: {story.priority}\n\n"
        "## Instructions\n"
        f"1. Create branch: git checkout -b feature/{story.id}\n"
        f"2. Read {story.epic_file} and find the full story section for {story.id}\n"
        "3. Follow TDD: write failing tests first, then implement\n"
        "4. Run all quality gates (tests, types, lint, security)\n"
        f"5. Commit: feat({story.epic_name}): {story.title} (#{story.id})\n"
        f"{push}\n\n"
        + _result_wrapper("build-agent-response.schema.json")
    )


def render_coverage_prompt(story: Story, opts: BuildOptions) -> str:
    return (
        f"Coverage gate for story {story.id}: {story.title}.\n"
        f"Branch: feature/{story.id}. Threshold: {opts.coverage_threshold}%.\n"
        "Fetch the branch, fill coverage gaps, push, open the PR, then emit the "
        "result block.\n"
        + _result_wrapper("coverage-agent-response.schema.json")
    )


def render_review_prompt(story: Story, pr_number: int | None) -> str:
    return (
        f"Review the PR for story {story.id}: {story.title} (PR #{pr_number}).\n"
        "Check architecture, security, performance, coverage, code quality; "
        "approve when satisfied, then emit the result block.\n"
        + _result_wrapper("review-agent-response.schema.json")
    )


def render_merge_prompt(story: Story, pr_number: int | None) -> str:
    return (
        f"Merge the PR for story {story.id}: {story.title} (PR #{pr_number}).\n"
        "Rebase before merge to absorb baseline drift, then emit the result block.\n"
        + _result_wrapper("merge-agent-response.schema.json")
    )


def render_bugfix_prompt(story: Story, failed_stage: str, failure: str) -> str:
    return (
        f"Bugfix story {story.id}: {story.title}. Stage '{failed_stage}' failed.\n"
        f"Failure: {failure}\n"
        "Classify (CODE_BUG/TEST_BUG/ENV_ISSUE), fix where possible, then emit "
        "the result block.\n"
        + _result_wrapper("bugfix-agent-response.schema.json")
    )


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------

# Stage pipeline. Coverage is conditionally skipped via --skip-coverage.
_STAGES = ("build", "coverage", "review", "merge")


def _ensure_repo_ignores(db_path: Path) -> None:
    """Keep the ledger files out of the target repo's ``git status`` (R9).

    Adds ``.sdlc-state.db*`` (covering the DB, its ``-shm``/``-wal`` sidecars, and
    the ``.sdlc-state.db.logs`` transcript dir) to the repo's
    ``.git/info/exclude`` — a *local* ignore that never modifies a tracked file,
    so the controller never dirties the repo it is building in. Best-effort:
    silently does nothing when there is no enclosing git repo.
    """
    pattern = ".sdlc-state.db*"
    try:
        start = Path(db_path).resolve().parent
        git_dir = next(
            (d / ".git" for d in (start, *start.parents) if (d / ".git").is_dir()),
            None,
        )
        if git_dir is None:
            return
        exclude = git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if pattern in existing.split():
            return
        with exclude.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(f"# sdlc ledger (auto-added)\n{pattern}\n")
    except OSError:
        pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``root``, capturing output (10s ceiling)."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=10,
    )


def _base_ref(root: Path) -> str | None:
    """The branch to diff a story branch against: origin/HEAD → main → master."""
    head = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if head.returncode == 0 and head.stdout.strip():
        # e.g. "refs/remotes/origin/main" → "origin/main"
        return head.stdout.strip().removeprefix("refs/remotes/")
    for candidate in ("main", "master"):
        if _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}").returncode == 0:
            return candidate
    return None


def story_commit_exists(story_id: str, root: Path | None = None) -> bool:
    """Best-effort: True when ``feature/<story_id>`` holds a commit beyond the base.

    Lets the controller detect that an agent already committed the story even when
    its result block was unparseable, so the work is never discarded (R10).
    Returns False — never raises — when git is absent, there is no repo, or the
    branch does not exist (mirrors ``_ensure_repo_ignores`` defensiveness).
    """
    root = root or Path.cwd()
    branch = f"feature/{story_id}"
    try:
        if _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
            return False
        base = _base_ref(root)
        if base is None:
            # No base to diff against, but the branch exists — treat as committed.
            return True
        out = _git(root, "rev-list", "--count", f"{base}..{branch}")
        count = out.stdout.strip()
        return out.returncode == 0 and count.isdigit() and int(count) > 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_build(
    opts: BuildOptions,
    *,
    queue: list[Story],
    ledger: Ledger,
    dispatcher: Dispatcher | None = None,
    preflight: Callable[[], bool] | None = None,
    render_view: Callable[[str], None] | None = None,
) -> BuildResult:
    """Run the build-stories orchestration deterministically.

    Phases: preflight → schedule (cohorts) → per-story 4-stage execution with a
    bounded bugfix loop → ledger close-out. Every stage transition is written to
    the ledger before the next stage begins, so a crash leaves a resumable
    state. Schema-invalid agent output is caught here and routed to the bugfix
    loop — the next stage never runs on garbage.

    ``dispatcher`` defaults to the real subprocess-backed
    :func:`sdlc.dispatch.dispatch_agent`. Tests inject a fake. ``preflight``
    defaults to running the detected test suite. ``render_view`` is an optional
    hook that regenerates the markdown progress view from the ledger.
    """
    dispatch = dispatcher or dispatch_agent
    check_preflight = preflight or (lambda: default_preflight(timeout=opts.preflight_timeout))

    # --- Partition: shipped (Done) stories are skipped unless --rebuild ------
    if opts.rebuild:
        buildable, done_skips = queue, []
    else:
        buildable = [s for s in queue if not s.done]
        done_skips = [s for s in queue if s.done]

    # --- Limit truncation (applies to the buildable set) ---------------------
    if opts.limit:
        buildable = truncate_queue(buildable, opts.limit)

    # --- Dry run: report the buildable plan, dispatch nothing ----------------
    # A dry run is plan-only — it must not run the (possibly slow/failing)
    # preflight gate, so this returns before Phase 1.
    if opts.dry_run:
        return BuildResult(dry_run=True, planned=len(buildable))

    # --- Phase 1: Preflight (real runs only) ---------------------------------
    if not opts.skip_preflight:
        if not check_preflight():
            return BuildResult(preflight_failed=True)

    # --- Ledger bootstrap ----------------------------------------------------
    ledger.init()
    _ensure_repo_ignores(ledger.db_path)  # keep ledger files out of git status (R9)
    mode = "serial" if opts.sequential else "parallel"
    run_id = ledger.run_create(opts.scope, mode)
    ledger.set_total(run_id, len(buildable))
    # Per-run transcript dir (next to the ledger; covered by the R9 ignore).
    logs_dir = Path(f"{ledger.db_path}.logs") / run_id
    ledger.event_log(
        run_id, "", "info", "controller", f"run started: scope={opts.scope} mode={mode}"
    )
    # Persist the run's options as an immutable config marker (read back by the
    # dashboard header). Kept as an event so no schema migration is needed.
    ledger.event_log(
        run_id, "", "info", "config",
        json.dumps({
            "preflight": "skipped" if opts.skip_preflight else "passed",
            "skip_coverage": opts.skip_coverage,
            "coverage_threshold": opts.coverage_threshold,
            "mode": mode,
            "rebuild": opts.rebuild,
            "limit": opts.limit,
        }),
    )
    # Record shipped stories as SKIPPED for the audit trail. They are NOT part of
    # the build and deliberately stay out of the cohort `status` map below, so a
    # buildable story that depends on a shipped one is treated as satisfied, not
    # blocked (R2/R4).
    for story in done_skips:
        ledger.story_upsert(
            run_id, story.id, story.epic_id, story.title, story.priority,
            story.points, story.agent_type, "", None, "SKIPPED",
        )
        ledger.event_log(
            run_id, story.id, "info", "controller",
            "skipped: story already Done in epic (use --rebuild to force)",
        )
    for story in buildable:
        ledger.story_upsert(
            run_id, story.id, story.epic_id, story.title, story.priority,
            story.points, story.agent_type, "", None, "TODO",
        )

    cohorts = compute_cohorts(buildable)
    status: dict[str, str] = {s.id: "TODO" for s in buildable}

    # --- Phase 2: cohort-by-cohort execution ---------------------------------
    for cohort in cohorts:
        for story in cohort:
            # A story whose dependency failed cannot proceed.
            blocked_by = [
                dep
                for dep in story.dependencies
                if status.get(dep) in {"FAILED", "BLOCKED", "SKIPPED"}
            ]
            if blocked_by:
                status[story.id] = "BLOCKED"
                ledger.set_story_status(run_id, story.id, "BLOCKED")
                ledger.event_log(
                    run_id,
                    story.id,
                    "warn",
                    "controller",
                    f"blocked: dependency not done ({', '.join(blocked_by)})",
                )
                continue

            outcome = _run_story(story, opts, ledger, run_id, dispatch, logs_dir)
            status[story.id] = outcome
            ledger.set_story_status(run_id, story.id, outcome)

    # --- Phase 3: close out --------------------------------------------------
    completed = sum(1 for v in status.values() if v == "DONE")
    failed = sum(1 for v in status.values() if v == "FAILED")
    blocked = sum(1 for v in status.values() if v == "BLOCKED")
    # Stories whose work was committed but whose result was unparseable (R10):
    # not a clean success, but deliberately not a destructive FAILED.
    needs_attention = sum(1 for v in status.values() if v == "NEEDS_ATTENTION")
    # Shipped stories were skipped before the loop; fold them into the tally.
    skipped = len(done_skips) + sum(1 for v in status.values() if v == "SKIPPED")

    if failed or blocked:
        run_terminal = "FAILED"
    elif needs_attention:
        run_terminal = "NEEDS_ATTENTION"
    else:
        run_terminal = "DONE"
    run_level = {"DONE": "success", "NEEDS_ATTENTION": "warn"}.get(run_terminal, "error")
    ledger.run_update_counts(run_id, completed, failed)
    ledger.event_log(
        run_id,
        "",
        run_level,
        "controller",
        f"run finished: {completed} done, {failed} failed, {blocked} blocked, "
        f"{needs_attention} need attention, {skipped} skipped",
    )
    ledger.run_update_status(run_id, run_terminal)

    if render_view is not None:
        render_view(run_id)

    # The returned per-story map includes the shipped skips for visibility,
    # even though they were kept out of the cohort `status` used for blocking.
    story_status = {s.id: "SKIPPED" for s in done_skips}
    story_status.update(status)
    return BuildResult(
        completed=completed,
        failed=failed,
        skipped=skipped,
        blocked=blocked,
        needs_attention=needs_attention,
        planned=len(buildable),
        run_id=run_id,
        story_status=story_status,
    )


def _run_story(
    story: Story,
    opts: BuildOptions,
    ledger: Ledger,
    run_id: str,
    dispatch: Dispatcher,
    logs_dir: Path,
    *,
    done_stages: frozenset[str] = frozenset(),
    start_attempt: int = 1,
    pr_number: int | None = None,
    bugfix_seq: int = 0,
) -> str:
    """Drive one story through build → coverage → review → merge.

    Returns the terminal story status: ``DONE``, ``FAILED``, or
    ``NEEDS_ATTENTION``. A stage failure (agent FAILED status, dispatch error, or
    schema-invalid output) enters the bounded bugfix loop; the stage is retried
    after a successful fix. If a result is *unparseable* (contract error) but the
    agent already committed the story branch, the work is preserved as
    ``NEEDS_ATTENTION`` instead of being discarded and rebuilt (R10). Each
    dispatch's transcript is persisted under ``logs_dir`` and its path recorded
    on the stage row (R8).

    Resume parameters (Story 10.1-001) let the controller re-enter mid-story
    without rebuilding completed work: ``done_stages`` are pipeline stages with a
    recorded DONE attempt and are skipped; ``start_attempt`` is the attempt
    number for the first stage actually run (continuing past a crashed attempt);
    ``pr_number`` / ``bugfix_seq`` carry forward the run's prior PR and bugfix
    sequence. The defaults reproduce a fresh full build exactly.
    """
    stages = [s for s in _STAGES if not (s == "coverage" and opts.skip_coverage)]
    # Already-completed stages are skipped on resume; a fresh build skips none.
    pending = [s for s in stages if s not in done_stages]
    # Monotonic across the whole story: the "bugfix" stage rows share one
    # (run_id, story_id, stage_name) key, so every bugfix dispatch — across both
    # retries of one stage and across different stages — needs a distinct attempt
    # number, or the second insert hits the stages UNIQUE constraint.

    for idx, stage in enumerate(pending):
        bugfix_attempts = 0
        # Only the first resumed stage continues a prior attempt count; later
        # stages start fresh at attempt 1.
        attempt = start_attempt if idx == 0 else 1
        while True:
            ledger.stage_start(run_id, story.id, stage, attempt)
            tpath = logs_dir / f"{story.id}-{stage}-{attempt}.log"
            ok, result, failure, kind = _dispatch_stage(
                stage, story, opts, pr_number, dispatch, tpath
            )
            if ok:
                ledger.stage_finish(
                    run_id, story.id, stage, attempt, "DONE", output_path=str(tpath)
                )
                _record_stage_usage(ledger, run_id, story.id, stage, attempt, result)
                pr_number = _extract_pr(result, pr_number)
                if pr_number is not None:
                    ledger.set_story_pr(run_id, story.id, pr_number)
                break

            # Stage failed: record it, then attempt a bounded bugfix.
            ledger.stage_finish(
                run_id, story.id, stage, attempt, "FAILED", f"{stage}-error", str(tpath)
            )
            # A schema-valid-but-FAILED agent response still carries usage.
            _record_stage_usage(ledger, run_id, story.id, stage, attempt, result)
            ledger.event_log(
                run_id, story.id, "error", "controller", f"{stage} failed: {failure}"
            )

            # R10: never discard committed work. If the result was unparseable
            # (contract error) but the agent already committed the story branch,
            # preserve it for manual push/MR rather than bugfixing from scratch.
            if kind == "contract" and story_commit_exists(story.id):
                ledger.event_log(
                    run_id,
                    story.id,
                    "warn",
                    "controller",
                    f"work committed on feature/{story.id} but result block "
                    "unparseable — preserved for manual push/MR (no bugfix re-run)",
                )
                return "NEEDS_ATTENTION"

            if bugfix_attempts >= MAX_BUGFIX_ATTEMPTS:
                return "FAILED"

            bugfix_attempts += 1
            bugfix_seq += 1
            bpath = logs_dir / f"{story.id}-bugfix-{stage}-{bugfix_seq}.log"
            if not _run_bugfix(
                story, stage, failure, ledger, run_id, dispatch, bpath, bugfix_seq
            ):
                return "FAILED"
            # Bugfix succeeded — retry the same stage as a new attempt.
            attempt += 1

    return "DONE"


def _dispatch_stage(
    stage: str,
    story: Story,
    opts: BuildOptions,
    pr_number: int | None,
    dispatch: Dispatcher,
    transcript_path: Path | None = None,
) -> tuple[bool, AgentResult | None, str, str]:
    """Dispatch one stage's agent and classify the outcome.

    Returns ``(ok, result, failure_summary, kind)``. ``ok`` is False on a
    dispatch error, a schema-invalid response (caught here, never passed
    downstream), or an agent that reported a non-success status for its stage.
    ``kind`` names the failure cause — ``"contract"`` / ``"dispatch"`` /
    ``"reported"`` (empty on success) — so the caller can treat an unparseable
    result that nonetheless committed work as recoverable rather than discard it
    (R10).
    """
    prompt = _render_stage_prompt(stage, story, opts, pr_number)
    try:
        result = dispatch(stage, prompt, story=story, transcript_path=transcript_path)
    except ContractError as exc:
        # Malformed / schema-invalid agent output is a build failure.
        return False, None, f"contract violation: {exc}", "contract"
    except AgentDispatchError as exc:
        return False, None, f"dispatch error: {exc}", "dispatch"

    if not _stage_succeeded(stage, result.data):
        return False, result, _stage_failure_summary(stage, result.data), "reported"
    return True, result, "", ""


def _render_stage_prompt(
    stage: str, story: Story, opts: BuildOptions, pr_number: int | None
) -> str:
    if stage == "build":
        return render_build_prompt(story, opts)
    if stage == "coverage":
        return render_coverage_prompt(story, opts)
    if stage == "review":
        return render_review_prompt(story, pr_number)
    return render_merge_prompt(story, pr_number)


def _stage_succeeded(stage: str, data: dict) -> bool:
    """Interpret a stage's schema-valid response as success or failure."""
    if stage == "build":
        return data.get("build_status") == "SUCCESS"
    if stage == "coverage":
        return data.get("coverage_status") != "FAIL"
    if stage == "review":
        return data.get("final_status") == "APPROVED"
    if stage == "merge":
        return data.get("merge_status") == "MERGED"
    return False


def _stage_failure_summary(stage: str, data: dict) -> str:
    if stage == "build":
        return data.get("error_summary", "build reported FAILED")
    return f"{stage} reported non-success status"


def _record_stage_usage(
    ledger: Ledger,
    run_id: str,
    story_id: str,
    stage: str,
    attempt: int,
    result: AgentResult | None,
) -> None:
    """Persist a stage's token/cost usage from its AgentResult (no-op if absent).

    Maps the agent envelope's usage keys (``cache_read_input_tokens`` etc.) to
    the ledger's column names. Skipped entirely when the agent emitted plain
    text (custom ``SDLC_AGENT_CMD``) and carried no usage.
    """
    if result is None or (result.usage is None and result.cost_usd is None):
        return
    u = result.usage or {}
    ledger.stage_set_usage(
        run_id, story_id, stage, attempt,
        session_id=result.session_id,
        input_tokens=u.get("input_tokens"),
        output_tokens=u.get("output_tokens"),
        cache_read_tokens=u.get("cache_read_input_tokens"),
        cache_creation_tokens=u.get("cache_creation_input_tokens"),
        cost_usd=result.cost_usd,
    )


def _extract_pr(result: AgentResult | None, current: int | None) -> int | None:
    if result is None:
        return current
    pr = result.data.get("pr_number")
    return pr if isinstance(pr, int) else current


def _run_bugfix(
    story: Story,
    failed_stage: str,
    failure: str,
    ledger: Ledger,
    run_id: str,
    dispatch: Dispatcher,
    transcript_path: Path | None = None,
    attempt: int = 1,
) -> bool:
    """Dispatch the bugfix agent. Returns True when the fix is confirmed.

    A bugfix is "confirmed" only when ``fix_status == FIXED`` and
    ``tests_passing`` is true — exactly the skill's Step 5d2 gate. Any dispatch
    or contract error during bugfix is itself a failure (no fix). ``attempt`` is
    a story-level monotonic sequence so each bugfix row is unique (the "bugfix"
    stage recurs across retries and stages and would otherwise collide on the
    stages UNIQUE key).
    """
    ledger.stage_start(run_id, story.id, "bugfix", attempt)
    out = str(transcript_path) if transcript_path is not None else ""
    prompt = render_bugfix_prompt(story, failed_stage, failure)
    try:
        result = dispatch("bugfix", prompt, story=story, transcript_path=transcript_path)
    except (ContractError, AgentDispatchError) as exc:
        ledger.stage_finish(
            run_id, story.id, "bugfix", attempt, "FAILED", "bugfix-error", out
        )
        ledger.event_log(
            run_id, story.id, "error", "controller", f"bugfix dispatch failed: {exc}"
        )
        return False

    data = result.data
    fixed = data.get("fix_status") == "FIXED" and bool(data.get("tests_passing"))
    ledger.stage_finish(
        run_id,
        story.id,
        "bugfix",
        attempt,
        "DONE" if fixed else "FAILED",
        str(data.get("failure_category", "")),
        out,
    )
    _record_stage_usage(ledger, run_id, story.id, "bugfix", attempt, result)
    ledger.event_log(
        run_id,
        story.id,
        "success" if fixed else "error",
        "controller",
        f"bugfix {'resolved' if fixed else 'exhausted'}: {failed_stage}",
    )
    return fixed
