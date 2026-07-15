# ABOUTME: Deterministic build-stories state machine ported from the skill (7.3-001).
# ABOUTME: Owns preflight, cohorts, agent dispatch, schema validation, ledger writes.

from __future__ import annotations

import concurrent.futures
import functools
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol

from sdlc.capability import preflight_harness, resolve_capabilities
from sdlc import build_issue
from sdlc.degradation import evaluate_degradations
from sdlc.cohort import Story, compute_cohorts, truncate_queue
from sdlc.commitlint import (
    build_commit_header,
    lint_commit_message,
    load_commitlint_config,
)
from sdlc.contracts import (
    AGENT_SCHEMAS,
    RESULT_START_MARKER,
    ContractError,
    _result_wrapper,  # re-exported for build.py prompt rendering (issue #435 move)
)
from sdlc.cost_estimate import (
    DEFAULT_USD_PER_MILLION_TOKENS,
    MODEL_USD_PER_MILLION_TOKENS,
    CostEstimateConfig,
    StageEstimate,
    estimate_stage,
)
from sdlc.discovery import canonical_scope
from sdlc.doc_currency import doc_currency_enabled
from sdlc.dispatch import (
    AgentDispatchError,
    AgentResult,
    ContextOverflowError,
    RateLimitError,
    dispatch_agent,
)
from sdlc.harness import DEFAULT_HARNESS, resolve_harness
from sdlc.model_routing import (
    ModelRoutingConfig,
    OVERRIDE_FILENAME as MODEL_ROUTING_OVERRIDE_FILENAME,
    escalate_model,
    load_routing_config,
    routing_config,
    select_model,
)
from sdlc.notify import notify
from sdlc.progress import ProgressCoalescer, UsageAccumulator, map_stream_event
from sdlc.rate_limit import RateLimitSignal, WindowQuota, seconds_until_reset, within_wait_cap
from sdlc.issue_host import (
    CR_FAILED,
    CR_NONE,
    CR_PENDING,
    CR_SUCCESS,
    CR_UNKNOWN,
    GITHUB_CR_TERMS,
    ChangeRequestChecks,
    ChangeRequestTerms,
    IssueHostAdapter,
)
from sdlc.risk_gate import RISK_APPROVED_LABEL, RISK_LABEL
from sdlc.registry import Registry, RunRecord

# Maximum bugfix iterations per story before giving up — mirrors the skill's
# "max 2 bugfix iterations" rule (Step 5d2) so behaviour matches the playbook.
MAX_BUGFIX_ATTEMPTS = 2

# Story 12.1-002: recursion-guard sentinel. The controller exports this in the
# environment of any test suite it runs during preflight; the `build`/`dashboard`
# verbs short-circuit when they see it, so a project test that invokes the
# controller's own verbs bare cannot recurse into real orchestration
# (pytest-within-pytest) or bind a server — which would hang the parent build.
# The controller's own unit tests never set it, so legitimate CLI coverage runs
# unchanged.
IN_TEST_ENV_VAR = "SDLC_IN_TEST"

# Per-test timeout (seconds) added to the detected pytest command when the
# project ships pytest-timeout, so a single hanging test fails fast instead of
# stalling the whole suite until the (much larger) preflight timeout. Best-effort
# and graceful: applied only when the plugin is present, like `-n auto` for xdist.
PER_TEST_TIMEOUT = 60


