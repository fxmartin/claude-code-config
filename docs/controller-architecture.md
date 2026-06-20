<!-- ABOUTME: Architecture of the external sdlc controller and the build state machine. -->
<!-- ABOUTME: Story 7.3-001 — documents the port of build-stories out of the skill. -->

# Controller Architecture (Epic-07)

The `sdlc` controller (`controller/`) owns the autonomous-SDLC state machine.
Before Epic-07 the orchestration lived inside the `build-stories` skill prompt,
which meant an LLM interpreted the control flow. Story 7.3-001 ports that
control flow into deterministic Python. The skill is now a thin wrapper that
shells out to `sdlc build $ARGUMENTS`.

## Module map

| Module | Responsibility |
|--------|----------------|
| `sdlc/cli.py` | Typer entry point. `build` parses args and drives `run_build`. |
| `sdlc/build.py` | The state machine: options, `Ledger`, preflight, prompts, `run_build`. |
| `sdlc/cohort.py` | Pure dependency-cohort scheduling + `--limit` truncation. |
| `sdlc/dispatch.py` | The agent-dispatch boundary — shells out, validates output. |
| `sdlc/discovery.py` | Reads stories from the markdown epic files into the queue. |
| `sdlc/contracts.py` | JSON-schema parse + validation (Story 7.2-001). |
| `sdlc/ledger_view.py` | DB-path resolution + markdown render hook. |
| `sdlc/resume.py` | Crash-resume: derives each story's resume point from the ledger and re-enters the loop (Story 10.1-001). |
| `sdlc/status.py` | Read-side `state` helpers — a greppable state-machine dump (Story 10.1-001). |
| `sdlc/rollback.py` | Returns a run to a prior checkpoint by resetting the later stories (Story 10.2-001). |

## The state machine

```
preflight ─▶ discovery ─▶ cohorts ─▶ for each story:
                                        build ─▶ coverage ─▶ review ─▶ merge
                                          │         │          │        │
                                          └─────────┴──────────┴────────┘
                                                     ▼ (on failure)
                                                  bugfix loop  (max 2 attempts)
```

1. **Preflight** — `default_preflight` shells out to the detected test command
   (`uv run pytest`, `npm test`, `make test`, or `bats test/`). A red suite
   aborts the run before any agent is dispatched (skip with `--skip-preflight`).
2. **Discovery** — `discover_queue(scope)` parses `##### Story X.Y-NNN:` headers
   from `docs/stories/epic-*.md`, extracting priority, points, and intra-project
   dependencies.
3. **Cohort scheduling** — `compute_cohorts` groups stories whose dependencies
   are all satisfied (already merged or in an earlier cohort). A cycle is a hard
   `ValueError`, never an infinite loop.
4. **Per-story execution** — each story walks `build → coverage → review →
   merge` (coverage is skipped under `--skip-coverage`). Each stage dispatches
   an agent and the response is validated against its schema *before* the next
   stage runs.
5. **Bugfix loop** — a stage failure (agent FAILED status, dispatch error, or
   **schema-invalid output**) routes to the bugfix agent. A fix is confirmed
   only when `fix_status == FIXED` and `tests_passing` is true; the stage is
   then retried. Bounded to `MAX_BUGFIX_ATTEMPTS` (2) per story.
6. **Dependency blocking** — if a dependency ends FAILED/BLOCKED/SKIPPED, the
   dependent story is marked BLOCKED and never dispatched.

## Why schema validation is the safety boundary

Every agent returns a `<<<RESULT_JSON>>> … <<<END_RESULT>>>` block. The
controller parses and validates it against the schemas bundled in the `sdlc`
package (`controller/src/sdlc/schemas/`). A missing or
malformed block raises a `ContractError`, which the state machine treats
exactly like a build failure — the next stage never runs on garbage. This is
the deterministic-control-flow guarantee Epic-07 was created for.

## The ledger

`Ledger` writes the Epic-04 SQLite schema (`state/schema.sql`) using stdlib
`sqlite3`. Every stage transition is persisted **before** the next stage begins,
so a crash leaves a resumable state. The DDL is embedded in `build.py` so a
standalone `uv tool install` works with no repo checkout. The markdown
read-model (`docs/stories/.build-progress.md`) is regenerated from the ledger
via `sdlc-state.sh render` when that script is present (best-effort — a render
failure never fails an otherwise-good build).

## Resume, status, and state

Because every stage transition is persisted **before** the next stage runs, the
ledger alone is enough to recover an interrupted run — no separate journal.

