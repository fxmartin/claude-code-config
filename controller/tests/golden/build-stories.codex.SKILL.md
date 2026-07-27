---
name: build-stories
description: Batch build all incomplete stories across epics — thin wrapper that shells
  out to the external `sdlc` controller, which owns the deterministic state machine.
metadata:
  short-description: Build story queues via the sdlc controller
---

# Build Stories

This Codex skill is a thin wrapper around the `sdlc` controller, which owns the build pipeline. It runs the same controller as the Claude `build-stories` skill — there is no Codex-specific orchestration here.

## Invocation

- `Use build-stories all --auto --limit=5`
- `Use build-stories epic-01 --dry-run`
- `Use build-stories resume`

Treat the user arguments as input to the workflow below.

You are a **thin wrapper** around the external `sdlc` controller (Epic-07).

Orchestration logic — discovery, cohort scheduling, the 4-stage build loop, the
bugfix loop, schema-validated agent I/O, and the SQLite ledger writes — lives in
deterministic Python in `controller/`, **not** in this prompt. Your only job is
to invoke the controller and return its exit code.

## Running the controller

Forward the user's arguments to the controller verbatim, then surface its
stdout/stderr and propagate its exit code unchanged. Do not re-implement any of
the orchestration here.

```bash
sdlc build <arguments from the Use invocation>
```

If `sdlc` is not on `PATH`, run it from the controller checkout instead:

```bash
( cd controller && uv run sdlc build <arguments from the Use invocation> )
```

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
