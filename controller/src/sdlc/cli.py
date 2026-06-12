# ABOUTME: Typer entry point for the `sdlc` controller CLI (Story 7.1-001).
# ABOUTME: Ships --version, --help, an init stub, and stubs for every subcommand.

from __future__ import annotations

import typer

from sdlc import __version__

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


@app.command(help=PLANNED_SUBCOMMANDS["build"])
def build() -> None:
    """Run the build-stories orchestration (stub, see Story 7.3-001)."""
    typer.echo("build: not yet implemented.")


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
def validate() -> None:
    """Validate an agent response against its schema (stub, see Story 7.2-001)."""
    typer.echo("validate: not yet implemented.")


@app.command(help=PLANNED_SUBCOMMANDS["rollback"])
def rollback() -> None:
    """Roll a run back to a prior checkpoint (stub)."""
    typer.echo("rollback: not yet implemented.")


if __name__ == "__main__":
    app()