- **`sdlc resume [scope]`** (`sdlc/resume.py`) finds the most recent run still
  marked `IN_PROGRESS` (a clean close-out stamps a terminal status, so a run
  left `IN_PROGRESS` is by definition interrupted), recomputes the queue from
  the markdown epics, and derives a per-story resume point with
  `compute_resume_plan`: the pipeline stages that already have a DONE attempt
  are skipped, the first stage still owed is re-entered, and the attempt counter
  continues past any crashed attempt (so a half-written IN_PROGRESS row is never
  overwritten). The PR number and bugfix sequence carry forward. `run_resume`
  reuses `_run_story` (via its `done_stages` / `start_attempt` / `pr_number` /
  `bugfix_seq` parameters), `compute_cohorts`, and the same dependency-blocking
  and close-out logic as `run_build`, so a resumed run reaches the same end
  state a full build would. Completed stories are never rebuilt; a run with no
  incomplete stories is a no-op.
- **`sdlc status`** reports the active/most-recent run from the ledger
  (`status_snapshot`): scope, run id, per-story current stage, and aggregate
  counts (done / failed / blocked / in-progress). It never reads
  `.build-progress.md`.
- **`sdlc state`** (`sdlc/status.py` + `Ledger.state_rows`) dumps every stage
  row (story id, stage, status, attempt, PR, branch) in a stable, greppable
  format for debugging.

## Rollback

A run's stories are scheduled in a stable order (the ledger's insertion order,
which mirrors cohort order), so any completed story is a natural **checkpoint**
— no separate checkpoint table is needed.

- **`sdlc rollback [run] --to <story_id>`** (`sdlc/rollback.py`) returns a run to
  the checkpoint named by `--to`: that story and every story scheduled *before*
  it are kept untouched, while every story scheduled *after* it is reset to a
  fresh unbuilt state via `Ledger.reset_story` (stage rows deleted, PR/branch
  cleared, status `TODO`). The run is reopened to `IN_PROGRESS`, so the next
  `sdlc resume`/`sdlc build` rebuilds **only** the reset stories — the checkpoint
  is never rebuilt.
- **Guard rails.** `run_rollback` raises `RollbackError` (CLI exit 2) and mutates
  nothing when there is no run, when the checkpoint is not a story in the run, or
  when a to-be-reset story has an already-merged PR (a `merge` stage marked
  DONE). A merged PR is committed work the ledger cannot unwind — revert it in
  git instead. Rolling back to the latest story is a benign no-op.

## There is no `init` verb

Epic-07 scaffolded an `init` stub, but `build` already creates the SQLite ledger
on first use (`Ledger.init()` runs inside `run_build` before any story is
dispatched), so a separate workspace-scaffold command had no distinct purpose.
Story 10.2-001 resolved it by **removal** rather than inventing a job for it; see
the addendum in `docs/adr/001-controller-runtime.md`.

## The dispatch seam

`dispatch_agent(agent_type, prompt, …)` is the single place the controller
shells out to a Claude Code agent (prompt on stdin, response on stdout). It is
the only seam tests mock: the entire suite injects a fake dispatcher that
returns canned schema-valid responses, so **no real agent is ever invoked in
CI**. Infrastructure failures (non-zero exit, timeout, missing executable)
surface as `AgentDispatchError`; contract failures surface as the
`ContractError` subclasses from `sdlc.contracts`.

## Backward compatibility

Users still type `/build-stories` in Claude Code. The skill shells out to
`sdlc build $ARGUMENTS` (falling back to `uv run sdlc` from the `controller/`
checkout when the tool is not installed). The migration is invisible to end
users; the worker agent prompts in the skill directory are unchanged.

## Known divergences from the legacy skill

The port preserves the skill's argument surface and end state, but a few of the
skill's behaviours are intentionally not (yet) reproduced. They are listed here
so the fidelity claim is honest:

- **Sequential execution only.** The controller walks cohorts and stories in a
  single thread regardless of `--sequential`. The skill's parallel
  batch-per-stage worktree scheduling (up to five concurrent agents, sequential
  merge stage) is deferred. The flag is accepted for compatibility; it is a
  no-op until parallel dispatch lands.
- **`--auto` is inert.** The controller is always non-interactive (it never
  prompts), so it already behaves as the skill did under `--auto` for the
  no-prompt aspect. It does **not** reclassify a FAILED story's dependents as
  SKIPPED — they are marked BLOCKED. The flag is accepted for compatibility.
- **No cmux sidebar emission from the controller.** Per-stage observability is
  written to the ledger `events` table and surfaced via the markdown render
  hook; the controller does not call `cmux-bridge.sh` directly. The skill
  wrapper still owns any cmux interaction (unchanged contract).
- **`current_stage` is not written.** The column exists in the schema for the
  resume story (4.3-001); the build state machine does not populate it yet.
