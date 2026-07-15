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
  issue), `all` (every open issue), or `next` (the highest-priority open bug).
- **Batch mode** — only when the target is `all` or `next` with `--limit=N`,
  read `${CLAUDE_SKILL_DIR}/batch-mode.md` for the batch-only flags and batch
  behavior; the single-issue path never needs it.
- `--skip-coverage` — the build agent opens the PR directly (no coverage gate).
- `--coverage-threshold=N` — required new-code coverage % (default 90).
- `--skip-preflight` — skip the preflight quality gate.
- `--e2e-gate=warn|off` — run the advisory E2E gate after review (default `off`);
  in `warn` mode a FAIL is logged and the fix still merges (it never blocks).
- `--skip-e2e` — alias for `--e2e-gate=off`.

**Changed from the pre-migration skill (issue #436):** two accepted inputs were
deliberately narrowed and now fail with an explicit `FixConfigError` instead of
silently misbehaving:
- **Issue URLs are no longer accepted** — pass the bare `<issue-number>`
  (`sdlc fix 123`, not `sdlc fix https://github.com/owner/repo/issues/123`).
- **`--e2e-gate=block` is no longer supported** — only `warn`/`off` exist. The
  old skill's blocking E2E mode (route a FAIL to the bugfix loop) is out of
  scope for this migration; `warn` is strictly advisory.

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
   on garbage. Model routing mirrors the Balanced profile: build, review, and
   bugfix default to `sonnet` and escalate to `opus` when the investigation
   reports `COMPLEXITY: HIGH` or the issue carries a high-risk/security label
   (`risk:high`, `high-risk`, `security`); coverage stays `sonnet`, merge and
   summary stay `haiku`. A **docs-only** fix (every built file matches the shared
   docs patterns — `**/*.md`, `docs/**`, plus any `.sdlc-change-class.yaml`
   allowlist) skips the coverage dispatch — the controller pushes the branch and
   opens the PR itself — recording it `SKIPPED` with `skip_reason=docs-only`
   (Story 27.2-003); the review still runs.
5. **E2E warn-gate** (optional) — with `--e2e-gate=warn`, an advisory `qa-engineer`
   pass runs the project's existing E2E suite after review; a FAIL is logged and
   merge proceeds. A docs-only fix skips this gate too (recorded `SKIPPED`).
6. **Bugfix loop** — bounded retries per stage before marking the issue FAILED.
7. **Merge** — rebases, squash-merges, closes the issue; a `risk:high` PR with no
   approval parks the run `AWAITING_APPROVAL` rather than force-merging.
8. **Summary** — a best-effort, non-blocking per-fix summary (batch runs add a
   doc-update phase; see `batch-mode.md`).
9. **Ledger writes** — every stage transition is persisted to the SQLite ledger
   (`.sdlc-state.db`) before the next begins, so a `fix` run shows up in
   `sdlc dashboard` beside `sdlc build` runs.

The `*-agent-prompt.md` / `*-gate-prompt.md` template files in this skill
directory are **retained for reference only** — the controller now renders every
fix-stage prompt inline, so the renderers in `controller/src/sdlc/fix_issue.py`
(`render_investigation_prompt`, `render_build_prompt`, `render_coverage_prompt`,
`render_review_prompt`, `render_e2e_prompt`, `render_merge_prompt`,
`render_bugfix_prompt`, `render_summary_prompt`, `render_doc_update_prompt`) are
the authoritative source of what each dispatched agent receives.
