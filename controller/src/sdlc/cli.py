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
    "runs": "List every known run from the host-level registry.",
    "state": "Inspect the persisted state machine for a run.",
    "validate": "Validate an agent response against its JSON schema.",
    "rollback": "Roll a run back to a prior ledger checkpoint.",
    "sync-check": "Verify the Codex mirror's shared-skills submodule is in sync.",
    "sast": "Classify a semgrep report into a CLEAN | WARN | BLOCK gate verdict.",
    "depscan": "Classify an osv-scanner report into a CLEAN | WARN | BLOCK gate verdict.",
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
  --limit=N                 build at most N stories
  --coverage-threshold=N    required new-code coverage % (default 90)
  --preflight-timeout=SEC   abort the preflight gate after SEC seconds (default 600)
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
    result = run_build(
        opts,
        queue=queue,
        ledger=ledger,
        render_view=make_render_view(ledger.db_path),
        registry=Registry(),
    )

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
        f"{result.skipped} skipped."
    )
    clean = result.failed == 0 and result.blocked == 0 and result.needs_attention == 0
    raise typer.Exit(code=0 if clean else 1)


@app.command(help=PLANNED_SUBCOMMANDS["resume"])
def resume(
    scope: str = typer.Argument(
        "all",
        help="Scope of the run to resume: all, epic-NN, an epic name, or X.Y-NNN.",
    ),
    run: str | None = typer.Option(
        None, "--run", help="Resume a specific run id (default: the latest incomplete run)."
    ),
    db: Path | None = typer.Option(
        None, "--db", help="Ledger DB path (default: ./.sdlc-state.db)."
    ),
) -> None:
    """Resume an interrupted build from the SQLite ledger.

    Finds the most recent incomplete run for ``scope`` (a run still marked
    IN_PROGRESS because it never reached a clean close-out), recomputes the
    remaining queue from the markdown epics, and re-enters the 4-stage loop at
    the exact stage each story was interrupted in — branch, PR number, and
    attempt count preserved. Completed stories are not rebuilt. A run with no
    incomplete stories is a no-op that reports "nothing to resume" and exits 0.
    """
    from sdlc.ledger_view import Ledger, default_db_path, make_render_view
    from sdlc.resume import run_resume

    db_path = db or default_db_path()
    ledger = Ledger(db_path)
    result = run_resume(
        scope, ledger=ledger, run_id=run, render_view=make_render_view(db_path)
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
        f"{result.blocked} blocked, {result.needs_attention} need attention "
        f"({result.resumed} resumed)."
    )
    clean = result.failed == 0 and result.blocked == 0 and result.needs_attention == 0
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
) -> None:
    """Show the progress of a build run from the SQLite ledger.

    Reads the ledger **read-only** (safe to poll while a build is writing) and
    prints a run summary, a per-story table, and the most recent events. With
    ``--json`` it emits one object so the build-stories skill can poll it and
    report progress. When there is no ledger or no run yet it says so and exits
    0 — absence means "not started", not an error.
    """
    from sdlc.build import Ledger, status_snapshot
    from sdlc.ledger_view import default_db_path

    db_path = db or default_db_path()
    snap = status_snapshot(Ledger(db_path), run)

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
    typer.echo(
        f"run {run_id[:8]}  {snap['run'].get('status', '?')}  "
        f"{counts['done']}/{counts['total']} done, {counts['failed']} failed, "
        f"{counts['blocked']} blocked, {counts['in_progress']} in progress  "
        f"(scope={snap['run'].get('scope', '?')}, {snap['run'].get('mode', '?')})"
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
        False, "--restart", help="Stop any running dashboard on this host:port, then start fresh."
    ),
) -> None:
    """Serve an auto-refreshing local dashboard of build progress.

    Reads the SQLite ledger read-only and serves it at ``http://host:port`` —
    progress display runs independently of any agent/turn loop, so it stays live
    for the whole build. Runs until Ctrl-C. Binds localhost only by default.

    Use ``--stop`` to stop a (possibly backgrounded) dashboard on this port, or
    ``--restart`` to replace it — handy after upgrading the controller.
    """
    from sdlc.dashboard import serve, stop_dashboard
    from sdlc.ledger_view import default_db_path

    if stop or restart:
        n = stop_dashboard(host, port)
        typer.echo(
            f"stopped {n} dashboard process(es) on {host}:{port}."
            if n
            else f"no dashboard running on {host}:{port}."
        )
        if stop:
            raise typer.Exit(code=0)

    db_path = db or default_db_path()
    try:
        serve(db_path, host=host, port=port, run_id=run, open_browser=open_browser)
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
    try:
        result = run_rollback(Ledger(db_path), run, to)
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

    report = report_file.read_text(encoding="utf-8") if report_file else sys.stdin.read()

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

    report = report_file.read_text(encoding="utf-8") if report_file else sys.stdin.read()

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


if __name__ == "__main__":
    app()
