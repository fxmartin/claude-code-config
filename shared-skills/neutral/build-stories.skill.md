---
name: build-stories
description: Batch build all incomplete stories across epics — thin wrapper that shells out to the external `sdlc` controller, which owns the deterministic state machine.
short_description: Build story queues via the sdlc controller
argument_hint: "[all|resume|epic-NN|epic-name|story-id] [--dry-run] [--auto] [--skip-coverage] [--rebuild] [--limit=N] [--sequential] [--coverage-threshold=N] [--skip-preflight] [--preflight-timeout=N]"
allowed_tools:
- Bash
model_invocation: disabled
invocation_examples:
- build-stories all --auto --limit=5
- build-stories epic-01 --dry-run
- build-stories resume
---
You are a **thin wrapper** around the external `sdlc` controller (Epic-07).

Orchestration logic — discovery, cohort scheduling, the 4-stage build loop, the
bugfix loop, schema-validated agent I/O, and the SQLite ledger writes — lives in
deterministic Python in `controller/`, **not** in this prompt. Your only job is
to invoke the controller and return its exit code.

## Running the controller

Forward the user's arguments to the controller verbatim, then surface its
stdout/stderr and propagate its exit code unchanged. Do not re-implement any of
the orchestration here.

<!-- harness:claude -->
```bash
sdlc build {{ARGUMENTS}}
```

If `sdlc` is not on `PATH`, fall back to running it from the controller
checkout:

```bash
if command -v sdlc >/dev/null 2>&1; then
    sdlc build {{ARGUMENTS}}
elif [ -d controller ]; then
    ( cd controller && uv run sdlc build {{ARGUMENTS}} )
else
    echo "error: sdlc controller not found. Install it with:" >&2
    echo "  uv tool install ./controller" >&2
    exit 1
fi
```
<!-- /harness -->
<!-- harness:codex -->
```bash
sdlc build <arguments from the Use invocation>
```

If `sdlc` is not on `PATH`, run it from the controller checkout instead:

```bash
( cd controller && uv run sdlc build <arguments from the Use invocation> )
```
<!-- /harness -->


## What the controller does (reference only)

The controller (`controller/src/sdlc/`) owns the full lifecycle:

1. **Preflight** — shells out to the detected test command; aborts if red
   (unless `--skip-preflight`).
2. **Discovery** — reads stories from the markdown epic files (or the SQLite
   ledger when resuming).
3. **Cohort scheduling** — groups stories into dependency cohorts.
4. **Build → Coverage → Review → Merge** — dispatches each agent as a
   subprocess and validates every response against its JSON-schema contract
   (`controller/schemas/`). A missing or schema-invalid result block is treated
   as a build failure and routed to the bugfix loop — the next stage never runs
   on garbage.
5. **Bugfix loop** — bounded retries per story before marking it FAILED.
6. **Ledger writes** — every stage transition is persisted to the Epic-04
   SQLite ledger (`.sdlc-state.db`) before the next stage begins.
7. **Markdown view** — regenerates `docs/stories/.build-progress.md` from the
   ledger via `sdlc-state.sh render`.

The worker agent prompts (`*-agent-prompt.md`, `coverage-gate-prompt.md`,
`merge-update-prompt.md`, `e2e-gate.md`, etc.) in this skill directory remain
the source of truth for what each dispatched agent does — the controller renders
them when it dispatches.
