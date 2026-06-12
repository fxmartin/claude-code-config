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


@app.command(
    help=PLANNED_SUBCOMMANDS["build"],
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def build(ctx: typer.Context) -> None:
    """Run the full build-stories orchestration for a scope.

    Accepts the same arguments the skill does today:
    ``[scope] [--dry-run] [--auto] [--skip-coverage] [--limit=N]
    [--sequential] [--coverage-threshold=N] [--skip-preflight]``. The
    controller owns the state machine; agents are dispatched as subprocesses
    and every response is schema-validated before the next stage runs.
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
def status() -> None:
    """Show current run status (stub)."""
    typer.echo("status: not yet implemented.")


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


if __name__ == "__main__":
    app()
