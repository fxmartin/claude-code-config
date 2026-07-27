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

If `sdlc` is not on `PATH`, use the guarded fallback below rather than assuming
a `controller/` checkout is present — most target repos do not have one, and a
bare `cd controller` fails with an unhelpful `no such file or directory`:

```bash
if command -v sdlc >/dev/null 2>&1; then
    sdlc build <arguments from the Use invocation>
elif [ -d controller ]; then
    ( cd controller && uv run sdlc build <arguments from the Use invocation> )
else
    echo "error: sdlc controller not found. Install it with:" >&2
    echo "  uv tool install ./controller" >&2
    exit 1
fi
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

The build pipeline's agent prompts are rendered **in Python** by the controller
(`render_build_prompt`, `render_review_prompt`, `render_bugfix_prompt` and
friends in `controller/src/sdlc/build.py`). The controller never reads the
`.md` files in this skill directory, so editing one does not change what
`sdlc build` dispatches — change the renderer instead.

`sdlc fix` works the same way — its prompts are rendered by `render_*_prompt`
functions in `controller/src/sdlc/fix_issue.py`, not loaded from disk.

The files are still maintained, though, so do not treat them as dead.
`bugfix-agent-prompt.md`, `coverage-gate-prompt.md` and `doc-update-prompt.md`
are symlinks into `skills/_shared/`, single-sourced across the `build-stories`
and `fix-issue` skill directories (Story 27.1-003), and a structural test fails
the build if one stops resolving. Separately, `coverage-gate-prompt.md` and
`merge-update-prompt.md` carry byte ceilings that fail on regrowth. Keep all of
them in step with the Python renderers they mirror; the remaining `.md` files
here carry neither guarantee.
