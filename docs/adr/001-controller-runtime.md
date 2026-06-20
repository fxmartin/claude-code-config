<!-- ABOUTME: ADR-001 recording the runtime choice for the sdlc controller. -->
<!-- ABOUTME: Decision: Python + uv + Typer + Pydantic. Status: Accepted. -->

# ADR-001: Controller Runtime

- **Status**: Accepted
- **Date**: 2026-06-12
- **Epic / Story**: Epic-07 / Story 7.1-001
- **Deciders**: FX

## Context

Epic-07 introduces an external controller that owns the autonomous-SDLC state
machine, validating every agent response against a JSON-schema contract instead
of leaving orchestration logic inside a Claude skill prompt. Before any controller
code can be written, the runtime has to be chosen and recorded, because the choice
constrains every subsequent story in the epic (typed contracts, the build port,
and the Codex mirror sync).

Two candidate runtimes were considered:

1. **Python + uv** (with Typer for the CLI and Pydantic for typed I/O).
2. **TypeScript + Bun** (with Commander for the CLI and `better-sqlite3` for the
   ledger, zod for schema validation).

## Decision

**The controller is written in Python, managed with `uv`, using Typer for the CLI
and Pydantic for typed I/O and schema validation.**

The package lives in `controller/`, is installable via `uv tool install .`, and
exposes a single console-script entry point: `sdlc`.

## Rationale

- **FX is a Python developer on weekends.** The Bun runtime guidance in
  `CLAUDE.md` is for *user projects*, not for the framework itself. Maintaining
  the controller should sit in FX's strongest language.
- **`sqlite3` is in the Python standard library.** Epic-04's SQLite ledger is the
  controller's primary data model; Python reads and writes it with zero extra
  dependencies. The TypeScript path needs `better-sqlite3`, a native add-on.
- **Pydantic gives schema validation for free.** Typed I/O, env-var parsing, and
  JSON (de)serialization come from one library, which is exactly what Feature 7.2
  (typed agent contracts) needs.
- **`uv tool install` is the lightest cross-platform install path that exists.**
  One line installs the CLI on macOS and WSL2 with an isolated environment and a
  pinned interpreter. No global `node_modules`, no `tsc` build step.
- **The repo already documents Python best practices** in
  `docs/python-best-practices.md`, so contributors have a reference.

## Consequences

### Positive

- Single-language framework core; low maintenance friction for FX.
- Stdlib SQLite, so the ledger has no third-party driver to keep current.
- Pydantic models double as the JSON-schema source of truth (Story 7.2-001).
- One-line cross-platform install and upgrade via `uv tool install` / `uv tool upgrade`.

### Negative / Trade-offs

- Slower cold-start than a Bun binary (acceptable: the controller runs
  long-lived orchestrations, not hot-path request handling).
- Python's type system is gradual, not structural like TypeScript's; we lean on
  Pydantic + `mypy`/`ty` to recover guarantees at the I/O boundary.
- Contributors who only know TypeScript face a small ramp.

## Alternatives Considered

- **TypeScript + Bun**: faster cold-start and a stronger structural type system,
  but a heavier install footprint (Bun or Node + `tsc`) and `better-sqlite3` as
  an extra native dependency. Rejected on install-footprint and language-fit
  grounds, not on technical capability.

## Addendum (2026-06-20, Epic-10 / Story 10.2-001): the `init` verb was removed

Story 7.1-001 scaffolded an `init` subcommand alongside the other stubs, on the
assumption the controller might need an explicit "create the workspace/ledger"
step. It never gained one. `build` (and, transitively, `resume`) create the
SQLite ledger on first use — `run_build` calls `Ledger.init()`, which runs the
schema DDL and migrations idempotently before any story is dispatched. A
separate `init` verb would either duplicate that or do nothing, so it was a dead
end that printed "not yet implemented."

Story 10.2-001 resolved it by **removal** rather than inventing a purpose: the
command and its `PLANNED_SUBCOMMANDS` entry are gone, and `sdlc --help` no longer
lists it. This keeps the CLI surface honest — every listed verb does real work.
If a future need for explicit scaffolding appears (e.g. seeding per-repo config
files), it can be reintroduced with a documented job; until then, the absence is
the correct state.

## References

- `docs/stories/epic-07-external-controller.md` (Story 7.1-001, Design Notes)
- `docs/stories/epic-10-controller-hardening.md` (Story 10.2-001)
- `docs/python-best-practices.md`
