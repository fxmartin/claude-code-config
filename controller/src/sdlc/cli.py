# ABOUTME: Typer entry point for the `sdlc` controller CLI (Story 7.1-001).
# ABOUTME: Ships --version, --help, an init stub, and stubs for every subcommand.

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
    "init": "Scaffold a controller workspace and SQLite ledger.",
    "build": "Run the full build-stories orchestration for a scope.",
    "resume": "Resume an interrupted build from the ledger state.",
    "status": "Show the current run status and stage progress.",
    "state": "Inspect the persisted state machine for a run.",
    "validate": "Validate an agent response against its JSON schema.",
    "rollback": "Roll a run back to a prior ledger checkpoint.",
    "sync-check": "Verify the Codex mirror's shared-skills submodule is in sync.",
    "sast": "Classify a semgrep report into a CLEAN | WARN | BLOCK gate verdict.",
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


@app.command(help=PLANNED_SUBCOMMANDS["init"])
def init() -> None:
    """Scaffold a controller workspace (stub)."""
    typer.echo("init: not yet implemented (Story 7.1-001 scaffold).")


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
        f"{result.blocked} blocked, {result.skipped} skipped."
    )
    raise typer.Exit(code=0 if result.failed == 0 and result.blocked == 0 else 1)


@app.command(help=PLANNED_SUBCOMMANDS["resume"])
def resume() -> None:
    """Resume an interrupted build (stub)."""
    typer.echo("resume: not yet implemented.")


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
        f"{counts['blocked']} blocked  "
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
    if events:
        typer.echo("recent:")
        for e in events:
            typer.echo(
                f"  {e.get('ts', '')}  {str(e.get('level', '')):<8}"
                f"{str(e.get('source') or ''):<11} {e.get('message', '')}"
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
def state() -> None:
    """Inspect the persisted state machine (stub)."""
    typer.echo("state: not yet implemented.")


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
def rollback() -> None:
    """Roll a run back to a prior checkpoint (stub)."""
    typer.echo("rollback: not yet implemented.")


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
