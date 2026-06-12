# ABOUTME: Deterministic build-stories state machine ported from the skill (7.3-001).
# ABOUTME: Owns preflight, cohorts, agent dispatch, schema validation, ledger writes.

from __future__ import annotations

import os
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

from sdlc.cohort import Story, compute_cohorts, truncate_queue
from sdlc.contracts import ContractError
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

_TERMINAL_RUN_STATES = {"DONE", "FAILED", "ABORTED"}

# Boolean flags the build subcommand accepts. Kept identical to the skill's
# argument-hint so `sdlc build $ARGUMENTS` is a drop-in for `/build-stories`.
_BOOL_FLAGS = {
    "--dry-run": "dry_run",
    "--auto": "auto",
    "--skip-coverage": "skip_coverage",
    "--sequential": "sequential",
    "--skip-preflight": "skip_preflight",
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


def parse_build_args(args: Iterable[str]) -> BuildOptions:
    """Parse the `sdlc build` argument vector into :class:`BuildOptions`.

    Accepts the exact flags the skill documents:
    ``[scope] [--dry-run] [--auto] [--skip-coverage] [--limit=N]
    [--sequential] [--coverage-threshold=N] [--skip-preflight]``. A bare
    positional token is the scope (default ``all``). Unknown flags raise
    :class:`ValueError` so a typo never silently changes behaviour.
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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def init(self) -> None:
        """Create the ledger schema if absent (idempotent)."""
        with self._connect() as conn:
            conn.executescript(_SCHEMA_DDL)

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
    planned: int = 0
    dry_run: bool = False
    preflight_failed: bool = False
    run_id: str | None = None
    story_status: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def detect_test_command(root: Path) -> list[str] | None:
    """Detect the project's test command, mirroring the skill's preference order.

    package.json (npm test) → pyproject.toml (uv run pytest) → Makefile (make
    test) → bats (bats test/). Returns ``None`` when no test harness is found.
    """
    if (root / "package.json").is_file():
        return ["npm", "test"]
    if (root / "pyproject.toml").is_file():
        return ["uv", "run", "pytest"]
    makefile = root / "Makefile"
    if makefile.is_file() and "test:" in makefile.read_text(encoding="utf-8"):
        return ["make", "test"]
    if (root / "test").is_dir():
        return ["bats", "test/"]
    return None


def default_preflight(root: Path | None = None) -> bool:
    """Run the detected test command and return True when it is green.

    This is the real preflight: it shells out to the test command. ``run_build``
    accepts a ``preflight`` callable so tests inject a deterministic stub instead
    of executing a suite.
    """
    root = root or Path.cwd()
    cmd = detect_test_command(root)
    if cmd is None:
        # No suite to run — treat as a pass rather than blocking the build.
        return True
    completed = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    return completed.returncode == 0


# ---------------------------------------------------------------------------
# Prompt rendering (kept terse — the agent reads the epic file itself)
# ---------------------------------------------------------------------------

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
        "Emit the result block per controller/schemas/build-agent-response.schema.json."
    )


def render_coverage_prompt(story: Story, opts: BuildOptions) -> str:
    return (
        f"Coverage gate for story {story.id}: {story.title}.\n"
        f"Branch: feature/{story.id}. Threshold: {opts.coverage_threshold}%.\n"
        "Fetch the branch, fill coverage gaps, push, open the PR, then emit the "
        "result block per controller/schemas/coverage-agent-response.schema.json."
    )


def render_review_prompt(story: Story, pr_number: int | None) -> str:
    return (
        f"Review the PR for story {story.id}: {story.title} (PR #{pr_number}).\n"
        "Check architecture, security, performance, coverage, code quality; "
        "approve when satisfied, then emit the result block per "
        "controller/schemas/review-agent-response.schema.json."
    )


def render_merge_prompt(story: Story, pr_number: int | None) -> str:
    return (
        f"Merge the PR for story {story.id}: {story.title} (PR #{pr_number}).\n"
        "Rebase before merge to absorb baseline drift, then emit the result "
        "block per controller/schemas/merge-agent-response.schema.json."
    )


def render_bugfix_prompt(story: Story, failed_stage: str, failure: str) -> str:
    return (
        f"Bugfix story {story.id}: {story.title}. Stage '{failed_stage}' failed.\n"
        f"Failure: {failure}\n"
        "Classify (CODE_BUG/TEST_BUG/ENV_ISSUE), fix where possible, then emit "
        "the result block per controller/schemas/bugfix-agent-response.schema.json."
    )


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------

# Stage pipeline. Coverage is conditionally skipped via --skip-coverage.
_STAGES = ("build", "coverage", "review", "merge")


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
    check_preflight = preflight or (lambda: default_preflight())

    # --- Phase 1: Preflight --------------------------------------------------
    if not opts.skip_preflight:
        if not check_preflight():
            return BuildResult(preflight_failed=True)

    # --- Limit truncation ----------------------------------------------------
    if opts.limit:
        queue = truncate_queue(queue, opts.limit)

    # --- Dry run: report the plan, dispatch nothing --------------------------
    if opts.dry_run:
        return BuildResult(dry_run=True, planned=len(queue))

    # --- Ledger bootstrap ----------------------------------------------------
    ledger.init()
    mode = "serial" if opts.sequential else "parallel"
    run_id = ledger.run_create(opts.scope, mode)
    ledger.set_total(run_id, len(queue))
    ledger.event_log(
        run_id, "", "info", "controller", f"run started: scope={opts.scope} mode={mode}"
    )
    for story in queue:
        ledger.story_upsert(
            run_id,
            story.id,
            story.epic_id,
            story.title,
            story.priority,
            story.points,
            story.agent_type,
            "",
            None,
            "TODO",
        )

    cohorts = compute_cohorts(queue)
    status: dict[str, str] = {s.id: "TODO" for s in queue}

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

            outcome = _run_story(story, opts, ledger, run_id, dispatch)
            status[story.id] = outcome
            ledger.set_story_status(run_id, story.id, outcome)

    # --- Phase 3: close out --------------------------------------------------
    completed = sum(1 for v in status.values() if v == "DONE")
    failed = sum(1 for v in status.values() if v == "FAILED")
    skipped = sum(1 for v in status.values() if v == "SKIPPED")
    blocked = sum(1 for v in status.values() if v == "BLOCKED")

    run_terminal = "DONE" if (failed == 0 and blocked == 0) else "FAILED"
    ledger.run_update_counts(run_id, completed, failed)
    ledger.event_log(
        run_id,
        "",
        "success" if run_terminal == "DONE" else "error",
        "controller",
        f"run finished: {completed} done, {failed} failed, {blocked} blocked",
    )
    ledger.run_update_status(run_id, run_terminal)

    if render_view is not None:
        render_view(run_id)

    return BuildResult(
        completed=completed,
        failed=failed,
        skipped=skipped,
        blocked=blocked,
        planned=len(queue),
        run_id=run_id,
        story_status=status,
    )


def _run_story(
    story: Story,
    opts: BuildOptions,
    ledger: Ledger,
    run_id: str,
    dispatch: Dispatcher,
) -> str:
    """Drive one story through build → coverage → review → merge.

    Returns the terminal story status: ``DONE`` or ``FAILED``. A stage failure
    (agent FAILED status, dispatch error, or schema-invalid output) enters the
    bounded bugfix loop; the stage is retried after a successful fix.
    """
    pr_number: int | None = None
    stages = [s for s in _STAGES if not (s == "coverage" and opts.skip_coverage)]

    for stage in stages:
        bugfix_attempts = 0
        attempt = 1
        while True:
            ledger.stage_start(run_id, story.id, stage, attempt)
            ok, result, failure = _dispatch_stage(
                stage, story, opts, pr_number, dispatch
            )
            if ok:
                ledger.stage_finish(run_id, story.id, stage, attempt, "DONE")
                pr_number = _extract_pr(result, pr_number)
                if pr_number is not None:
                    ledger.set_story_pr(run_id, story.id, pr_number)
                break

            # Stage failed: record it, then attempt a bounded bugfix.
            ledger.stage_finish(
                run_id, story.id, stage, attempt, "FAILED", f"{stage}-error"
            )
            ledger.event_log(
                run_id, story.id, "error", "controller", f"{stage} failed: {failure}"
            )
            if bugfix_attempts >= MAX_BUGFIX_ATTEMPTS:
                return "FAILED"

            bugfix_attempts += 1
            if not _run_bugfix(story, stage, failure, ledger, run_id, dispatch):
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
) -> tuple[bool, AgentResult | None, str]:
    """Dispatch one stage's agent and classify the outcome.

    Returns ``(ok, result, failure_summary)``. ``ok`` is False on a dispatch
    error, a schema-invalid response (caught here, never passed downstream), or
    an agent that reported a non-success status for its stage.
    """
    prompt = _render_stage_prompt(stage, story, opts, pr_number)
    try:
        result = dispatch(stage, prompt, story=story)
    except ContractError as exc:
        # Malformed / schema-invalid agent output is a build failure.
        return False, None, f"contract violation: {exc}"
    except AgentDispatchError as exc:
        return False, None, f"dispatch error: {exc}"

    if not _stage_succeeded(stage, result.data):
        return False, result, _stage_failure_summary(stage, result.data)
    return True, result, ""


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
) -> bool:
    """Dispatch the bugfix agent. Returns True when the fix is confirmed.

    A bugfix is "confirmed" only when ``fix_status == FIXED`` and
    ``tests_passing`` is true — exactly the skill's Step 5d2 gate. Any dispatch
    or contract error during bugfix is itself a failure (no fix).
    """
    ledger.stage_start(run_id, story.id, "bugfix", 1)
    prompt = render_bugfix_prompt(story, failed_stage, failure)
    try:
        result = dispatch("bugfix", prompt, story=story)
    except (ContractError, AgentDispatchError) as exc:
        ledger.stage_finish(run_id, story.id, "bugfix", 1, "FAILED", "bugfix-error")
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
        1,
        "DONE" if fixed else "FAILED",
        str(data.get("failure_category", "")),
    )
    ledger.event_log(
        run_id,
        story.id,
        "success" if fixed else "error",
        "controller",
        f"bugfix {'resolved' if fixed else 'exhausted'}: {failed_stage}",
    )
    return fixed
