---
name: fix-issue
description: Autonomously fix one or many GitHub issues end-to-end — thin wrapper that shells out to the external `sdlc` controller, which owns the deterministic fix pipeline.
user-invocable: true
disable-model-invocation: false
argument-hint: "<issue-number|next|all> [--skip-coverage] [--e2e-gate=warn|off] [--skip-e2e] [--limit=N] [--coverage-threshold=N] [--skip-preflight] [--sequential] [--concurrency=N]"
allowed-tools: Bash
---

You are a **thin wrapper** around the external `sdlc` controller (issue #436).

Orchestration logic — issue fetch, the stop conditions, investigation, the
build → coverage → review → merge loop, the bugfix loop, the optional E2E
warn-gate, the batch doc-update phase, schema-validated agent I/O, and the
SQLite ledger writes — lives in deterministic Python in `controller/`, **not** in
this prompt. Your only job is to invoke the controller and return its exit code.

## Running the controller

Forward the user's arguments to the controller verbatim, then surface its
stdout/stderr and propagate its exit code unchanged. Do not re-implement any of
the orchestration here.

```bash
sdlc fix $ARGUMENTS
```

If `sdlc` is not on `PATH`, fall back to running it from the controller
checkout:

```bash
if command -v sdlc >/dev/null 2>&1; then
    sdlc fix $ARGUMENTS
elif [ -d controller ]; then
    ( cd controller && uv run sdlc fix $ARGUMENTS )
else
    echo "error: sdlc controller not found. Install it with:" >&2
    echo "  uv tool install ./controller" >&2
    exit 1
fi
```

## Flag surface (forwarded to the controller)

- **Target** (positional, pass exactly one): `<issue-number>` (a single open
  issue), `all` (every open issue — bugs first, then enhancements by priority),
  or `next` (the highest-priority open bug; see `--limit`).
- `--limit=N` — batch only: cap the issue set (`next` defaults to 1).
- `--sequential` — batch only: one issue fully completes before the next.
- `--concurrency=N` — batch only: issue-level worker cap (default 5).
- `--skip-coverage` — the build agent opens the PR directly (no coverage gate).
- `--coverage-threshold=N` — required new-code coverage % (default 90).
- `--skip-preflight` — skip the preflight quality gate.
- `--e2e-gate=warn|off` — run the advisory E2E gate after review (default `off`);
  in `warn` mode a FAIL is logged and the fix still merges (it never blocks).
- `--skip-e2e` — alias for `--e2e-gate=off`.

## What the controller does (reference only)

The controller (`controller/src/sdlc/fix_issue.py`) owns the full lifecycle:

1. **Fetch + stop conditions** — reads the issue(s) via `gh`; a closed / assigned
   elsewhere / `wontfix` issue is a deliberate stop (no run row).
2. **Preflight** — shells out to the detected test command; aborts if red
   (unless `--skip-preflight`).
3. **Investigation** — a `sonnet` agent produces a structured, schema-validated
   fix plan; a BLOCKED investigation parks the issue for a human decision.
4. **Build → Coverage → Review → Merge** — dispatches each agent as a subprocess
   and validates every response against its JSON-schema contract
   (`controller/src/sdlc/schemas/`). A missing or schema-invalid result block is
   treated as a failure and routed to the bugfix loop — the next stage never runs
   on garbage. Model routing is opus-parity with the original skill.
5. **E2E warn-gate** (optional) — with `--e2e-gate=warn`, an advisory `qa-engineer`
   pass runs the project's existing E2E suite after review; a FAIL is logged and
   merge proceeds.
6. **Bugfix loop** — bounded retries per stage before marking the issue FAILED.
7. **Merge** — rebases, squash-merges, closes the issue; a `risk:high` PR with no
   approval parks the run `AWAITING_APPROVAL` rather than force-merging.
8. **Summary + batch doc-update** — a best-effort per-fix summary, and (batch
   only, when ≥1 issue merged) a single best-effort doc-update agent that opens a
   docs PR. Both are non-blocking.
9. **Ledger writes** — every stage transition is persisted to the SQLite ledger
   (`.sdlc-state.db`) before the next begins, so a `fix` run shows up in
   `sdlc dashboard` beside `sdlc build` runs. Batch mode investigates every issue
   first, then serializes only issues that touch overlapping files while
   independent ones run concurrently.

The `*-agent-prompt.md` / `*-gate-prompt.md` template files in this skill
directory are **retained for reference only** — the controller now renders every
fix-stage prompt inline, so the renderers in `controller/src/sdlc/fix_issue.py`
(`render_investigation_prompt`, `render_build_prompt`, `render_coverage_prompt`,
`render_review_prompt`, `render_e2e_prompt`, `render_merge_prompt`,
`render_bugfix_prompt`, `render_summary_prompt`, `render_doc_update_prompt`) are
the authoritative source of what each dispatched agent receives.
