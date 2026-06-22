# ABOUTME: Minimal run-logging API so the markdown `fix-issue` skill surfaces in the dashboard.
# ABOUTME: Story 11.2-013 — open/stage/close reuse Ledger + Registry; logging is always best-effort.

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sdlc.build import Ledger
from sdlc.registry import Registry, RunRecord

__all__ = [
    "RunHandle",
    "DEFAULT_DB_NAME",
    "FIX_ISSUE_MODE",
    "run_open",
    "run_stage",
    "run_close",
]

# The per-repo ledger lives beside the repo root, matching `sdlc build`.
DEFAULT_DB_NAME = ".sdlc-state.db"
# Marks the run's lineage in the ledger/registry so the dashboard can tell a
# fix-issue session apart from a controller `sdlc build` run.
FIX_ISSUE_MODE = "fix-issue"


@dataclass
class RunHandle:
    """What ``run_open`` hands back so later phases can address the same run."""

    run_id: str
    db: str
    story_id: str


def _resolve_db(db: str | os.PathLike[str] | None) -> Path:
    """The ledger path to log against (default: ``./.sdlc-state.db``)."""
    return Path(db) if db is not None else Path.cwd() / DEFAULT_DB_NAME


def _sole_story(ledger: Ledger, run_id: str) -> str | None:
    """The single story id of a fix-issue run, or None when none is recorded.

    ``run_open`` seeds exactly one story per run, so ``run_stage`` / ``run_close``
    can default ``--story-id`` from the ledger rather than re-threading it through
    every skill phase. The first row wins; an empty run yields None.
    """
    stories = ledger.story_rows(run_id)
    return stories[0]["story_id"] if stories else None


def run_open(
    *,
    scope: str,
    db: str | os.PathLike[str] | None = None,
    repo: str | os.PathLike[str] | None = None,
    mode: str = FIX_ISSUE_MODE,
    story_id: str | None = None,
    title: str | None = None,
    pid: int | None = None,
    registry: Registry | None = None,
) -> RunHandle | None:
    """Register a fix-issue run in the ledger + host registry.

    Creates a fresh ledger run with one synthetic story (the issue itself) so the
    multi-run dashboard discovers it beside ``sdlc build`` runs and renders the
    per-phase pipeline against it. Returns a :class:`RunHandle` the skill threads
    into later ``run_stage`` / ``run_close`` calls, or None when the ledger could
    not be written — best-effort logging must never block the fix.

    ``pid`` is the long-lived process whose liveness stands in for the run's: when
    it dies before :func:`run_close`, the registry derives the run ``DEAD`` (a
    crash). A markdown skill must pass the *orchestrator's* pid (its ``$PPID`` —
    the Claude session), **not** the pid of this ephemeral ``sdlc run-open``
    subprocess, which exits the instant it returns and would wrongly mark a live
    run dead. Defaults to ``os.getpid()`` for an in-process caller (e.g. a future
    long-running controller path), matching ``sdlc build``.
    """
    db_path = _resolve_db(db)
    story = story_id or scope
    try:
        ledger = Ledger(db_path)
        ledger.init()
        run_id = ledger.run_create(scope, mode)
        ledger.set_total(run_id, 1)
        ledger.story_upsert(
            run_id,
            story,
            epic_id="",
            title=title or scope,
            priority="",
            points=None,
            agent_type=mode,
            branch="",
            pr_number=None,
            status="IN_PROGRESS",
        )
        ledger.event_log(
            run_id, "", "info", "controller", f"run started: scope={scope} mode={mode}"
        )
    except (OSError, sqlite3.Error):
        # A ledger we cannot create/write must not abort the fix.
        return None

    # The registry is a separate best-effort cache: a registration failure leaves
    # the run logged in its own ledger but undiscovered, never failing the fix.
    reg = registry if registry is not None else Registry()
    try:
        reg.register(
            RunRecord(
                run_id=run_id,
                repo=str(Path(repo or Path.cwd()).resolve()),
                db=str(db_path.resolve()),
                scope=scope,
                pid=pid if pid is not None else os.getpid(),
                status="IN_PROGRESS",
                started_at="",  # registry stamps the start time
                total=1,
                completed=0,
            )
        )
    except OSError:
        pass

    return RunHandle(run_id=run_id, db=str(db_path), story_id=story)


def run_stage(
    *,
    action: str,
    run_id: str,
    stage: str,
    db: str | os.PathLike[str] | None = None,
    story_id: str | None = None,
    attempt: int = 1,
    status: str = "DONE",
    failure_category: str = "",
    output_path: str = "",
) -> bool:
    """Log a fix-issue phase boundary (``start`` or ``finish``) to the ledger.

    Writes an IN_PROGRESS stage row on ``start`` and transitions it to ``status``
    on ``finish``. Returns True on success, False on an unknown action, a missing
    story, or any ledger IO error — the caller treats False as "not logged" and
    carries on.
    """
    if action not in ("start", "finish"):
        return False
    db_path = _resolve_db(db)
    try:
        ledger = Ledger(db_path)
        ledger.ensure_migrated()
        sid = story_id or _sole_story(ledger, run_id)
        if sid is None:
            return False
        if action == "start":
            ledger.stage_start(run_id, sid, stage, attempt)
        else:
            ledger.stage_finish(
                run_id, sid, stage, attempt, status, failure_category, output_path
            )
    except (OSError, sqlite3.Error):
        return False
    return True


def run_close(
    *,
    run_id: str,
    db: str | os.PathLike[str] | None = None,
    status: str = "DONE",
    completed: int | None = None,
    story_id: str | None = None,
    registry: Registry | None = None,
) -> bool:
    """Finalize a fix-issue run terminal in the ledger and the registry.

    Stamps the run row (and its sole story) ``status`` — terminal states record
    ``finished_at`` — and mirrors the same into the registry so a clean finish no
    longer derives as DEAD. Returns True when the ledger write succeeded; the
    registry update is best-effort and never flips the result to False.
    """
    db_path = _resolve_db(db)
    ok = True
    try:
        ledger = Ledger(db_path)
        ledger.ensure_migrated()
        ledger.run_update_status(run_id, status)
        if completed is not None:
            ledger.run_update_counts(run_id, completed, 0)
        sid = story_id or _sole_story(ledger, run_id)
        if sid is not None:
            ledger.set_story_status(run_id, sid, status)
        ledger.event_log(
            run_id, "", "info", "controller", f"run finished: status={status}"
        )
    except (OSError, sqlite3.Error):
        ok = False

    reg = registry if registry is not None else Registry()
    try:
        reg.mark_finished(run_id, status, completed=completed)
    except OSError:
        pass

    return ok