def in_test_sentinel() -> bool:
    """True when the in-test sentinel env var is set to a truthy value.

    Truthy values: ``1``/``true``/``yes``/``on`` (case-insensitive). Anything
    else — including unset, empty, ``0``, ``false`` — is False.
    """
    return os.environ.get(IN_TEST_ENV_VAR, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

# Maximum commit-message re-asks after a commitlint violation (Story 12.2-002).
# Bounded like the bugfix loop: try to get a compliant header before the work
# reaches a PR, but never spin forever over a cosmetic message issue.
MAX_COMMITLINT_REASK = 2

# How long a contended ledger writer waits out the WAL writer lock before it
# errors with "database is locked" (Story 17.1-002). WAL allows concurrent
# readers plus a single writer, so when several parallel-cohort workers write at
# once SQLite serializes them; the busy_timeout makes the losers *retry
# internally* for this window rather than fail immediately. Set explicitly on the
# write connection so the guarantee never silently depends on Python's implicit
# ``sqlite3.connect`` default. At least as generous as the read side's 2000ms.
LEDGER_BUSY_TIMEOUT_MS = 5000

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
    status          TEXT NOT NULL,
    actor           TEXT                            -- host login that drove the run (Story 22.5-001).
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
    wave            INTEGER,
    dependencies    TEXT,
    worktree_path   TEXT,
    merge_sha       TEXT,
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
    estimated_tokens    INTEGER,
    estimated_cost_usd  REAL,
    harness             TEXT,
    model               TEXT,
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
    message     TEXT NOT NULL,
    stage       TEXT,
    kind        TEXT
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

# story_inventory: Epic-22 (Story 22.1-001) cross-backlog cache — one row per
# story across *every* epic, keyed by the bare story id (not per-run like the
# `stories` table). The local projection the host issue mirror and the portfolio
# dashboard both render from. `status`/`owner`/`issue_ref` are written by sync and
# the build (not hand-edited); `host`+`issue_ref` together identify the remote
# item (GitHub issue number / GitLab iid, stored host-neutral as text); `harness`
# is the derived per-story harness summary (Epic-20 20.2-002). Kept as a standalone
# constant so it can be both embedded in `_SCHEMA_DDL` (fresh DB) and run by
# Migration 7 (upgrade an existing ledger), guaranteeing both paths are identical.
_STORY_INVENTORY_DDL = """
CREATE TABLE IF NOT EXISTS story_inventory (
    story_id    TEXT PRIMARY KEY,                -- bare story id, e.g. '22.1-001'.
    epic        TEXT,                            -- e.g. '22'.
    feature     TEXT,                            -- e.g. '22.1'.
    title       TEXT,
    points      INTEGER,
    risk        TEXT,                            -- 'Low' | 'Medium' | 'High'.
    status      TEXT,                            -- cached execution status (sync/build owned).
    owner       TEXT,                            -- cached read of the host assignee.
    human_status TEXT,                           -- pulled human signal: 'blocked' | 'wontfix' (Story 22.4-001).
    host        TEXT,                            -- 'github' | 'gitlab'.
    issue_ref   TEXT,                            -- GitHub issue number / GitLab iid (host-neutral text).
    harness     TEXT,                            -- derived per-story harness summary (Epic-20 20.2-002).
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# The base DDL plus every standalone table constant, run as one script by
# ``Ledger.init()`` on a fresh DB (each statement is ``IF NOT EXISTS``).
_SCHEMA_DDL = _SCHEMA_DDL + _STORY_INVENTORY_DDL

_TERMINAL_RUN_STATES = {
    "DONE", "FAILED", "ABORTED", "NEEDS_ATTENTION", "AWAITING_APPROVAL",
}

# Schema migrations applied by Ledger.init() after the base DDL. Each entry is
# ``(version, name, table, columns, create_sql)``. Most entries add missing
# columns to an existing ledger idempotently (guarded by PRAGMA table_info, so a
# fresh DB created with the up-to-date DDL is a no-op); an entry whose
# ``create_sql`` is set instead runs that ``CREATE TABLE IF NOT EXISTS`` to add a
# whole new table to a pre-existing ledger (the column-add path cannot, since the
# table is absent). Each is recorded in the _migrations table so it runs at most
# once per DB. Migration 1 adds the per-stage token/cost columns to a pre-existing
# ledger without touching its rows (old stages keep NULL usage and render as "—").
_MIGRATIONS: list[tuple[int, str, str, list[tuple[str, str]], str | None]] = [
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
        None,
    ),
    # Migration 2 adds the sub-stage progress columns (Story 11.1-002) to a
    # pre-existing ledger. Additive and back-compatible: old event rows keep
    # NULL stage/kind and are unaffected; only the new `progress`-level rows
    # populate them. A fresh DB created from the up-to-date DDL is a no-op.
    (
        2,
        "event progress columns",
        "events",
        [
            ("stage", "TEXT"),
            ("kind", "TEXT"),
        ],
        None,
    ),
    # Migration 3 adds the pre-dispatch estimate columns (Story 14.1-002) to a
    # pre-existing ledger. Additive and back-compatible: old stage rows keep NULL
    # estimates and render as "—"; only stages dispatched after this story
    # populate them. A fresh DB created from the up-to-date DDL is a no-op.
    (
        3,
        "stage estimate columns",
        "stages",
        [
            ("estimated_tokens", "INTEGER"),
            ("estimated_cost_usd", "REAL"),
        ],
        None,
    ),
    # Migration 4 adds the wave (cohort) index + intra-queue dependency list
    # (Story 11.2-007) to a pre-existing ledger's `stories` table. Additive and
    # back-compatible: old story rows keep NULL wave/dependencies and read as
    # "no recorded parallelism structure"; only stories scheduled after this
    # story populate them. A fresh DB created from the up-to-date DDL is a no-op.
    (
        4,
        "story wave + dependency columns",
        "stories",
        [
            ("wave", "INTEGER"),
            ("dependencies", "TEXT"),
        ],
        None,
    ),
    # Migration 5 adds the per-story worktree path (Story 17.2-001) to a
    # pre-existing ledger's `stories` table. Additive and back-compatible: old
    # story rows keep NULL worktree_path (they built in the shared repo root);
    # only stories the controller isolates in a dedicated git worktree populate
    # it. A fresh DB created from the up-to-date DDL is a no-op.
    (
        5,
        "story worktree path column",
        "stories",
        [
            ("worktree_path", "TEXT"),
        ],
        None,
    ),
    # Migration 6 adds the per-stage harness (Story 20.2-002) to a pre-existing
    # ledger's `stages` table. Additive and back-compatible: old stage rows keep
    # NULL harness and the read side renders them as the built-in default
    # ``claude`` (everything before this story ran on Claude); only stages
    # dispatched after this story populate the column. A fresh DB created from
    # the up-to-date DDL is a no-op.
    (
        6,
        "stage harness column",
        "stages",
        [
            ("harness", "TEXT"),
        ],
        None,
    ),
    # Migration 7 adds the Epic-22 (Story 22.1-001) `story_inventory` cross-backlog
    # cache to a pre-existing ledger. Unlike the column-add migrations above, the
    # table is wholly new, so it carries a ``CREATE TABLE IF NOT EXISTS`` rather
    # than an ALTER column list. Additive and back-compatible: existing rows in
    # every other table are untouched; a fresh DB created from the up-to-date DDL
    # already has the table, so this is a no-op there.
    (
        7,
        "story inventory table",
        "story_inventory",
        [],
        _STORY_INVENTORY_DDL,
    ),
    # Migration 8 adds the Epic-22 (Story 22.4-001) `human_status` column to the
    # `story_inventory` table — the pulled human signal (`blocked`/`wontfix`) the
    # reconcile writes back from the host. Additive and back-compatible: older
    # ledgers keep NULL (no signal) until the next sync pulls one; a fresh DB
    # created from the up-to-date DDL already has the column, so this is a no-op.
    (
        8,
        "story inventory human_status column",
        "story_inventory",
        [
            ("human_status", "TEXT"),
        ],
        None,
    ),
    # Migration 9 adds the per-run `actor` (Story 22.5-001) to a pre-existing
    # ledger's `runs` table — the host login (`gh api user` / `glab` equivalent)
    # that drove the run. Additive and back-compatible: old run rows keep NULL
    # actor and read as "unattributed"; only runs stamped after this story
    # populate it (``unknown`` when host auth is absent — it degrades, never
    # crashes). A fresh DB created from the up-to-date DDL is a no-op.
    (
        9,
        "run actor column",
        "runs",
        [
            ("actor", "TEXT"),
        ],
        None,
    ),
    # Migration 10 adds the GitLab/GitHub merge sha (Story 23.2-003) to a
    # pre-existing ledger's `stories` table — the merge commit a story landed at,
    # recorded when the merge stage succeeds so the ledger marks the story DONE
    # *with* the merge sha (AC3). Additive and back-compatible: old story rows
    # keep NULL merge_sha (they predate the capture); only stories merged after
    # this story populate it. A fresh DB created from the up-to-date DDL is a
    # no-op.
    (
        10,
        "story merge sha column",
        "stories",
        [
            ("merge_sha", "TEXT"),
        ],
        None,
    ),
    # Migration 11 adds the per-stage resolved model (Issue #427) to a
    # pre-existing ledger's `stages` table, mirroring Migration 6's harness
    # column. Additive and back-compatible: old stage rows keep NULL model and
    # the harness-aware history ladder treats them as the widest (any) cohort
    # only, never polluting the harness+model bucket; only stages dispatched
    # after this migration populate it. A fresh DB created from the up-to-date
    # DDL is a no-op.
    (
        11,
        "stage model column",
        "stages",
        [
            ("model", "TEXT"),
        ],
        None,
    ),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending schema migrations on an open connection (idempotent).

    Adds any missing columns via ``ALTER TABLE`` and records each applied
    version in ``_migrations``. Identifiers come from the internal ``_MIGRATIONS``
    table (never user input), so the f-string interpolation is safe — SQLite
    cannot parametrise column/table names.

    The bookkeeping ``_migrations`` table is created on the fly if missing:
    ``init`` already creates it via the base DDL, but ``ensure_migrated`` runs
    against a pre-existing ledger that may predate the migration framework
    entirely (exactly the ledger Migration 1 targets), so this function cannot
    assume the table exists.
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
        # A table-creation migration brings a wholly new table onto a pre-existing
        # ledger (the column-add path below cannot, since PRAGMA table_info is
        # empty for an absent table). The DDL is ``CREATE TABLE IF NOT EXISTS``, so
        # it is a no-op when ``init`` already created the table from the base DDL.
        if create_sql:
            conn.executescript(create_sql)
        # A pre-framework ledger may be missing the target table entirely (a
        # partial ancient DB). PRAGMA table_info returns empty for an absent
        # table, so guard the ALTERs on table existence and still record the
        # version: if the table is later created it comes from the up-to-date
        # DDL, which already carries the column.
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if existing:
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
    # Story 13.4-002: run dispatched agents inside the container sandbox.
    "--sandbox": "sandbox",
}


# ---------------------------------------------------------------------------
# Options + argument parsing
# ---------------------------------------------------------------------------

# Sentinel distinguishing "model-routing config not yet resolved" from a
# resolved value of None (routing off). Stored on BuildOptions._model_config so
# the per-repo override file is read at most once per run, not once per stage.
_UNRESOLVED: object = object()


@dataclass
class BuildOptions:
    """Parsed `sdlc build` arguments — the same surface the skill exposes."""

    scope: str = "all"
    dry_run: bool = False
    auto: bool = False
    skip_coverage: bool = False
    limit: int = 0
    sequential: bool = False
    # Story 17.1-001: bounded concurrent cohort execution. ``concurrency`` is the
    # worker cap a ``parallel`` run dispatches a cohort's ready stories through
    # (default 5). ``--sequential`` forces an effective cap of 1 — the byte-for-byte
    # serial path — regardless of this value; see :func:`effective_concurrency`.
    concurrency: int = 5
    coverage_threshold: int = 90
    skip_preflight: bool = False
    rebuild: bool = False
    preflight_timeout: int = 600
    # Story 14.1-001: per-run token budget gate. ``budget`` is the token ceiling
    # (the governance primitive — 0 means no ceiling, today's behaviour). A
    # ``$``-denominated budget is accepted as a convenience and converted to the
    # notional API-equivalent token ceiling; the original dollars are kept in
    # ``budget_usd`` only for the labelled-notional display. ``budget_policy`` is
    # ``pause`` (NEEDS_ATTENTION-style resumable hold) or ``abort`` (terminal).
    budget: int = 0
    budget_usd: float | None = None
    budget_policy: str = "pause"
    # Story 14.1-003: Max rate-limit / quota awareness with automatic resume.
    # ``rate_limit_max_wait_s`` is the in-process auto-wait cap (~one window): a
    # reset within it is waited out and the same run auto-resumes; a reset beyond
    # it durably parks for `sdlc resume`. ``window_budget`` (tokens; 0 = off, rely
    # only on agent rate-limit signals) is a *configured* per-window token budget
    # tracked from the 11.1-003 accrual; ``window_s`` is its rolling-window length
    # and ``rate_limit_threshold`` (< 1.0) pauses *near* the limit, not only at it.
    rate_limit_max_wait_s: int = 18000  # ~5h, one Max rolling window
    window_budget: int = 0
    window_s: int = 18000
    rate_limit_threshold: float = 1.0
    # Story 14.2-001: per-task model routing. ``model_profile`` selects the
    # per-stage model map — "" / "off" keeps today's behaviour (no ``--model``,
    # CLI default = Opus for every stage); "balanced" is the shipped default map,
    # with "quality-first" / "quota-max" as documented alternatives.
    # ``model_overrides`` maps a stage to an explicit model that *wins* over the
    # map (the per-stage escape hatch). ``_model_config`` memoizes the resolved
    # config (profile + per-repo override) so it is read at most once per run.
    model_profile: str = ""
    model_overrides: dict[str, str] = field(default_factory=dict)
    _model_config: object = field(default=_UNRESOLVED, repr=False, compare=False)
    # Story 14.1-002: pre-dispatch cost-estimate gate. ``cost_estimate_threshold``
    # is a per-stage token ceiling for the *estimate* (0 = off → estimate is still
    # computed and recorded, but never warns or gates). A ``$``-denominated value
    # is accepted as a convenience and converted to the notional API-equivalent
    # token count, mirroring ``--budget``. When an estimate crosses the threshold
    # the controller warns; interactively (no ``--auto``) it gates the stage before
    # any spend, in ``--auto`` it proceeds.
    cost_estimate_threshold: int = 0
    # Story 14.2-002: per-request thinking-token cap surfaced to dispatched agents
    # as ``MAX_THINKING_TOKENS`` (bounds hidden extended-thinking cost on long
    # runs). 0 = no cap → behaviour unchanged (the agent's default thinking
    # budget). It is a per-run constant, bound onto the real dispatch seam in
    # :func:`_resolve_dispatch`; auto-compaction stays at Claude Code's default.
    thinking_cap: int = 0
    # Story 13.4-002: run every dispatched agent inside a no-egress, cap-dropped,
    # non-root container with the worktree bind-mounted — the recommended path for
    # an untrusted repo. False = host path (today's default). Bound onto the real
    # dispatch seam in :func:`_resolve_dispatch`; persisted per run so a resume
    # keeps the isolation. ``SDLC_SANDBOX`` is the env equivalent honoured directly
    # at the dispatch boundary, so it covers runs this flag never threaded through.
    sandbox: bool = False
    # Story 20.2-001: per-role harness routing. ``harness_map`` maps a pipeline
    # role (build/coverage/review/merge/docs; ``qa`` aliases ``coverage``) to the
    # harness that runs it, e.g. ``{"build": "claude", "review": "codex"}``. Empty
    # = today's behaviour (every role on the built-in ``claude`` default). Parsed
    # eagerly from ``--harness=`` and resolved/validated against the registry in
    # the CLI preflight so an unknown/disabled harness fails fast (no half-run).
    harness_map: dict[str, str] = field(default_factory=dict)
    # Story 23.2-002: gate the merge on the change request's CI/pipeline status.
    # Before the merge stage dispatches, the controller polls the CR's normalised
    # CI status (the `gh pr` check rollup / the GitLab MR pipeline, via the
    # adapter) and only proceeds on green — a red/timed-out pipeline routes the
    # story into the bugfix loop, exactly as a failed merge does. ``ci_gate_timeout_s``
    # bounds the poll so a never-finishing pipeline can never hang the run (0 = a
    # single status read, no wait); ``ci_gate_poll_s`` is the between-poll
    # interval; ``ci_gate_no_ci`` degrades a project with *no* CI signal — ``allow``
    # warns and merges (today's behaviour), ``deny`` blocks. The gate is a no-op for
    # a story with no host mapping or no CR ref, so the unmapped path is unchanged.
    ci_gate_timeout_s: int = 1800
    ci_gate_poll_s: int = 30
    ci_gate_no_ci: str = "allow"


# Story 14.1-001: notional API-equivalent rate for converting a ``$``-budget into
# the token primitive the gate enforces. On a Claude Max subscription the dollar
# figure is an API-list-price equivalent computed from token usage — never real
# spend on the flat monthly fee — so this is a documented convenience constant,
# not a billing fact. A blended ~$15/Mtok keeps the conversion easy to reason
# about ($15 ⇒ 1M tokens) without pretending to be authoritative.
NOTIONAL_USD_PER_MILLION_TOKENS = 15.0


def usd_to_notional_tokens(usd: float) -> int:
    """Convert a ``$``-budget into its notional API-equivalent token ceiling.

    Story 14.1-001: the gate's primitive is tokens; a dollar budget is a
    convenience. The rate is notional (see :data:`NOTIONAL_USD_PER_MILLION_TOKENS`),
    so this is guidance for the ceiling, never a billing computation.
    """
    return int(round(usd / NOTIONAL_USD_PER_MILLION_TOKENS * 1_000_000))


def _budget_exceeded(ledger: "Ledger", run_id: str, budget: int) -> bool:
    """Whether the run's accrued tokens have reached the budget ceiling.

    Story 14.1-001: a ``budget`` of 0 means "no ceiling" (never gates). The
    accrual is the live total the ledger exposes, so this reflects every stage
    recorded so far — including those from before a pause, which is what makes a
    resumed run honour the same ceiling rather than continue unbounded.
    """
    return bool(budget) and ledger.run_usage_totals(run_id)["tokens"] >= budget


# ---------------------------------------------------------------------------
# Story 14.1-003: Max rate-limit / quota awareness with automatic resume
# ---------------------------------------------------------------------------

# Cadence of the countdown log emitted while waiting in-process for a rate-limit
# window to reopen. ~5 min keeps the ledger/dashboard showing the run is alive
# and how long is left, without spamming the event log on a multi-hour wait.
RATE_LIMIT_POLL_S = 300


@dataclass
class _RateLimitContext:
    """The injectable knobs the rate-limit wait/park path needs (Story 14.1-003).

    ``window`` is the optional configured rolling-window token budget (None when
    only reactive 429 signals gate). ``clock`` / ``sleep_fn`` are injected so the
    in-process auto-wait is deterministic and instant under test (the per-agent
    dispatch timeout bounds the agent subprocess, not this controller-side wait).
    """

    opts: BuildOptions
    window: "WindowQuota | None"
    clock: Callable[[], float]
    sleep_fn: Callable[[float], None]


@dataclass
class _StoryRunOutcome:
    """Result of driving one story through the rate-limit-aware runner.

    ``status`` is the terminal story status on a normal finish (then
    ``parked`` is False). When the window reset was beyond the auto-wait cap the
    story is left for `sdlc resume`: ``parked`` is True, ``status`` is None, and
    ``signal`` carries the pause cause. ``waited_s`` is the total time auto-waited
    in-process across any within-cap pauses before this outcome.
    """

    status: str | None
    parked: bool = False
    signal: "RateLimitSignal | None" = None
    waited_s: int = 0


class _RateLimitPark(Exception):
    """Internal signal: a rate-limit reset is beyond the auto-wait cap (14.1-003).

    Raised from :func:`_run_story` (which owns the in-process auto-wait so the
    in-story attempt/PR/bugfix state survives a within-cap pause) and caught by
    :func:`_run_story_rate_limited`, which converts it to a ``parked`` outcome so
    the caller durably parks the run RATE_LIMITED. ``waited_s`` is the time
    already auto-waited in-process before giving up and parking.
    """

    def __init__(self, *, signal: "RateLimitSignal", waited_s: int) -> None:
        super().__init__("rate-limit reset beyond auto-wait cap")
        self.signal = signal
        self.waited_s = waited_s


class _CostGatePause(Exception):
    """Internal signal: the interactive pre-dispatch cost gate halted a stage (14.1-002).

    Raised from :func:`_run_story` when an over-threshold estimate must gate a
    stage in interactive mode. It propagates *past* :func:`_run_story_rate_limited`
    (which catches only :class:`_RateLimitPark`) up to the cohort loop in
    ``run_build`` / ``run_resume``, which pauses the run **resumably** — leaving it
    IN_PROGRESS like a budget pause rather than stamping a NEEDS_ATTENTION terminal
    that :meth:`Ledger.latest_resumable_run` would never surface. Carrying the
    gated stage + estimate lets the close-out write an actionable reason. Raising
    ``--cost-threshold`` on ``sdlc resume`` lets the gated stage proceed.
    """

    def __init__(self, *, story_id: str, stage: str, estimate: StageEstimate) -> None:
        super().__init__(f"cost gate halted {stage} for {story_id}")
        self.story_id = story_id
        self.stage = stage
        self.estimate = estimate


def _make_rate_limit_context(
    opts: BuildOptions,
    *,
    clock: Callable[[], float] | None,
    sleep_fn: Callable[[float], None] | None,
    baseline: int = 0,
) -> _RateLimitContext:
    """Build the rate-limit context, defaulting the clock/sleep to wall-clock.

    ``baseline`` is the run's *already-accrued* tokens when the window opens — 0
    for a fresh build (no spend yet), but the current accrual for a **resume**.
    This is essential: the configured window budget measures usage *within* the
    window as ``total - baseline``. If a resume of a durably-parked RATE_LIMITED
    run started the window at baseline 0, the cumulative pre-park spend would
    already exceed the budget and the run would re-park forever, making zero
    forward progress. Seeding the baseline with the current accrual treats the
    resume as a freshly-reopened window (the documented approximate heuristic), so
    each resume can build at least up to another window-budget of work.
    """
    the_clock = clock or time.time
    window: WindowQuota | None = None
    if opts.window_budget > 0:
        window = WindowQuota(
            budget=opts.window_budget,
            window_s=opts.window_s,
            threshold=opts.rate_limit_threshold,
            start=the_clock(),
            baseline=baseline,
        )
    return _RateLimitContext(
        opts=opts, window=window, clock=the_clock, sleep_fn=sleep_fn or time.sleep,
    )


def _rate_limit_wait(
    ledger: "Ledger",
    run_id: str,
    signal: "RateLimitSignal",
    wait_s: int,
    *,
    sleep_fn: Callable[[float], None],
) -> int:
    """Wait in-process for the window to reopen, logging a periodic countdown.

    The run is flagged ``RATE_LIMITED`` for the duration so a concurrent `sdlc
    status` / dashboard read shows the pause distinctly, then restored to
    ``IN_PROGRESS`` before dispatch resumes. The loop is driven by the accumulated
    sleep (not the wall clock) so an injected no-op ``sleep_fn`` terminates
    deterministically under test. Returns the total seconds waited.
    """
    ledger.run_update_status(run_id, "RATE_LIMITED")
    ledger.event_log(
        run_id, "", "warn", "controller",
        f"rate limit hit ({signal.source}) — waiting ~{wait_s}s in-process for the "
        "window to reopen, then auto-resuming this run (no manual resume needed).",
    )
    waited = 0
    while waited < wait_s:
        chunk = min(RATE_LIMIT_POLL_S, wait_s - waited)
        sleep_fn(chunk)
        waited += chunk
        remaining = wait_s - waited
        if remaining > 0:
            ledger.event_log(
                run_id, "", "info", "controller",
                f"rate-limit wait: ~{remaining}s until the window reopens.",
            )
    ledger.run_update_status(run_id, "IN_PROGRESS")
    ledger.event_log(
        run_id, "", "success", "controller",
        "rate-limit window reopened — resuming dispatch.",
    )
    return waited


def apply_rate_limit_park(
    ledger: "Ledger",
    run_id: str,
    signal: "RateLimitSignal",
    *,
    now: float,
    waited_s: int,
    window_s: int,
) -> float:
    """Durably park a run whose window reset is beyond the auto-wait cap (14.1-003).

    Records a distinct ``RATE_LIMITED`` run state — resumable (NOT terminal) and
    deliberately NOT ``NEEDS_ATTENTION``: the run is waiting for *time*, not human
    attention. The process does not hold indefinitely; `sdlc resume` (or a
    scheduled wake) continues it once the window reopens. Committed work from
    finished stories is untouched (R10). Returns the approximate reset epoch.

    The reset epoch is resolved the same way :func:`seconds_until_reset` resolves
    the wait: an explicit ``reset_at`` → a relative ``retry_after`` → else a full
    ``window_s`` fallback. The fallback matters: a throttle that surfaced *no*
    explicit reset (and so parked only because a full window exceeds the cap) must
    still record a reset epoch, or the resume gate would have nothing to honour
    and would dispatch early into the still-closed window.
    """
    reset_at = signal.reset_at
    if reset_at is None and signal.retry_after_s is not None:
        reset_at = now + signal.retry_after_s
    if reset_at is None:
        reset_at = now + window_s  # fallback: assume a full window must elapse
    waited_note = f" after auto-waiting {waited_s}s" if waited_s else ""
    ledger.event_log(
        run_id, "", "warn", "controller",
        f"rate limit hit ({signal.source}); reset is beyond the auto-wait cap"
        f"{waited_note}; window reopens ~epoch {int(reset_at)} — parking run "
        "RATE_LIMITED (waiting for time, not a failure); `sdlc resume` continues "
        "it once the window reopens.",
    )
    # Persist the reset epoch into the run config so a resume *honours* it rather
    # than dispatching early (which would blow the still-closed window). Merge it
    # into the existing config event so no original key is lost; run_config reads
    # the latest config event, so this becomes the effective config on resume.
    cfg = ledger.run_config(run_id)
    cfg["rate_limit_reset_at"] = float(reset_at)
    ledger.event_log(run_id, "", "info", "config", json.dumps(cfg))
    ledger.run_update_status(run_id, "RATE_LIMITED")
    try:  # best-effort lifecycle notification; never fail a run
        notify(
            "rate_limited", run=run_id, source=signal.source,
            reset_at=int(reset_at), waited_s=waited_s,
        )
    except Exception:
        pass
    return reset_at


def _honor_parked_reset(
    ledger: "Ledger",
    run_id: str,
    opts: BuildOptions,
    rl_ctx: _RateLimitContext,
    reset_at: float | None,
) -> _StoryRunOutcome | None:
    """At resume, wait for — or re-park until — a persisted window-reset time.

    Story 14.1-003: a durably-parked ``RATE_LIMITED`` run records the approximate
    epoch its Max window reopens (``apply_rate_limit_park`` persists it in the run
    config). Resuming *before* that epoch must not dispatch early — that would
    spend into a still-closed window and blow the quota the original park was
    protecting. So when ``now`` is before the reset, the controller waits
    in-process (when the remaining time is within the auto-wait cap) or durably
    re-parks (beyond it). Returns a parked :class:`_StoryRunOutcome` to re-park,
    or ``None`` once the window has reopened (or no reset is pending) so the caller
    proceeds with a fresh window. (The fresh-window baseline is seeded by
    :func:`_make_rate_limit_context`, so progress is then unbounded by pre-park
    spend.)
    """
    if reset_at is None:
        return None
    now = rl_ctx.clock()
    if now >= reset_at:
        return None  # the window has already reopened — proceed
    signal = RateLimitSignal(source="window-reset", reset_at=float(reset_at))
    wait_s = seconds_until_reset(signal, now=now, window_s=opts.window_s)
    if not within_wait_cap(wait_s, opts.rate_limit_max_wait_s):
        return _StoryRunOutcome(status=None, parked=True, signal=signal, waited_s=0)
    _rate_limit_wait(ledger, run_id, signal, wait_s, sleep_fn=rl_ctx.sleep_fn)
    if rl_ctx.window is not None:
        rl_ctx.window.reopen(rl_ctx.clock(), ledger.run_usage_totals(run_id)["tokens"])
    return None


def _run_story_rate_limited(
    ctx: _RateLimitContext,
    story: Story,
    ledger: "Ledger",
    run_id: str,
    dispatch: "Dispatcher",
    logs_dir: Path,
    **run_story_kwargs,
) -> _StoryRunOutcome:
    """Drive one story to a terminal status, absorbing Max rate-limit pauses.

    Shared by :func:`run_build` and ``run_resume`` so both react identically.
    Two signal sources are handled the same way — compute the window-reset time,
    then wait in-process (reset within the cap → automatic resume) or return a
    ``parked`` outcome (reset beyond the cap → durable `sdlc resume` handoff):

    * proactive: a configured rolling-window token budget exhausted *before* the
      story is dispatched (checked here);
    * reactive: a :class:`~sdlc.dispatch.RateLimitError` raised mid-stage, which
      :func:`_run_story` absorbs internally (so the in-story attempt/PR/bugfix
      state survives the wait) and escalates as :class:`_RateLimitPark` only when
      the reset is beyond the cap.

    A throttle never enters the bugfix loop, so it can never burn a stage attempt.
    """
    opts = ctx.opts
    waited_total = 0

    # Proactive configured-window-budget gate (before any dispatch this story).
    if ctx.window is not None:
        while ctx.window.exhausted(ledger.run_usage_totals(run_id)["tokens"]):
            signal = ctx.window.signal()
            wait_s = seconds_until_reset(
                signal, now=ctx.clock(), window_s=opts.window_s
            )
            if not within_wait_cap(wait_s, opts.rate_limit_max_wait_s):
                return _StoryRunOutcome(
                    status=None, parked=True, signal=signal, waited_s=waited_total,
                )
            waited_total += _rate_limit_wait(
                ledger, run_id, signal, wait_s, sleep_fn=ctx.sleep_fn,
            )
            ctx.window.reopen(ctx.clock(), ledger.run_usage_totals(run_id)["tokens"])

    try:
        status = _run_story(
            story, opts, ledger, run_id, dispatch, logs_dir,
            rl_ctx=ctx, **run_story_kwargs,
        )
    except _RateLimitPark as park:
        return _StoryRunOutcome(
            status=None, parked=True, signal=park.signal,
            waited_s=waited_total + park.waited_s,
        )
    return _StoryRunOutcome(status=status, waited_s=waited_total)


# ---------------------------------------------------------------------------
# Story 17.1-001: bounded concurrent cohort execution
# ---------------------------------------------------------------------------

def effective_concurrency(opts: BuildOptions) -> int:
    """The worker cap a run actually dispatches a cohort through (Story 17.1-001).

    ``--sequential`` forces ``1`` — one story at a time, byte-for-byte today's
    serial path — regardless of ``--concurrency``. Otherwise the run honours
    ``opts.concurrency`` (default 5), floored at 1 so a nonsensical 0/negative
    value can never disable dispatch entirely.
    """
    if opts.sequential:
        return 1
    return max(1, opts.concurrency)


def authoritative_mode(opts: BuildOptions) -> str:
    """The run's ``mode`` label, made truthful by actual dispatch behaviour (17.3-001).

    A run drives a cohort through :func:`effective_concurrency` workers; when that
    is ``1`` — ``--sequential`` *or* ``--concurrency=1`` — the run is byte-for-byte
    serial and must never wear a ``parallel`` label. Any higher worker cap is a
    genuine ``parallel`` run. Keeping the label derived from the same figure the
    executor uses means the displayed mode can never disagree with reality (AC2).
    """
    return "serial" if effective_concurrency(opts) == 1 else "parallel"


@dataclass
class _StoryDispatch:
    """One story's result from a concurrent cohort dispatch (Story 17.1-001).

    Exactly one of ``outcome`` / ``cost_gate`` / ``error`` is set. ``outcome``
    is the normal :class:`_StoryRunOutcome` (terminal status or a rate-limit
    park); ``cost_gate`` is a :class:`_CostGatePause` the worker raised; ``error``
    is any *unexpected* exception, captured so one story blowing up never crashes
    the pool or its peers (failure isolation).
    """

    story: Story
    outcome: _StoryRunOutcome | None = None
    cost_gate: _CostGatePause | None = None
    error: BaseException | None = None


def _dispatch_cohort(
    dispatchable: list[Story],
    *,
    max_workers: int,
    run_one: Callable[[Story], _StoryRunOutcome],
    on_terminal: Callable[[Story, str], None] | None = None,
) -> list[_StoryDispatch]:
    """Drive a cohort's ready stories through a bounded thread pool (Story 17.1-001).

    Each story is run by ``run_one`` (which prepares its isolated worktree and
    drives the full build→coverage→review→merge sequence via
    :func:`_run_story_rate_limited`) on its own worker thread, at most
    ``max_workers`` at once. Work is I/O-bound on the agent subprocesses, so
    threads suffice — exactly the model :mod:`sdlc.adversarial` already uses.

    The call is a **barrier**: it returns only once every story finishes, with
    results in the cohort's submission order so the caller applies outcomes
    deterministically. A worker that raises an unexpected exception is captured
    on its :class:`_StoryDispatch` (``error``) rather than propagated, so the
    other workers run to completion (failure isolation, AC4). ``_CostGatePause``
    — a deliberate run-level pause signal — is captured separately so the caller
    can halt the run after the barrier, exactly as the serial path does.

    Story 19.2-002: ``on_terminal(story, status)`` — when supplied — fires the
    instant a worker reaches a *terminal* outcome (a non-parked status, or an
    unexpected raise → ``FAILED``), so the caller can persist that story's status
    live rather than waiting for the barrier. A parked outcome is a resumable
    run-pause, not a terminal story state, so it is never credited here.
    """
    results = {story.id: _StoryDispatch(story=story) for story in dispatchable}

    def _worker(story: Story) -> None:
        try:
            outcome = run_one(story)
        except _CostGatePause as gate:
            results[story.id].cost_gate = gate
            return
        except Exception as exc:  # noqa: BLE001 — failure isolation (AC4)
            results[story.id].error = exc
            if on_terminal is not None:
                on_terminal(story, "FAILED")
            return
        results[story.id].outcome = outcome
        if on_terminal is not None and not outcome.parked:
            on_terminal(story, outcome.status or "FAILED")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_worker, story) for story in dispatchable]
        # Barrier: wait for the whole cohort before the caller proceeds. Each
        # _worker swallows its own exception, so .result() never raises here.
        concurrent.futures.wait(futures)

    return [results[story.id] for story in dispatchable]


def notional_cost_label(cost_usd: float | None) -> str:
    """Render a dollar figure with the mandated not-real-spend disclaimer.

    Story 14.1-001: any ``$`` the controller surfaces is an API-list-price
    equivalent derived from token usage, never actual spend on a flat-fee Max
    subscription. Centralised so every surface (ledger events, status, dashboard)
    reads identically and a reader can never mistake it for a real bill.
    """
    figure = "$—" if cost_usd is None else f"${cost_usd:.2f}"
    return f"{figure} (API-equivalent, not billed on subscription)"


def _parse_budget_value(raw: str) -> tuple[int, float | None]:
    """Parse a ``--budget`` value into ``(token_ceiling, notional_usd_or_None)``.

    A bare number is a token ceiling (the primary unit). A ``$``-prefixed or
    ``usd``-suffixed value is a convenience dollar budget converted to the
    notional API-equivalent token ceiling (Story 14.1-001). Thousands separators
    (``,`` / ``_``) are tolerated. A negative value is rejected.
    """
    s = raw.strip()
    is_dollars = s.startswith("$") or s.lower().endswith("usd")
    num = s[1:] if s.startswith("$") else s
    if num.lower().endswith("usd"):
        num = num[:-3]
    num = num.strip().replace(",", "").replace("_", "")
    if is_dollars:
        usd = float(num)
        if usd < 0:
            raise ValueError(f"--budget must be non-negative: {raw}")
        return usd_to_notional_tokens(usd), usd
    tokens = int(num)
    if tokens < 0:
        raise ValueError(f"--budget must be non-negative: {raw}")
    return tokens, None


def parse_build_args(args: Iterable[str]) -> BuildOptions:
    """Parse the `sdlc build` argument vector into :class:`BuildOptions`.

    Accepts the exact flags the skill documents:
    ``[scope...] [--dry-run] [--auto] [--skip-coverage] [--limit=N]
    [--sequential] [--concurrency=N] [--coverage-threshold=N] [--skip-preflight]
    [--rebuild] [--preflight-timeout=SEC]``. Each ``scope`` is ``all``,
    ``epic-NN``, an epic name, or a single story id ``X.Y-NNN`` (default ``all``).
    Several scopes may be given (space- or comma-separated); they are collapsed
    into one canonical, sorted, deduped label so a composite run resolves and
    resumes regardless of the order they were typed (Story 19.1-001). Unknown
    flags raise :class:`ValueError` so a typo never silently changes behaviour.
    """
    opts = BuildOptions()
    scopes: list[str] = []
    # An explicit iterator lets a two-token flag (`--harness build=claude,…`,
    # Story 20.2-001) consume its value; every other flag stays single-token.
    arg_iter = iter(args)
    for arg in arg_iter:
        if arg in _BOOL_FLAGS:
            setattr(opts, _BOOL_FLAGS[arg], True)
        elif arg.startswith("--limit="):
            opts.limit = int(arg.split("=", 1)[1])
        elif arg.startswith("--coverage-threshold="):
            opts.coverage_threshold = int(arg.split("=", 1)[1])
        elif arg.startswith("--concurrency="):
            # Story 17.1-001: cap on a parallel cohort's concurrent workers.
            # Must be >= 1 — `--concurrency=1` is the explicit serial path.
            concurrency = int(arg.split("=", 1)[1])
            if concurrency < 1:
                raise ValueError(f"--concurrency must be >= 1: {arg}")
            opts.concurrency = concurrency
        elif arg.startswith("--preflight-timeout="):
            opts.preflight_timeout = int(arg.split("=", 1)[1])
        elif arg.startswith("--budget="):
            opts.budget, opts.budget_usd = _parse_budget_value(arg.split("=", 1)[1])
        elif arg.startswith("--budget-policy="):
            policy = arg.split("=", 1)[1]
            if policy not in {"pause", "abort"}:
                raise ValueError(
                    f"invalid --budget-policy: {policy} (expected pause|abort)"
                )
            opts.budget_policy = policy
        elif arg.startswith("--cost-threshold="):
            # Story 14.1-002: per-stage pre-dispatch estimate ceiling. Reuse the
            # token/$ convenience parser — a $-threshold converts to the notional
            # API-equivalent token count, mirroring --budget. 0 = off.
            opts.cost_estimate_threshold, _ = _parse_budget_value(arg.split("=", 1)[1])
        elif arg.startswith("--thinking-cap="):
            # Story 14.2-002: cap per-request thinking tokens (MAX_THINKING_TOKENS).
            # 0 = no cap (today's default thinking budget); negative is nonsense.
            cap = int(arg.split("=", 1)[1])
            if cap < 0:
                raise ValueError(f"--thinking-cap must be non-negative: {arg}")
            opts.thinking_cap = cap
        elif arg.startswith("--ci-gate-timeout="):
            # Story 23.2-002: bounded poll for the merge CI gate (seconds).
            # 0 = a single status read with no wait; negative is nonsense.
            timeout = int(arg.split("=", 1)[1])
            if timeout < 0:
                raise ValueError(f"--ci-gate-timeout must be non-negative: {arg}")
            opts.ci_gate_timeout_s = timeout
        elif arg.startswith("--ci-gate-poll="):
            poll = int(arg.split("=", 1)[1])
            if poll < 0:
                raise ValueError(f"--ci-gate-poll must be non-negative: {arg}")
            opts.ci_gate_poll_s = poll
        elif arg.startswith("--ci-gate-no-ci="):
            policy = arg.split("=", 1)[1]
            if policy not in {"allow", "deny"}:
                raise ValueError(
                    f"invalid --ci-gate-no-ci: {policy} (expected allow|deny)"
                )
            opts.ci_gate_no_ci = policy
        elif arg.startswith("--rate-limit-max-wait="):
            wait = int(arg.split("=", 1)[1])
            if wait < 0:
                raise ValueError(f"--rate-limit-max-wait must be non-negative: {arg}")
            opts.rate_limit_max_wait_s = wait
        elif arg.startswith("--window-budget="):
            # Reuse the token/$ convenience parser (Story 14.1-001) — a $-window
            # budget converts to the notional API-equivalent token ceiling.
            opts.window_budget, _ = _parse_budget_value(arg.split("=", 1)[1])
        elif arg.startswith("--window="):
            window = int(arg.split("=", 1)[1])
            if window <= 0:
                raise ValueError(f"--window must be positive: {arg}")
            opts.window_s = window
        elif arg.startswith("--rate-limit-threshold="):
            threshold = float(arg.split("=", 1)[1])
            if not 0 < threshold <= 1:
                raise ValueError(
                    f"--rate-limit-threshold must be in (0, 1]: {arg}"
                )
            opts.rate_limit_threshold = threshold
        elif arg.startswith("--model-routing="):
            # Story 14.2-001: select the per-stage model map ("" / off = today's
            # CLI-default behaviour). Validated eagerly so a typo'd profile fails
            # the parse rather than silently disabling routing.
            opts.model_profile = arg.split("=", 1)[1]
            routing_config(opts.model_profile)  # raises on an unknown profile
        elif arg.startswith("--model-"):
            # Story 14.2-001: an explicit per-stage model override that wins over
            # the map (escape hatch), e.g. `--model-build=opus`. Restricted to the
            # known pipeline stages so a typo is a hard error, not a silent no-op.
            stage, _, model = arg[len("--model-"):].partition("=")
            if not model:
                raise ValueError(f"--model-<stage> needs a value: {arg}")
            if stage not in _ROUTABLE_STAGES:
                raise ValueError(
                    f"unknown stage in {arg}: {stage} "
                    f"(expected one of {sorted(_ROUTABLE_STAGES)})"
                )
            opts.model_overrides[stage] = model
        elif arg == "--harness" or arg.startswith("--harness="):
            # Story 20.2-001: per-role harness routing, e.g.
            # `--harness build=claude,review=codex,qa=codex` (the space-separated
            # form the skill documents) or `--harness=…`. Parsed eagerly so a
            # malformed entry or unknown role fails the parse; the registry-bound
            # checks (unknown/disabled harness) happen in the CLI preflight.
            from sdlc.role_routing import RoleRoutingError, parse_role_harness_map

            if arg == "--harness":
                spec = next(arg_iter, None)
                if spec is None:
                    raise ValueError(
                        "--harness needs a value, e.g. "
                        "--harness build=claude,review=codex,qa=codex"
                    )
            else:
                spec = arg.split("=", 1)[1]
            try:
                opts.harness_map = parse_role_harness_map(spec)
            except RoleRoutingError as exc:
                raise ValueError(str(exc)) from exc
        elif arg.startswith("--"):
            raise ValueError(f"unknown flag: {arg}")
        else:
            # Story 19.1-001: collect every positional as a scope token instead of
            # rejecting the second. They are canonicalised below into one label.
            scopes.append(arg)
    # Story 19.1-001: fold all positionals into one canonical scope label (sorted,
    # deduped, lowercased, comma-joined). No positional → the BuildOptions default
    # (`all`) is left untouched so the single-scope and `all` paths are unchanged.
    if scopes:
        opts.scope = canonical_scope(scopes)
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
        # ``timeout`` is set explicitly (not left to Python's 5.0s default) and
        # mirrored by an explicit ``PRAGMA busy_timeout`` so concurrent
        # parallel-cohort writers wait out the WAL writer lock and retry
        # internally rather than erroring with "database is locked"
        # (Story 17.1-002).
        conn = sqlite3.connect(self.db_path, timeout=LEDGER_BUSY_TIMEOUT_MS / 1000)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(f"PRAGMA busy_timeout = {LEDGER_BUSY_TIMEOUT_MS};")
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

    def ensure_migrated(self) -> None:
        """Bring a *pre-existing* ledger up to the current schema (idempotent).

        Unlike :meth:`init`, this never creates the schema: when the DB file is
        absent it is a no-op, so a read/recovery verb launched against a
        never-built repo does not leave behind a spurious empty ledger. When the
        DB exists it opens a *writable* connection (a read-only connection cannot
        ``ALTER TABLE``) and runs :func:`_apply_migrations`, so a verb that then
        reads via :meth:`_connect_ro` can no longer crash with "no such column"
        against a stale schema (e.g. a ledger predating a later migration).

        Concurrent launches are safe. A busy timeout makes a second controller
        wait out the brief writer lock, and ``BEGIN IMMEDIATE`` takes that lock
        *before* the version check so the loser cannot slip an ``ALTER`` in
        between our read of ``_migrations`` and our own ``ALTER`` (which would
        otherwise raise "duplicate column name"); the version guard then makes
        its pass a no-op.
        """
        if not self.db_path.exists():
            return
        with self._connect() as conn:
            conn.execute("PRAGMA busy_timeout = 2000;")
            conn.execute("BEGIN IMMEDIATE;")
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

    def run_set_mode(self, run_id: str, mode: str) -> None:
        """Re-stamp a run's ``mode`` (Story 17.3-001).

        A resume can change the effective worker cap (``--concurrency``), so the
        run's serial/parallel label must be re-derived to stay authoritative —
        otherwise ``status``/the dashboard would report the original run's stale
        mode and worker cap.
        """
        with self._connect() as conn:
            conn.execute("UPDATE runs SET mode = ? WHERE id = ?", (mode, run_id))

    def run_set_actor(self, run_id: str, actor: str) -> None:
        """Stamp the host login that drove this run (Story 22.5-001).

        Resolved once per run from the code host's own auth (`gh api user` /
        `glab` equivalent) — the host *is* the identity provider, so there is no
        shared token to attribute. When host auth is absent the caller passes
        ``unknown`` (identity degrades, it never crashes); the column stays its
        own writer so a re-stamp never touches the run's other fields.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET actor = ? WHERE id = ?", (actor, run_id)
            )

    def run_get_actor(self, run_id: str) -> str | None:
        """Return the stamped actor for a run, or None when unattributed."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT actor FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return row[0] if row else None

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

    def set_story_merge_sha(self, run_id: str, story_id: str, merge_sha: str) -> None:
        """Record the merge commit sha a story landed at (Story 23.2-003).

        Written when the merge stage succeeds (the merge agent's reported
        ``merge_sha``) so the ledger marks the story DONE *with* the GitLab/GitHub
        merge sha (AC3). Host-neutral — a `gh pr merge` and a `glab mr merge` both
        yield a merge commit, so the same column holds either.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE stories SET merge_sha = ? WHERE run_id = ? AND story_id = ?",
                (merge_sha, run_id, story_id),
            )

    def story_merge_sha(self, run_id: str, story_id: str) -> str | None:
        """The recorded merge sha for a story, or ``None`` when it has not merged."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT merge_sha FROM stories WHERE run_id = ? AND story_id = ?",
                (run_id, story_id),
            ).fetchone()
        return row[0] if row else None

    def set_story_wave(
        self, run_id: str, story_id: str, wave: int, dependencies: list[str]
    ) -> None:
        """Record a story's cohort wave index and intra-queue dependency list.

        Story 11.2-007: written at schedule time (run_build / resume) from the
        ``compute_cohorts`` result. ``wave`` is the cohort's 0-based position —
        stories sharing a wave run in parallel — and ``dependencies`` is the JSON
        array of in-queue story ids this story waits on. Both columns are
        nullable, so older ledgers read as NULL/empty (no parallelism structure).
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE stories SET wave = ?, dependencies = ? "
                "WHERE run_id = ? AND story_id = ?",
                (wave, json.dumps(dependencies), run_id, story_id),
            )

    def set_story_worktree(self, run_id: str, story_id: str, path: str) -> None:
        """Record the git worktree a story's agent was dispatched into (17.2-001).

        Written when the controller creates a dedicated per-story worktree so
        concurrent stories cannot collide in a shared checkout. The column is
        nullable — a sequential / shared-root run leaves it NULL — and is read
        back by teardown (17.2-002) and observability to locate the checkout.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE stories SET worktree_path = ? "
                "WHERE run_id = ? AND story_id = ?",
                (path, run_id, story_id),
            )

    def story_worktree(self, run_id: str, story_id: str) -> str | None:
        """The recorded worktree path for a story, or ``None`` (shared root)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT worktree_path FROM stories "
                "WHERE run_id = ? AND story_id = ?",
                (run_id, story_id),
            ).fetchone()
        return row[0] if row else None

    def stage_start(
        self,
        run_id: str,
        story_id: str,
        stage_name: str,
        attempt: int = 1,
        harness: str = DEFAULT_HARNESS,
        model: str | None = None,
    ) -> None:
        """Append an IN_PROGRESS stage attempt row.

        ``harness`` (Story 20.2-002) records which harness ran this stage so a
        heterogeneous run is auditable; it defaults to the built-in ``claude``,
        keeping a run that passes no ``--harness`` map unchanged. ``model`` (Issue
        #427) records the resolved model id so history can be segmented per model;
        it is nullable and defaults to None (routing off / un-recorded), leaving
        old rows and the default path unchanged.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO stages "
                "(run_id, story_id, stage_name, attempt, status, started_at, "
                "harness, model) "
                "VALUES (?, ?, ?, ?, 'IN_PROGRESS', CURRENT_TIMESTAMP, ?, ?)",
                (run_id, story_id, stage_name, attempt, harness, model),
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

    def stage_set_estimate(
        self,
        run_id: str,
        story_id: str,
        stage_name: str,
        attempt: int,
        *,
        estimated_tokens: int | None,
        estimated_cost_usd: float | None,
    ) -> None:
        """Record a stage attempt's pre-dispatch estimate (Story 14.1-002).

        Written *before* the agent runs so the estimate is visible alongside the
        actual usage the terminal :meth:`stage_set_usage` later records on the
        same row — the two columns together are the persisted estimate-vs-actual
        reconciliation. Best-effort estimation skips this when it cannot run, so
        old/un-estimated rows keep NULL and render as "—".
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE stages SET estimated_tokens = ?, estimated_cost_usd = ? "
                "WHERE run_id = ? AND story_id = ? AND stage_name = ? AND attempt = ?",
                (
                    estimated_tokens, estimated_cost_usd,
                    run_id, story_id, stage_name, attempt,
                ),
            )

    def historical_stage_tokens(
        self,
        stage_name: str,
        *,
        harness: str | None = None,
        model: str | None = None,
    ) -> tuple[float, str] | None:
        """Average recorded total tokens for ``stage_name``, segmented by harness+model.

        Story 14.1-002 calibrates the pre-dispatch estimate against the per-stage
        usage already in the ledger. Issue #427 makes it harness-aware so a Codex
        dispatch is not calibrated from predominantly-Claude history. Tries three
        progressively looser cohorts and returns the first with data, tagged with
        which tier matched:

          1. ``harness+model`` — same harness *and* model as the pending dispatch.
          2. ``harness``       — same harness, any model.
          3. ``any``           — every DONE attempt of the stage (the pre-#427 query).

        Each cohort averages the four token components over DONE attempts *that
        recorded usage* (all-NULL-token rows excluded so un-instrumented stages do
        not drag the mean toward zero). A tier whose ``harness``/``model`` filter is
        unknown (``None``) is skipped, so a caller that passes neither collapses to
        the ``any`` rung — today's behaviour. Pre-migration rows (NULL harness/model)
        never match the ``harness``/``harness+model`` filters, so they cannot pollute
        those cohorts; they still serve the ``any`` rung. Returns ``(avg, tier)``, or
        ``None`` when even the widest cohort has no data (caller falls back to the
        crude heuristic).
        """
        tiers: list[tuple[str, str, tuple[object, ...]]] = []
        if harness is not None and model is not None:
            tiers.append(
                ("harness+model", "harness = ? AND model = ?", (harness, model))
            )
        if harness is not None:
            tiers.append(("harness", "harness = ?", (harness,)))
        tiers.append(("any", "", ()))

        base = (
            "SELECT AVG("
            "  COALESCE(input_tokens,0) + COALESCE(output_tokens,0) + "
            "  COALESCE(cache_read_tokens,0) + COALESCE(cache_creation_tokens,0)"
            ") AS avg_tokens "
            "FROM stages WHERE stage_name = ? AND status = 'DONE' AND ("
            "  input_tokens IS NOT NULL OR output_tokens IS NOT NULL OR "
            "  cache_read_tokens IS NOT NULL OR cache_creation_tokens IS NOT NULL)"
        )
        with self._connect_ro() as conn:
            for tier, extra, params in tiers:
                sql = base + (f" AND {extra}" if extra else "")
                row = conn.execute(sql, (stage_name, *params)).fetchone()
                if row is not None and row["avg_tokens"] is not None:
                    return float(row["avg_tokens"]), tier
        return None

    def reset_story(self, run_id: str, story_id: str) -> None:
        """Roll a story back to a fresh, unbuilt state (Story 10.2-001).

        Deletes every stage attempt for the story and clears its branch, PR, and
        current stage, then sets its status to TODO so the next ``resume``/
        ``build`` rebuilds it from the first pipeline stage. The story row itself
        is preserved (title/epic/priority stay) so the queue is unaffected. Used
        only by ``sdlc rollback``, which guards merged stories before calling
        this.
        """
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM stages WHERE run_id = ? AND story_id = ?",
                (run_id, story_id),
            )
            conn.execute(
                "UPDATE stories SET status = 'TODO', pr_number = NULL, "
                "branch = NULL, current_stage = NULL "
                "WHERE run_id = ? AND story_id = ?",
                (run_id, story_id),
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

    def progress_log(
        self, run_id: str, story_id: str, stage: str, kind: str, message: str
    ) -> None:
        """Append a fine-grained sub-stage progress event (Story 11.1-002).

        Recorded at ``level = 'progress'`` / ``source = 'agent'`` with the
        sub-stage ``stage`` and a fixed ``kind`` (see :mod:`sdlc.progress`), so
        the dashboard and ``sdlc status`` can show what an agent is doing
        mid-stage. Kept out of the human ``recent_events`` audit list (which
        filters progress rows) so a high-volume stream never floods it.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events(run_id, story_id, level, source, message, "
                "stage, kind) VALUES (?, ?, 'progress', 'agent', ?, ?, ?)",
                (run_id or None, story_id or None, message, stage or None, kind or None),
            )

    # --- Story inventory (Epic-22) -----------------------------------------
    # The cross-backlog cache the MD-spec projector (Story 22.1-002) fills and
    # the host issue mirror + portfolio dashboard read. The projector owns the
    # *spec* columns (epic/feature/title/points/risk); the sync/build paths own
    # the *cache* columns (status/owner/host/issue_ref/harness), so the upsert
    # below deliberately refreshes only the spec columns and leaves the cache
    # columns untouched on conflict.

    def inventory_story_ids(self) -> set[str]:
        """Return the bare story ids already present in the inventory cache."""
        with self._connect() as conn:
            return {
                r[0] for r in conn.execute("SELECT story_id FROM story_inventory")
            }

    def inventory_upsert_specs(
        self, rows: Iterable[tuple[str, str, str, str, int | None, str | None]]
    ) -> None:
        """Upsert MD-projected spec rows in one transaction (Story 22.1-002).

        Each ``rows`` tuple is ``(story_id, epic, feature, title, points, risk)``
        — the spec projected from the MD. On conflict (the story already exists)
        only those spec columns are refreshed; ``status``/``owner``/``host``/
        ``issue_ref``/``harness`` are left as-is so a story already linked to an
        issue keeps its cached host fields (MD owns the spec, sync/build own the
        cache — one writer per field). Rows for stories no longer in the MD are
        never touched here; the projector reports them as removed (it does not
        silently drop them).
        """
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO story_inventory "
                "  (story_id, epic, feature, title, points, risk, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(story_id) DO UPDATE SET "
                "  epic = excluded.epic, "
                "  feature = excluded.feature, "
                "  title = excluded.title, "
                "  points = excluded.points, "
                "  risk = excluded.risk, "
                "  updated_at = CURRENT_TIMESTAMP",
                rows,
            )

    def inventory_get_mapping(self, story_id: str) -> tuple[str, str] | None:
        """Return ``(host, issue_ref)`` for a mapped story, or None when unmapped.

        Story 22.2-003: the story↔issue mapping lives in the inventory cache. A
        row exists for every projected story but is *unmapped* until the mirror
        records its host issue; both columns must be set for a mapping to count
        (a half-written row reads as unmapped, so the mirror recovers it via the
        body marker rather than trusting a dangling ref).
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT host, issue_ref FROM story_inventory WHERE story_id = ?",
                (story_id,),
            ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None
        return (row[0], row[1])

    def inventory_set_mapping(
        self, story_id: str, host: str | None, issue_ref: str | None
    ) -> None:
        """Record (or clear) a story's host issue mapping in the inventory cache.

        Story 22.2-003: the mirror calls this after creating/recovering an issue
        so a re-run updates that issue instead of duplicating it. Passing
        ``None``/``None`` clears the mapping (used to recover a story whose issue
        survives but whose local ref was lost). Spec columns are untouched — one
        writer per field. A no-op when the story row is absent (the projector
        owns row creation).
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE story_inventory "
                "SET host = ?, issue_ref = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE story_id = ?",
                (host, issue_ref, story_id),
            )

    # --- Reconcile cache columns (Story 22.4-001) --------------------------
    # The sync reconcile owns `status` (pushed *to* the host as a status label)
    # and the pulled human fields `owner`/`human_status` (read *from* the host).
    # Each is a single-column update so the field-directional contract is exact:
    # push touches managed fields only, pull touches human fields only — one
    # writer per field, no echo loop.

    def _inventory_get_column(self, story_id: str, column: str) -> str | None:
        """Read one inventory cache column for a story, or None when unset/absent.

        ``column`` is one of the fixed reconcile column names (never user input),
        so the f-string is safe — SQLite cannot parametrise a column name.
        """
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {column} FROM story_inventory WHERE story_id = ?",
                (story_id,),
            ).fetchone()
        return row[0] if row is not None else None

    def _inventory_set_column(self, story_id: str, column: str, value: str | None) -> None:
        """Write one inventory cache column for a story (a no-op when the row is absent)."""
        with self._connect() as conn:
            conn.execute(
                f"UPDATE story_inventory "
                f"SET {column} = ?, updated_at = CURRENT_TIMESTAMP "
                f"WHERE story_id = ?",
                (value, story_id),
            )

    def inventory_get_status(self, story_id: str) -> str | None:
        """The cached execution status the reconcile pushes as a status label."""
        return self._inventory_get_column(story_id, "status")

    def inventory_set_status(self, story_id: str, status: str | None) -> None:
        """Set the cached execution status (build/sync owned)."""
        self._inventory_set_column(story_id, "status", status)

    def build_done_story_ids(self) -> set[str]:
        """Story ids that completed a build run (any run reached ``DONE``).

        Story 22.6-001: `sdlc issues init` adopting an already-built repo seeds the
        inventory `status` from this so the portfolio reflects shipped work instead
        of a blanket TODO. A story counts as done if *any* run completed it — the
        `stories` table is keyed per-run, so it can also hold earlier non-DONE rows.
        """
        with self._connect() as conn:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT story_id FROM stories WHERE status = 'DONE'"
                )
            }

    def inventory_get_owner(self, story_id: str) -> str | None:
        """The cached host assignee pulled by the reconcile."""
        return self._inventory_get_column(story_id, "owner")

    def inventory_set_owner(self, story_id: str, owner: str | None) -> None:
        """Set the cached owner (the pull side writes this from the host assignee)."""
        self._inventory_set_column(story_id, "owner", owner)

    def inventory_get_human_status(self, story_id: str) -> str | None:
        """The pulled human signal (`blocked`/`wontfix`), or None."""
        return self._inventory_get_column(story_id, "human_status")

    def inventory_set_human_status(self, story_id: str, human_status: str | None) -> None:
        """Set the pulled human signal (the pull side writes this from host labels)."""
        self._inventory_set_column(story_id, "human_status", human_status)

    def inventory_wontfix_story_ids(self) -> set[str]:
        """Story ids the host has flagged ``wontfix`` — the build skips these.

        Story 22.4-001: the reconcile pulls a `wontfix` label into
        `human_status`; the build consults this set so it never works a story a
        human has explicitly declined.
        """
        with self._connect() as conn:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT story_id FROM story_inventory WHERE human_status = 'wontfix'"
                )
            }

    def inventory_stories_for_epic(self, epic: str) -> list[str]:
        """Return every story id in ``epic`` from the inventory, sorted.

        Story 22.5-002: the epic-cascade enumerates an epic's stories from the
        inventory (projected by Story 22.1-002). Ordered by story id so a cascade
        is deterministic and a resumed pass covers stories in a stable sequence.
        """
        with self._connect() as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT story_id FROM story_inventory WHERE epic = ? "
                    "ORDER BY story_id",
                    (epic,),
                )
            ]

    def inventory_rows(self) -> list[dict]:
        """Every ``story_inventory`` row, read-only, for the portfolio panel.

        Story 22.6-001: the dashboard portfolio renders offline from this local
        cache (no host call). Read-only with the busy-timeout connection so a
        dashboard poll issued while the controller is writing waits out the lock
        instead of erroring. Returns ``[]`` when the DB is absent or predates the
        inventory migration (a read-only viewer never migrates), so the panel
        shows its empty state rather than raising. Ordered by story id for a
        deterministic render.
        """
        if not self.db_path.exists():
            return []
        with self._connect_ro() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "story_inventory" not in tables:
                return []
            rows = conn.execute(
                "SELECT story_id, epic, feature, title, points, risk, status, "
                "owner, human_status, host, issue_ref, harness "
                "FROM story_inventory ORDER BY story_id"
            ).fetchall()
        return [dict(r) for r in rows]

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
        """The most recent resumable run id (optionally for ``scope``), or None.

        A clean build close-out stamps a terminal status, so a run still marked
        ``IN_PROGRESS`` is one that was interrupted before finishing — exactly
        what ``sdlc resume`` recovers. Story 14.1-003 adds ``RATE_LIMITED``: a run
        durably parked because the Max plan's window reset is beyond the auto-wait
        cap is *also* resumable (it is waiting for time, not done), so `sdlc
        resume` continues it once the window reopens. ``scope`` ``None``/``all``
        matches any scope; a specific scope (``epic-99``, a story id) filters to
        that run.
        """
        if not self.db_path.exists():
            return None
        # Story 19.1-001: a composite scope is stored as a canonical (sorted,
        # deduped) label, so canonicalise the lookup key too — `epic-18 epic-15`
        # and `epic-15,epic-18` then exact-match the same run, order-independent.
        if scope is not None:
            scope = canonical_scope(scope)
        with self._connect_ro() as conn:
            if scope and scope.lower() != "all":
                row = conn.execute(
                    "SELECT id FROM runs WHERE status IN ('IN_PROGRESS', 'RATE_LIMITED') "
                    "AND scope = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                    (scope,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM runs WHERE status IN ('IN_PROGRESS', 'RATE_LIMITED') "
                    "ORDER BY started_at DESC, rowid DESC LIMIT 1"
                ).fetchone()
        return row["id"] if row else None

    def state_rows(self, run_id: str) -> list[dict]:
        """Every persisted stage-machine row for ``run_id`` for `sdlc state`.

        Each row is ``{story_id, stage_name, status, attempt, branch,
        pr_number, harness}`` in a stable, chronological order (by story, then
        start time) — a greppable dump of the state machine for debugging.
        ``harness`` (Story 20.2-002) is the harness that ran the stage; a
        pre-migration ledger (no ``harness`` column) and old NULL rows both read
        as the built-in default ``claude``.
        """
        if not self.db_path.exists():
            return []
        with self._connect_ro() as conn:
            # Tolerate a ledger created before the harness column existed
            # (read-only viewers never migrate): select it only when present.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(stages)").fetchall()}
            harness_sel = (
                "COALESCE(st.harness, ?)" if "harness" in cols else "?"
            )
            rows = conn.execute(
                f"""
                SELECT st.story_id, st.stage_name, st.status, st.attempt,
                       s.branch, s.pr_number, {harness_sel} AS harness
                FROM stages st
                JOIN stories s
                  ON st.run_id = s.run_id AND st.story_id = s.story_id
                WHERE st.run_id = ?
                ORDER BY st.story_id, st.started_at, st.rowid
                """,
                (DEFAULT_HARNESS, run_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def run_row(self, run_id: str) -> dict | None:
        """The `runs` row for ``run_id`` as a dict, or None when absent."""
        if not self.db_path.exists():
            return None
        with self._connect_ro() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def run_usage_totals(self, run_id: str) -> dict:
        """Running token + notional-cost accrual for ``run_id`` (Story 14.1-001).

        Sums the four per-stage token components and the notional ``cost_usd``
        across every recorded stage attempt for the run — the live accrual the
        budget gate reads between stories. Returns ``{"tokens": int,
        "cost_usd": float}``; both are 0 when no usage has been recorded yet, the
        ledger predates token capture, or the DB is absent, so the gate degrades
        to "no spend seen" rather than crashing.
        """
        zero = {"tokens": 0, "cost_usd": 0.0}
        if not self.db_path.exists():
            return zero
        with self._connect_ro() as conn:
            stage_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(stages)").fetchall()
            }
            if "input_tokens" not in stage_cols:
                return zero
            row = conn.execute(
                "SELECT "
                "SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)"
                "+COALESCE(cache_read_tokens,0)+COALESCE(cache_creation_tokens,0)) AS tok, "
                "SUM(COALESCE(cost_usd,0)) AS cost "
                "FROM stages WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return {
            "tokens": int(row["tok"] or 0),
            "cost_usd": float(row["cost"] or 0.0),
        }

    def story_rows(self, run_id: str) -> list[dict]:
        """Per-story progress for ``run_id``, newest-stage first.

        ``current_stage`` / ``stage_status`` are derived from the ``stages``
        table (the controller never populates ``stories.current_stage``): the
        latest stage attempt by start time wins.
        """
        if not self.db_path.exists():
            return []
        with self._connect_ro() as conn:
            # Story 11.2-007: the dashboard reads read-only and never migrates, so
            # an unmigrated ledger may lack the wave/dependencies columns. Select
            # them only when present and degrade to NULL otherwise — mirroring the
            # column guard stage_breakdown uses for the usage columns.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(stories)").fetchall()}
            wave_sel = "s.wave" if "wave" in cols else "NULL AS wave"
            deps_sel = (
                "s.dependencies" if "dependencies" in cols else "NULL AS dependencies"
            )
            rows = conn.execute(
                f"""
                SELECT
                    s.story_id, s.title, s.priority, s.status, s.pr_number,
                    {wave_sel}, {deps_sel},
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
        # `dependencies` is stored as a JSON array; an un-recorded story (older
        # ledger, `--sequential` schedule, or a done-skip outside the cohorts)
        # reads back as an empty list / NULL wave.
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            raw_deps = d.get("dependencies")
            d["dependencies"] = json.loads(raw_deps) if raw_deps else []
            out.append(d)
        return out

    def recent_events(self, run_id: str, limit: int = 10) -> list[dict]:
        """The last ``limit`` human audit events for ``run_id``, oldest-first.

        The internal ``config`` marker event (run options) and the high-volume
        ``progress`` sub-stage events (Story 11.1-002, surfaced separately via
        :meth:`latest_progress`) are excluded so neither clutters or floods the
        human-facing event log.
        """
        if not self.db_path.exists():
            return []
        with self._connect_ro() as conn:
            rows = conn.execute(
                "SELECT ts, level, source, story_id, message FROM events "
                "WHERE run_id = ? AND level != 'progress' "
                "AND (source IS NULL OR source != 'config') "
                "ORDER BY id DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def latest_progress(self, run_id: str) -> dict[str, dict]:
        """The most recent sub-stage progress event per story for ``run_id``.

        Returns ``{story_id: {stage, kind, message, ts}}`` — the single newest
        ``progress`` event for each story, which is what ``sdlc status`` and the
        dashboard render as current sub-stage activity. Empty when the run has no
        progress events (older runs / captured-mode fallback). The grouped
        ``MAX(id)`` makes SQLite return the bare columns from the latest row.
        """
        if not self.db_path.exists():
            return {}
        with self._connect_ro() as conn:
            # Tolerate a ledger created before the progress columns existed
            # (read-only viewers never migrate): no stage/kind column means no
            # progress events were ever recorded, so report none.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
            if "stage" not in cols or "kind" not in cols:
                return {}
            rows = conn.execute(
                "SELECT story_id, stage, kind, message, ts, MAX(id) AS id "
                "FROM events WHERE run_id = ? AND level = 'progress' "
                "AND story_id IS NOT NULL GROUP BY story_id",
                (run_id,),
            ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            out[r["story_id"]] = {
                "stage": r["stage"],
                "kind": r["kind"],
                "message": r["message"],
                "ts": r["ts"],
            }
        return out

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
            # Story 20.2-002: surface the per-stage harness when present; a
            # pre-migration ledger and old NULL rows read as the default claude.
            harness_sel = (
                f", COALESCE(harness, '{DEFAULT_HARNESS}') AS harness"
                if "harness" in cols else f", '{DEFAULT_HARNESS}' AS harness"
            )
            rows = conn.execute(
                "SELECT story_id, stage_name AS name, attempt, status, started_at, "
                "finished_at, failure_category, output_path" + usage_sel + harness_sel +
                " FROM stages WHERE run_id = ? ORDER BY story_id, started_at, rowid",
                (run_id,),
            ).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            d = dict(r)
            d["tokens"] = _sum_tokens(d)
            out.setdefault(d.pop("story_id"), []).append(d)
        return out

    def change_token(self) -> str:
        """An opaque token that changes whenever a dashboard-visible field does.

        Used by the live SSE transport (Story 11.2-003) to decide when to push a
        delta. ``MAX(events.id)`` alone is not enough: the dashboard also renders
        per-stage status/usage and per-story status/PR, and those are written by
        in-place ``UPDATE``\\ s (``stage_finish``, ``stage_set_usage``,
        ``set_story_status``, ``set_story_pr``) that emit no event row — and the
        ledger runs in WAL mode, so the file's mtime is no proxy either. So we
        digest every mutable field the dashboard shows across ``runs``/
        ``stories``/``stages`` plus the event high-water mark. Returns ``"0"``
        for a missing/unreadable ledger. The row counts here are small (tens of
        rows per run), so this stays cheap enough to poll sub-second.
        """
        if not self.db_path.exists():
            return "0"
        try:
            with self._connect_ro() as conn:
                ev = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
                runs = conn.execute(
                    "SELECT id, status, total_stories, completed, failed, "
                    "started_at, finished_at FROM runs ORDER BY id"
                ).fetchall()
                stories = conn.execute(
                    "SELECT run_id, story_id, status, pr_number, current_stage "
                    "FROM stories ORDER BY run_id, story_id"
                ).fetchall()
                stages = conn.execute(
                    "SELECT run_id, story_id, stage_name, attempt, status, "
                    "failure_category, output_path, session_id, input_tokens, "
                    "output_tokens, cache_read_tokens, cache_creation_tokens, "
                    "cost_usd, started_at, finished_at FROM stages "
                    "ORDER BY run_id, story_id, stage_name, attempt"
                ).fetchall()
        except sqlite3.Error:
            return "0"
        # Rows come back as ``sqlite3.Row`` (set on the read-only connection),
        # whose ``repr`` embeds the object's memory address and so differs every
        # call — digest the plain tuple values instead, which depend only on the
        # data, so an unchanged ledger yields a stable token.
        payload = (ev, [tuple(r) for r in runs], [tuple(r) for r in stories],
                   [tuple(r) for r in stages])
        digest = hashlib.blake2b(digest_size=16)
        digest.update(repr(payload).encode("utf-8"))
        return digest.hexdigest()

    def list_runs(self, limit: int = 50) -> list[dict]:
        """The most recent ``limit`` runs (newest first) for the runs browser.

        Each entry is ``{id, scope, mode, status, started_at, finished_at,
        duration_seconds, total, done, failed, total_tokens, total_cost_usd}``.
        ``duration_seconds`` is the run's total span (elapsed-so-far while
        in-progress), or None when the start is missing (renders "—"). The
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
                    "duration_seconds": _duration_seconds(
                        r["started_at"], r["finished_at"]
                    ),
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
    "awaiting_approval": 0,
}

# The four per-stage token components recorded from the agent usage envelope.
_TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
)


# --- durations (Story 11.2-005) --------------------------------------------
# Durations are derived from the ledger timestamps already persisted (no new
# schema). The dashboard renders these with a shared human-readable formatter;
# in-progress spans use a "now" anchor so the value reads as elapsed-so-far.


def _parse_ts(value) -> datetime | None:
    """Parse a ledger timestamp into an aware UTC datetime, or None.

    Tolerates the SQLite ``CURRENT_TIMESTAMP`` shape (``"YYYY-MM-DD HH:MM:SS"``,
    space-separated) and ISO-8601 (``"…T…"``); both are UTC. A naive value is
    assumed UTC. Returns None for null/empty/garbage so callers degrade to "—".
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _duration_seconds(started_at, finished_at, now: datetime | None = None) -> int | None:
    """Whole-second span from ``started_at`` to ``finished_at`` (or ``now``).

    Returns None when the start is missing/unparseable (renders "—"), uses
    ``now`` (default: current UTC time) as the end for an in-progress span
    (``finished_at`` null), and degrades a negative span (clock skew / bad
    data) to None so the dashboard never shows a negative duration.
    """
    start = _parse_ts(started_at)
    if start is None:
        return None
    end = _parse_ts(finished_at) if finished_at else (now or datetime.now(timezone.utc))
    if end is None:
        return None
    secs = (end - start).total_seconds()
    return int(secs) if secs >= 0 else None


def _story_duration_seconds(stage_attempts: list[dict], now: datetime | None = None) -> int | None:
    """Wall-clock span of a story: earliest stage start → latest stage finish.

    The span (not the sum of stage durations) reflects real elapsed time
    including gaps between stages. An in-flight story (any started stage with no
    ``finished_at``) shows elapsed-so-far against ``now``. Returns None when no
    stage has started yet (renders "—").
    """
    starts = [_parse_ts(a.get("started_at")) for a in stage_attempts]
    starts = [s for s in starts if s is not None]
    if not starts:
        return None
    earliest = min(starts)
    in_flight = any(a.get("started_at") and not a.get("finished_at") for a in stage_attempts)
    if in_flight:
        end = now or datetime.now(timezone.utc)
    else:
        ends = [_parse_ts(a.get("finished_at")) for a in stage_attempts]
        ends = [e for e in ends if e is not None]
        if not ends:
            return None
        end = max(ends)
    secs = (end - earliest).total_seconds()
    return int(secs) if secs >= 0 else None


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


def _effective_worker_limit(mode: str | None, config: dict) -> int:
    """The worker cap a persisted run actually dispatched through (Story 17.3-001).

    A ``serial`` run is one worker by definition. A ``parallel`` run honours the
    persisted ``concurrency`` config, floored at 1; when that figure is missing
    (an older ledger) it falls back to the build default so the snapshot still
    reports a sensible cap rather than zero. Mirrors :func:`effective_concurrency`
    on the read side, where only the persisted run row and config are available.
    """
    if mode == "serial":
        return 1
    raw = config.get("concurrency")
    if isinstance(raw, (int, float, str)):
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return max(1, BuildOptions.concurrency)


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
    activity = ledger.latest_progress(rid)
    # One "now" anchor so the run's elapsed and every in-flight story's
    # elapsed-so-far are measured against the same instant in this snapshot.
    now = datetime.now(timezone.utc)

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
        # A story skipped wholesale (already Done in a prior run) writes no stage
        # rows, so its cells would otherwise render PENDING; surface them SKIPPED.
        story_skipped = s.get("status") == "SKIPPED"
        for name in _STAGES:
            row = latest.get(name)
            if row is not None:
                pipeline.append(row)
            elif name == "coverage" and skip_coverage:
                pipeline.append({"name": name, "status": "SKIPPED"})
            elif story_skipped:
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
        # Current sub-stage activity (Story 11.1-002): the latest progress event
        # for the story, or None for runs with no streamed progress (captured
        # fallback / older runs) so consumers degrade to the stage name.
        s["activity"] = activity.get(s["story_id"])
        # Per-story wall-clock duration (Story 11.2-005): earliest stage start →
        # latest stage finish, elapsed-so-far while in flight. From the same
        # attempts above, before PENDING/SKIPPED placeholders were appended.
        s["duration_seconds"] = _story_duration_seconds(attempts, now=now)

    # Run-level usage rollup across every stage attempt of every story.
    run_usage = _aggregate_run_usage(breakdown)

    # Story 17.3-001: surface the run's real concurrency so tooling reflects
    # that several stories run at once, not just one. ``limit`` is the worker
    # cap the executor uses (1 for a serial run); ``active`` is how many of
    # those workers are busy right now. Consumers (e.g. the Epic-11 dashboard)
    # render "active of limit workers busy" — this side produces only the truth.
    concurrency = {
        "limit": _effective_worker_limit(run_row.get("mode"), config),
        "active": _count("IN_PROGRESS"),
    }

    payload["run"] = {
        "id": rid,
        "scope": run_row.get("scope"),
        "mode": run_row.get("mode"),
        "status": run_row.get("status"),
        "started_at": run_row.get("started_at"),
        "finished_at": run_row.get("finished_at"),
        "duration_seconds": _duration_seconds(
            run_row.get("started_at"), run_row.get("finished_at"), now=now
        ),
        "config": config,
        "concurrency": concurrency,
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
        "awaiting_approval": _count("AWAITING_APPROVAL"),
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


def _resolve_dispatch(
    dispatcher: "Dispatcher | None",
    opts: BuildOptions,
    default: Callable[..., AgentResult] = dispatch_agent,
):
    """The dispatch seam for a run, with the thinking-token cap bound (14.2-002).

    Tests inject a fake ``dispatcher`` and own its signature, so the cap is bound
    only onto the real default seam (``dispatcher is None``) — an injected fake is
    returned untouched. With no cap configured the real seam is returned
    unchanged, so the no-cap path is byte-for-byte today's. The cap is a per-run
    constant, so binding it here once reaches every stage's dispatch
    (build/coverage/review/merge/bugfix/reask) without threading it through each
    call site.

    ``default`` is the real dispatcher to fall back to; each caller passes its
    *own* module-level ``dispatch_agent`` so a test that monkeypatches that
    module's symbol (e.g. ``sdlc.resume.dispatch_agent``) still routes through its
    fake.
    """
    dispatch = dispatcher or default
    if dispatcher is None and opts.thinking_cap:
        dispatch = functools.partial(dispatch, thinking_cap=opts.thinking_cap)
    # Story 13.4-002: bind the container-sandbox flag onto the real seam so every
    # stage's dispatch (build/coverage/review/merge/bugfix/reask) runs inside the
    # no-egress container. Only the real default seam is wrapped (an injected fake
    # owns its own signature); off → the seam is returned unchanged (host path).
    if dispatcher is None and opts.sandbox:
        dispatch = functools.partial(dispatch, sandbox=True)
    return dispatch


@dataclass
class BuildResult:
    """The terminal outcome of a build run."""

    completed: int = 0
    failed: int = 0
    skipped: int = 0
    blocked: int = 0
    needs_attention: int = 0
    # Story 12.3-003: stories parked waiting on FX's high-risk merge approval —
    # a non-FAILED, non-DONE bucket distinct from NEEDS_ATTENTION.
    awaiting_approval: int = 0
    planned: int = 0
    dry_run: bool = False
    preflight_failed: bool = False
    # Story 12.1-002: set when a real run was short-circuited by the recursion
    # guard (the SDLC_IN_TEST sentinel was set), so the caller can report it and
    # exit cleanly instead of launching preflight/orchestration.
    skipped_in_test: bool = False
    run_id: str | None = None
    story_status: dict[str, str] = field(default_factory=dict)
    # Story 14.1-001: set when the per-run token budget gate halted dispatch.
    # ``budget_policy`` is the honoured policy (``pause``/``abort``);
    # ``accrued_tokens``/``notional_cost_usd`` are the accrual at the stop (the
    # dollar figure is notional API-equivalent, never real subscription spend).
    budget_stopped: bool = False
    budget_policy: str = ""
    accrued_tokens: int = 0
    notional_cost_usd: float = 0.0
    # Story 14.1-003: set when the run was durably parked because the Max plan's
    # rate-limit / quota window reset is beyond the in-process auto-wait cap. The
    # run is left RATE_LIMITED (resumable, not terminal); ``rate_limit_reset_at``
    # is the approximate epoch the window reopens and ``rate_limit_waited_s`` is
    # the total time auto-waited in-process before parking (0 if it parked
    # immediately). ``rate_limited`` stays False on the auto-wait-and-resume path.
    rate_limited: bool = False
    rate_limit_reset_at: float | None = None
    rate_limit_waited_s: int = 0
    # Story 14.1-002: set when the interactive pre-dispatch cost gate paused the
    # run (an estimate crossed ``--cost-threshold`` and ``--auto`` was off). The
    # run is left IN_PROGRESS (resumable, not terminal); raise ``--cost-threshold``
    # on ``sdlc resume`` to continue the gated stage.
    cost_gated: bool = False


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
        if _has_pytest_timeout(root):
            # Bound each test so a hanging agent-added test fails fast with a
            # clear pytest-timeout message rather than stalling until preflight's
            # whole-suite timeout (Story 12.1-002). thread method works without
            # signals so it is safe under xdist workers.
            cmd += [f"--timeout={PER_TEST_TIMEOUT}", "--timeout-method=thread"]
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


def _has_pytest_timeout(root: Path) -> bool:
    """True when pytest-timeout appears in the project's deps/lock (Story 12.1-002)."""
    for name in ("pyproject.toml", "uv.lock", "requirements.txt"):
        path = root / name
        if path.is_file() and "pytest-timeout" in path.read_text(encoding="utf-8"):
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
    # Export the recursion-guard sentinel into the child only (a copy of the
    # parent env), so a project test that invokes the controller's `build`/
    # `dashboard` verbs short-circuits instead of recursing into orchestration
    # (Story 12.1-002). The parent process env is left untouched.
    env = {**os.environ, IN_TEST_ENV_VAR: "1"}
    try:
        completed = subprocess.run(cmd, cwd=root, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        print(
            f"PRE_FLIGHT_TIMEOUT: '{' '.join(cmd)}' exceeded {timeout}s — aborting. "
            "Raise --preflight-timeout=N or bypass with --skip-preflight.",
            file=sys.stderr,
        )
        return False
    return completed.returncode == 0


def _log_harness_preflight(
    ledger: "Ledger", run_id: str, requested_mode: str, opts: "BuildOptions"
) -> None:
    """Resolve and log the dispatch harness's capabilities (Story 20.5-001).

    Resolves the default-slot harness, decides the safe run mode for
    ``requested_mode``, and records each capability/probe/degradation line to
    stderr and the ledger event log. A degradation downgrades the event level to
    ``warn`` so a capability gap is never silent. Best-effort: a logging failure
    must never fail an otherwise-good build.

    Issue #426 (UX): the default-slot preflight always resolves the built-in
    ``claude`` harness — it never consulted ``opts.harness_map`` — so a fully
    per-role-routed Codex run still logged an unlabeled ``harness 'claude': ...``
    line, making a successful Codex run look like it used Claude. When
    ``opts.harness_map`` is set (``--harness role=harness,...``), this now emits
    a ``harness routing: build=... coverage=... review=... merge=... docs=...``
    line first showing the *effective* per-role map, and the default-slot lines
    below it are labeled ``(default slot)`` so they are never confused with the
    harness actually dispatching a stage's work.
    """
    try:
        if opts.harness_map:
            # Local import mirrors the existing pattern elsewhere in this module
            # (e.g. the `--harness` CLI parsing above) — avoids a module-load
            # import cycle between build.py and role_routing.py.
            from sdlc.role_routing import PIPELINE_ROLES

            routing = " ".join(
                f"{role}={opts.harness_map.get(role, DEFAULT_HARNESS)}"
                for role in PIPELINE_ROLES
            )
            line = f"harness routing: {routing}"
            print(line, file=sys.stderr)
            ledger.event_log(run_id, "", "info", "harness", line)

        harness = resolve_harness()
        preflight = preflight_harness(harness, requested_mode=requested_mode)
        level = "warn" if preflight.degraded else "info"
        for line in preflight.log_lines(label="default slot"):
            print(line, file=sys.stderr)
            ledger.event_log(run_id, "", level, "harness", line)
    except Exception:
        pass


def _record_degradations(ledger: "Ledger", run_id: str, requested_mode: str) -> None:
    """Record the dispatch harness's degradation plan in the ledger (Story 20.5-002).

    Resolves the default-slot harness, evaluates the centralized degradation
    matrix for ``requested_mode``, and writes one ``warn`` event per applied
    fallback (parallel→serial, usage "unavailable", rate-limit backoff skipped) to
    the ``degradation`` event source so any capability gap is auditable in the run
    summary (AC3). For the built-in Claude harness the plan is empty, so this is
    purely additive and writes nothing. Best-effort: a logging failure must never
    fail an otherwise-good build.
    """
    try:
        harness = resolve_harness()
        capabilities = resolve_capabilities(harness)
        plan = evaluate_degradations(
            harness.name, capabilities, requested_mode=requested_mode
        )
        for record in plan.to_records():
            print(record["message"], file=sys.stderr)
            ledger.event_log(run_id, "", "warn", "degradation", record["message"])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Prompt rendering (kept terse — the agent reads the epic file itself)
# ---------------------------------------------------------------------------

def render_build_prompt(
    story: Story,
    opts: BuildOptions,
    close_link: str | None = None,
    *,
    cr_terms: ChangeRequestTerms = GITHUB_CR_TERMS,
    base_ref: str = "origin/main",
) -> str:
    """Render the build-agent instructions for one story.

    Deliberately mirrors the skill's build-agent prompt: create the branch, read
    the epic, TDD, quality gates, commit, and emit the result block the
    controller validates.

    ``close_link`` (Story 22.4-002) is the ``Closes #N`` keyword for the story's
    mapped issue. It is only appended when *this* agent opens the change request
    (the ``--skip-coverage`` path); otherwise the coverage agent opens it and
    carries the close-link instead.

    ``cr_terms`` (Story 23.2-001) carries the host-correct change-request phrasing
    so a GitLab target opens a *Merge Request* (`glab mr create`) instead of a PR;
    ``base_ref`` is the default branch ``feature/<id>`` is cut from and the change
    request targets. Both default to GitHub's wording, so the GitHub path is
    byte-identical to today (AC2).
    """
    # Only inject the close-link when the build agent itself opens the change request.
    close_hint = (
        _close_link_instruction(close_link, cr_terms=cr_terms)
        if opts.skip_coverage
        else ""
    )
    push = (
        f"6. Push and create {cr_terms.abbr}{cr_terms.cli_hint}; "
        f"include the {cr_terms.ref_noun} in the result block."
        if opts.skip_coverage
        else f"6. Commit locally; the coverage agent pushes and opens the {cr_terms.abbr}."
    )
    # Story 18.3-001: keep user-facing docs current with each story. When the
    # documentation-currency lens is enabled (the default), instruct the build
    # agent to update the affected docs in the SAME commit as the code change —
    # scoped to what the diff actually touches, never the CHANGELOG (Epic-05's
    # release workflow owns that). Disabling the lens reverts to today's prompt.
    docs_instruction = (
        "If this story changes user-facing behavior (a CLI verb/flag, a skill, a "
        "hook, a documented config key, or an installer step), update the affected "
        "user-facing docs (README.md, docs/, usage/help text) in the SAME commit as "
        "step 5 — scoped to what the change actually affects; do not write "
        "speculative docs and do NOT touch the CHANGELOG (the release workflow owns "
        "it).\n\n"
        if doc_currency_enabled()
        else ""
    )
    # Story 12.2-004: derive a commitlint-compliant subject by construction
    # rather than asking the agent to transcribe the (often long, Title-Case)
    # story title verbatim — which fails ``header-max-length``/``subject-case``
    # and forces a re-ask. The ``(#id)`` tag reconciliation keys off is always
    # preserved, and the subject is trimmed to the header budget.
    commit_header = build_commit_header(
        ctype="feat",
        scope=story.epic_name,
        subject=story.title,
        trailer=f" (#{story.id})",
    )
    return (
        f"You are building story {story.id}: {story.title}\n"
        f"Epic: {story.epic_name} (from {story.epic_file})\n"
        f"Priority: {story.priority}\n\n"
        "## Instructions\n"
        # Story 12.4-001: cut the branch from a freshly-fetched base ref
        # (``base_ref`` — the host default branch, Story 23.2-001 AC3; ``origin/main``
        # by default), not from whatever HEAD happens to be checked out. A base-less
        # ``git checkout -b feature/<id>`` lets the branch stack on a previous
        # story's leftover feature branch, so a later successful merge can
        # transitively land the earlier (parked) story's commits on the base.
        f"1. Create branch: git fetch origin && git checkout -b feature/{story.id} {base_ref}\n"
        # Issue #214: if the branch cannot be created (it already exists, a worktree
        # conflict, etc.) the agent must NOT fall back to committing story work on the
        # currently checked-out branch (typically main). Fail the build immediately.
        f"   If branch creation fails for any reason, emit BUILD_STATUS: FAILED "
        "immediately and do not commit on the current branch or any other branch.\n"
        f"2. Read {story.epic_file} and find the full story section for {story.id}\n"
        "3. Follow TDD: write failing tests first, then implement\n"
        "4. Run all quality gates (tests, types, lint, security)\n"
        "5. Commit with this exact, conventional-commit-compliant message — do "
        f"not alter it:\n   {commit_header}\n"
        f"{push}\n\n"
        + close_hint
        + docs_instruction
        + _result_wrapper("build-agent-response.schema.json")
    )


def _close_link_instruction(
    close_link: str | None,
    *,
    cr_terms: ChangeRequestTerms = GITHUB_CR_TERMS,
) -> str:
    """The close-link instruction for a change-request-opening stage (22.4-002).

    ``close_link`` is the ``Closes #N`` keyword for the story's mapped issue;
    empty when the story has no issue (then this is the empty string and the
    prompt is byte-for-byte unchanged). When set, the agent is told to include it
    in the change request's description so merging it auto-closes the tracking
    issue. ``cr_terms`` (Story 23.2-001) picks the host noun (PR/MR); GitHub is
    the default, so its phrasing is unchanged.
    """
    if not close_link:
        return ""
    return (
        f'When you open the {cr_terms.abbr}, include "{close_link}" on its own line '
        f"in the {cr_terms.abbr} description so merging the {cr_terms.abbr} "
        "auto-closes the story's tracking issue.\n"
    )


def render_coverage_prompt(
    story: Story,
    opts: BuildOptions,
    close_link: str | None = None,
    *,
    cr_terms: ChangeRequestTerms = GITHUB_CR_TERMS,
) -> str:
    # Story 12.2-004: the coverage agent authors a commit too (it is linted via
    # the build/coverage success gate), so supply a commitlint-compliant header
    # by construction rather than letting it improvise a non-compliant one.
    commit_header = build_commit_header(
        ctype="test",
        scope=story.epic_name,
        subject=story.title,
        trailer=f" (#{story.id})",
    )
    # Story 22.4-002: the coverage agent opens the change request on the default
    # path, so the close-link rides here; empty when the story has no mapped issue.
    # Story 23.2-001: ``cr_terms`` picks the host noun/CLI (PR via gh / MR via glab).
    return (
        f"Coverage gate for story {story.id}: {story.title}.\n"
        f"Branch: feature/{story.id}. Threshold: {opts.coverage_threshold}%.\n"
        "Fetch the branch, fill coverage gaps, then commit with this exact, "
        "conventional-commit-compliant message — do not alter it:\n"
        f"   {commit_header}\n"
        f"Push, open the {cr_terms.abbr}{cr_terms.cli_hint}, then emit the result block.\n"
        + _close_link_instruction(close_link, cr_terms=cr_terms)
        + _result_wrapper("coverage-agent-response.schema.json")
    )


def render_review_prompt(story: Story, pr_number: int | None) -> str:
    # Story 18.3-001: documentation-currency review dimension. When enabled (the
    # default), the reviewer also checks that a behavior-changing diff shipped its
    # doc update; gaps are flagged as advisory findings (never blocks shipping).
    # Disabling the lens reverts to today's review prompt.
    docs_dimension = (
        "Also apply the documentation-currency dimension: if the diff changes "
        "user-facing behavior (a CLI verb/flag, a skill, a hook, a documented "
        "config key, or an installer step) but ships no matching doc update, flag "
        "the stale doc as an advisory finding (name the doc + a one-line why) — "
        "advisory only, it does not block shipping. Stay quiet on "
        "docs-only/behavior-neutral diffs, and never flag the CHANGELOG.\n"
        if doc_currency_enabled()
        else ""
    )
    # Story 26.2-002: the PR description, commit messages, and any implementer
    # summary are self-reports — the reviewer must verify them against the diff
    # rather than accept them (pattern: superpowers task-reviewer-prompt).
    return (
        f"Review the PR for story {story.id}: {story.title} (PR #{pr_number}).\n"
        "Check architecture, security, performance, coverage, code quality; "
        "approve when satisfied, then emit the result block.\n"
        "Do not trust the implementer's report: the PR description, commit "
        "messages, and any summary are unverified claims — including design "
        'rationales like "kept it simple per YAGNI" — until you have checked '
        "each claim against the diff itself.\n"
        "Inspect code outside the diff only for a concrete named risk; when "
        "you do, name both the risk and what you checked in your review "
        "summary.\n"
        + docs_dimension
        + _result_wrapper("review-agent-response.schema.json")
    )


def render_merge_prompt(
    story: Story,
    pr_number: int | None,
    *,
    cr_terms: ChangeRequestTerms = GITHUB_CR_TERMS,
) -> str:
    # Story 23.2-003: the merge stage is the last change-request prompt that was
    # GitHub-coupled. ``cr_terms`` picks the host noun (PR via gh / MR via glab)
    # and the merge-CLI hint so a GitLab build merges the *MR* via ``glab mr
    # merge``; the ``Closes #N`` injected at create time (Story 22.4-002) then
    # auto-closes the story's issue via the Epic-22 mapping on either host. The
    # GitHub default leaves ``abbr="PR"`` and an empty ``merge_cli_hint`` so this
    # prompt is byte-identical to today on the GitHub path.
    abbr = cr_terms.abbr
    return (
        f"Merge the {abbr}{cr_terms.merge_cli_hint} for story {story.id}: "
        f"{story.title} ({abbr} #{pr_number}).\n"
        "Rebase before merge to absorb baseline drift, then emit the result block.\n"
        # Story 12.3-003: surface a high-risk human-approval block additively so
        # the controller parks AWAITING_APPROVAL instead of entering the bugfix
        # loop (which cannot self-approve). The instruction below is part of the
        # agent-facing prompt string, not a comment.
        f"If the {abbr} is blocked only by the high-risk approval gate (it carries "
        "the `risk:high` label with no `risk-approved` label and no "
        "`risk-approver` review), do NOT force-merge or override the gate: "
        'report merge_status="FAILED" and set the extra field "block_reason" to '
        '"BLOCKED_HIGH_RISK" so the run is parked awaiting human approval.\n'
        + _result_wrapper("merge-agent-response.schema.json")
    )


# ---------------------------------------------------------------------------
# Story 23.2-002: gate the merge on the change request's CI/pipeline status
# ---------------------------------------------------------------------------

# The gate's three verdicts. ``pass`` lets the merge stage dispatch; ``block``
# routes the story into the bugfix loop (a red/timed-out pipeline is a fixable
# failure, never a silent merge); ``skip`` means there is nothing host-side to
# gate on (no mapping / no CR ref / unresolvable status) so the merge proceeds
# exactly as today — the unmapped path stays byte-identical.
_GATE_PASS = "pass"
_GATE_BLOCK = "block"
_GATE_SKIP = "skip"


@dataclass(frozen=True)
class _MergeCIGate:
    """The outcome of polling a change request's CI status before merge (Story 23.2-002)."""

    verdict: str  # _GATE_PASS | _GATE_BLOCK | _GATE_SKIP
    status: str | None  # the CR_* status observed (None when unresolvable)
    reason: str
    polls: int
    waited_s: float


def _poll_cr_status(
    status_fn: Callable[[], str | None],
    *,
    timeout_s: float,
    poll_s: float,
    sleep_fn: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[str | None, int, float]:
    """Poll ``status_fn`` until the pipeline finishes or the bounded timeout lapses.

    Story 23.2-002 AC1: an open MR/PR with a running pipeline is polled until it
    flips to a terminal status. Returns ``(status, polls, waited_s)``. A status
    other than :data:`CR_PENDING` returns immediately; while pending it sleeps
    ``poll_s`` (clamped so the cumulative wait never overshoots ``timeout_s``) and
    re-reads, returning :data:`CR_PENDING` once the deadline passes so the caller
    treats a never-finishing pipeline as a timeout rather than hanging. ``clock``
    and ``sleep_fn`` are injected so the wait is deterministic under test.
    """
    start = clock()
    polls = 0
    while True:
        status = status_fn()
        polls += 1
        if status != CR_PENDING:
            return status, polls, clock() - start
        elapsed = clock() - start
        if elapsed >= timeout_s:
            return CR_PENDING, polls, elapsed
        sleep_fn(min(poll_s, max(0.0, timeout_s - elapsed)))


def _evaluate_ci_gate(status: str | None, *, no_ci_policy: str) -> tuple[str, str]:
    """Map a terminal CI ``status`` (+ the no-CI policy) to a gate ``(verdict, reason)``.

    Story 23.2-002: a green pipeline passes (AC3); a failed/unknown/timed-out
    (still-pending) pipeline blocks the merge (AC1/AC2); a resolved-but-absent CI
    signal (:data:`CR_NONE`) degrades per ``no_ci_policy`` — ``allow`` warns and
    merges, ``deny`` blocks (AC4); an unresolvable status (None — unmapped story
    or a host error) skips the gate so the merge path is unchanged.
    """
    if status is None:
        return _GATE_SKIP, "no resolvable CI source (unmapped or host error) — gate skipped"
    if status == CR_SUCCESS:
        return _GATE_PASS, "pipeline passed"
    if status in (CR_FAILED, CR_UNKNOWN):
        return _GATE_BLOCK, f"pipeline {status} — merge blocked"
    if status == CR_PENDING:
        return _GATE_BLOCK, "pipeline still running at timeout — merge blocked"
    if status == CR_NONE:
        if no_ci_policy == "deny":
            return _GATE_BLOCK, "no CI configured — denied by --ci-gate-no-ci=deny"
        return _GATE_PASS, "no CI configured — allowed by --ci-gate-no-ci=allow"
    return _GATE_BLOCK, f"unexpected CI status {status!r} — merge blocked"


def _run_merge_ci_gate(
    stage: str,
    ledger: Ledger,
    run_id: str,
    story: Story,
    pr_number: int | None,
    opts: BuildOptions,
    *,
    status_fn: Callable[[], str | None] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> _MergeCIGate | None:
    """Poll the merge stage's CR pipeline and decide whether the merge may proceed.

    Story 23.2-002: the host-neutral analogue of ``gh pr checks``. Returns None
    (a no-op) for any stage other than ``merge`` or when the story has no recorded
    CR ref, so non-merge stages and unmapped stories are unchanged. Otherwise it
    polls the CR's normalised CI status — via :func:`build_issue.change_request_status`
    by default, or the injected ``status_fn`` under test — within
    ``opts.ci_gate_timeout_s``, evaluates the gate, records the outcome in the
    ledger events, and returns the :class:`_MergeCIGate`. A ``_GATE_BLOCK`` tells
    the caller to route the story into the bugfix loop instead of merging.
    """
    if stage != "merge" or pr_number is None:
        return None
    sleep_fn = sleep_fn or time.sleep
    clock = clock or time.monotonic

    def _default_status() -> str | None:
        return build_issue.change_request_status(ledger, story.id, pr_number)

    status, polls, waited = _poll_cr_status(
        status_fn or _default_status,
        timeout_s=opts.ci_gate_timeout_s,
        poll_s=opts.ci_gate_poll_s,
        sleep_fn=sleep_fn,
        clock=clock,
    )
    verdict, reason = _evaluate_ci_gate(status, no_ci_policy=opts.ci_gate_no_ci)
    # A no-CI allow is a notable warning (the merge ships ungated), a block is an
    # error, a clean pass/skip is informational.
    if verdict == _GATE_BLOCK:
        level = "error"
    elif verdict == _GATE_PASS and status == CR_NONE:
        level = "warn"
    else:
        level = "info"
    ledger.event_log(
        run_id, story.id, level, "controller",
        f"merge CI gate: {reason} (status={status}, polls={polls}, "
        f"waited={int(waited)}s, cr=#{pr_number})",
    )
    return _MergeCIGate(
        verdict=verdict, status=status, reason=reason, polls=polls, waited_s=waited
    )


def render_bugfix_prompt(story: Story, failed_stage: str, failure: str) -> str:
    # Story 12.2-004: the bugfix agent commits its fix (linted mid-loop), so give
    # it a commitlint-compliant header by construction too.
    commit_header = build_commit_header(
        ctype="fix",
        scope=story.epic_name,
        subject=story.title,
        trailer=f" (#{story.id})",
    )
    # Story 26.1-001: root-cause-first discipline. Investigation precedes any
    # fix, and the reported root_cause must diagnose the defect — the schema
    # makes the field required, so the skeleton below already demands it.
    # Story 26.2-001: review-finding reception. When the failure carries review
    # findings, each is a claim to verify against the code — not an order — with
    # a structured dispute channel (finding_dispositions) for the ones the agent
    # can refute, so a wrong finding is disputed rather than blindly implemented.
    return (
        f"Bugfix story {story.id}: {story.title}. Stage '{failed_stage}' failed.\n"
        f"Failure: {failure}\n"
        "Investigate the root cause BEFORE attempting any fix — no guard, retry, "
        "or patch until you can state what broke and why. Refuse the shortcuts: "
        "'the fix is obvious', 'just see if CI passes', 'retry budget is low'. "
        "Report it in root_cause (what broke and why — "
        "not a restatement of the symptom).\n"
        "Review findings are claims, not orders. If the failure carries review "
        "findings, process each with the reception sequence "
        "read → restate → verify → evaluate → respond → implement: verify the "
        "finding against the actual code BEFORE implementing it — never agree "
        "performatively. Implement a finding only once you have confirmed it; "
        "dispute — with concrete technical reasoning — any finding you can refute "
        "against the code. Report every finding's verdict in finding_dispositions "
        "(each an object: finding, disposition implemented|disputed, and reasoning "
        "— required when disputed). A disputed finding is surfaced, not silently "
        "dropped, and is never reported as fixed.\n"
        "Classify (CODE_BUG/TEST_BUG/ENV_ISSUE), fix where possible. If you "
        "commit the fix, use this exact, conventional-commit-compliant message "
        f"— do not alter it:\n   {commit_header}\n"
        "Then emit the result block.\n"
        + _result_wrapper("bugfix-agent-response.schema.json")
    )


def render_envelope_reask_prompt(
    stage: str, story: Story, opts: BuildOptions, pr_number: int | None
) -> str:
    """Re-prompt the ``stage`` agent to emit ONLY its result block (Story 12.1-001).

    A missing/malformed result envelope usually means the agent finished the work
    but failed only to wrap it in the ``<<<RESULT_JSON>>>`` markers. This is a
    cheap, bounded **envelope-only re-ask**: it tells the agent the work is
    already done and asks it to inspect the current branch state and report the
    result block — explicitly NOT to redo the work or create new commits, so
    committed work is preserved (R10). It validates against the same stage schema
    as the original dispatch.
    """
    schema = AGENT_SCHEMAS[stage]
    pr_hint = f" (PR #{pr_number})" if pr_number is not None else ""
    return (
        f"You already completed the '{stage}' stage for story {story.id}: "
        f"{story.title}{pr_hint}, but your previous reply omitted or malformed "
        f"its {RESULT_START_MARKER} result block. This is an envelope-only re-ask.\n"
        "Do NOT redo the work or create new commits. Inspect the current state of "
        f"branch feature/{story.id} (git log/status, the open PR if any) and report "
        "what you already did as the result block below.\n"
        + _result_wrapper(schema)
    )


def render_commit_lint_reask_prompt(
    stage: str, story: Story, message: str, violations: list[str]
) -> str:
    """Re-prompt the ``stage`` agent to amend a commitlint-violating commit (12.2-002).

    The agent already committed compliant *code*; only the commit *message*
    breaks the repo's commitlint rules and would fail the commit-format CI job at
    PR time. This asks it to ``git commit --amend`` the HEAD commit on the story
    branch into a compliant header — explicitly NOT to change code or add new
    commits, so the work is preserved (R10) — then re-emit the ``stage`` result
    block with the amended ``commit_sha``. Used for any commit-authoring stage
    (build / coverage / bugfix), so the result block is validated against that
    stage's own schema.
    """
    bullets = "\n".join(f"  - {v}" for v in violations)
    return (
        f"The commit you authored on branch feature/{story.id} for the '{stage}' "
        f"stage of story {story.id}: {story.title} violates the repo's commitlint "
        "rules and will fail the commit-format CI job at PR time.\n\n"
        f"Current commit message:\n{message}\n\n"
        f"Violations:\n{bullets}\n\n"
        "Amend ONLY the commit message to be commitlint-compliant: "
        "`git commit --amend` the HEAD commit on the story branch into a "
        "conventional header (`type(scope): subject`) with a lower-case subject, "
        "a header of at most 72 characters, and an allowed type. Do NOT change any "
        f"code or create new commits. Then re-emit the '{stage}' result block with "
        "the amended commit_sha.\n"
        + _result_wrapper(AGENT_SCHEMAS[stage])
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


def _origin_default_ref(root: Path) -> str:
    """The ``origin/<default-branch>`` ref a story branch is cut from (Story 23.2-001).

    Resolves the remote's default branch from ``origin/HEAD`` so a build on a
    GitLab target cuts ``feature/<id>`` from — and its MR targets — that project's
    default branch rather than a hardcoded ``main`` (AC3). Falls back to
    ``origin/main`` whenever ``origin/HEAD`` is unset or unresolvable, so a GitHub
    repo (and every prompt-rendering test) is byte-identical to today (AC2).
    """
    try:
        head = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    except (OSError, subprocess.SubprocessError):
        return "origin/main"
    if head.returncode == 0 and head.stdout.strip():
        # e.g. "refs/remotes/origin/develop" → "origin/develop"
        return head.stdout.strip().removeprefix("refs/remotes/")
    return "origin/main"


def _refresh_base_ref(root: Path) -> None:
    """Advance the local ``origin/*`` tracking refs before a cohort dispatches (#231).

    Story worktrees are created detached at :func:`_base_ref` — ``origin/main``
    when ``origin/HEAD`` is set. In a long parallel run, earlier cohorts merge and
    push to ``main`` while the run is still in flight, but the *local* ``origin/main``
    ref only moves when something fetches. Without this refresh, a cohort dispatched
    after an earlier merge would branch from the pre-merge tip — re-introducing the
    avoidable merge conflicts, stale sibling trees, and review noise of #231. A
    guarded ``git fetch origin`` at each cohort boundary pulls the ref up to the
    current remote tip so the cohort's worktrees branch from the latest merged state.

    Best-effort and never fatal (mirrors :func:`reconcile_run`'s fetch contract):
    offline — or any git/OS failure — degrades silently to the current local ref
    rather than aborting the run. Does **not** touch in-flight worktrees; it only
    moves the ref future :func:`create_story_worktree` calls read.
    """
    try:
        _git(root, "fetch", "origin")
    except (OSError, subprocess.SubprocessError):
        pass


def _reposition_head(root: Path) -> None:
    """Return the working dir to the base branch between stories (Story 12.4-001).

    The merge agent only returns HEAD to ``main`` on its success path; on a
    parked/blocked/conflict path it leaves the shared working dir on the story's
    ``feature/<id>`` branch. Repositioning HEAD onto the base before the next
    story keeps that single working dir honest. The target is the **local**
    branch (``main``/``master``) — :func:`_base_ref` yields the remote-tracking
    ref ``origin/main`` when ``origin/HEAD`` is set, and checking that out would
    leave a real run in *detached HEAD*; stripping the ``origin/`` prefix and
    landing on the local branch keeps HEAD on a branch. Only if no matching local
    branch exists does it fall back to the ref ``_base_ref`` returned. Best-effort
    and non-fatal: it only ``checkout``s an existing ref — it **never** deletes a
    feature branch or its commits (R10) — and swallows every git/OS error so it
    can never fail an otherwise-good run.
    """
    try:
        base = _base_ref(root)
        if base is None:
            return
        # Prefer the local branch over the remote-tracking ref so HEAD lands on a
        # branch rather than detached at ``origin/main``.
        target = base.removeprefix("origin/")
        local = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{target}")
        if local.returncode != 0:
            target = base  # no local branch — fall back (rare; may detach)
        _git(root, "checkout", target)
    except (OSError, subprocess.SubprocessError):
        pass


class WorktreeError(Exception):
    """A per-story git worktree could not be created (Story 17.2-001).

    Raised by :func:`create_story_worktree` when ``git worktree add`` fails (no
    repo, a colliding path, a detached base that cannot be resolved). The caller
    (:func:`_prepare_story_workdir`) treats it as recoverable: it logs the reason
    and falls back to the shared repo root, so a worktree problem never fails a
    build.
    """


# Per-story worktrees live under this directory, matching the agent-* convention
# the orphan-sweeper (`hooks/sweep-orphan-worktrees.sh`) and the worktree
# bootstrap hook (`hooks/forge-worktree-bootstrap.sh`) already key off, and it is
# gitignored (`.claude/worktrees/`) so the checkouts never show in git status.
_WORKTREE_SUBDIR = (".claude", "worktrees")


def _worktree_registered_paths(root: Path) -> set[Path]:
    """The resolved paths git currently tracks as live worktrees (Story 17.2-002).

    The single source of truth for "is this checkout in use right now": callers
    consult it before tearing a worktree down (so a peer story's in-flight
    checkout is never removed) and to decide whether a re-entered story can
    re-attach to its existing worktree rather than re-create it. Best-effort —
    outside a git repo, or if ``git worktree list`` fails, it returns an empty
    set rather than raising, so a degraded git never crashes the controller.
    """
    try:
        res = _git(root, "worktree", "list", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return set()
    if res.returncode != 0:
        return set()
    paths: set[Path] = set()
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            paths.add(Path(line.removeprefix("worktree ")).resolve())
    return paths


def create_story_worktree(root: Path, story_id: str, run_id: str) -> Path:
    """Create (or deterministically re-attach) a story's git worktree, returning it.

    Story 17.2-001: the worktree lives at
    ``<root>/.claude/worktrees/agent-<run>-<story>`` so it matches the ``agent-*``
    orphan-sweeper and worktree-bootstrap conventions and stays out of git status.
    It is checked out **detached** at the base ref (``origin/main`` when set, else
    ``HEAD``) so the build agent cuts its own ``feature/<id>`` branch inside it
    exactly as on the shared-root path — concurrent stories therefore land on
    separate worktrees and separate branches over one shared object store, with no
    shared index or working-tree contention. Raises :class:`WorktreeError` when
    ``git worktree add`` fails so the caller can fall back to the shared root.

    Story 17.2-002 makes re-entry deterministic so a `resume` never trips the
    "already exists"/"already registered" `git worktree add` failure:

    * if the target path is **still a live registered worktree**, it is re-attached
      — returned as-is, preserving the in-flight branch and committed work; and
    * if a **stale directory** is left at the path (a crash that git no longer
      tracks), it is cleared and pruned before the fresh ``worktree add``.
    """
    worktrees_dir = root.joinpath(*_WORKTREE_SUBDIR)
    try:
        worktrees_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorktreeError(f"could not create {worktrees_dir}: {exc}") from exc
    # A short run prefix keeps the directory unique across concurrent runs while
    # the story id keeps it human-legible and grep-able in `git worktree list`.
    short_run = run_id.split("-")[0]
    path = worktrees_dir / f"agent-{short_run}-{story_id}"
    # Resume re-attach (17.2-002): an already-live worktree at this exact path is
    # reused verbatim — re-adding it would fail, and recreating it would discard
    # the resumed story's in-flight work.
    if path.resolve() in _worktree_registered_paths(root):
        # Re-assert the lock on resume so a pre-fix (unlocked) checkout is still
        # protected from the Stop-hook reaper (#180); idempotent — a redundant
        # lock just returns non-zero, which we ignore.
        _lock_story_worktree(root, path, story_id, run_id)
        return path
    # A directory git no longer tracks (crash debris) would make `worktree add`
    # fail with "already exists"; clear it and prune the registry first so the
    # add below is deterministic.
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
        _git(root, "worktree", "prune")
    base = _base_ref(root) or "HEAD"
    try:
        res = _git(root, "worktree", "add", "--detach", "--force", str(path), base)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorktreeError(f"git worktree add failed: {exc}") from exc
    if res.returncode != 0:
        raise WorktreeError(
            f"git worktree add for {story_id} failed: {res.stderr.strip()}"
        )
    # Lock the worktree so the global Stop-hook reaper (hooks/worktree-gc.sh)
    # cannot force-remove it mid-build (#180). A story's feature branch is 0
    # commits ahead of main until the build agent commits late, so the reaper's
    # `git branch --merged main` lists it as "merged"; the lock is what marks it
    # as in-use. Best-effort — a lock failure must not abort the build.
    _lock_story_worktree(root, path, story_id, run_id)
    return path


def _lock_story_worktree(root: Path, path: Path, story_id: str, run_id: str) -> None:
    """Lock a story worktree as in-use so the Stop-hook reaper spares it (#180).

    Best-effort and idempotent: re-locking an already-locked worktree returns
    non-zero, which is ignored, and any git/OS failure is swallowed so a lock
    problem never fails an otherwise-good build.
    """
    try:
        _git(
            root,
            "worktree",
            "lock",
            str(path),
            "--reason",
            f"sdlc run {run_id} story {story_id}",
        )
    except (OSError, subprocess.SubprocessError):
        pass


def remove_story_worktree(root: Path, path: Path) -> bool:
    """Remove one story's worktree on close-out, preserving its branch (Story 17.2-002).

    Reuses the merged-worktree removal semantics of ``hooks/worktree-gc.sh``:
    ``git worktree remove --force`` drops the working-tree checkout and its
    registration, then ``git worktree prune`` clears any dangling metadata. The
    story's ``feature/<id>`` branch and every commit on it are **never** touched —
    the branch/PR is the deliverable, so committed work survives the teardown
    (R10). ``--force`` only discards the worktree's own (expendable) working-tree
    state, not history.

    Best-effort and idempotent: removing a path git no longer tracks (a crash
    already cleaned it, or it was never created) is a no-op that still returns
    ``True``; only a genuine git/OS failure — e.g. not a repo — returns ``False``.
    Never raises, so close-out cannot fail an otherwise-good story.
    """
    try:
        if _git(root, "rev-parse", "--git-dir").returncode != 0:
            return False
        if path.resolve() in _worktree_registered_paths(root):
            # The controller locks its worktrees (#180); unlock before removing
            # since `git worktree remove --force` refuses a locked worktree.
            # Ignore unlock failures — an unlocked worktree returns non-zero.
            _git(root, "worktree", "unlock", str(path))
            _git(root, "worktree", "remove", "--force", str(path))
        # Belt-and-braces: a leftover directory git did not deregister is cleared
        # so the checkout never lingers on disk.
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        _git(root, "worktree", "prune")
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _prepare_story_workdir(
    opts: "BuildOptions",
    story: "Story",
    ledger: "Ledger",
    run_id: str,
    *,
    real_run: bool,
) -> Path | None:
    """The cwd a story's agent should run in: a per-story worktree, or the root.

    Story 17.2-001: a real ``parallel`` run isolates each story in its own git
    worktree (created here and recorded on the story row) so concurrent agents
    never collide in the shared checkout. Returns ``None`` — meaning "reuse the
    repo root", today's behaviour — when:

    * the run has an effective concurrency of 1 — ``--sequential`` *or*
      ``--concurrency=1`` (Story 17.1-001): one story at a time cannot collide,
      so the shared root is kept for byte-for-byte back-compat. Keying off
      :func:`effective_concurrency` (not ``opts.sequential`` alone) is what makes
      a real ``--concurrency=1`` run behave identically to ``--sequential``;
    * this is not a real run (``real_run=False``, i.e. a test injected a fake
      dispatcher): the orchestration is exercised without touching the real repo;
    * worktree creation fails: it degrades to the shared root and logs the
      reason rather than failing the build (best-effort, never fatal).
    """
    if effective_concurrency(opts) == 1 or not real_run:
        return None
    root = Path.cwd()
    try:
        path = create_story_worktree(root, story.id, run_id)
    except WorktreeError as exc:
        ledger.event_log(
            run_id, story.id, "warn", "controller",
            f"worktree isolation unavailable ({exc}); building in the repo root",
        )
        return None
    ledger.set_story_worktree(run_id, story.id, str(path))
    ledger.event_log(
        run_id, story.id, "info", "controller",
        f"isolated build worktree ready at {path}",
    )
    return path


def _teardown_story_workdir(
    ledger: "Ledger",
    run_id: str,
    story_id: str,
    *,
    real_run: bool,
) -> None:
    """Remove a story's isolated worktree once it closes out (Story 17.2-002).

    Called when a story reaches a terminal outcome (DONE / FAILED /
    NEEDS_ATTENTION): its ``feature/<id>`` branch and PR are the deliverable and
    are preserved, while the now-idle worktree checkout is removed so a long
    parallel run never leaks ``agent-*`` worktrees on disk. Teardown is keyed by
    *this* story's recorded worktree path, so it can never race or remove a peer
    worker's in-flight checkout (AC2).

    A no-op when the story built in the shared root (NULL ``worktree_path`` —
    ``--sequential`` / fallback) or for a fake-dispatcher run that never touched
    the real repo. Best-effort and never fatal: a removal failure is logged, not
    raised, so close-out cannot fail an otherwise-good story. Resumable holds
    (rate-limit park, cost gate) deliberately do **not** call this — their
    worktree is kept so the re-entered story can continue in place.
    """
    if not real_run:
        return
    path = ledger.story_worktree(run_id, story_id)
    if not path:
        return
    if remove_story_worktree(Path.cwd(), Path(path)):
        ledger.event_log(
            run_id, story_id, "info", "controller",
            f"isolated build worktree torn down ({path}); branch/PR preserved",
        )
    else:
        ledger.event_log(
            run_id, story_id, "warn", "controller",
            f"could not remove worktree {path}; orphan-sweeper will reclaim it",
        )


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


def _stage_artifact_exists(
    stage: str,
    story_id: str,
    pr_number: int | None = None,
    root: Path | None = None,
) -> bool:
    """True when ``stage``'s expected git artifact for ``story_id`` actually exists.

    The deliverable differs by stage, so one uniform "did the work survive?"
    probe would misjudge one of them (#232):

    - ``build`` / ``coverage`` / ``review`` author a commit on ``feature/<id>``
      *ahead of base* — not necessarily merged — so :func:`story_commit_exists`
      is the right probe. A merged-to-main check would wrongly reject an honestly
      committed-but-unmerged branch here, so these stages deliberately never
      consult landing detection.
    - ``merge`` *lands* the work, so its artifact is a merged PR / commit present
      on base. Its existence is decided **solely** by reconcile's
      :func:`_detect_landing` (is-ancestor / git-cherry / gh-pr-merged /
      commit-tag) — the same detection a later ``sdlc reconcile`` uses, so the
      two paths agree. The branch-commit probe is deliberately NOT consulted for
      ``merge``: by the time the merge stage runs, ``feature/<id>`` always
      carries the earlier build/coverage/review commits, so
      :func:`story_commit_exists` is unconditionally true here and is *not*
      evidence the merge landed. Accepting it would mask an unlanded merge
      (conflict, ``gh`` failure) as recoverable instead of the honest FAILED.

    Best-effort and never raises: the underlying probes swallow git errors and
    degrade to "no artifact", preserving the conservative hard-FAILED path.
    """
    root = root or Path.cwd()
    if stage == "merge":
        # The merge artifact is a *landed* merge, not a branch commit (which is
        # always present here). Imported lazily: reconcile imports from build, so
        # a top-level import would be circular.
        from sdlc.reconcile import _detect_landing

        base = _base_ref(root)
        return _detect_landing(story_id, pr_number, base, root) is not None
    return story_commit_exists(story_id, root=root)


def _commit_message(ref: str, root: Path | None = None) -> str | None:
    """The full commit message of ``ref``'s tip, or ``None`` if unreadable.

    Used to lint an agent-authored commit against commitlint (Story 12.2-002).
    Returns ``None`` — never raises — when git is absent, there is no repo, or the
    ref does not exist, so a commit-lint check degrades to a no-op exactly like
    :func:`story_commit_exists`.
    """
    root = root or Path.cwd()
    try:
        out = _git(root, "log", "-1", "--format=%B", ref)
        if out.returncode != 0:
            return None
        return out.stdout.rstrip("\n")
    except (OSError, subprocess.SubprocessError):
        return None


def _registry_register(
    registry: Registry, run_id: str, scope: str, db_path: Path, total: int
) -> None:
    """Register a starting run; swallow any cache IO error (never fails a build)."""
    try:
        registry.register(
            RunRecord(
                run_id=run_id,
                repo=str(Path.cwd().resolve()),
                db=str(Path(db_path).resolve()),
                scope=scope,
                pid=os.getpid(),
                status="IN_PROGRESS",
                started_at="",  # registry stamps the start time
                total=total,
                completed=0,
            )
        )
    except OSError:
        pass


def _registry_finish(
    registry: Registry, run_id: str, status: str, completed: int
) -> None:
    """Stamp a run terminal in the registry; swallow any cache IO error."""
    try:
        registry.mark_finished(run_id, status, completed=completed)
    except OSError:
        pass


def persist_cohort_structure(
    ledger: Ledger, run_id: str, cohorts: list[list[Story]]
) -> None:
    """Persist each story's wave index + intra-queue deps (Story 11.2-007).

    Enumerates the :func:`compute_cohorts` result so a story's ``wave`` is its
    cohort position (stories sharing a wave run in parallel) and records only
    intra-queue dependency edges — matching ``compute_cohorts`` semantics, so a
    dependency on an already-merged (out-of-queue) story is dropped and the
    persisted structure reflects the actual runtime parallelism. Shared by
    ``run_build`` (Phase 2) and ``resume`` so both scheduling paths record
    identical waves for the same queue.
    """
    in_queue = {story.id for cohort in cohorts for story in cohort}
    for wave, cohort in enumerate(cohorts):
        for story in cohort:
            intra_deps = [dep for dep in story.dependencies if dep in in_queue]
            ledger.set_story_wave(run_id, story.id, wave, intra_deps)


def _filter_git_landed(
    buildable: list[Story], done_skips: list[Story], root: Path | None = None
) -> tuple[list[Story], list[Story]]:
    """Move git-landed stories out of ``buildable`` into ``done_skips`` (#227).

    Discovery decides ``Story.done`` from markdown status alone, so a story merged
    in a *prior* run whose markdown was never flipped to ``Status: Done`` is
    markdown-``done=False`` and lands in ``buildable`` — where re-executing it
    fails (the work is already on the base branch) and cascade-blocks every story
    that depends on it, forcing the scoped-build workaround. This pass treats such
    a story as done when its work is *git-landed* on the base branch, exactly as a
    later :func:`sdlc.reconcile.reconcile_run` would (so the two paths agree and
    the partition is idempotent with ``sdlc reconcile``). A landed story moved
    into ``done_skips`` stays out of the cohort DAG, so its dependents see it as a
    satisfied out-of-queue edge rather than a blocked in-queue one.

    Performance: only ``buildable`` stories are probed, and those are by
    construction the *non-markdown-done* set — a markdown-done story is already in
    ``done_skips`` and never reaches the git probe (the short-circuit #227
    requires). Each probe reuses reconcile's :func:`_detect_landing` with
    ``pr_number=None`` so ``gh`` is never shelled — a probe is a small bounded set
    of local git calls, run once per buildable story at schedule time. Best-effort
    and offline-safe: a missing base ref or any git error leaves the story in
    ``buildable``, degrading to today's markdown-only behaviour.
    """
    if not buildable:
        return buildable, done_skips
    root = root or Path.cwd()
    base = _base_ref(root)
    if base is None:
        return buildable, done_skips
    # reconcile imports from build, so a top-level import would be circular.
    from sdlc.reconcile import _detect_landing

    still_buildable: list[Story] = []
    landed: list[Story] = []
    for story in buildable:
        try:
            if _detect_landing(story.id, None, base, root) is not None:
                landed.append(story)
            else:
                still_buildable.append(story)
        except (OSError, subprocess.SubprocessError):
            still_buildable.append(story)
    return still_buildable, done_skips + landed


def _stamp_run_actor(
    ledger: Ledger, run_id: str, adapter: IssueHostAdapter | None
) -> str:
    """Stamp this run's actor from host identity (Story 22.5-001 AC1).

    The code host *is* the identity provider (`gh api user` / `glab` equivalent),
    so attribution needs no shared token. Best-effort and self-degrading: with no
    ``adapter`` (the host could not be determined) or when host auth is absent,
    the actor is :data:`~sdlc.identity.UNKNOWN_ACTOR` rather than a crash (AC3) —
    so a run is always attributed *something*. Returns the stamped actor.
    """
    from sdlc.identity import UNKNOWN_ACTOR, cache_actor

    if adapter is None:
        ledger.run_set_actor(run_id, UNKNOWN_ACTOR)
        return UNKNOWN_ACTOR
    # cache_actor resolves via the adapter's whoami and degrades to `unknown`
    # internally when host auth is missing, so this never raises on auth gaps.
    return cache_actor(ledger, run_id, adapter)


def run_build(
    opts: BuildOptions,
    *,
    queue: list[Story],
    ledger: Ledger,
    dispatcher: Dispatcher | None = None,
    preflight: Callable[[], bool] | None = None,
    render_view: Callable[[str], None] | None = None,
    registry: "Registry | None" = None,
    clock: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    root: Path | None = None,
    actor_adapter: "IssueHostAdapter | None" = None,
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
    hook that regenerates the markdown progress view from the ledger. ``root``
    is the git project root the git-landed partition probe (#227) consults;
    it defaults to the cwd in production and lets tests point at a non-repo dir.
    """
    dispatch = _resolve_dispatch(dispatcher, opts, dispatch_agent)
    check_preflight = preflight or (lambda: default_preflight(timeout=opts.preflight_timeout))

    # --- Partition: shipped (Done) stories are skipped unless --rebuild ------
    if opts.rebuild:
        buildable, done_skips = queue, []
    else:
        buildable = [s for s in queue if not s.done]
        done_skips = [s for s in queue if s.done]
        # Markdown done-detection misses stories merged in a prior run whose
        # markdown was never flipped to Status: Done; treat those as done when
        # their work is git-landed so an epic re-run skips them instead of
        # rebuilding and cascade-blocking their dependents (#227). Only the
        # non-markdown-done `buildable` set is probed — markdown-done stories
        # already sit in `done_skips` and never trigger a git probe. ``root``
        # (default cwd) lets tests point the probe at a non-repo dir to stay
        # hermetic; production resolves git against the project root as before.
        buildable, done_skips = _filter_git_landed(buildable, done_skips, root)

    # --- Limit truncation (applies to the buildable set) ---------------------
    if opts.limit:
        buildable = truncate_queue(buildable, opts.limit)

    # --- Dry run: report the buildable plan, dispatch nothing ----------------
    # A dry run is plan-only — it must not run the (possibly slow/failing)
    # preflight gate, so this returns before Phase 1.
    if opts.dry_run:
        return BuildResult(dry_run=True, planned=len(buildable))

    # --- Recursion guard (Story 12.1-002) ------------------------------------
    # When we are running inside another build's preflight test suite (the
    # SDLC_IN_TEST sentinel is set) AND this is a *real* run using the default
    # dispatcher and preflight (no injected fakes), short-circuit before the real
    # preflight subprocess: otherwise a project test that invoked `sdlc build`
    # bare would recurse into pytest-within-pytest and hang the parent run. Tests
    # that inject a fake dispatcher/preflight are exercising orchestration
    # deliberately, so they are never blocked — this guards only the side-effecting
    # real path, not unit coverage (AC3).
    if dispatcher is None and preflight is None and in_test_sentinel():
        return BuildResult(skipped_in_test=True, planned=len(buildable))

    # --- Phase 1: Preflight (real runs only) ---------------------------------
    if not opts.skip_preflight:
        if not check_preflight():
            return BuildResult(preflight_failed=True)

    # --- Ledger bootstrap ----------------------------------------------------
    ledger.init()
    _ensure_repo_ignores(ledger.db_path)  # keep ledger files out of git status (R9)
    # Story 17.3-001: the label is derived from the worker cap the executor will
    # actually use, so `--concurrency=1` reports `serial` rather than lying.
    mode = authoritative_mode(opts)
    run_id = ledger.run_create(opts.scope, mode)
    # Story 22.5-001 AC1: stamp the run's actor from host identity, resolved once
    # per run. Best-effort and self-degrading — no adapter (host indeterminate)
    # or absent host auth yields `unknown`, never a crash (AC3).
    _stamp_run_actor(ledger, run_id, actor_adapter)
    ledger.set_total(run_id, len(buildable))
    # Announce the run in the host-level registry so a single dashboard can
    # discover it across repos (Story 11.2-001). Best-effort: a registry IO
    # failure must never fail an otherwise-good build.
    if registry is not None:
        _registry_register(registry, run_id, opts.scope, ledger.db_path, len(buildable))
    # Per-run transcript dir (next to the ledger; covered by the R9 ignore).
    logs_dir = Path(f"{ledger.db_path}.logs") / run_id
    ledger.event_log(
        run_id, "", "info", "controller", f"run started: scope={opts.scope} mode={mode}"
    )
    # Story 20.5-001: resolve and log the dispatch harness's capabilities so a
    # heterogeneous run is auditable and any mode downgrade is explicit. The
    # default slot is the built-in Claude harness (no probe, all capabilities),
    # so this is purely additive logging and never alters dispatch behaviour.
    _log_harness_preflight(ledger, run_id, mode, opts)
    # Story 20.5-002: record any safe fallback the harness's capability gaps force
    # (parallel→serial, usage "unavailable", rate-limit backoff skipped) so a
    # degradation is auditable in the run summary, never silent. Empty (no-op) for
    # the built-in Claude harness, which has every capability.
    _record_degradations(ledger, run_id, mode)
    try:  # best-effort lifecycle notification; never fail a build
        notify("run_started", run=run_id, scope=opts.scope, mode=mode)
    except Exception:
        pass
    # Persist the run's options as an immutable config marker (read back by the
    # dashboard header). Kept as an event so no schema migration is needed.
    ledger.event_log(
        run_id, "", "info", "config",
        json.dumps({
            "preflight": "skipped" if opts.skip_preflight else "passed",
            "skip_coverage": opts.skip_coverage,
            "coverage_threshold": opts.coverage_threshold,
            "mode": mode,
            # Story 17.1-001: persist the worker cap so a resume fans out a
            # cohort with the same effective concurrency the original run used.
            "concurrency": opts.concurrency,
            "rebuild": opts.rebuild,
            "limit": opts.limit,
            # Story 14.1-001: persist the budget so a resume re-enforces the same
            # ceiling (a paused run must not continue unbounded). Carried through
            # the accrual already in the ledger; `sdlc resume --budget` raises it.
            "budget": opts.budget,
            "budget_policy": opts.budget_policy,
            # Story 14.1-003: persist the rate-limit knobs so a resume of a
            # RATE_LIMITED run honours the same auto-wait cap and window budget.
            "rate_limit_max_wait_s": opts.rate_limit_max_wait_s,
            "window_budget": opts.window_budget,
            "window_s": opts.window_s,
            "rate_limit_threshold": opts.rate_limit_threshold,
            # Story 14.2-001: persist the model-routing profile + per-stage
            # overrides so a resume routes identically and the dashboard can show
            # which map a run used. "" / off keeps today's CLI-default behaviour.
            "model_profile": opts.model_profile,
            "model_overrides": opts.model_overrides,
            # Story 14.1-002: persist the per-stage cost-estimate threshold so a
            # resume **re-enforces** the same gate. Without this a resumed run
            # would rebuild opts with threshold=0 and silently dispatch a stage
            # the original run gated. `sdlc resume --cost-threshold` raises/clears
            # it to let a gated story proceed.
            "cost_estimate_threshold": opts.cost_estimate_threshold,
            # Story 14.1-002: persist `--auto` so a resumed run keeps the same
            # cost-gate posture. An auto run warns-and-proceeds over the threshold;
            # without this the resume would default to interactive and wrongly gate
            # stages the original auto run would have proceeded through.
            "auto": opts.auto,
            # Story 14.2-002: persist the thinking-token cap so a resume re-applies
            # the same MAX_THINKING_TOKENS bound and the dashboard can show it.
            # 0 keeps today's default thinking budget.
            "thinking_cap": opts.thinking_cap,
            # Story 13.4-002: persist the container-sandbox flag so a resumed run
            # keeps the same isolation posture (and the dashboard can show it).
            # False keeps the host path — unchanged for runs that predate this.
            "sandbox": opts.sandbox,
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
    # Story 11.2-007: record each story's wave (cohort) index + intra-queue deps
    # at schedule time so the dashboard / `sdlc status` can show the run's
    # parallelism structure without re-reading the epic files. Done-skips stay
    # out of the cohorts (NULL wave) — they are not part of the build's DAG.
    persist_cohort_structure(ledger, run_id, cohorts)
    status: dict[str, str] = {s.id: "TODO" for s in buildable}

    # --- Phase 2: cohort-by-cohort execution ---------------------------------
    # Story 14.1-001: the budget gate is checked *before* dispatching each story
    # (the prior story's stages are already finished/committed, so R10 holds). A
    # crossed ceiling halts further dispatch and hands off to the policy-aware
    # close-out below — pause leaves the run resumable, abort stamps it terminal.
    # The pre-dispatch position means a resume of an already-over run dispatches
    # nothing and re-parks until the budget is raised.
    # Story 14.1-003: the rate-limit context owns the in-process auto-wait. A
    # reactive 429 (RateLimitError) or a proactive configured-window-budget
    # exhaustion pauses the run; within the auto-wait cap it waits and resumes the
    # same run, beyond it durably parks RATE_LIMITED for `sdlc resume`.
    # Seed the window baseline with any accrual already on the run (0 for a fresh
    # build; non-zero only in the degenerate re-run case) so the window measures
    # spend from now — see _make_rate_limit_context for why this matters on resume.
    rl_ctx = _make_rate_limit_context(
        opts, clock=clock, sleep_fn=sleep_fn,
        baseline=ledger.run_usage_totals(run_id)["tokens"],
    )
    budget_stopped = False
    rate_limit_park: _StoryRunOutcome | None = None
    cost_gated: _CostGatePause | None = None

    # Story 17.1-001: drive one ready story through its isolated worktree and the
    # full build→coverage→review→merge sequence. Shared verbatim by the serial
    # and parallel paths so the two can never diverge in how a story is run — only
    # in how many run at once.
    def _run_one(story: Story) -> _StoryRunOutcome:
        # Story 17.2-001: isolate a real parallel story in its own git worktree so
        # concurrent agents never collide in the shared checkout (None reuses the
        # root for --sequential / fakes / on creation failure).
        workdir = _prepare_story_workdir(
            opts, story, ledger, run_id, real_run=dispatcher is None
        )
        return _run_story_rate_limited(
            rl_ctx, story, ledger, run_id, dispatch, logs_dir, workdir=workdir,
        )

    # Story 19.2-002: credit a parallel story's terminal status the instant its
    # worker finishes — not at the cohort barrier — so status_snapshot's derived
    # done/total counts (and the dashboard's top bar) move live as each story
    # completes. The post-barrier loop re-applies the same status as part of its
    # control-flow bookkeeping; set_story_status is a by-value UPDATE on the
    # single story row, so the repeat write can never double-count, keeping the
    # barrier finalize idempotent. Serial mode never routes through here.
    def _credit_terminal(story: Story, outcome_status: str) -> None:
        ledger.set_story_status(run_id, story.id, outcome_status)
        # Story 22.4-002: announce a parked terminal status (NEEDS_ATTENTION etc.)
        # on the story's issue — best-effort, a no-op for DONE / unmapped stories.
        build_issue.announce_terminal(ledger, story.id, outcome_status)

    workers = effective_concurrency(opts)
    for cohort in cohorts:
        if budget_stopped or rate_limit_park is not None or cost_gated is not None:
            break

        # --- Serial path (--sequential / --concurrency=1) ---------------------
        # Byte-for-byte today's one-at-a-time behaviour (AC3): the budget gate is
        # re-checked before *every* story, and a park/gate breaks mid-cohort.
        if workers == 1:
            for story in cohort:
                if _budget_exceeded(ledger, run_id, opts.budget):
                    budget_stopped = True
                    break
                # A story whose dependency did not cleanly finish cannot proceed.
                # NEEDS_ATTENTION counts as not-done: the dependency's work is
                # committed but unmerged (parked for manual push/MR or a
                # commit-message fix), so a dependent built on top of it would race
                # incomplete work — block it like any other non-DONE dependency.
                blocked_by = [
                    dep
                    for dep in story.dependencies
                    if status.get(dep)
                    in {"FAILED", "BLOCKED", "SKIPPED", "NEEDS_ATTENTION", "AWAITING_APPROVAL"}
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

                try:
                    sr = _run_one(story)
                except _CostGatePause as gate:
                    # Story 14.1-002: the interactive cost gate halted a stage.
                    # Park the story NEEDS_ATTENTION but leave the run resumable.
                    status[story.id] = "NEEDS_ATTENTION"
                    ledger.set_story_status(run_id, story.id, "NEEDS_ATTENTION")
                    cost_gated = gate
                    break
                if sr.parked:
                    # Reset is beyond the auto-wait cap: leave the in-flight story
                    # RATE_LIMITED (resumable, distinct from NEEDS_ATTENTION) and
                    # hand off to the durable park close-out below.
                    status[story.id] = "RATE_LIMITED"
                    ledger.set_story_status(run_id, story.id, "RATE_LIMITED")
                    rate_limit_park = sr
                    break
                outcome = sr.status or "FAILED"  # non-parked always carries a status
                status[story.id] = outcome
                ledger.set_story_status(run_id, story.id, outcome)
                # Story 22.4-002: mirror a parked terminal status onto the issue
                # (best-effort; no-op for DONE / unmapped stories).
                build_issue.announce_terminal(ledger, story.id, outcome)
                if outcome == "FAILED":
                    try:  # best-effort; terminal FAILED only (no bugfix-retry noise)
                        notify("story_failed", run=run_id, story_id=story.id)
                    except Exception:
                        pass
                # Story 17.2-002: a terminal story closes out — remove its isolated
                # worktree (branch/PR preserved). Resumable holds above `break`d out
                # first, so they keep their worktree for re-entry.
                _teardown_story_workdir(
                    ledger, run_id, story.id, real_run=dispatcher is None
                )

                # Story 12.4-001: reposition HEAD back to the base between stories
                # so a parked/blocked story's feature branch (the merge agent only
                # returns to main on its success path) is never the base the next
                # story's branch stacks on. Real runs only — injected fakes operate
                # on the test's cwd and must not touch the real repo, exactly like
                # the close-out reconcile guard below. Best-effort, never fatal.
                if dispatcher is None:
                    _reposition_head(Path.cwd())
            continue

        # --- Parallel path (Story 17.1-001) -----------------------------------
        # Dispatch the cohort's ready stories through a bounded worker pool, then
        # a barrier waits for the whole cohort before the next begins. The budget
        # gate is checked once at the cohort boundary (mid-cohort spend is bounded
        # by the barrier, not by a per-story check the pool cannot interleave).
        if _budget_exceeded(ledger, run_id, opts.budget):
            budget_stopped = True
            break

        # Dependency-block check stays *before* dispatch (AC4): a story whose dep
        # did not cleanly finish is marked BLOCKED and never submitted.
        dispatchable: list[Story] = []
        for story in cohort:
            blocked_by = [
                dep
                for dep in story.dependencies
                if status.get(dep)
                in {"FAILED", "BLOCKED", "SKIPPED", "NEEDS_ATTENTION", "AWAITING_APPROVAL"}
            ]
            if blocked_by:
                status[story.id] = "BLOCKED"
                ledger.set_story_status(run_id, story.id, "BLOCKED")
                ledger.event_log(
                    run_id, story.id, "warn", "controller",
                    f"blocked: dependency not done ({', '.join(blocked_by)})",
                )
                continue
            dispatchable.append(story)
        if not dispatchable:
            continue

        # Story #231: refresh origin/main before this cohort's worktrees are cut so
        # they branch from the latest merged state, not the main captured at run
        # start. Earlier cohorts merge and push during a long run, so without this
        # a later cohort would build against a stale base — avoidable conflicts and
        # missing just-merged sibling changes. Real runs only (injected fakes must
        # not touch the real repo, like the close-out reconcile/reposition guards);
        # guarded and best-effort — offline degrades to the current local ref. This
        # moves only the ref future create_story_worktree calls read; in-flight
        # worktrees from earlier cohorts are never rebased.
        if dispatcher is None:
            _refresh_base_ref(Path.cwd())

        # Apply outcomes in cohort (submission) order so the result is
        # deterministic regardless of which worker finished first.
        for result in _dispatch_cohort(
            dispatchable, max_workers=workers, run_one=_run_one,
            on_terminal=_credit_terminal,
        ):
            story = result.story
            if result.cost_gate is not None:
                status[story.id] = "NEEDS_ATTENTION"
                ledger.set_story_status(run_id, story.id, "NEEDS_ATTENTION")
                if cost_gated is None:  # first gate in cohort order wins
                    cost_gated = result.cost_gate
                continue
            if result.error is not None:
                # Failure isolation (AC4): an unexpected raise is recorded FAILED
                # for this story; its peers already ran to completion in the pool.
                status[story.id] = "FAILED"
                ledger.set_story_status(run_id, story.id, "FAILED")
                ledger.event_log(
                    run_id, story.id, "error", "controller",
                    f"story raised during concurrent execution: {result.error}",
                )
                try:
                    notify("story_failed", run=run_id, story_id=story.id)
                except Exception:
                    pass
                # Story 17.2-002: failure isolation still closes the story out —
                # tear its worktree down (committed work stays on its branch).
                _teardown_story_workdir(
                    ledger, run_id, story.id, real_run=dispatcher is None
                )
                continue
            sr = result.outcome
            assert sr is not None  # exactly one of outcome/cost_gate/error is set
            if sr.parked:
                status[story.id] = "RATE_LIMITED"
                ledger.set_story_status(run_id, story.id, "RATE_LIMITED")
                if rate_limit_park is None:  # first park in cohort order wins
                    rate_limit_park = sr
                continue
            outcome = sr.status or "FAILED"
            status[story.id] = outcome
            ledger.set_story_status(run_id, story.id, outcome)
            if outcome == "FAILED":
                try:
                    notify("story_failed", run=run_id, story_id=story.id)
                except Exception:
                    pass
            # Story 17.2-002: terminal story → remove its isolated worktree once
            # the cohort barrier has it in hand (single-threaded here, so this
            # never races a live worker); parked holds above `continue`d first.
            _teardown_story_workdir(
                ledger, run_id, story.id, real_run=dispatcher is None
            )

        # Reposition HEAD once after the cohort barrier (real runs only). Each
        # concurrent story committed in its own worktree, so the shared root only
        # needs returning to base once the whole cohort is done.
        if dispatcher is None:
            _reposition_head(Path.cwd())

    # Story 14.1-003: a rate-limit park (reset beyond the auto-wait cap) skips the
    # terminal close-out in favour of a durable RATE_LIMITED handoff — resumable,
    # never terminal, distinct from NEEDS_ATTENTION. Committed work is untouched.
    if rate_limit_park is not None:
        return _rate_limit_close_out(
            opts, ledger, run_id, status, done_skips, buildable,
            rate_limit_park, rl_ctx.clock(), render_view,
        )

    # Story 14.1-001: a crossed budget ceiling skips the normal close-out (which
    # would stamp a terminal status and run reconcile network I/O) in favour of a
    # policy-aware handoff: pause keeps the run IN_PROGRESS (resumable), abort
    # stamps it ABORTED. Committed work from finished stories is untouched (R10).
    if budget_stopped:
        return _budget_close_out(
            opts, ledger, run_id, status, done_skips, buildable,
            registry, render_view,
        )

    # Story 14.1-002: the interactive cost gate paused the run. Like a budget
    # pause, leave it IN_PROGRESS (resumable) instead of stamping a terminal —
    # `sdlc resume --cost-threshold` raises the gate to continue.
    if cost_gated is not None:
        return _cost_gate_close_out(
            opts, ledger, run_id, status, done_skips, buildable,
            cost_gated, render_view,
        )

    # --- Phase 3: close out via the shared finalize helper (12.3-004) --------
    # finalize_run runs reconciliation against origin/main (real runs only, hence
    # the dispatcher-None gate), recomputes the tally — folding in the shipped
    # `done_skips` skipped before the loop — logs the finish event, stamps the run
    # terminal, and finishes the registry. The identical close-out is shared with
    # `run_resume` so the two paths can never diverge.
    outcome = finalize_run(
        ledger,
        run_id,
        status,
        reconcile=dispatcher is None,
        root=Path.cwd(),
        registry=registry,
        extra_skipped=len(done_skips),
        finish_label="run finished",
        render_view=render_view,
    )

    # The returned per-story map includes the shipped skips for visibility,
    # even though they were kept out of the cohort `status` used for blocking.
    story_status = {s.id: "SKIPPED" for s in done_skips}
    story_status.update(status)
    return BuildResult(
        completed=outcome.completed,
        failed=outcome.failed,
        skipped=outcome.skipped,
        blocked=outcome.blocked,
        needs_attention=outcome.needs_attention,
        awaiting_approval=outcome.awaiting_approval,
        planned=len(buildable),
        run_id=run_id,
        story_status=story_status,
    )


def apply_budget_stop(
    ledger: "Ledger",
    run_id: str,
    budget: int,
    budget_policy: str,
    completed: int,
    *,
    registry: "Registry | None" = None,
) -> dict:
    """Record a budget-gate stop and apply the policy (Story 14.1-001).

    Shared by :func:`run_build` and ``run_resume`` so both halt identically.
    ``pause`` (the default) records a NEEDS_ATTENTION-style reason and leaves the
    run ``IN_PROGRESS`` so :meth:`Ledger.latest_resumable_run` — and therefore
    ``sdlc resume`` — picks it up once the budget is raised. ``abort`` records the
    stop and stamps the run ``ABORTED`` (terminal). Returns the accrual usage
    dict so the caller can surface it on its result type. Finished stories keep
    their committed work (R10); the unbuilt stories stay ``TODO``.
    """
    usage = ledger.run_usage_totals(run_id)
    label = notional_cost_label(usage["cost_usd"])
    reason = (
        f"budget ceiling crossed: {usage['tokens']} ≥ {budget} tokens "
        f"(notional {label})"
    )
    if budget_policy == "abort":
        ledger.event_log(
            run_id, "", "error", "controller",
            f"{reason} — aborting run (policy=abort).",
        )
        ledger.run_update_status(run_id, "ABORTED")
        if registry is not None:
            _registry_finish(registry, run_id, "ABORTED", completed)
    else:  # pause: leave the run IN_PROGRESS so it resumes like any interruption
        ledger.event_log(
            run_id, "", "warn", "controller",
            f"{reason} — pausing run (policy=pause); raise --budget and "
            "`sdlc resume` to continue.",
        )
    return usage


def _budget_close_out(
    opts: BuildOptions,
    ledger: Ledger,
    run_id: str,
    status: dict[str, str],
    done_skips: list[Story],
    buildable: list[Story],
    registry: "Registry | None",
    render_view: Callable[[str], None] | None,
) -> BuildResult:
    """Close out a ``run_build`` run halted by the budget gate (Story 14.1-001)."""
    completed = sum(1 for v in status.values() if v == "DONE")
    failed = sum(1 for v in status.values() if v == "FAILED")
    blocked = sum(1 for v in status.values() if v == "BLOCKED")
    needs_attention = sum(1 for v in status.values() if v == "NEEDS_ATTENTION")
    awaiting_approval = sum(1 for v in status.values() if v == "AWAITING_APPROVAL")
    skipped = len(done_skips) + sum(1 for v in status.values() if v == "SKIPPED")
    ledger.run_update_counts(run_id, completed, failed)

    usage = apply_budget_stop(
        ledger, run_id, opts.budget, opts.budget_policy, completed,
        registry=registry,
    )

    if render_view is not None:
        render_view(run_id)

    story_status = {s.id: "SKIPPED" for s in done_skips}
    story_status.update(status)
    return BuildResult(
        completed=completed,
        failed=failed,
        skipped=skipped,
        blocked=blocked,
        needs_attention=needs_attention,
        awaiting_approval=awaiting_approval,
        planned=len(buildable),
        run_id=run_id,
        story_status=story_status,
        budget_stopped=True,
        budget_policy=opts.budget_policy,
        accrued_tokens=usage["tokens"],
        notional_cost_usd=usage["cost_usd"],
    )


def apply_cost_gate_pause(
    ledger: "Ledger", run_id: str, threshold: int, gate: "_CostGatePause"
) -> None:
    """Record an interactive cost-gate pause and leave the run resumable (14.1-002).

    Shared by :func:`run_build` and ``run_resume`` so both halt identically. The
    run is deliberately left ``IN_PROGRESS`` (never stamped terminal) so
    :meth:`Ledger.latest_resumable_run` — and therefore ``sdlc resume`` — picks it
    up; raising ``--cost-threshold`` on resume lets the gated stage proceed.
    Finished stories keep their committed work (R10).
    """
    est = gate.estimate
    ledger.event_log(
        run_id, "", "warn", "controller",
        f"cost gate paused run: {gate.stage} for {gate.story_id} estimated "
        f"~{est.estimated_tokens} tokens "
        f"({notional_cost_label(est.estimated_cost_usd)}) ≥ "
        f"--cost-threshold={threshold} — run left IN_PROGRESS; raise "
        "--cost-threshold and `sdlc resume` to continue.",
    )


def _cost_gate_close_out(
    opts: BuildOptions,
    ledger: Ledger,
    run_id: str,
    status: dict[str, str],
    done_skips: list[Story],
    buildable: list[Story],
    gate: "_CostGatePause",
    render_view: Callable[[str], None] | None,
) -> BuildResult:
    """Close out a ``run_build`` run paused by the interactive cost gate (14.1-002)."""
    completed = sum(1 for v in status.values() if v == "DONE")
    failed = sum(1 for v in status.values() if v == "FAILED")
    blocked = sum(1 for v in status.values() if v == "BLOCKED")
    needs_attention = sum(1 for v in status.values() if v == "NEEDS_ATTENTION")
    awaiting_approval = sum(1 for v in status.values() if v == "AWAITING_APPROVAL")
    skipped = len(done_skips) + sum(1 for v in status.values() if v == "SKIPPED")
    ledger.run_update_counts(run_id, completed, failed)

    # Leave the run IN_PROGRESS (resumable) — deliberately skip finalize_run, which
    # would stamp a terminal status `latest_resumable_run` could never surface.
    apply_cost_gate_pause(ledger, run_id, opts.cost_estimate_threshold, gate)

    if render_view is not None:
        render_view(run_id)

    story_status = {s.id: "SKIPPED" for s in done_skips}
    story_status.update(status)
    return BuildResult(
        completed=completed,
        failed=failed,
        skipped=skipped,
        blocked=blocked,
        needs_attention=needs_attention,
        awaiting_approval=awaiting_approval,
        planned=len(buildable),
        run_id=run_id,
        story_status=story_status,
        cost_gated=True,
    )


def _rate_limit_close_out(
    opts: BuildOptions,
    ledger: Ledger,
    run_id: str,
    status: dict[str, str],
    done_skips: list[Story],
    buildable: list[Story],
    park: _StoryRunOutcome,
    now: float,
    render_view: Callable[[str], None] | None,
) -> BuildResult:
    """Close out a ``run_build`` run durably parked for rate limits (Story 14.1-003).

    The run is left ``RATE_LIMITED`` (resumable, not terminal) so `sdlc resume`
    continues it once the Max plan's window reopens. Finished stories keep their
    committed work (R10); the unbuilt stories stay ``TODO`` and the in-flight one
    is ``RATE_LIMITED``.
    """
    completed = sum(1 for v in status.values() if v == "DONE")
    failed = sum(1 for v in status.values() if v == "FAILED")
    blocked = sum(1 for v in status.values() if v == "BLOCKED")
    needs_attention = sum(1 for v in status.values() if v == "NEEDS_ATTENTION")
    awaiting_approval = sum(1 for v in status.values() if v == "AWAITING_APPROVAL")
    skipped = len(done_skips) + sum(1 for v in status.values() if v == "SKIPPED")
    ledger.run_update_counts(run_id, completed, failed)

    assert park.signal is not None  # a park always carries the pause cause
    reset_at = apply_rate_limit_park(
        ledger, run_id, park.signal, now=now, waited_s=park.waited_s,
        window_s=opts.window_s,
    )

    if render_view is not None:
        render_view(run_id)

    story_status = {s.id: "SKIPPED" for s in done_skips}
    story_status.update(status)
    return BuildResult(
        completed=completed,
        failed=failed,
        skipped=skipped,
        blocked=blocked,
        needs_attention=needs_attention,
        awaiting_approval=awaiting_approval,
        planned=len(buildable),
        run_id=run_id,
        story_status=story_status,
        rate_limited=True,
        rate_limit_reset_at=reset_at,
        rate_limit_waited_s=park.waited_s,
    )


def _run_terminal(
    failed: int, blocked: int, needs_attention: int, awaiting_approval: int
) -> str:
    """The run terminal implied by the per-story outcome counts (Story 12.3-003).

    Precedence: a ``FAILED``/``BLOCKED`` story makes the run ``FAILED``;
    otherwise a leftover ``NEEDS_ATTENTION`` (the more-urgent "work is stuck,
    push/fix it" signal) wins over ``AWAITING_APPROVAL`` so a mixed run never
    hides stuck work behind the approval state; a run whose only non-DONE
    stories are ``AWAITING_APPROVAL`` is reported ``AWAITING_APPROVAL`` (never
    ``FAILED``); an all-DONE/SKIPPED run is ``DONE``. Shared by ``run_build`` and
    ``run_resume`` close-out.
    """
    if failed or blocked:
        return "FAILED"
    if needs_attention:
        return "NEEDS_ATTENTION"
    if awaiting_approval:
        return "AWAITING_APPROVAL"
    return "DONE"


@dataclass
class FinalizeOutcome:
    """The per-status tally and run terminal computed by :func:`finalize_run`."""

    run_terminal: str
    completed: int
    failed: int
    blocked: int
    needs_attention: int
    awaiting_approval: int
    skipped: int


def finalize_run(
    ledger: Ledger,
    run_id: str,
    status: dict[str, str],
    *,
    reconcile: bool = False,
    root: Path | None = None,
    registry: "Registry | None" = None,
    extra_skipped: int = 0,
    finish_label: str = "run finished",
    finish_suffix: str = "",
    render_view: Callable[[str], None] | None = None,
) -> FinalizeOutcome:
    """The single close-out shared by ``run_build`` and ``run_resume`` (12.3-004).

    Computing the run terminal (including ``AWAITING_APPROVAL``), recomputing the
    counts, logging the finish event, stamping ``run_update_status`` and the
    optional registry, and — at one defined point — running reconciliation, all
    live here so the ``build`` and ``resume`` paths can never drift apart again.

    ``status`` is mutated in place: any story reconciliation flips to ``DONE`` is
    reflected for the caller's returned per-story map. ``reconcile`` gates the
    real-run-only reconciliation pass (callers pass ``dispatcher is None``);
    ``extra_skipped`` folds in shipped skips counted outside ``status`` (build's
    pre-loop ``done_skips``); ``finish_label``/``finish_suffix`` shape the event
    text; ``registry`` is stamped only on the build path that owns one.
    """
    # --- Reconcile parked stories against origin/main (single shared point) ---
    # Only on real runs (it does network/git I/O); injected fakes — the
    # controller's own orchestration tests — skip it. It never raises and never
    # fails an otherwise-good run.
    if reconcile:
        try:
            from sdlc.reconcile import reconcile_run

            recon = reconcile_run(ledger, run_id, root=root or Path.cwd(), fetch=True)
            for item in recon.reclassified:
                status[item["story_id"]] = "DONE"
        except Exception:  # belt-and-suspenders: never fail an otherwise-good run
            pass

    # --- Tally the final per-story outcomes ----------------------------------
    completed = sum(1 for v in status.values() if v == "DONE")
    failed = sum(1 for v in status.values() if v == "FAILED")
    blocked = sum(1 for v in status.values() if v == "BLOCKED")
    needs_attention = sum(1 for v in status.values() if v == "NEEDS_ATTENTION")
    awaiting_approval = sum(1 for v in status.values() if v == "AWAITING_APPROVAL")
    skipped = extra_skipped + sum(1 for v in status.values() if v == "SKIPPED")

    run_terminal = _run_terminal(failed, blocked, needs_attention, awaiting_approval)
    run_level = {
        "DONE": "success", "NEEDS_ATTENTION": "warn", "AWAITING_APPROVAL": "warn",
    }.get(run_terminal, "error")
    ledger.run_update_counts(run_id, completed, failed)
    ledger.event_log(
        run_id,
        "",
        run_level,
        "controller",
        f"{finish_label}: {completed} done, {failed} failed, {blocked} blocked, "
        f"{needs_attention} need attention, {awaiting_approval} awaiting approval, "
        f"{skipped} skipped{finish_suffix}",
    )
    ledger.run_update_status(run_id, run_terminal)
    try:  # best-effort lifecycle notification; never fail a run
        notify(
            "run_finished", run=run_id, terminal=run_terminal,
            done=completed, failed=failed, blocked=blocked,
            needs_attention=needs_attention, awaiting_approval=awaiting_approval,
            skipped=skipped,
        )
    except Exception:
        pass
    if registry is not None:
        _registry_finish(registry, run_id, run_terminal, completed)

    if render_view is not None:
        render_view(run_id)

    return FinalizeOutcome(
        run_terminal=run_terminal,
        completed=completed,
        failed=failed,
        blocked=blocked,
        needs_attention=needs_attention,
        awaiting_approval=awaiting_approval,
        skipped=skipped,
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
    start_escalation: int = 0,
    pr_number: int | None = None,
    bugfix_seq: int = 0,
    rl_ctx: "_RateLimitContext | None" = None,
    workdir: Path | None = None,
) -> str:
    """Drive one story through build → coverage → review → merge.

    Story 14.1-003: when ``rl_ctx`` is provided, a :class:`RateLimitError` raised
    by any stage/recovery dispatch is absorbed here as a recoverable, time-based
    pause rather than a stage failure: within the auto-wait cap the controller
    waits in-process and retries the *same* stage as a fresh attempt (the prior
    attempt's IN_PROGRESS row stays as a crashed attempt, so the PR/bugfix state
    and committed work are preserved); beyond the cap it raises
    :class:`_RateLimitPark` so the caller durably parks the run. A throttle thus
    never records a FAILED attempt nor enters the bugfix loop.

    Returns the terminal story status: ``DONE``, ``FAILED``,
    ``NEEDS_ATTENTION``, or ``AWAITING_APPROVAL``. A stage failure (agent FAILED
    status, dispatch error, or schema-invalid output) enters the bounded bugfix
    loop; the stage is retried after a successful fix. A merge blocked only by
    the high-risk human-approval gate short-circuits to ``AWAITING_APPROVAL``
    *before* the bugfix loop (Story 12.3-003) — it cannot self-approve. If a
    result is *unparseable* (contract error) but the
    agent already committed the story branch, the work is preserved as
    ``NEEDS_ATTENTION`` instead of being discarded and rebuilt (R10). Each
    dispatch's transcript is persisted under ``logs_dir`` and its path recorded
    on the stage row (R8).

    Resume parameters (Story 10.1-001) let the controller re-enter mid-story
    without rebuilding completed work: ``done_stages`` are pipeline stages with a
    recorded DONE attempt and are skipped; ``start_attempt`` is the attempt
    number for the first stage actually run (continuing past a crashed attempt);
    ``pr_number`` / ``bugfix_seq`` carry forward the run's prior PR and bugfix
    sequence. ``start_escalation`` (Story 14.2-003) carries the cheap-first model
    escalation level the first resumed stage had reached — the count of its prior
    FAILED attempts — so a stage that had climbed to a stronger tier before an
    interruption resumes on that tier rather than dropping back to its cheap base.
    The defaults reproduce a fresh full build exactly.

    ``workdir`` (Story 17.2-001) is the per-story git worktree the agent runs in.
    When set it is bound onto the dispatch seam as ``cwd`` so **every** dispatch
    for this story — each stage, the envelope re-ask, the commit-lint amend, and
    the bugfix loop — runs inside the isolated checkout. ``None`` keeps the
    shared-root path (sequential / back-compat) byte-for-byte unchanged.
    """
    if workdir is not None:
        dispatch = functools.partial(dispatch, cwd=workdir)
    stages = [s for s in _STAGES if not (s == "coverage" and opts.skip_coverage)]
    # Already-completed stages are skipped on resume; a fresh build skips none.
    pending = [s for s in stages if s not in done_stages]
    # Mark the story IN_PROGRESS the moment real work starts (Story 11.1-002):
    # without this a story goes straight TODO → terminal, so `sdlc status` /
    # the dashboard never see an in-flight window and never render its live
    # sub-stage activity (and the in_progress count stays 0 mid-run). The
    # terminal status is stamped by the caller once the story finishes; resume
    # is unaffected since it keys off stage rows, not this status, and treats
    # IN_PROGRESS as resumable.
    if pending:
        ledger.set_story_status(run_id, story.id, "IN_PROGRESS")
    # Story 22.4-002: the close-link for this story's mapped issue (``Closes #N``),
    # injected into the PR-opening stage's prompt so the merge auto-closes the
    # issue. Resolved once per story; None when the story has no mapped issue (the
    # common case today) or any host lookup fails — best-effort, never blocks.
    close_link = build_issue.close_link(ledger, story.id)
    # Story 23.2-001: the host-correct change-request phrasing (PR via gh / MR via
    # glab) and the default branch ``feature/<id>`` is cut from + the change
    # request targets. Resolved once per story from the story's mapped host and the
    # working tree's ``origin/HEAD``; both fall back to GitHub/``origin/main`` so an
    # unmapped story on GitHub is byte-identical to today (AC2).
    cr_terms = build_issue.change_request_terms(ledger, story.id)
    base_ref = _origin_default_ref(workdir or Path.cwd())
    # Monotonic across the whole story: the "bugfix" stage rows share one
    # (run_id, story_id, stage_name) key, so every bugfix dispatch — across both
    # retries of one stage and across different stages — needs a distinct attempt
    # number, or the second insert hits the stages UNIQUE constraint.

    rl_waited = 0  # cumulative in-process auto-wait across reactive pauses (14.1-003)
    last_status: str | None = None  # last status slug announced on the issue (22.4-002)
    for idx, stage in enumerate(pending):
        # Story 22.4-002: post a live status comment/label on the story's issue as
        # the build enters each stage (building → in-review → merging). Deduped
        # against the previous stage so build→coverage (both "building") is silent.
        # Best-effort and a no-op when the story has no mapped issue.
        slug = build_issue.stage_status(stage)
        if slug and slug != last_status:
            build_issue.announce_status(ledger, story.id, slug)
            last_status = slug
        bugfix_attempts = 0
        # Only the first resumed stage continues a prior attempt count; later
        # stages start fresh at attempt 1.
        attempt = start_attempt if idx == 0 else 1
        # Story 14.2-003: the cheap-first escalation level for this stage is its
        # prior FAILED-attempt count (carried only into the first resumed stage,
        # 0 on a fresh run) plus any bugfix attempts spent in *this* process. So a
        # stage that had already climbed a tier before an interruption resumes on
        # that tier, while later stages and fresh runs start cheap. This is a
        # display/routing offset only — the bounded bugfix budget (``bugfix_attempts``)
        # is unchanged, preserving its existing per-resume reset semantics.
        stage_escalation_base = start_escalation if idx == 0 else 0
        while True:
            # Issue #427: resolve the (harness, model) for this dispatch *before*
            # the ledger write and the estimate, so both record the identical
            # resolution the dispatch will use. escalation_steps (the cheap-first
            # retry lever, Story 14.2-003) drives model selection, so it must be
            # known here — see the block below where it is also consumed for the
            # retry-escalation log.
            escalation_steps = stage_escalation_base + bugfix_attempts
            stage_harness = _stage_harness(stage, opts)
            resolved_model = _resolved_stage_model(
                stage, story, opts, escalation_steps=escalation_steps
            )
            ledger.stage_start(
                run_id, story.id, stage, attempt,
                harness=stage_harness, model=resolved_model,
            )
            tpath = logs_dir / f"{story.id}-{stage}-{attempt}.log"
            sink = _make_progress_sink(ledger, run_id, story.id, stage, attempt)
            # Story 14.1-002: estimate this stage's usage before dispatch, record
            # it on the row, and (when a threshold is configured) warn — gating
            # the stage before any spend in interactive mode. Issue #427: the
            # estimate is harness+model-aware via the resolved (harness, model).
            estimate = _estimate_stage_cost(
                stage, story, opts, pr_number, ledger, run_id, attempt,
                harness=stage_harness, model=resolved_model,
            )
            if estimate is not None and _over_cost_threshold(estimate, opts):
                gated = not opts.auto
                ledger.event_log(
                    run_id, story.id, "warn", "controller",
                    f"{stage} estimate ~{estimate.estimated_tokens} tokens exceeds "
                    f"--cost-threshold={opts.cost_estimate_threshold} "
                    f"({notional_cost_label(estimate.estimated_cost_usd)}) — "
                    + (
                        "gating before dispatch; raise --cost-threshold or pass "
                        "--auto to proceed"
                        if gated
                        else "proceeding (--auto)"
                    ),
                )
                if gated:
                    ledger.stage_finish(
                        run_id, story.id, stage, attempt, "SKIPPED",
                        "cost-gate", str(tpath),
                    )
                    ledger.event_log(
                        run_id, story.id, "warn", "controller",
                        f"{stage} gated pre-dispatch: no agent dispatched, run "
                        "paused for review (R10: no work started, nothing discarded)",
                    )
                    # Pause the *run* resumably (IN_PROGRESS), not a terminal park:
                    # propagate to the cohort loop so `sdlc resume --cost-threshold`
                    # can raise the gate and continue the gated stage.
                    raise _CostGatePause(
                        story_id=story.id, stage=stage, estimate=estimate
                    )
            # Story 14.2-003: ``escalation_steps`` (computed above, before the
            # ledger write) is the cheap-first escalation level for this dispatch —
            # the bugfix attempts already spent on this stage (0 on the first,
            # cheap pass, +1 per retry) plus any tier already reached before a
            # resume (``stage_escalation_base``). A passing first attempt on a
            # fresh run therefore never escalates (the common path stays cheap);
            # only a stage that failed into the bugfix loop (or resumed mid-climb)
            # is retried one tier stronger.
            if escalation_steps > 0:
                esc_model = _select_stage_model(
                    stage, story, opts, escalation_steps=escalation_steps
                )
                ledger.event_log(
                    run_id, story.id, "info", "controller",
                    f"{stage} retry (attempt {attempt}) escalated to "
                    f"model={esc_model or 'cli-default'} after {escalation_steps} "
                    "failed attempt(s) (Story 14.2-003 cheap-first)",
                )
            try:
                # Story 23.2-002: gate the merge on the CR's CI/pipeline status.
                # A red/timed-out pipeline is a synthetic stage failure (kind
                # ``ci-gate``) so the existing bugfix loop tries to fix it before a
                # retry re-polls; a green/no-CI-allow/unmapped gate falls through to
                # the normal merge dispatch. The gate is a no-op for non-merge
                # stages, so build/coverage/review are unchanged.
                gate = _run_merge_ci_gate(
                    stage, ledger, run_id, story, pr_number, opts
                )
                if gate is not None and gate.verdict == _GATE_BLOCK:
                    ok, result, failure, kind = False, None, gate.reason, "ci-gate"
                else:
                    ok, result, failure, kind = _dispatch_stage(
                        stage, story, opts, pr_number, dispatch, tpath,
                        on_progress=sink, escalation_steps=escalation_steps,
                        close_link=close_link, cr_terms=cr_terms, base_ref=base_ref,
                    )
                if ok:
                    ledger.stage_finish(
                        run_id, story.id, stage, attempt, "DONE", output_path=str(tpath)
                    )
                    _record_stage_usage(ledger, run_id, story.id, stage, attempt, result)
                    # Story 14.1-002: reconcile the pre-dispatch estimate against
                    # the authoritative usage now known, for future calibration.
                    _reconcile_estimate(
                        ledger, run_id, story.id, stage, estimate, result
                    )
                    pr_number = _extract_pr(result, pr_number)
                    if pr_number is not None:
                        ledger.set_story_pr(run_id, story.id, pr_number)
                    # Story 23.2-003: a landed merge records its sha so the story
                    # is marked DONE *with* the GitLab/GitHub merge sha (AC3);
                    # no-op for non-merge stages.
                    _record_merge_landing(
                        stage, result, ledger, run_id, story, pr_number
                    )
                    # Story 12.2-002: lint the commit a commit-authoring stage just
                    # produced against commitlint before it can reach a PR; bounded
                    # amend re-ask on violation, no-op when there is no config /
                    # nothing to lint. Build and coverage both author commits on the
                    # story branch; review/merge do not. If the message is still
                    # non-compliant after the bounded re-asks, park the story rather
                    # than advance a known-non-compliant commit to review/merge/PR
                    # (work preserved on the branch, R10).
                    if stage in ("build", "coverage"):
                        bugfix_seq, lint_ok = _lint_stage_commit(
                            stage, story, opts, pr_number, ledger, run_id, dispatch,
                            logs_dir, bugfix_seq,
                        )
                        if not lint_ok:
                            return "NEEDS_ATTENTION"
                    # Issue #445: the over-engineering lens (Story 18.2-001) shipped
                    # but was never dispatched. Wire it here, advisory-only, right
                    # after a successful review — a best-effort side note that can
                    # never fail the stage or block the merge that follows.
                    if stage == "review":
                        _dispatch_overengineering_advisory(
                            story, pr_number, ledger, run_id
                        )
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

                # Story 25.1-001: the agent-text detection in _dispatch_stage is
                # advisory and proved path-dependent — a pre-dispatch CI-gate
                # block (kind="ci-gate") or an agent that omits block_reason
                # carries no marker at all (the exact epic-23 resume gap, run
                # 0541804d). Deterministically re-check any other merge failure
                # against the CR itself (risk labels + named check states) so a
                # gate-only block parks identically on build and resume.
                if (
                    stage == "merge"
                    and kind != "awaiting_approval"
                    and _merge_gate_only_block(ledger, run_id, story, pr_number)
                ):
                    kind = "awaiting_approval"

                # Story 12.3-003: a merge blocked only by the high-risk human-approval
                # gate is parked in a distinct AWAITING_APPROVAL terminal — *before*
                # any recovery. The bugfix loop cannot self-approve and would only
                # exhaust into FAILED, misreporting a run that is honestly
                # awaiting-human. The committed work / open PR are preserved (R10):
                # nothing is discarded or rebuilt. Reconciliation (12.3-001) flips it
                # to DONE once FX approves and the PR merges.
                if kind == "awaiting_approval":
                    ledger.event_log(
                        run_id, story.id, "warn", "controller",
                        f"merge blocked awaiting human approval (high-risk) — parking "
                        f"AWAITING_APPROVAL; PR/branch on feature/{story.id} preserved",
                    )
                    return "AWAITING_APPROVAL"

                # Story 12.1-001: a missing/malformed result envelope (contract
                # error) usually means the agent did good work but failed only to
                # emit the result block. Before any heavier recovery, issue a cheap,
                # bounded envelope-only re-ask (AC1). On success the stage is treated
                # as DONE and the run proceeds exactly as if the agent had emitted the
                # block the first time (AC4); committed work is never discarded (R10).
                if kind == "contract":
                    bugfix_seq += 1
                    rpath = logs_dir / f"{story.id}-reask-{stage}-{bugfix_seq}.log"
                    ok_r, result_r = _reask_envelope(
                        stage, story, opts, pr_number, ledger, run_id, dispatch,
                        rpath, bugfix_seq,
                    )
                    if ok_r:
                        ledger.stage_finish(
                            run_id, story.id, stage, attempt, "DONE",
                            output_path=str(rpath),
                        )
                        _record_stage_usage(
                            ledger, run_id, story.id, stage, attempt, result_r
                        )
                        pr_number = _extract_pr(result_r, pr_number)
                        if pr_number is not None:
                            ledger.set_story_pr(run_id, story.id, pr_number)
                        # Story 23.2-003: an envelope-recovered merge still landed —
                        # record its sha so the DONE story carries the merge sha (AC3).
                        _record_merge_landing(
                            stage, result_r, ledger, run_id, story, pr_number
                        )
                        # Story 12.2-002: an envelope-recovered stage committed work
                        # just like a first-pass success — lint its commit too, or
                        # an envelope-only failure would smuggle a non-compliant
                        # header straight to the PR. Park on exhausted re-asks (R10).
                        if stage in ("build", "coverage"):
                            bugfix_seq, lint_ok = _lint_stage_commit(
                                stage, story, opts, pr_number, ledger, run_id,
                                dispatch, logs_dir, bugfix_seq,
                            )
                            if not lint_ok:
                                return "NEEDS_ATTENTION"
                        break

                if bugfix_attempts >= MAX_BUGFIX_ATTEMPTS:
                    # Recovery exhausted (AC2). R10: never discard committed work —
                    # if the agent already committed the story branch, park it for
                    # manual push/MR rather than reporting an outright failure.
                    return _exhausted_status(
                        kind, stage, story.id, pr_number, ledger, run_id
                    )

                bugfix_attempts += 1
                bugfix_seq += 1
                bpath = logs_dir / f"{story.id}-bugfix-{stage}-{bugfix_seq}.log"
                if not _run_bugfix(
                    story, stage, failure, opts, ledger, run_id, dispatch,
                    bpath, bugfix_seq,
                    escalation_steps=stage_escalation_base + bugfix_attempts,
                ):
                    return _exhausted_status(
                        kind, stage, story.id, pr_number, ledger, run_id
                    )
                # Story 12.2-002: the bugfix agent authors a commit too — lint its
                # message and amend early. This is best-effort (no park): the stage
                # is about to be retried, and that retry's own success-time lint is
                # the terminal gate that parks a still-non-compliant commit.
                bugfix_seq, _ = _lint_stage_commit(
                    "bugfix", story, opts, pr_number, ledger, run_id, dispatch,
                    logs_dir, bugfix_seq,
                )
                # Bugfix succeeded — retry the same stage as a new attempt.
                attempt += 1
            except ContextOverflowError as exc:
                # Issue #104: the agent's prompt exceeded the model context
                # window. A fresh dispatch cannot shrink the in-session context,
                # so the bugfix loop would only re-overflow — fail the stage fast
                # with a distinct failure_category="context-overflow". Prompt
                # reductions (merge-update-prompt.md) prevent recurrence; any
                # committed work on the branch is preserved (R10).
                ledger.stage_finish(
                    run_id, story.id, stage, attempt, "FAILED",
                    "context-overflow", str(tpath),
                )
                ledger.event_log(
                    run_id, story.id, "error", "controller",
                    f"{stage} failed: context window exceeded — failing fast "
                    f"(no bugfix loop): {exc}",
                )
                return "FAILED"
            except RateLimitError as exc:
                # Story 14.1-003: a Max rate-limit hit anywhere in this stage's
                # dispatch/recovery is a recoverable, time-based pause — never a
                # stage FAILED. The interrupted attempt's IN_PROGRESS row is left as
                # a crashed attempt; we wait in-process (within the cap) and retry
                # the same stage as a *fresh* attempt, or escalate _RateLimitPark
                # (beyond the cap) so the caller durably parks the run.
                if rl_ctx is None:
                    raise
                wait_s = seconds_until_reset(
                    exc.signal, now=rl_ctx.clock(), window_s=rl_ctx.opts.window_s
                )
                if not within_wait_cap(wait_s, rl_ctx.opts.rate_limit_max_wait_s):
                    raise _RateLimitPark(signal=exc.signal, waited_s=rl_waited) from exc
                rl_waited += _rate_limit_wait(
                    ledger, run_id, exc.signal, wait_s, sleep_fn=rl_ctx.sleep_fn
                )
                if rl_ctx.window is not None:
                    rl_ctx.window.reopen(
                        rl_ctx.clock(), ledger.run_usage_totals(run_id)["tokens"]
                    )
                attempt += 1
                continue

    return "DONE"


def _exhausted_status(
    kind: str,
    stage: str,
    story_id: str,
    pr_number: int | None,
    ledger: Ledger,
    run_id: str,
) -> str:
    """Terminal status once bounded recovery is exhausted (Story 12.1-001 AC2).

    R10: a contract failure (missing/malformed envelope) whose stage already
    produced its expected git artifact is parked ``NEEDS_ATTENTION`` for manual
    push/MR — committed work is never discarded. The artifact probe is
    stage-aware (#232): ``build``/``coverage``/``review`` look for a commit on
    ``feature/<id>``; ``merge`` looks for a landed PR via reconcile's
    ``_detect_landing`` (a commit-ahead probe cannot see a merged-away branch).
    Any other exhausted failure (a ``dispatch``/``reported`` kind, or a contract
    failure with no artifact) is an outright ``FAILED``, unchanged from before —
    a genuine no-work failure is never masked. The parking decision is recorded
    in the ledger events.
    """
    if kind == "contract" and _stage_artifact_exists(stage, story_id, pr_number):
        ledger.event_log(
            run_id, story_id, "warn", "controller",
            f"recovery exhausted but work committed on feature/{story_id} — "
            "preserved for manual push/MR (no further re-run)",
        )
        return "NEEDS_ATTENTION"
    return "FAILED"


def _make_progress_sink(
    ledger: Ledger, run_id: str, story_id: str, stage: str, attempt: int = 1
):
    """A best-effort sink that maps stream events → coalesced ledger progress rows.

    Returned callable is handed to ``dispatch_agent(on_progress=…)`` (Story
    11.1-002). It maps each stream-json event to zero or more
    :class:`~sdlc.progress.ProgressEvent`, rate-limits/de-dupes them through a
    per-stage :class:`~sdlc.progress.ProgressCoalescer`, and appends the
    survivors to the ledger. Coalescing keeps ledger writes infrequent so the
    agent stream is never materially blocked; dispatch already isolates any
    exception raised here so progress recording can never fail the run.

    It also accrues running token usage (Story 11.1-003): each usage-bearing
    event folds into a :class:`~sdlc.progress.UsageAccumulator`, and the new
    running total is written to this stage attempt's row, so a mid-stage query
    sees spend building up. The stage's terminal :func:`_record_stage_usage`
    later overwrites this with the authoritative envelope figure — final value
    wins, no double counting (both write the same columns via
    :meth:`Ledger.stage_set_usage`).
    """
    coalescer = ProgressCoalescer()
    usage = UsageAccumulator()

    def sink(event: dict) -> None:
        now = time.monotonic()
        for pe in map_stream_event(event):
            if coalescer.admit(pe, now):
                ledger.progress_log(run_id, story_id, stage, pe.kind, pe.message)
        if usage.observe(event):
            t = usage.totals
            ledger.stage_set_usage(
                run_id, story_id, stage, attempt,
                session_id=t.session_id,
                input_tokens=t.input_tokens,
                output_tokens=t.output_tokens,
                cache_read_tokens=t.cache_read_tokens,
                cache_creation_tokens=t.cache_creation_tokens,
                cost_usd=None,
            )

    return sink


# Story 14.2-001: the stages this controller actually dispatches and therefore
# routes — the four pipeline stages plus the bugfix/reask recovery agents (all
# go through the dispatch seam and receive a routed `--model`). A
# `--model-<stage>` CLI override is accepted only for these, so an override for a
# stage routing can't honour is a hard error rather than a silent no-op.
# `discovery` and `adversarial` are dispatched outside this pipeline (the
# discovery agent and the standalone adversarial-reviewer slot), so they are
# deliberately excluded here even though the profile map still defines their
# tiers for `select_model` / the adversarial Opus pin.
_ROUTABLE_STAGES = frozenset(
    {"build", "coverage", "review", "merge", "bugfix", "reask"}
)

# Story 20.2-002: map a dispatch stage to the pipeline role whose harness runs it
# so the ledger can record *which* harness executed each stage. The pipeline
# stages (`_STAGES`) map 1:1 to roles; the recovery stages (bugfix/reask/
# commitlint) re-dispatch the *originating* stage's agent, so callers pass that
# originating stage here rather than the recovery stage's own name. Any unknown
# stage falls back to the build role — the conservative default for a heavy
# code-producing dispatch.
_STAGE_ROLE: dict[str, str] = {
    "build": "build",
    "coverage": "coverage",
    "review": "review",
    "merge": "merge",
    "docs": "docs",
}


def _stage_harness(stage: str, opts: BuildOptions) -> str:
    """The harness name that runs ``stage`` (Story 20.2-002).

    Resolves the stage's pipeline role and looks it up in the run's per-role
    ``--harness`` map (Story 20.2-001). A role absent from the map collapses to
    the built-in default ``claude`` — today's behaviour — so a run with no
    ``--harness`` flag records every stage as ``claude``.
    """
    role = _STAGE_ROLE.get(stage, "build")
    return opts.harness_map.get(role) or DEFAULT_HARNESS


def _harness_dispatch_kwargs(
    harness_stage: str, opts: BuildOptions, model: str | None
) -> dict[str, object]:
    """Per-role ``--harness`` routing → dispatch kwargs (Story 20.7-001).

    Resolves the harness that runs ``harness_stage`` (the same role lookup
    :func:`_stage_harness` records in the ledger) and returns the
    ``agent_cmd``/``parser`` that route the dispatch to it — so a role mapped to
    a registry harness (e.g. ``codex``) *actually runs* that harness's argv with
    its declared parser, not just a label in the ledger. The ``builtin``/``env``
    Claude slots return ``parser=None`` so dispatch keeps its stream-json
    default, and ``model`` still decorates the Claude argv via
    :meth:`HarnessConfig.to_argv`. A registry harness owns its own argv: it
    ignores the Claude tier alias in ``model`` and instead routes *its own* model
    per stage — ``harness_stage`` is threaded into :meth:`HarnessConfig.to_argv`
    so a registry entry with a ``{model}`` placeholder launches with the model its
    ``models`` map assigns this stage (Story 20.7-004).

    Returns an **empty dict** when no ``--harness`` map is set, so the default
    path passes no ``agent_cmd``/``parser`` and dispatch is byte-identical to
    today (AC3). The registry is the repo's checked-in
    ``controller/config/harnesses.yaml`` — already validated in ``cli.py`` before
    any stage runs; an absent registry resolves the built-in Claude default.
    """
    if not opts.harness_map:
        return {}
    from sdlc.role_routing import default_registry_path

    harness = resolve_harness(
        _stage_harness(harness_stage, opts), config_path=default_registry_path()
    )
    return {
        "agent_cmd": harness.to_argv(model=model, stage=harness_stage),
        "parser": None if harness.source in ("builtin", "env") else harness.parser,
    }


# Story 14.2-001: stages whose changed-files risk signal is *stable* at the
# moment their model is chosen — the story branch is already pushed, so the same
# diff (and therefore the same risk verdict) is seen on the original run and on a
# resume. `build` is deliberately excluded: its branch does not yet exist when
# its model is chosen on a fresh run, so a live-git lookup would return False on
# first build but True on resume (the branch now exists), silently changing the
# routed model between the two. `build` therefore escalates on **points** only —
# a spec-derived signal identical across build and resume — keeping routing
# deterministic. `review` escalation still uses the risk signal (stable there).
_RISK_AWARE_STAGES = frozenset({"review"})


def _routing_config_for(opts: BuildOptions) -> ModelRoutingConfig | None:
    """Resolve (and memoize) the run's model-routing config (Story 14.2-001).

    Returns None when routing is off. The per-repo override file
    (``.sdlc-model-routing.yaml`` at the working tree root) is read at most once
    per run — the result is cached on ``opts._model_config`` — so per-stage
    selection stays cheap. A run with no profile never touches disk.
    """
    cached = opts._model_config
    if cached is not _UNRESOLVED:
        return cached  # type: ignore[return-value]
    config = load_routing_config(
        opts.model_profile, override_path=Path(MODEL_ROUTING_OVERRIDE_FILENAME)
    )
    opts._model_config = config
    return config


def _story_high_risk(story: Story, opts: BuildOptions) -> bool:
    """Best-effort: does this story touch a high-risk path (Epic-08 risk_gate)?

    Used to escalate the **review** model to Opus (Story 14.2-001). The signal is
    the changed files on the story branch matched against the risk-gate patterns.
    It is consulted only for stages where the branch is already pushed (see
    ``_RISK_AWARE_STAGES``), so the same verdict is reached on the original run
    and on a resume — routing never silently changes across a resume. Entirely
    best-effort: any git/import error degrades to False so routing never fails a
    build, and it is a no-op when routing is off.
    """
    if opts.model_profile.strip().lower() in {"", "off", "none"}:
        return False
    try:
        from sdlc.risk_gate import match_high_risk

        changed = subprocess.run(
            ["git", "diff", "--name-only", f"origin/main...feature/{story.id}"],
            capture_output=True, text=True, timeout=30,
        )
        files = [ln for ln in changed.stdout.splitlines() if ln.strip()]
        if not files:
            return False
        return bool(match_high_risk(files))
    except Exception:  # noqa: BLE001 - risk detection is strictly best-effort
        return False


def _select_stage_model(
    stage: str, story: Story, opts: BuildOptions, *, escalation_steps: int = 0
) -> str | None:
    """Pick the model for ``stage`` (Story 14.2-001), or None for the CLI default.

    Precedence: an explicit ``--model-<stage>`` override wins over the map (and is
    an operator pin — never escalated); then the routing profile's
    :func:`select_model` (with the story's points and a best-effort high-risk
    signal driving build/review escalation); else None when routing is off — in
    which case the dispatcher adds no ``--model`` and behaviour is unchanged from
    today.

    ``escalation_steps`` (Story 14.2-003) bumps the mapped tier up the ladder by
    that many steps, capped at the strongest tier — the cheap-first retry lever.
    It defaults to 0 (the common passing path), so the first dispatch of every
    stage runs on its cheap mapped tier; only a bugfix retry passes a positive
    count. An explicit override and the routing-off path both ignore it, so an
    operator pin and today's behaviour are never silently escalated.
    """
    override = opts.model_overrides.get(stage)
    if override:
        return override
    config = _routing_config_for(opts)
    if config is None:
        return None
    # The file-based risk signal is consulted only where the diff is stable at
    # decision time (review), so a resume routes identically to the original run;
    # build escalates on points alone. See _RISK_AWARE_STAGES.
    high_risk = _story_high_risk(story, opts) if stage in _RISK_AWARE_STAGES else False
    base = select_model(stage, config, points=story.points, high_risk=high_risk)
    return escalate_model(base, escalation_steps)


def _resolved_stage_model(
    stage: str, story: Story, opts: BuildOptions, *, escalation_steps: int = 0
) -> str | None:
    """The model id the harness that runs ``stage`` will actually use (Issue #427).

    For the built-in/env Claude slot this is the routed tier alias from
    :func:`_select_stage_model`; a registry harness (e.g. ``codex``) owns its own
    per-stage model map, so :meth:`HarnessConfig.resolve_model` wins there. Shared
    by dispatch (:func:`_dispatch_stage`) and the pre-dispatch estimate/ledger
    write so the two can never resolve a different model for the same stage. The
    common Claude-only path (no ``--harness`` map) never touches the registry.
    Best-effort on the registry path: any resolve error degrades to the routed
    Claude model rather than failing a build.
    """
    claude_model = _select_stage_model(
        stage, story, opts, escalation_steps=escalation_steps
    )
    if not opts.harness_map:
        return claude_model
    try:
        from sdlc.role_routing import default_registry_path

        harness = resolve_harness(
            _stage_harness(stage, opts), config_path=default_registry_path()
        )
    except Exception:  # noqa: BLE001 - registry resolution is best-effort
        return claude_model
    if harness.source in ("builtin", "env"):
        return claude_model
    return harness.resolve_model(stage)


def _model_price_key(model: str | None) -> str:
    """Normalise a resolved model id to a ``MODEL_USD_PER_MILLION_TOKENS`` key.

    Routing yields the Claude tier aliases (``haiku``/``sonnet``/``opus``)
    directly, but an operator pin or per-repo override can name a full id (e.g.
    ``claude-opus-4-8``); match those by the tier substring. A registry harness's
    own model id (e.g. a Codex ``gpt-*``) matches no tier and returns unchanged,
    so the rate lookup falls through to ``DEFAULT_USD_PER_MILLION_TOKENS``.
    """
    if not model:
        return ""
    lowered = model.lower()
    for tier in ("haiku", "sonnet", "opus"):
        if tier in lowered:
            return tier
    return model


def _dispatch_stage(
    stage: str,
    story: Story,
    opts: BuildOptions,
    pr_number: int | None,
    dispatch: Dispatcher,
    transcript_path: Path | None = None,
    on_progress=None,
    *,
    escalation_steps: int = 0,
    close_link: str | None = None,
    cr_terms: ChangeRequestTerms = GITHUB_CR_TERMS,
    base_ref: str = "origin/main",
) -> tuple[bool, AgentResult | None, str, str]:
    """Dispatch one stage's agent and classify the outcome.

    Returns ``(ok, result, failure_summary, kind)``. ``ok`` is False on a
    dispatch error, a schema-invalid response (caught here, never passed
    downstream), or an agent that reported a non-success status for its stage.
    ``kind`` names the failure cause — ``"contract"`` / ``"dispatch"`` /
    ``"reported"`` (empty on success) — so the caller can treat an unparseable
    result that nonetheless committed work as recoverable rather than discard it
    (R10). ``on_progress`` (Story 11.1-002) is forwarded to the dispatcher so the
    streamed stage emits sub-stage progress to the ledger.
    """
    prompt = _render_stage_prompt(
        stage, story, opts, pr_number, close_link=close_link,
        cr_terms=cr_terms, base_ref=base_ref,
    )
    # Issue #427: resolve the effective model through the shared helper so
    # dispatch and the pre-dispatch estimate/ledger write can never diverge. For
    # the built-in/env Claude slot this is the routed tier alias (identical to the
    # prior _select_stage_model call); a registry harness's own model id is inert
    # here — to_argv (registry branch) and resolve_agent_cmd (agent_cmd present)
    # both ignore the passed model — so dispatch stays byte-identical.
    model = _resolved_stage_model(stage, story, opts, escalation_steps=escalation_steps)
    # Story 20.7-001: route this stage to its mapped harness's argv/parser when a
    # `--harness` map is set; empty (the default path) leaves dispatch unchanged.
    harness_kwargs = _harness_dispatch_kwargs(stage, opts, model)
    try:
        result = dispatch(
            stage, prompt, story=story, model=model,
            transcript_path=transcript_path, on_progress=on_progress,
            **harness_kwargs,
        )
    except RateLimitError:
        # Story 14.1-003: a Max rate-limit hit is a recoverable, time-based pause,
        # NOT a stage failure — let it propagate past the bugfix loop so a throttle
        # never burns a bugfix attempt. The caller (run_build/run_resume) waits or
        # durably parks. Re-raised before the AgentDispatchError catch below since
        # RateLimitError subclasses it.
        raise
    except ContextOverflowError:
        # Issue #104: a context-window overflow is unshrinkable in-session — let
        # it propagate past the generic AgentDispatchError catch so _run_story
        # fails the stage fast instead of re-overflowing through the bugfix loop.
        # Re-raised before the AgentDispatchError catch since it subclasses it.
        raise
    except ContractError as exc:
        # Malformed / schema-invalid agent output is a build failure.
        return False, None, f"contract violation: {exc}", "contract"
    except AgentDispatchError as exc:
        return False, None, f"dispatch error: {exc}", "dispatch"

    if not _stage_succeeded(stage, result.data):
        # Story 12.3-003: a merge blocked only by the high-risk human-approval
        # gate is not a generic stage failure — it is a run waiting on FX. Tag
        # it ``awaiting_approval`` so the caller short-circuits before the bugfix
        # loop (which cannot self-approve) and parks it as a distinct,
        # non-FAILED terminal rather than exhausting into FAILED.
        kind = (
            "awaiting_approval"
            if _merge_awaiting_approval(stage, result.data)
            else "reported"
        )
        return False, result, _stage_failure_summary(stage, result.data), kind
    return True, result, "", ""


def _render_stage_prompt(
    stage: str,
    story: Story,
    opts: BuildOptions,
    pr_number: int | None,
    close_link: str | None = None,
    *,
    cr_terms: ChangeRequestTerms = GITHUB_CR_TERMS,
    base_ref: str = "origin/main",
) -> str:
    if stage == "build":
        return render_build_prompt(
            story, opts, close_link=close_link, cr_terms=cr_terms, base_ref=base_ref
        )
    if stage == "coverage":
        return render_coverage_prompt(story, opts, close_link=close_link, cr_terms=cr_terms)
    if stage == "review":
        return render_review_prompt(story, pr_number)
    return render_merge_prompt(story, pr_number, cr_terms=cr_terms)


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
    if stage == "merge" and _merge_awaiting_approval(stage, data):
        return "merge blocked awaiting human approval (high-risk gate)"
    return f"{stage} reported non-success status"


# Markers a merge agent uses to signal a high-risk human-approval block. The
# merge schema enum is only MERGED|FAILED|SKIPPED (re-enumerating it is a
# non-goal), so the block is surfaced *additively* — a ``block_reason`` field
# (extra properties are allowed) and/or the marker named in free text.
_AWAITING_APPROVAL_MARKERS = ("BLOCKED_HIGH_RISK", "AWAITING_APPROVAL")
# Free-text fields a merge agent might name the block in when it omits the
# explicit ``block_reason`` field.
_BLOCK_TEXT_FIELDS = ("block_reason", "error_summary", "detail", "summary", "notes")


def _merge_awaiting_approval(stage: str, data: dict) -> bool:
    """True when a non-success merge response is a high-risk approval block (12.3-003).

    A PR carrying ``risk:high`` (from ``risk_gate.py`` /
    ``.github/workflows/risk-gate.yml``) with no ``risk-approved`` label or
    ``risk-approver`` review is blocked pending FX's manual approval, not a
    fixable failure. Only the ``merge`` stage can be awaiting approval. The
    primary signal is an additive ``block_reason`` field equal to a known marker
    (case-insensitive); a free-text fallback recognizes the marker embedded in
    other reason fields, so the signal survives even when the agent narrates it.
    """
    if stage != "merge":
        return False
    reason = str(data.get("block_reason", "")).strip().upper()
    if reason in _AWAITING_APPROVAL_MARKERS:
        return True
    haystack = " ".join(str(data.get(k, "")) for k in _BLOCK_TEXT_FIELDS).upper()
    return any(marker in haystack for marker in _AWAITING_APPROVAL_MARKERS)


# The check/job name the high-risk gate publishes on a change request, per
# host: the GitHub workflow job (`.github/workflows/risk-gate.yml` →
# `name: High-risk file approval gate`) and the GitLab CI template job
# (`templates/gitlab-ci.yml` → `risk-gate`). Matched case-insensitively.
_GATE_CHECK_NAMES = frozenset({"high-risk file approval gate", "risk-gate"})


def _gate_only_block(view: ChangeRequestChecks) -> bool:
    """True when a CR's own state says it is blocked *solely* by the high-risk gate.

    Story 25.1-001: the deterministic, host-side complement to
    :func:`_merge_awaiting_approval`. The agent-text detection proved
    path-dependent — on the epic-23 resume (run ``0541804d``) the gate check was
    already red when the merge stage re-entered, so the pre-dispatch CI-gate
    block (Story 23.2-002) fired first and no agent ``block_reason`` ever
    existed to parse. The authoritative signal is the CR itself: the
    ``risk:high`` label with no ``risk-approved`` label, every failing check
    being the gate's own check, and no *other* check still pending (an
    in-flight check could yet fail — parking then would be a false positive;
    the normal failure path re-polls instead).
    """
    labels = {label.strip().lower() for label in view.labels}
    if RISK_LABEL not in labels or RISK_APPROVED_LABEL in labels:
        return False
    failing = [name for name, status in view.checks if status == CR_FAILED]
    if not failing:
        return False
    if any(name.strip().lower() not in _GATE_CHECK_NAMES for name in failing):
        return False
    return not any(
        status == CR_PENDING and name.strip().lower() not in _GATE_CHECK_NAMES
        for name, status in view.checks
    )


def _merge_gate_only_block(
    ledger: Ledger, run_id: str, story: Story, pr_number: int | None
) -> bool:
    """Re-check a failed merge against the CR's own gate state (Story 25.1-001).

    Reads the CR's labels + named check states through the best-effort
    :func:`build_issue.change_request_checks` seam and applies
    :func:`_gate_only_block`. An unmapped story or any host failure reads as
    False, so the existing failure routing is unchanged whenever the CR cannot
    be consulted.
    """
    if pr_number is None:
        return False
    view = build_issue.change_request_checks(ledger, story.id, pr_number)
    if view is None or not _gate_only_block(view):
        return False
    ledger.event_log(
        run_id, story.id, "info", "controller",
        "merge re-check: CR blocked solely by the high-risk approval gate "
        f"(risk:high unapproved, cr=#{pr_number}) — treating as awaiting approval",
    )
    return True


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


# Map of the agent envelope's usage keys (Story 14.1-002 reconciliation reads the
# same keys _record_stage_usage maps to the ledger columns).
_RESULT_USAGE_KEYS = (
    "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens",
)


def _result_total_tokens(result: AgentResult | None) -> int | None:
    """Sum the four token components of an AgentResult's usage envelope, or None.

    None (not 0) means the agent carried no usage (plain-text custom agent), so
    reconciliation is skipped rather than comparing against a misleading zero.
    """
    if result is None or result.usage is None:
        return None
    vals = [result.usage.get(k) for k in _RESULT_USAGE_KEYS]
    if all(v is None for v in vals):
        return None
    return sum(int(v or 0) for v in vals)


def _estimate_stage_cost(
    stage: str,
    story: Story,
    opts: BuildOptions,
    pr_number: int | None,
    ledger: Ledger,
    run_id: str,
    attempt: int,
    *,
    harness: str | None = None,
    model: str | None = None,
) -> StageEstimate | None:
    """Estimate + record a stage's usage before dispatch (Story 14.1-002).

    Renders the same prompt the dispatcher will send, estimates its total usage
    (calibrating against the ledger's historical per-stage average when present),
    records the estimate on the stage row, and logs a notional-$ info event.
    Issue #427: the token forecast calibrates from same-``harness``+``model``
    history via :meth:`Ledger.historical_stage_tokens`'s fallback ladder, and the
    notional dollar figure uses the resolved model's blended rate instead of the
    flat default. Entirely best-effort: any failure (render/DB error) degrades to
    None so a bad estimate never breaks a build — the stage dispatches exactly as
    today.
    """
    try:
        prompt = _render_stage_prompt(stage, story, opts, pr_number)
        calibration = ledger.historical_stage_tokens(
            stage, harness=harness, model=model
        )
        historical = calibration[0] if calibration is not None else None
        rate = MODEL_USD_PER_MILLION_TOKENS.get(
            _model_price_key(model), DEFAULT_USD_PER_MILLION_TOKENS
        )
        est = estimate_stage(
            stage, prompt,
            config=CostEstimateConfig(usd_per_million_tokens=rate),
            historical_tokens=historical,
        )
        ledger.stage_set_estimate(
            run_id, story.id, stage, attempt,
            estimated_tokens=est.estimated_tokens,
            estimated_cost_usd=est.estimated_cost_usd,
        )
        suffix = ""
        if est.calibrated and calibration is not None:
            suffix = f" [calibrated from history: {calibration[1]}]"
        ledger.event_log(
            run_id, story.id, "info", "controller",
            f"{stage} pre-dispatch estimate: ~{est.estimated_tokens} tokens "
            f"({notional_cost_label(est.estimated_cost_usd)})"
            + suffix,
        )
        return est
    except Exception:  # noqa: BLE001 - estimation is best-effort, never fatal
        return None


def _over_cost_threshold(
    estimate: StageEstimate | None, opts: BuildOptions
) -> bool:
    """Whether a stage estimate crosses the configured per-stage threshold.

    A threshold of 0 (the default) means "no gate" — the estimate is still
    computed and recorded, but never warns or gates (behaviour unchanged).
    """
    return (
        estimate is not None
        and opts.cost_estimate_threshold > 0
        and estimate.estimated_tokens >= opts.cost_estimate_threshold
    )


def _reconcile_estimate(
    ledger: Ledger,
    run_id: str,
    story_id: str,
    stage: str,
    estimate: StageEstimate | None,
    result: AgentResult | None,
) -> None:
    """Log estimate-vs-actual once the authoritative usage is known (14.1-002).

    The persisted reconciliation is the estimate + actual columns on the same
    stage row (written by :meth:`Ledger.stage_set_estimate` /
    :meth:`Ledger.stage_set_usage`); this surfaces the delta as a calibration
    event. No-op when there is no estimate or the agent carried no usage.
    """
    if estimate is None:
        return
    actual = _result_total_tokens(result)
    if actual is None:
        return
    pct = (
        (actual - estimate.estimated_tokens) / estimate.estimated_tokens * 100.0
        if estimate.estimated_tokens
        else 0.0
    )
    ledger.event_log(
        run_id, story_id, "info", "controller",
        f"{stage} estimate reconciled: est ~{estimate.estimated_tokens} vs "
        f"actual {actual} tokens ({pct:+.0f}%)",
    )


def _extract_pr(result: AgentResult | None, current: int | None) -> int | None:
    if result is None:
        return current
    pr = result.data.get("pr_number")
    return pr if isinstance(pr, int) else current


def _extract_merge_sha(result: AgentResult | None) -> str | None:
    """The merge commit sha from a successful merge agent response, or None (23.2-003).

    The merge schema requires a non-empty ``merge_sha`` on a ``MERGED`` outcome;
    a FAILED/SKIPPED response carries an empty string. Returns the trimmed sha
    only when it is a non-blank string so the ledger records a real landing sha
    and never an empty one.
    """
    if result is None:
        return None
    sha = result.data.get("merge_sha")
    if isinstance(sha, str) and sha.strip():
        return sha.strip()
    return None


def _record_merge_landing(
    stage: str,
    result: AgentResult | None,
    ledger: Ledger,
    run_id: str,
    story: Story,
    pr_number: int | None,
) -> None:
    """Stamp a landed merge's sha onto the story row (Story 23.2-003 AC3).

    A no-op for any stage other than ``merge`` or when the merge agent reported
    no sha, so every non-merge stage and a non-MERGED outcome are unchanged. When
    a merge lands, the caller is about to mark the story DONE; this records the
    GitLab/GitHub merge sha (and logs the landing) so the ledger marks the story
    DONE *with* the merge sha. The story's issue auto-closes via the ``Closes #N``
    injected at create time (Story 22.4-002), so no separate close call is needed
    here (AC1); branch teardown is handled by the existing worktree/branch GC
    (``hooks/worktree-gc.sh`` / :func:`remove_story_worktree`), host-agnostic
    local git, so it works unchanged on a GitLab target (AC2).
    """
    if stage != "merge":
        return
    sha = _extract_merge_sha(result)
    if not sha:
        return
    ledger.set_story_merge_sha(run_id, story.id, sha)
    cr = f" (cr=#{pr_number})" if pr_number is not None else ""
    ledger.event_log(
        run_id, story.id, "info", "controller",
        f"merge landed: story DONE at {sha}{cr}",
    )


def _dispatch_overengineering_advisory(
    story: Story, pr_number: int | None, ledger: Ledger, run_id: str
) -> None:
    """Advisory-only over-engineering lens dispatch after a successful review (#445).

    Story 18.2-001 shipped the lens (config, prompt, schema, policy routing) but
    nothing dispatched it. This wires it in: disabled by default (the bundled
    ``overengineering-lens.yaml``'s ``enabled: false``), so an un-opted-in run is
    byte-for-byte unchanged — :func:`dispatch_overengineering_lens` itself
    short-circuits without spending any quota when disabled. When enabled, a
    non-empty delete-list is recorded as a ledger event only; it is *never*
    surfaced as a stage failure and never gates the merge that follows,
    regardless of the configured policy (``route_to_simplify`` is honoured only
    as far as labelling the log line — routing cuts into the bugfix loop is
    deliberately left for a future story, not done here).

    A no-op when there is no PR yet (nothing for the lens to review) or the
    bundled config is missing. Any error loading the config, invoking the lens,
    or parsing its response is caught and logged rather than raised — this is a
    best-effort side note, not a gate, so it must never fail the review stage.
    """
    if pr_number is None:
        return
    from sdlc.role_routing import bundled_config_path

    config_path = bundled_config_path("overengineering-lens.yaml")
    if config_path is None:
        return

    from sdlc.overengineering import (
        OverEngineeringContractError,
        OverEngineeringError,
        dispatch_overengineering_lens,
    )

    try:
        outcome = dispatch_overengineering_lens(
            pr_number=pr_number, story_id=story.id, diff="", config_path=config_path,
        )
    except (OverEngineeringError, OverEngineeringContractError) as exc:
        ledger.event_log(
            run_id, story.id, "warn", "controller",
            f"over-engineering lens failed (advisory-only, ignored): {exc}",
        )
        return
    except Exception as exc:  # noqa: BLE001 - advisory-only, must never fail the stage
        ledger.event_log(
            run_id, story.id, "warn", "controller",
            f"over-engineering lens raised an unexpected error (advisory-only, "
            f"ignored): {exc}",
        )
        return

    if not outcome.has_findings:
        return  # disabled or clean — stay quiet, no ledger noise
    ledger.event_log(
        run_id, story.id, "info", "controller",
        f"over-engineering lens ({outcome.action}): {outcome.advisory_comment()}",
    )


def _reask_envelope(
    stage: str,
    story: Story,
    opts: BuildOptions,
    pr_number: int | None,
    ledger: Ledger,
    run_id: str,
    dispatch: Dispatcher,
    transcript_path: Path | None,
    seq: int,
) -> tuple[bool, AgentResult | None]:
    """Bounded envelope-only re-ask after a missing/malformed result block (12.1-001).

    The agent likely did good ``stage`` work but failed only to emit a valid
    ``<<<RESULT_JSON>>>`` block. Before any heavier recovery, re-prompt the same
    stage agent to emit just the result block for the work it already did. This
    is cheaper than a full stage re-run and never discards committed work (R10).

    Returns ``(ok, result)``. ``ok`` is True only when the re-ask yields a
    schema-valid response that reports success for ``stage`` — then the caller
    treats the stage as DONE and proceeds (AC4). A dispatch/contract error or a
    non-success status is a failed recovery (``ok=False``), not fatal: the caller
    falls through to the bugfix path (AC2). The attempt is recorded as a
    ``reask`` stage row and logged to the ledger events (AC3).
    """
    # The re-ask re-dispatches the originating `stage` agent, so record it on the
    # harness that runs that stage (Story 20.2-002), not a notional "reask" role.
    ledger.stage_start(
        run_id, story.id, "reask", seq, harness=_stage_harness(stage, opts)
    )
    out = str(transcript_path) if transcript_path is not None else ""
    ledger.event_log(
        run_id, story.id, "warn", "controller",
        f"{stage} result envelope missing/malformed — issuing envelope-only re-ask",
    )
    prompt = render_envelope_reask_prompt(stage, story, opts, pr_number)
    sink = _make_progress_sink(ledger, run_id, story.id, "reask", seq)
    # Story 14.2-001: the envelope-only re-ask is cheap reformatting work, so it
    # routes on the map's `reask` tier (its own override beats it) rather than the
    # stage's — and never the unconfigured CLI default under an active profile.
    model = _select_stage_model("reask", story, opts)
    # The re-ask re-dispatches the originating `stage` agent, so route it to that
    # stage's harness too (Story 20.7-001) — the ledger and reality stay in sync.
    harness_kwargs = _harness_dispatch_kwargs(stage, opts, model)
    try:
        result = dispatch(
            stage, prompt, story=story, model=model,
            transcript_path=transcript_path, on_progress=sink,
            **harness_kwargs,
        )
    except RateLimitError:
        # Story 14.1-003: a throttle during recovery is a pause, not a failed fix —
        # propagate so the controller waits/parks. The reask row stays IN_PROGRESS
        # (from stage_start), so resume re-enters cleanly.
        raise
    except (ContractError, AgentDispatchError) as exc:
        ledger.stage_finish(run_id, story.id, "reask", seq, "FAILED", "reask-error", out)
        ledger.event_log(
            run_id, story.id, "error", "controller", f"envelope re-ask failed: {exc}"
        )
        return False, None

    if not _stage_succeeded(stage, result.data):
        ledger.stage_finish(
            run_id, story.id, "reask", seq, "FAILED", "reask-reported", out
        )
        ledger.event_log(
            run_id, story.id, "warn", "controller",
            f"envelope re-ask returned a non-success {stage} status",
        )
        return False, result

    ledger.stage_finish(run_id, story.id, "reask", seq, "DONE", output_path=out)
    _record_stage_usage(ledger, run_id, story.id, "reask", seq, result)
    ledger.event_log(
        run_id, story.id, "success", "controller",
        f"envelope re-ask recovered the {stage} result block",
    )
    return True, result


def _lint_stage_commit(
    stage: str,
    story: Story,
    opts: BuildOptions,
    pr_number: int | None,
    ledger: Ledger,
    run_id: str,
    dispatch: Dispatcher,
    logs_dir: Path,
    seq: int,
) -> tuple[int, bool]:
    """Lint a stage's commit against commitlint and re-ask on violation (12.2-002).

    After any commit-authoring stage (build / coverage / bugfix) succeeds,
    validate the HEAD commit message of ``feature/<story_id>`` against the repo's
    commitlint rules. When the message violates them, issue a bounded
    message-only re-ask (``git commit --amend`` by the *same* ``stage`` agent) so
    a non-compliant header never reaches a PR and fails the commit-format CI job.
    Graceful no-op when the repo has no commitlint config, the commit can't be
    read, or the message is already compliant — then there is no behaviour
    change.

    Story 12.2-004 (AC4): a re-ask whose response is malformed (a missing/garbled
    result envelope, e.g. the missing ``branch_name`` of run ``7df64f19``) must
    not dead-end the story. Such a contract error is routed through the same
    envelope-only recovery other stages use (:func:`_reask_envelope`, 12.1-001):
    the amend itself almost always landed, so the recovered envelope lets the
    re-lint see the now-compliant message instead of parking on a transport-level
    failure.

    Returns ``(seq, compliant)``. ``seq`` is the (possibly advanced) attempt
    counter so the caller keeps stage rows unique. ``compliant`` is False only
    when the message still violates commitlint after the bounded re-asks are
    exhausted — the caller then **parks the story** rather than advancing a
    known-non-compliant commit to a PR (the epic's "zero commitlint failures
    reach a PR" guarantee). Committed work is never discarded (R10); the no-op
    cases all report ``True``.
    """
    root = Path.cwd()
    config = load_commitlint_config(root)
    if config is None:
        return seq, True  # No config → invent no rules (AC2).
    ref = f"feature/{story.id}"
    message = _commit_message(ref, root)
    if message is None:
        return seq, True  # Unreadable commit → degrade to a no-op.
    violations = lint_commit_message(message, config)
    if not violations:
        return seq, True  # Compliant → no behaviour change (AC3).

    attempt = 0
    while violations and attempt < MAX_COMMITLINT_REASK:
        attempt += 1
        seq += 1
        lint_seq = seq
        ledger.event_log(
            run_id, story.id, "warn", "controller",
            f"{stage} commit message violates commitlint "
            f"({'; '.join(violations)}) — re-asking the {stage} agent to amend",
        )
        cpath = logs_dir / f"{story.id}-commitlint-{lint_seq}.log"
        # The amend re-dispatches the originating `stage` agent (Story 20.2-002).
        ledger.stage_start(
            run_id, story.id, "commitlint", lint_seq,
            harness=_stage_harness(stage, opts),
        )
        prompt = render_commit_lint_reask_prompt(stage, story, message, violations)
        sink = _make_progress_sink(ledger, run_id, story.id, "commitlint", lint_seq)
        # The amend re-dispatches the originating `stage` agent (Story 20.7-001).
        harness_kwargs = _harness_dispatch_kwargs(stage, opts, None)
        try:
            result = dispatch(
                stage, prompt, story=story, transcript_path=cpath, on_progress=sink,
                **harness_kwargs,
            )
        except RateLimitError:
            # Story 14.1-003: a throttle during the commit-lint amend is a pause,
            # not a failed lint — propagate so the controller waits/parks.
            raise
        except (ContractError, AgentDispatchError) as exc:
            # Story 12.2-004 AC4: a malformed re-ask envelope is recovered via
            # the shared envelope-only re-ask (12.1-001), not dead-ended. The
            # amend usually landed; the recovered envelope lets us re-read and
            # re-lint the now-compliant commit on the next iteration.
            ledger.event_log(
                run_id, story.id, "warn", "controller",
                f"commit-lint re-ask response malformed ({exc}) — routing "
                "through envelope recovery (12.2-004)",
            )
            seq += 1
            rpath = logs_dir / f"{story.id}-reask-commitlint-{seq}.log"
            ok_r, _ = _reask_envelope(
                stage, story, opts, pr_number, ledger, run_id, dispatch,
                rpath, seq,
            )
            if not ok_r:
                ledger.stage_finish(
                    run_id, story.id, "commitlint", lint_seq, "FAILED",
                    "commitlint-error", str(cpath),
                )
                break
            ledger.stage_finish(
                run_id, story.id, "commitlint", lint_seq, "DONE",
                output_path=str(cpath),
            )
            message = _commit_message(ref, root)
            if message is None:
                break
            violations = lint_commit_message(message, config)
            continue
        _record_stage_usage(ledger, run_id, story.id, "commitlint", lint_seq, result)
        ledger.stage_finish(
            run_id, story.id, "commitlint", lint_seq, "DONE", output_path=str(cpath)
        )
        message = _commit_message(ref, root)
        if message is None:
            break
        violations = lint_commit_message(message, config)

    if violations:
        ledger.event_log(
            run_id, story.id, "warn", "controller",
            f"{stage} commit message still violates commitlint after {attempt} "
            f"re-ask(s) ({'; '.join(violations)}) — parking for manual fix so a "
            "non-compliant header never reaches the PR (work preserved)",
        )
        return seq, False
    ledger.event_log(
        run_id, story.id, "success", "controller",
        f"{stage} commit message is now commitlint-compliant",
    )
    return seq, True


def _run_bugfix(
    story: Story,
    failed_stage: str,
    failure: str,
    opts: BuildOptions,
    ledger: Ledger,
    run_id: str,
    dispatch: Dispatcher,
    transcript_path: Path | None = None,
    attempt: int = 1,
    *,
    escalation_steps: int = 0,
) -> bool:
    """Dispatch the bugfix agent. Returns True when the fix is confirmed.

    A bugfix is "confirmed" only when ``fix_status == FIXED`` and
    ``tests_passing`` is true — exactly the skill's Step 5d2 gate. Any dispatch
    or contract error during bugfix is itself a failure (no fix). ``attempt`` is
    a story-level monotonic sequence so each bugfix row is unique (the "bugfix"
    stage recurs across retries and stages and would otherwise collide on the
    stages UNIQUE key).
    """
    # The bugfix re-dispatches the originating `failed_stage` agent, so record it
    # on that stage's harness (Story 20.2-002).
    ledger.stage_start(
        run_id, story.id, "bugfix", attempt,
        harness=_stage_harness(failed_stage, opts),
    )
    out = str(transcript_path) if transcript_path is not None else ""
    prompt = render_bugfix_prompt(story, failed_stage, failure)
    sink = _make_progress_sink(ledger, run_id, story.id, "bugfix", attempt)
    # Story 14.2-001: route the bugfix agent on the map's `bugfix` tier (its own
    # override beats it) instead of the unconfigured CLI default.
    # Story 14.2-003: ``escalation_steps`` bumps that tier one rung per bugfix
    # attempt (capped at the strongest tier), so a stuck stage's recovery climbs
    # toward Opus rather than re-running on the model that just failed. Record the
    # chosen model per attempt so the eval harness (Epic-18) can see cheap-first's
    # success rate.
    model = _select_stage_model("bugfix", story, opts, escalation_steps=escalation_steps)
    ledger.event_log(
        run_id, story.id, "info", "controller",
        f"bugfix attempt {attempt} for {failed_stage} on "
        f"model={model or 'cli-default'} (escalation +{escalation_steps}, "
        "Story 14.2-003 cheap-first)",
    )
    # The bugfix re-dispatches the originating `failed_stage` agent, so route it
    # to that stage's harness (Story 20.7-001), matching its ledger record above.
    harness_kwargs = _harness_dispatch_kwargs(failed_stage, opts, model)
    try:
        result = dispatch(
            "bugfix", prompt, story=story, model=model,
            transcript_path=transcript_path, on_progress=sink,
            **harness_kwargs,
        )
    except RateLimitError:
        # Story 14.1-003: a throttle during the bugfix dispatch is a pause, not a
        # failed fix — propagate so the controller waits/parks rather than burning
        # the bugfix attempt. The bugfix row stays IN_PROGRESS for a clean resume.
        raise
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
    _surface_finding_dispositions(ledger, run_id, story.id, data)
    return fixed


def _surface_finding_dispositions(
    ledger: Ledger, run_id: str, story_id: str, data: dict[str, Any]
) -> None:
    """Surface each disputed review finding as a visible ledger event (Story 26.2-001).

    A bugfix agent dispatched with review findings reports each one's disposition
    in ``finding_dispositions`` (``implemented`` | ``disputed``). A dispute is a
    claim the agent refuted against the code — it must never be silently swallowed,
    and the story must not falsely report the finding as fixed. Each dispute
    becomes a ``warn`` audit event so it shows up in ``sdlc status`` and the
    dashboard's recent-events, in front of FX and the ledger. Implemented
    dispositions are the normal path and stay quiet so the log is not flooded.
    """
    dispositions = data.get("finding_dispositions")
    if not isinstance(dispositions, list):
        return
    for item in dispositions:
        if not isinstance(item, dict) or item.get("disposition") != "disputed":
            continue
        finding = str(item.get("finding", "")).strip() or "(unnamed finding)"
        reasoning = str(item.get("reasoning", "")).strip()
        detail = f": {reasoning}" if reasoning else ""
        ledger.event_log(
            run_id, story_id, "warn", "controller",
            f"review finding disputed — {finding}{detail}",
        )
