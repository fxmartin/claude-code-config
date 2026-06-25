# ABOUTME: Typer entry point for the `sdlc` controller CLI (Story 7.1-001).
# ABOUTME: Ships --version, --help, and every build/resume/observability verb.

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from sdlc import __version__
from sdlc.contracts import AGENT_SCHEMAS, ContractError, parse_and_validate

# The full set of planned subcommands with one-line descriptions. `--help`
# renders these even while the bodies are stubs, so the surface area is visible
# from day one. Keep the keys in sync with the Epic-07 success metrics.
PLANNED_SUBCOMMANDS: dict[str, str] = {
    "build": "Run the full build-stories orchestration for a scope.",
    "resume": "Resume an interrupted build from the ledger state.",
    "status": "Show the current run status and stage progress.",
    "doctor": "Health-check the install, ledger, runs, config, and dependencies.",
    "runs": "List every known run from the host-level registry.",
    "state": "Inspect the persisted state machine for a run.",
    "validate": "Validate an agent response against its JSON schema.",
    "rollback": "Roll a run back to a prior ledger checkpoint.",
    "reconcile": "Re-check a run against origin/main and correct the ledger.",
    "clean": "Garbage-collect build leftovers (orphan worktrees, merged branches, stale logs).",
    "sync-check": "Verify the Codex mirror's shared-skills submodule is in sync.",
    "repair": "Restore the framework's managed symlinks/config without a full reinstall.",
    "sast": "Classify a semgrep report into a CLEAN | WARN | BLOCK gate verdict.",
    "depscan": "Classify an osv-scanner report into a CLEAN | WARN | BLOCK gate verdict.",
    "supplychain": "Scan hooks/skills/MCP/settings for dangerous patterns (CLEAN | WARN | BLOCK).",
    "run-open": "Register a fix-issue run so the dashboard surfaces it.",
    "run-stage": "Log a fix-issue phase start/finish to the ledger.",
    "run-close": "Finalize a fix-issue run (DONE/FAILED) in ledger + registry.",
}

app = typer.Typer(
    name="sdlc",
    help="External controller for the autonomous-SDLC state machine.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    _version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the controller version and exit.",
    ),
) -> None:
    """sdlc — deterministic controller for autonomous SDLC runs."""


# Note: there is no `init` verb. Epic-07 scaffolded one as a stub, but `build`
# already creates the SQLite ledger on first use (`Ledger.init()` runs inside
# `run_build`), so a separate workspace-scaffold command had no distinct job.
# Story 10.2-001 resolved it by removal — see docs/adr/001-controller-runtime.md.


_BUILD_EPILOG = """\
\b
Scope (first positional, default `all`):
  all              every epic
  epic-NN          one epic by number, e.g. epic-34
  <name>           epic-name substring, e.g. user-management
  X.Y-NNN          a single story, e.g. 34.5-003

\b
Flags:
  --dry-run                 plan only; dispatch nothing
  --auto                    non-interactive run
  --skip-coverage           build agent opens the PR directly (no coverage gate)
  --skip-preflight          skip the preflight quality gate
  --rebuild                 rebuild stories the epic already marks Done
  --sequential              one story at a time (no cohort parallelism)
  --concurrency=N           max stories of a cohort to run at once in parallel
                            mode (default 5; --sequential forces 1)
  --limit=N                 build at most N stories
  --coverage-threshold=N    required new-code coverage % (default 90)
  --preflight-timeout=SEC   abort the preflight gate after SEC seconds (default 600)
  --budget=N                token ceiling for the run; a $-value (e.g. $5) is
                            converted to a notional API-equivalent token ceiling
  --budget-policy=POLICY    on crossing the ceiling: pause (resumable, default)
                            or abort (terminal stop)
  --rate-limit-max-wait=SEC in-process auto-wait cap for a Max rate-limit pause
                            (default 18000 ≈ 5h); a reset within it auto-resumes
                            the same run, beyond it parks RATE_LIMITED for resume
  --window-budget=N         configured per-window token budget (a $-value
                            converts to a notional ceiling); 0 = rely only on
                            live rate-limit signals (default)
  --window=SEC              rolling rate-limit window length (default 18000 ≈ 5h)
  --rate-limit-threshold=F  pause at this fraction of the window budget
                            (default 1.0; <1 pauses near the limit)
  --cost-threshold=N        per-stage pre-dispatch estimate ceiling; over it,
                            --auto warns and proceeds, interactive gates the
                            stage before spend. A $-value converts to a notional
                            token ceiling; 0 = estimate only, no gate (default)
  --model-routing=PROFILE   per-stage model map: balanced | quality-first |
                            quota-max | off (default off = CLI default for all
                            stages). Balanced cuts quota burn; the adversarial
                            skeptic is always Opus
  --model-<stage>=MODEL     pin one stage's model, winning over the map (escape
                            hatch), e.g. --model-build=opus --model-merge=haiku
  --thinking-cap=N          cap per-request thinking tokens (MAX_THINKING_TOKENS)
                            on every dispatched agent; 0 = no cap (default)
  --sandbox                 run every dispatched agent inside a no-egress,
                            cap-dropped, non-root container with the worktree
                            mounted (recommended for untrusted repos); fails fast
                            if no container runtime is present. SDLC_SANDBOX=1 is
                            the per-repo config equivalent
"""


@app.command(
    help=PLANNED_SUBCOMMANDS["build"],
    epilog=_BUILD_EPILOG,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def build(ctx: typer.Context) -> None:
    """Run the full build-stories orchestration for a scope.

    Scope is ``all``, ``epic-NN``, an epic name, or a single story ``X.Y-NNN``.
    Flags: ``--dry-run --auto --skip-coverage --skip-preflight --rebuild
    --sequential --limit=N --coverage-threshold=N --preflight-timeout=SEC`` (see
    the epilog in ``build --help``). The controller owns the state machine;
    agents are dispatched as subprocesses and every response is schema-validated
    before the next stage runs.
    """
    from sdlc.build import parse_build_args, run_build
    from sdlc.discovery import discover_queue
    from sdlc.ledger_view import Ledger, default_db_path, make_render_view
    from sdlc.registry import Registry

    try:
        opts = parse_build_args(ctx.args)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    queue = discover_queue(opts.scope)

    # A targeted scope that matches nothing is almost always a typo or an
    # unsupported form — fail loudly rather than reporting a hollow success.
    # (`all` legitimately yields an empty queue in a fresh repo; leave it be.)
    if opts.scope.lower() != "all" and not queue:
        typer.echo(
            f"error: scope '{opts.scope}' matched no stories — check the story id "
            "or epic name/number.",
            err=True,
        )
        raise typer.Exit(code=2)

    ledger = Ledger(default_db_path())
    # Bring a pre-existing ledger up to the current schema before the build
    # reads or writes it (run_build's init() also migrates, but a stale DB must
    # be safe the moment any code touches it). No-op when no DB exists yet.
    ledger.ensure_migrated()
    result = run_build(
        opts,
        queue=queue,
        ledger=ledger,
        render_view=make_render_view(ledger.db_path),
        registry=Registry(),
    )

    if result.skipped_in_test:
        # Story 12.1-002: the recursion guard fired (SDLC_IN_TEST set) — a project
        # test invoked `sdlc build` bare during preflight. Report and exit cleanly
        # so the parent suite does not recurse into pytest-within-pytest.
        from sdlc.build import IN_TEST_ENV_VAR

        typer.echo(
            f"{IN_TEST_ENV_VAR} is set — skipping real build orchestration "
            "(recursion guard, Story 12.1-002)."
        )
        raise typer.Exit(code=0)

    if result.preflight_failed:
        typer.echo(
            "PRE_FLIGHT_FAILURE: test suite is red on main — fix before building.",
            err=True,
        )
        raise typer.Exit(code=1)

    if result.dry_run:
        typer.echo(f"dry run: {result.planned} stories queued (not building).")
        raise typer.Exit(code=0)

    typer.echo(
        f"build finished: {result.completed} done, {result.failed} failed, "
        f"{result.blocked} blocked, {result.needs_attention} need attention, "
        f"{result.awaiting_approval} awaiting approval, {result.skipped} skipped."
    )
    # Story 14.1-001: a budget-gated stop is reported with the labelled-notional
    # dollar figure so the $ is never mistaken for real subscription spend. Pause
    # leaves the run resumable; abort is a terminal stop.
    if result.budget_stopped:
        from sdlc.build import notional_cost_label

        tail = (
            "run paused — raise --budget and `sdlc resume` to continue."
            if result.budget_policy == "pause"
            else "run aborted."
        )
        typer.echo(
            f"budget ceiling crossed: {result.accrued_tokens} tokens accrued "
            f"(ceiling {opts.budget}); "
            f"{notional_cost_label(result.notional_cost_usd)} — {tail}"
        )
    # Story 14.1-003: a rate-limit park (reset beyond the auto-wait cap) leaves
    # the run RATE_LIMITED (resumable). Report it as a time-based pause, not a
    # failure, so a wrapper knows `sdlc resume` will continue it once the window
    # reopens. (Within-cap auto-waits never reach here — they resume in-process.)
    if result.rate_limited:
        waited = (
            f" (auto-waited {result.rate_limit_waited_s}s first)"
            if result.rate_limit_waited_s
            else ""
        )
        typer.echo(
            f"rate limit reached{waited} — run parked RATE_LIMITED; `sdlc resume` "
            "continues it once the Max plan's window reopens."
        )
    # Story 14.1-002: the interactive cost gate paused the run (resumable). Report
    # it so a wrapper knows to raise --cost-threshold and resume.
    if result.cost_gated:
        typer.echo(
            "cost gate reached — run paused IN_PROGRESS; raise --cost-threshold "
            "and `sdlc resume` to continue the gated stage."
        )
    # An AWAITING_APPROVAL run is honestly not a failure (Story 12.3-003), but it
    # still needs FX to act, so it is not "clean" — exit non-zero like
    # NEEDS_ATTENTION so a wrapping script never reads it as fully done. A
    # budget-stopped, rate-limited, or cost-gated run is likewise not fully done.
    clean = (
        result.failed == 0
        and result.blocked == 0
        and result.needs_attention == 0
        and result.awaiting_approval == 0
        and not result.budget_stopped
        and not result.rate_limited
        and not result.cost_gated
    )
    raise typer.Exit(code=0 if clean else 1)


@app.command(help=PLANNED_SUBCOMMANDS["resume"])
def resume(
    scope: str = typer.Argument(
        "all",
        help="Scope of the run to resume: all, epic-NN, an epic name, or X.Y-NNN.",
    ),
    run: str | None = typer.Option(
        None,
        "--run",
        help="Resume a specific run id (default: the latest incomplete run).",
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    budget: str | None = typer.Option(
        None,
        "--budget",
        help="Raise/override the run's token budget (a $-value converts to a "
        "notional API-equivalent ceiling). Required to continue a budget-paused run.",
    ),
    budget_policy: str | None = typer.Option(
        None,
        "--budget-policy",
        help="Override the budget policy: pause or abort.",
    ),
    cost_threshold: str | None = typer.Option(
        None,
        "--cost-threshold",
        help="Raise/override the per-stage cost-estimate gate (a $-value converts "
        "to a notional token ceiling; 0 disables it). Pass this to continue a "
        "story the gate parked.",
    ),
    concurrency: int | None = typer.Option(
        None,
        "--concurrency",
        help="Override the cohort worker cap for this resume (>= 1; default: the "
        "value the original run used). A serial-mode run stays one-at-a-time.",
        min=1,
    ),
) -> None:
    """Resume an interrupted build from the SQLite ledger.

    Finds the most recent incomplete run for ``scope`` (a run still marked
    IN_PROGRESS because it never reached a clean close-out), recomputes the
    remaining queue from the markdown epics, and re-enters the 4-stage loop at
    the exact stage each story was interrupted in — branch, PR number, and
    attempt count preserved. Completed stories are not rebuilt. A run with no
    incomplete stories is a no-op that reports "nothing to resume" and exits 0.

    Story 14.1-001: a budget-paused run carries its token ceiling, so resuming
    without raising it re-pauses immediately. Pass ``--budget`` to raise it and
    continue.
    """
    from sdlc.build import _parse_budget_value
    from sdlc.ledger_view import Ledger, default_db_path, make_render_view
    from sdlc.resume import run_resume

    budget_tokens: int | None = None
    if budget is not None:
        try:
            budget_tokens, _ = _parse_budget_value(budget)
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
    if budget_policy is not None and budget_policy not in {"pause", "abort"}:
        typer.echo(
            f"error: invalid --budget-policy: {budget_policy} (expected pause|abort)",
            err=True,
        )
        raise typer.Exit(code=2)
    cost_threshold_tokens: int | None = None
    if cost_threshold is not None:
        try:
            cost_threshold_tokens, _ = _parse_budget_value(cost_threshold)
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc

    db_path = db or default_db_path()
    ledger = Ledger(db_path)
    # Migrate a pre-existing (possibly stale) ledger before resume reads it.
    ledger.ensure_migrated()
    result = run_resume(
        scope,
        ledger=ledger,
        run_id=run,
        render_view=make_render_view(db_path),
        budget=budget_tokens,
        budget_policy=budget_policy,
        cost_threshold=cost_threshold_tokens,
        concurrency=concurrency,
    )

    if result.nothing_to_resume:
        if result.run_id is None:
            typer.echo(f"nothing to resume: no incomplete run for scope '{scope}'.")
        else:
            typer.echo(
                f"nothing to resume: run {result.run_id[:8]} has no incomplete stories."
            )
        raise typer.Exit(code=0)

    typer.echo(
        f"resume finished: {result.completed} done, {result.failed} failed, "
        f"{result.blocked} blocked, {result.needs_attention} need attention, "
        f"{result.awaiting_approval} awaiting approval ({result.resumed} resumed)."
    )
    if result.budget_stopped:
        from sdlc.build import notional_cost_label

        tail = (
            "run paused — raise --budget further and `sdlc resume` to continue."
            if result.budget_policy == "pause"
            else "run aborted."
        )
        typer.echo(
            f"budget ceiling crossed: {result.accrued_tokens} tokens accrued; "
            f"{notional_cost_label(result.notional_cost_usd)} — {tail}"
        )
    # Story 14.1-003: a resume that re-hit the window beyond the cap re-parks.
    if result.rate_limited:
        typer.echo(
            "rate limit still in effect — run re-parked RATE_LIMITED; `sdlc resume` "
            "again once the Max plan's window reopens."
        )
    # Story 14.1-002: an un-raised resume re-trips the cost gate (still IN_PROGRESS).
    if result.cost_gated:
        typer.echo(
            "cost gate still in effect — run left IN_PROGRESS; raise "
            "--cost-threshold further and `sdlc resume` to continue."
        )
    clean = (
        result.failed == 0
        and result.blocked == 0
        and result.needs_attention == 0
        and result.awaiting_approval == 0
        and not result.budget_stopped
        and not result.rate_limited
        and not result.cost_gated
    )
    raise typer.Exit(code=0 if clean else 1)


@app.command(help=PLANNED_SUBCOMMANDS["status"])
def status(
    run: str | None = typer.Option(
        None, "--run", help="Run id to inspect (default: the most recent run)."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit a machine-readable JSON snapshot."
    ),
    markdown: bool = typer.Option(
        False,
        "--markdown",
        help="Emit a portable, secret-free markdown handoff (readiness + run + approvals).",
    ),
    write: Path | None = typer.Option(
        None,
        "--write",
        help="With --markdown, write the report to this file instead of stdout.",
    ),
) -> None:
    """Show the progress of a build run from the SQLite ledger.

    Reads the ledger **read-only** (safe to poll while a build is writing) and
    prints a run summary, a per-story table, and the most recent events. With
    ``--json`` it emits one object so the build-stories skill can poll it and
    report progress. With ``--markdown`` (optionally ``--write <file>``) it emits
    a portable handoff a colleague can paste into an issue or chat. When there is
    no ledger or no run yet it says so and exits 0 — absence means "not started",
    not an error.
    """
    from sdlc.build import Ledger, status_snapshot
    from sdlc.ledger_view import default_db_path

    db_path = db or default_db_path()
    ledger = Ledger(db_path)
    # Apply any pending migrations (writable connection) before the read-only
    # snapshot, so a stale ledger never crashes the query with "no such column".
    # No-op when the DB does not exist — absence stays "not started".
    ledger.ensure_migrated()
    snap = status_snapshot(ledger, run)

    # The markdown handoff (Story 15.1-002) renders even with no run — readiness
    # and "no active run" are exactly what a colleague needs to share.
    if markdown:
        from sdlc.doctor import run_doctor
        from sdlc.status import format_markdown

        report = run_doctor(db_path=db_path)
        md = format_markdown(snap, report.to_dict())
        if write is not None:
            write.write_text(md, encoding="utf-8")
            typer.echo(f"wrote status handoff to {write}")
        else:
            typer.echo(md, nl=False)
        raise typer.Exit(code=0)

    if snap["run"] is None:
        if as_json:
            typer.echo(json.dumps(snap, default=str))
        else:
            typer.echo(f"no build run found in ledger: {db_path}")
        raise typer.Exit(code=0)

    if as_json:
        typer.echo(json.dumps(snap, default=str))
        raise typer.Exit(code=0)

    # Human-readable snapshot.
    run_id = snap["run"]["id"]
    counts = snap["counts"]
    stories = snap["stories"]
    events = snap["events"]
    # Story 17.3-001: surface the run's real concurrency — "N/M workers busy" —
    # so a parallel run shows several stories active at once, not just one. Only
    # a genuine parallel run (cap > 1) carries the figure; a serial run is silent.
    concurrency = snap["run"].get("concurrency") or {}
    worker_limit = concurrency.get("limit") or 1
    workers = (
        f", {concurrency.get('active', 0)}/{worker_limit} workers busy"
        if worker_limit > 1
        else ""
    )
    typer.echo(
        f"run {run_id[:8]}  {snap['run'].get('status', '?')}  "
        f"{counts['done']}/{counts['total']} done, {counts['failed']} failed, "
        f"{counts['blocked']} blocked, {counts['in_progress']} in progress  "
        f"(scope={snap['run'].get('scope', '?')}, {snap['run'].get('mode', '?')}{workers})"
    )
    if stories:
        typer.echo(f"  {'STORY':<14}{'STATUS':<13}{'STAGE':<11}PR")
        for s in stories:
            stage = s.get("current_stage") or "-"
            pr = s.get("pr_number")
            pr_disp = f"#{pr}" if pr else "-"
            typer.echo(
                f"  {str(s.get('story_id', '?')):<14}"
                f"{str(s.get('status', '?')):<13}{str(stage):<11}{pr_disp}"
            )
            # Sub-stage activity for an in-flight story (Story 11.1-002): the
            # latest progress milestone, e.g. "↳ build: editing cli.py". Absent
            # for finished stories or runs without streamed progress.
            activity = s.get("activity")
            if s.get("status") == "IN_PROGRESS" and activity:
                msg = activity.get("message") or activity.get("kind") or ""
                act_stage = activity.get("stage") or stage
                if msg:
                    typer.echo(f"    ↳ {act_stage}: {msg}")
    if events:
        typer.echo("recent:")
        for e in events:
            typer.echo(
                f"  {e.get('ts', '')}  {str(e.get('level', '')):<8}"
                f"{str(e.get('source') or ''):<11} {e.get('message', '')}"
            )
    raise typer.Exit(code=0)


@app.command(help=PLANNED_SUBCOMMANDS["doctor"])
def doctor(
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    claude_dir: Path | None = typer.Option(
        None,
        "--claude-dir",
        help="Install dir to check (default: ~/.claude).",
    ),
    repo_root: Path | None = typer.Option(
        None,
        "--repo-root",
        help="Config repo root for config checks (default: the git toplevel).",
    ),
    exit_code: bool = typer.Option(
        False,
        "--exit-code",
        help="Exit non-zero when any check is WARN (1) or FAIL (2), for automation.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the report as a JSON object."
    ),
) -> None:
    """Check the install and run state, reporting a remedy for each problem.

    Runs read-only health-checks across install integrity (managed symlinks),
    ledger schema currency + integrity, stuck/stale runs (an IN_PROGRESS run with
    a dead pid or no recent activity), config validity (settings/schemas parse),
    and dependency availability (gh, claude, semgrep, osv-scanner). Each finding
    reports CLEAN/WARN/FAIL plus the command or doc that fixes it.

    Always exits 0 by default so it is safe to run anywhere; ``--exit-code`` makes
    a WARN exit 1 and a FAIL exit 2 so a wrapping script can gate on health.
    """
    from sdlc.doctor import run_doctor

    report = run_doctor(
        repo_root=repo_root,
        claude_dir=claude_dir,
        db_path=db,
    )

    if as_json:
        typer.echo(json.dumps(report.to_dict(), default=str))
        raise typer.Exit(code=0)

    for finding in report.findings:
        typer.echo(f"[{finding.status}] {finding.name} — {finding.detail}")
        if finding.remedy:
            typer.echo(f"    ↳ remedy: {finding.remedy}")
    if report.status == "CLEAN":
        typer.echo(f"doctor: all {len(report.findings)} checks passed (CLEAN).")
    else:
        typer.echo(f"doctor: overall {report.status} — see remedies above.")

    if exit_code and report.status != "CLEAN":
        raise typer.Exit(code=2 if report.status == "FAIL" else 1)
    raise typer.Exit(code=0)


@app.command(help=PLANNED_SUBCOMMANDS["runs"])
def runs(
    as_json: bool = typer.Option(
        False, "--json", help="Emit the registry view as a JSON array."
    ),
    prune: bool = typer.Option(
        False, "--prune", help="Remove crashed (dead-pid) entries before listing."
    ),
) -> None:
    """List every known run across repos from the host-level registry.

    The registry is a discovery cache written by each ``sdlc build`` (default
    ``~/.sdlc/registry.json``, XDG-aware). A run whose process has died without a
    clean finish surfaces as ``DEAD`` rather than lingering as in-progress;
    ``--prune`` drops those entries. Each repo's own ledger stays authoritative
    for run detail. Exits 0 even when empty — no runs means "nothing started".
    """
    from sdlc.registry import Registry

    registry = Registry()
    if prune:
        removed = registry.prune()
        if not as_json:
            typer.echo(f"pruned {removed} dead run(s).")

    rows = registry.view()
    if as_json:
        typer.echo(json.dumps(rows, default=str))
        raise typer.Exit(code=0)

    if not rows:
        typer.echo("no runs registered.")
        raise typer.Exit(code=0)

    typer.echo(f"{'RUN':<14}{'SCOPE':<14}{'STATE':<14}{'PROGRESS':<10}REPO")
    for r in rows:
        run_disp = str(r.get("run_id", "?"))[:12]
        total = r.get("total")
        completed = r.get("completed")
        progress = f"{completed}/{total}" if total is not None else "-"
        typer.echo(
            f"{run_disp:<14}{str(r.get('scope', '?')):<14}"
            f"{str(r.get('state', '?')):<14}{progress:<10}{r.get('repo', '?')}"
        )
    raise typer.Exit(code=0)


@app.command(name="run-open", help=PLANNED_SUBCOMMANDS["run-open"])
def run_open_cmd(
    scope: str = typer.Option(
        ..., "--scope", help="The issue being fixed (e.g. issue-42); the run's scope."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    repo: Path | None = typer.Option(
        None, "--repo", help="Repo path recorded in the registry (default: cwd)."
    ),
    mode: str = typer.Option(
        "fix-issue", "--mode", help="Run lineage tag (default: fix-issue)."
    ),
    story_id: str | None = typer.Option(
        None, "--story-id", help="Synthetic story id (default: the scope)."
    ),
    title: str | None = typer.Option(
        None, "--title", help="Human title for the run's story (default: the scope)."
    ),
    pid: int | None = typer.Option(
        None,
        "--pid",
        help="Orchestrator pid whose liveness stands in for the run's (a markdown "
        "skill passes its $PPID, not this subprocess's pid). Default: this process.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit {run_id, db, story_id} as JSON instead of the id."
    ),
) -> None:
    """Open a fix-issue run in the ledger + host registry.

    Minimal run-logging verb the markdown ``fix-issue`` skill shells out to so its
    session shows up in the multi-run dashboard beside ``sdlc build`` runs. Prints
    the new run id on stdout for the skill to capture; exits non-zero when logging
    is unavailable so the skill's best-effort guard (``|| true``) can carry on.

    Pass ``--pid`` the *orchestrator's* long-lived pid (the skill's ``$PPID``): the
    ``sdlc run-open`` process itself exits immediately, so registering its own pid
    would make the registry derive a still-running fix ``DEAD``.
    """
    from sdlc.runlog import run_open

    handle = run_open(
        scope=scope,
        db=db,
        repo=repo,
        mode=mode,
        story_id=story_id,
        title=title,
        pid=pid,
    )
    if handle is None:
        typer.echo("run-open: logging unavailable (fix continues unlogged).", err=True)
        raise typer.Exit(code=1)
    if as_json:
        typer.echo(
            json.dumps(
                {"run_id": handle.run_id, "db": handle.db, "story_id": handle.story_id}
            )
        )
    else:
        typer.echo(handle.run_id)
    raise typer.Exit(code=0)


@app.command(name="run-stage", help=PLANNED_SUBCOMMANDS["run-stage"])
def run_stage_cmd(
    action: str = typer.Argument(..., help="start | finish"),
    run: str = typer.Option(..., "--run", help="Run id from run-open."),
    stage: str = typer.Option(
        ..., "--stage", help="Phase name (investigate/build/coverage/review/e2e/merge)."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    story_id: str | None = typer.Option(
        None, "--story-id", help="Story id (default: the run's sole story)."
    ),
    attempt: int = typer.Option(1, "--attempt", help="Attempt number (default: 1)."),
    status: str = typer.Option(
        "DONE", "--status", help="Terminal status for `finish` (default: DONE)."
    ),
    failure_category: str = typer.Option(
        "", "--failure-category", help="Optional failure category for `finish`."
    ),
) -> None:
    """Log a fix-issue phase boundary to the ledger (best-effort).

    ``start`` appends an IN_PROGRESS stage row; ``finish`` transitions it to
    ``--status``. Exits 2 on a bad action and 1 when the write failed, so the
    skill can ignore logging failures without aborting the fix.
    """
    if action not in ("start", "finish"):
        typer.echo("run-stage: action must be 'start' or 'finish'.", err=True)
        raise typer.Exit(code=2)

    from sdlc.runlog import run_stage

    ok = run_stage(
        action=action,
        run_id=run,
        stage=stage,
        db=db,
        story_id=story_id,
        attempt=attempt,
        status=status,
        failure_category=failure_category,
    )
    raise typer.Exit(code=0 if ok else 1)


@app.command(name="run-close", help=PLANNED_SUBCOMMANDS["run-close"])
def run_close_cmd(
    run: str = typer.Option(..., "--run", help="Run id from run-open."),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    status: str = typer.Option(
        "DONE", "--status", help="Terminal run status (DONE/FAILED/ABORTED)."
    ),
    completed: int | None = typer.Option(
        None, "--completed", help="Completed count recorded on the run + registry."
    ),
    story_id: str | None = typer.Option(
        None, "--story-id", help="Story id (default: the run's sole story)."
    ),
) -> None:
    """Finalize a fix-issue run terminal in the ledger and the registry.

    Stamps the run (and its sole story) ``--status`` and mirrors it into the
    registry so a clean finish stops deriving as DEAD. Exits 1 when the ledger
    write failed; the registry mirror is best-effort.
    """
    from sdlc.runlog import run_close

    ok = run_close(
        run_id=run, db=db, status=status, completed=completed, story_id=story_id
    )
    raise typer.Exit(code=0 if ok else 1)


@app.command(help="Serve a local web dashboard of live build progress.")
def dashboard(
    run: str | None = typer.Option(
        None, "--run", help="Run id to show (default: always the most recent run)."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    port: int = typer.Option(8787, "--port", help="Port to bind (default: 8787)."),
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Host to bind (default: localhost-only)."
    ),
    open_browser: bool = typer.Option(
        False, "--open", help="Open the dashboard in a browser on start."
    ),
    stop: bool = typer.Option(
        False, "--stop", help="Stop a dashboard running on this host:port and exit."
    ),
    restart: bool = typer.Option(
        False,
        "--restart",
        help="Stop any running dashboard on this host:port, then start fresh.",
    ),
) -> None:
    """Serve an auto-refreshing local dashboard of build progress.

    Reads the SQLite ledger read-only and serves it at ``http://host:port`` —
    progress display runs independently of any agent/turn loop, so it stays live
    for the whole build. Runs until Ctrl-C. Binds localhost only by default.

    Use ``--stop`` to stop a (possibly backgrounded) dashboard on this port, or
    ``--restart`` to replace it — handy after upgrading the controller.

    With no ``--db`` the dashboard discovers every run across repos from the
    host-level registry; pass ``--db <path>`` for the single-repo view.
    """
    from sdlc.build import in_test_sentinel
    from sdlc.dashboard import serve, stop_dashboard

    if stop or restart:
        n = stop_dashboard(host, port)
        typer.echo(
            f"stopped {n} dashboard process(es) on {host}:{port}."
            if n
            else f"no dashboard running on {host}:{port}."
        )
        if stop:
            raise typer.Exit(code=0)

    # Story 12.1-002: never bind a server when running inside another build's
    # preflight test suite (sentinel set) — a project test that invokes `sdlc
    # dashboard` bare would otherwise block the run on a socket. Placed after the
    # --stop/--restart handling (which only kills a server, never binds one) so
    # that legitimate coverage of those paths still runs under the sentinel (AC3).
    if in_test_sentinel():
        from sdlc.build import IN_TEST_ENV_VAR

        typer.echo(
            f"{IN_TEST_ENV_VAR} is set — skipping dashboard server "
            "(recursion guard, Story 12.1-002)."
        )
        raise typer.Exit(code=0)

    try:
        # db is None → registry-discovery mode (multi-run overview, Story 11.2-002).
        serve(db, host=host, port=port, run_id=run, open_browser=open_browser)
    except OSError as exc:
        # Almost always "address already in use" — a dashboard is likely running.
        typer.echo(
            f"could not bind {host}:{port} ({exc}). A dashboard is probably already "
            f"running at http://{host}:{port} — use `sdlc dashboard --restart` "
            f"to replace it, or `--stop` to stop it.",
            err=True,
        )
        raise typer.Exit(code=0) from exc


@app.command(help=PLANNED_SUBCOMMANDS["state"])
def state(
    run: str | None = typer.Option(
        None, "--run", help="Run id to dump (default: the most recent run)."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the stage rows as a JSON array."
    ),
) -> None:
    """Dump the persisted state-machine rows for a run, for debugging.

    Reads the ledger **read-only** and prints one line per stage attempt
    (story id, stage, status, attempt, PR, branch) in a stable, greppable
    format. With ``--json`` it emits the same rows as an array. When there is no
    run it says so and exits 0.
    """
    from sdlc.build import Ledger
    from sdlc.ledger_view import default_db_path
    from sdlc.status import format_state, state_report

    db_path = db or default_db_path()
    ledger = Ledger(db_path)
    # Migrate a pre-existing (possibly stale) ledger before the read-only dump.
    ledger.ensure_migrated()
    rid = run or ledger.latest_run_id()

    if rid is None:
        if as_json:
            typer.echo(json.dumps([]))
        else:
            typer.echo(f"no build run found in ledger: {db_path}")
        raise typer.Exit(code=0)

    rows = state_report(ledger, rid)
    if as_json:
        typer.echo(json.dumps(rows, default=str))
        raise typer.Exit(code=0)

    typer.echo(f"state for run {rid[:8]} ({len(rows)} stage rows)")
    for line in format_state(rows):
        typer.echo(line)
    raise typer.Exit(code=0)


@app.command(help=PLANNED_SUBCOMMANDS["validate"])
def validate(
    agent_type: str = typer.Argument(
        ...,
        help=f"Agent type to validate. One of: {', '.join(sorted(AGENT_SCHEMAS))}.",
    ),
    response_file: Path | None = typer.Argument(
        None,
        help="File containing the agent response. Reads stdin when omitted.",
    ),
) -> None:
    """Validate an agent response against its JSON-schema contract.

    Reads the agent's free-form response (from a file or stdin), extracts the
    fenced result block, validates it against the schema for ``agent_type``,
    and prints the validated JSON. Validation failures exit non-zero with an
    actionable message naming the offending field.
    """
    if agent_type not in AGENT_SCHEMAS:
        valid = ", ".join(sorted(AGENT_SCHEMAS))
        typer.echo(
            f"error: unknown agent type {agent_type!r}; expected one of: {valid}",
            err=True,
        )
        raise typer.Exit(code=2)

    if response_file is not None:
        response = response_file.read_text(encoding="utf-8")
    else:
        response = sys.stdin.read()

    try:
        data = parse_and_validate(agent_type, response)
    except ContractError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(data, indent=2, sort_keys=True))


@app.command(help=PLANNED_SUBCOMMANDS["rollback"])
def rollback(
    run: str | None = typer.Argument(
        None, help="Run id to roll back (default: the most recent run)."
    ),
    to: str = typer.Option(
        ...,
        "--to",
        help="Checkpoint story id to roll back to (kept; later stories reset).",
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
) -> None:
    """Roll a run back to a prior ledger checkpoint.

    ``--to <story_id>`` names the checkpoint to return to: that story and every
    story scheduled before it are kept untouched, while every story scheduled
    *after* it is reset to a fresh unbuilt state (stage history deleted, PR and
    branch cleared, status TODO). The run is reopened so the next ``sdlc
    resume``/``sdlc build`` rebuilds only the reset stories.

    Refuses (non-zero exit) when the checkpoint does not exist or when the
    rollback would discard a story whose PR has already merged — a merged PR is
    committed work the ledger cannot unwind. Rolling back to the latest story is
    a benign no-op.
    """
    from sdlc.ledger_view import Ledger, default_db_path
    from sdlc.rollback import RollbackError, run_rollback

    db_path = db or default_db_path()
    ledger = Ledger(db_path)
    # Migrate a pre-existing (possibly stale) ledger before rollback reads/writes.
    ledger.ensure_migrated()
    try:
        result = run_rollback(ledger, run, to)
    except RollbackError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if not result.reset_stories:
        typer.echo(
            f"nothing to roll back: '{to}' is already the latest checkpoint in "
            f"run {result.run_id[:8]}."
        )
        raise typer.Exit(code=0)

    typer.echo(
        f"rolled run {result.run_id[:8]} back to '{result.checkpoint}': "
        f"reset {len(result.reset_stories)} story(ies) "
        f"({', '.join(result.reset_stories)}) — "
        f"run `sdlc resume` to rebuild them."
    )
    raise typer.Exit(code=0)


@app.command(help=PLANNED_SUBCOMMANDS["reconcile"])
def reconcile(
    run: str | None = typer.Argument(
        None, help="Run id to reconcile (default: the most recent run)."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
) -> None:
    """Re-check a finished/interrupted run against ``origin/main``, then re-terminal.

    The manual counterpart to the automatic close-out reconciliation (Story
    12.3-001): a recovery verb in the same spirit as ``resume``/``rollback`` — not
    new orchestration. When an overnight run aborts (e.g. a 429) before its
    already-open PRs were merged by hand the next morning, the ledger can show
    FAILED days later even though the work shipped. ``sdlc reconcile`` runs the
    same ``reconcile_run`` algorithm as close-out: it fetches ``origin``,
    reclassifies any parked story whose ``feature/<id>`` work is provably on the
    base, re-stamps the run terminal, and prints a human summary.

    Defaults to the most recent run (mirrors ``rollback``). Idempotent — a run
    with nothing to reconcile reports "nothing to reconcile" and exits 0. Offline
    / no-remote degrades to a clean skip. No ledger / no runs reports cleanly;
    only a genuinely-unknown *explicit* run id exits non-zero, and no spurious
    empty ledger is ever created.
    """
    from sdlc.ledger_view import Ledger, default_db_path
    from sdlc.reconcile import reconcile_run

    db_path = db or default_db_path()
    ledger = Ledger(db_path)
    # Migrate a pre-existing (possibly stale) ledger before reconcile reads it, so
    # an old DB never crashes with "no such column". No-op when no DB exists.
    ledger.ensure_migrated()

    # An explicit run id that does not exist is the one genuinely-unknown case
    # that exits non-zero; absence of any ledger/run reports cleanly below.
    if run is not None and ledger.run_row(run) is None:
        typer.echo(f"error: no such run '{run}' in ledger: {db_path}", err=True)
        raise typer.Exit(code=2)

    result = reconcile_run(ledger, run)

    if not result.run_id:
        typer.echo(f"no build run found in ledger: {db_path}")
        raise typer.Exit(code=0)

    if result.skipped:
        typer.echo(
            f"reconcile skipped for run {result.run_id[:8]}: could not fetch "
            "origin (offline / no remote)."
        )
        raise typer.Exit(code=0)

    if not result.changed:
        typer.echo(f"nothing to reconcile: run {result.run_id[:8]} unchanged.")
        raise typer.Exit(code=0)

    for item in result.reclassified:
        sha = item.get("sha") or ""
        typer.echo(
            f"  {item['story_id']}: {item['from_status']} → DONE "
            f"via {item['signal']}" + (f" ({sha[:8]})" if sha else "")
        )
    typer.echo(
        f"reconciled {len(result.reclassified)} story(ies) to DONE; "
        f"run {result.run_id[:8]} {result.run_status_before} → "
        f"{result.run_status_after}."
    )
    raise typer.Exit(code=0)


@app.command(help=PLANNED_SUBCOMMANDS["clean"])
def clean(
    force: bool = typer.Option(
        False,
        "--force",
        "--yes",
        help="Actually remove the candidates. Without it, clean is dry-run only.",
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the plan as JSON instead of a human summary."
    ),
) -> None:
    """Safely garbage-collect build leftovers — dry-run by default.

    Reclaims three classes of cruft a finished/crashed build leaves behind:
    orphaned ``agent-*`` git worktrees, squash-merged ``feature/<id>`` branches,
    and stale ``.sdlc-state.db.logs/`` transcript dirs. It is **dry-run by
    default** — it reports what it *would* remove and removes nothing until
    ``--force``/``--yes`` is passed.

    Safe to run beside a live build: every candidate is cross-checked against the
    host run registry + a live-pid probe, so a worktree or branch an
    ``IN_PROGRESS`` run owns (here or in another session/clone) is never touched.
    "Merged" is decided by the ledger (``status=DONE``) and the PR's merge state —
    not ``git branch --merged``, which misreports squash-merges. Removals are
    recoverable where git allows (a deleted branch's tip stays reachable via
    reflog) and the command never pushes to or fetches from the remote. Exits 0.
    """
    from sdlc.clean import run_clean
    from sdlc.ledger_view import default_db_path

    db_path = db or default_db_path()
    plan = run_clean(db_path=db_path, force=force)

    if as_json:
        typer.echo(json.dumps(plan.to_dict(), default=str))
        raise typer.Exit(code=0)

    verb = "removed" if force else "would remove"
    if not plan.candidates:
        typer.echo("clean: nothing to do — workspace already tidy.")
        raise typer.Exit(code=0)

    for kind, label in (("worktree", "worktrees"), ("branch", "branches"), ("logs", "logs")):
        items = plan.by_kind(kind)
        if not items:
            continue
        typer.echo(f"{label} ({len(items)}):")
        for item in items:
            mark = "✓" if item.removed else ("·" if not force else "✗")
            target = item.path or item.name
            typer.echo(f"  {mark} {verb}: {target} — {item.reason}")

    if force:
        typer.echo(f"clean: removed {plan.removed_count}/{plan.total} candidate(s).")
        for err in plan.errors:
            typer.echo(f"  warning: {err}", err=True)
    else:
        typer.echo(
            f"clean: {plan.total} candidate(s) — dry-run, nothing removed. "
            "Re-run with --force to act."
        )
    raise typer.Exit(code=0)


@app.command(help=PLANNED_SUBCOMMANDS["sast"])
def sast(
    report_file: Path | None = typer.Argument(
        None,
        help="semgrep --json report. Reads stdin when omitted.",
    ),
    config_file: Path = typer.Option(
        Path(".sast-config.yaml"),
        "--config",
        help="Per-repo SAST overrides (suppressions, extra rulesets).",
    ),
) -> None:
    """Classify a semgrep report into a SAST gate verdict.

    Reads a semgrep ``--json`` report (from a file or stdin), applies any
    per-repo suppressions from ``.sast-config.yaml``, and prints the verdict as
    ``SAST_STATUS: CLEAN | WARN | BLOCK`` followed by one line per gating
    finding. Exits 0 for CLEAN/WARN and 1 for BLOCK so a shell gate can branch
    on the exit code.
    """
    from sdlc.security_scan import (
        SastConfigError,
        SastReportError,
        classify_report,
        load_sast_config,
    )

    report = (
        report_file.read_text(encoding="utf-8") if report_file else sys.stdin.read()
    )

    try:
        config = load_sast_config(config_file)
        result = classify_report(report, config=config)
    except (SastReportError, SastConfigError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"SAST_STATUS: {result.status}")
    for finding in result.findings:
        typer.echo(f"  [{finding.severity}] {finding.location()} {finding.check_id}")
    for finding in result.suppressed:
        typer.echo(f"  [suppressed] {finding.location()} {finding.check_id}")

    raise typer.Exit(code=1 if result.status == "BLOCK" else 0)


@app.command(help=PLANNED_SUBCOMMANDS["depscan"])
def depscan(
    report_file: Path | None = typer.Argument(
        None,
        help="osv-scanner --format=json report. Reads stdin when omitted.",
    ),
    suppressions_file: Path = typer.Option(
        Path(".dep-scan-suppressions.yaml"),
        "--suppressions",
        help="Per-repo OSV-ID suppressions (each needs a reason and an expiry).",
    ),
) -> None:
    """Classify an osv-scanner report into a dependency-scan gate verdict.

    Reads an osv-scanner ``--format=json`` report (from a file or stdin),
    applies any per-repo suppressions from ``.dep-scan-suppressions.yaml``, and
    prints the verdict as ``DEP_SCAN_STATUS: CLEAN | WARN | BLOCK`` followed by
    one line per gating finding. Exits 0 for CLEAN/WARN and 1 for BLOCK so a
    shell gate can branch on the exit code. A malformed report or an expired
    suppression exits 2.
    """
    from sdlc.dependency_scan import (
        DepScanConfigError,
        DepScanReportError,
        classify_osv_report,
        load_dep_scan_suppressions,
    )

    report = (
        report_file.read_text(encoding="utf-8") if report_file else sys.stdin.read()
    )

    try:
        suppressions = load_dep_scan_suppressions(suppressions_file)
        result = classify_osv_report(report, suppressions=suppressions)
    except (DepScanReportError, DepScanConfigError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"DEP_SCAN_STATUS: {result.status}")
    for finding in result.findings:
        typer.echo(
            f"  [{finding.severity}] {finding.coordinate()} "
            f"({finding.ecosystem}) {finding.osv_id}"
        )
    for finding in result.suppressed:
        typer.echo(f"  [suppressed] {finding.coordinate()} {finding.osv_id}")

    raise typer.Exit(code=1 if result.status == "BLOCK" else 0)


@app.command(help=PLANNED_SUBCOMMANDS["supplychain"])
def supplychain(
    root: Path = typer.Argument(
        Path("."),
        help="Repo root to scan (hooks/, skills/, plugins/*/skills/, mcp, settings.json).",
    ),
    allowlist_file: Path = typer.Option(
        Path(".supply-chain-allowlist.yaml"),
        "--allowlist",
        help="Per-finding allowlist (each entry needs path, pattern, and reason).",
    ),
) -> None:
    """Scan installed hooks/skills/MCP/settings for dangerous patterns.

    Treats those config artifacts as supply-chain inputs and flags egress tools,
    MCP auto-trust, ANTHROPIC_BASE_URL overrides, encoded payloads, and hidden
    Unicode. Prints ``SUPPLY_CHAIN_STATUS: CLEAN | WARN | BLOCK`` followed by one
    line per gating finding (file, line, pattern). Exits 0 for CLEAN/WARN and 1
    for BLOCK so a CI gate can branch on the exit code. A malformed allowlist
    exits 2.
    """
    from sdlc.supply_chain_scan import (
        SupplyChainConfigError,
        load_allowlist,
        scan_repo,
    )

    try:
        allowlist = load_allowlist(allowlist_file)
        result = scan_repo(root, allowlist=allowlist)
    except SupplyChainConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"SUPPLY_CHAIN_STATUS: {result.status}")
    for finding in result.findings:
        # The sha256 is printed so an operator can copy path/line/pattern/sha256
        # straight into a .supply-chain-allowlist.yaml entry.
        typer.echo(
            f"  [{finding.band}] {finding.location()} {finding.pattern_id}"
            f" sha256:{finding.digest} — {finding.description}"
        )
    for finding in result.suppressed:
        typer.echo(
            f"  [suppressed] {finding.location()} {finding.pattern_id}"
            f" sha256:{finding.digest}"
        )

    raise typer.Exit(code=1 if result.status == "BLOCK" else 0)


@app.command(name="sync-check", help=PLANNED_SUBCOMMANDS["sync-check"])
def sync_check(
    source_dir: Path = typer.Argument(
        ...,
        help="Source-of-truth shared-skills directory (this repo).",
    ),
    consumer_dir: Path = typer.Argument(
        ...,
        help="Consumer submodule checkout (e.g. the nix-install Codex mirror).",
    ),
) -> None:
    """Verify a consumer's shared-skills submodule mirrors the source of truth.

    Run this after ``git submodule update --remote`` in a consumer repo to
    confirm the propagation landed byte-for-byte. Exits 0 when every shared
    skill is in sync, 1 when any skill drifted or is missing/extra, and 2 when
    a directory is missing entirely.
    """
    from sdlc.sync import SkillState, parity_report

    try:
        report = parity_report(source_dir, consumer_dir)
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if report.in_sync:
        typer.echo(f"shared skills in sync ({len(report.skills)} skills).")
        raise typer.Exit(code=0)

    for skill in report.skills:
        if skill.state is not SkillState.IN_SYNC:
            typer.echo(f"{skill.name}: {skill.state.value}")
    typer.echo("shared skills drifted — run `git submodule update --remote`.")
    raise typer.Exit(code=1)


@app.command(help=PLANNED_SUBCOMMANDS["repair"])
def repair(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would be restored without changing anything.",
    ),
    claude_dir: Path = typer.Option(
        None,
        "--claude-dir",
        help="Claude config dir to repair (default: ~/.claude).",
        show_default=False,
    ),
    root: Path = typer.Option(
        None,
        "--root",
        help="Framework repo root that owns the managed set (default: this install).",
        show_default=False,
    ),
) -> None:
    """Restore the framework's managed symlinks/config without a full reinstall.

    Recreates any of the install's managed symlinks (``CLAUDE.md``, ``agents/``,
    ``hooks/``, the plugin marketplace, …) that are missing or have drifted to a
    wrong target. A real file occupying a managed slot is moved into a timestamped
    backup dir (never deleted) before linking. Idempotent: a healthy install is a
    no-op, and nothing outside the managed set is ever touched. ``--dry-run``
    reports the plan without acting.
    """
    from sdlc.repair import (
        RepairAction,
        apply_plan,
        build_plan,
        default_backup_dir,
        default_claude_dir,
        default_repo_root,
    )

    repo_root = (root or default_repo_root()).resolve()
    cdir = claude_dir or default_claude_dir()

    plan = build_plan(repo_root, cdir)
    if plan.healthy:
        typer.echo(
            f"install healthy — {len(plan.artifacts)} managed artifacts in place, "
            "nothing to repair."
        )
        raise typer.Exit(code=0)

    results = apply_plan(plan, dry_run=dry_run, backup_dir=default_backup_dir(cdir))

    prefix = "[dry-run] would " if dry_run else ""
    restored = 0
    for r in results:
        a = r.artifact
        if r.action is RepairAction.LINKED:
            typer.echo(f"{prefix}link {a.rel_dest} → {a.src} (was missing)")
        elif r.action is RepairAction.RELINKED:
            typer.echo(f"{prefix}relink {a.rel_dest} → {a.src} (pointed elsewhere)")
        elif r.action is RepairAction.BACKED_UP:
            typer.echo(
                f"{prefix}back up {a.rel_dest} to {r.backup_path} and link → {a.src} "
                "(a real file occupied the slot)"
            )
        else:
            continue
        restored += 1

    verb = "would restore" if dry_run else "restored"
    typer.echo(f"{verb} {restored} managed artifact(s).")
    raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
