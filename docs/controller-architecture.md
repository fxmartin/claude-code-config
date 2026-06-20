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
| `sdlc/registry.py` | Host-level run registry — a cross-repo discovery cache for `sdlc runs`/dashboard (Story 11.2-001). |

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

## The run registry (cross-repo discovery)

The per-repo ledger (`.sdlc-state.db`) is authoritative for a single run's
detail, but it can only be found if you already know its path. To let **one**
dashboard watch builds running in several repos at once, each `sdlc build`
announces itself in a host-level **registry** (`sdlc/registry.py`).

- **Location.** `default_registry_path()` resolves `SDLC_REGISTRY_PATH` (explicit
  override, used by tests) → `XDG_STATE_HOME/sdlc/registry.json` →
  `~/.sdlc/registry.json`. It is a single JSON array of run entries.
- **Lifecycle.** `run_build` calls `Registry.register` right after `run_create`
  (entry: `run_id`, absolute `repo`, ledger `db`, `scope`, `pid`, `status`,
  `started_at`, `total`, `completed`) and `Registry.mark_finished` at clean
  close-out (stamps the terminal `status`, `finished_at`, reconciled `completed`).
  Both calls are best-effort — a registry IO error never fails a build.
- **Concurrency + atomicity.** Two builds may register at once, so every
  read-modify-write runs under an exclusive `flock` on a sidecar `.lock` file and
  commits via an atomic `os.replace` of a temp file. A missing or corrupt file
  degrades to an empty list — a damaged cache must never break discovery.
- **Stale/dead detection.** The registry is a cache, not truth: a crashed build
  leaves an `IN_PROGRESS` entry whose `finished_at` is never stamped.
  `derive_state` reports such an entry as `DEAD` when its `pid` is no longer alive
  (`os.kill(pid, 0)`), so it does not linger as in-progress forever.
  `Registry.prune` drops `DEAD` entries (and, optionally, finished ones).
- **`sdlc runs`** lists the registry view (repo, scope, derived state, progress);
  `--json` emits it for tooling and `--prune` clears crashed entries first.

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

### Streaming vs captured dispatch (Story 11.1-001)

The default command is `claude -p --output-format stream-json --verbose
--dangerously-skip-permissions`. When the resolved command requests
`stream-json`, `dispatch_agent` takes the **streaming** path
(`_dispatch_streaming`): it launches the agent with `subprocess.Popen`, writes
the prompt to stdin, and reads stdout **line by line**. Each line is appended
and flushed to the per-stage transcript
(`.sdlc-state.db.logs/<run>/<story>-<stage>-<attempt>.log`) as it arrives, so
`tail -f` on that file shows live activity within ~1 s instead of only when the
stage finishes. stderr is drained on a background thread to avoid a pipe-buffer
deadlock. Because reading stdout line-by-line blocks, a watchdog timer enforces
the wall-clock `timeout` the captured path got for free from
`subprocess.run(timeout=…)`: a stalled agent is killed at the deadline, which
closes stdout, ends the read loop, and surfaces as an `AgentDispatchError`
instead of hanging the build.

The terminal `result` event in the stream has the **same shape** as the old
`--output-format json` envelope, so the controller captures it during the loop
and hands it to a shared `_interpret` step: `<<<RESULT_JSON>>>` extraction,
`usage`/`total_cost_usd`/`session_id` capture, and schema validation are
byte-for-byte identical to the captured path. Unknown / non-JSON stream lines
are teed to the transcript but ignored for control flow.

Any command **without** `stream-json` (a custom `SDLC_AGENT_CMD`, or an older
agent) takes the **captured** path (`_dispatch_captured`, the original
`subprocess.run` with `capture_output=True`). A streamed run whose terminal
`result` event never arrives degrades gracefully: `_interpret` falls back to
parsing the accumulated stdout exactly as the captured path would, so a
malformed stream never fails the run on its own. The streaming path keeps the
verbatim stream in the transcript (it is the live view); the captured envelope
path still rewrites the transcript to the readable agent text.

On the streaming path the same per-stage progress sink also accrues **running
token usage** (Story 11.1-003): a `UsageAccumulator` sums each assistant turn's
`message.usage`, and the running total is written to that stage attempt's row
(`stages.input_tokens` etc.) via `Ledger.stage_set_usage` as events arrive — so
a query mid-stage (`sdlc status`, the dashboard) sees spend building up rather
than only the end-of-stage total. The terminal `result` event is excluded from
the accumulator; when the stage finishes, `_record_stage_usage` overwrites the
row with the envelope's authoritative `usage`/`total_cost_usd` — **final value
wins, no double counting**, because both writes target the same columns. Cost is
not carried per turn in `stream-json`, so it lands only at this reconciliation
(still a strict improvement over the previous run-level-only total). The per-run
and per-story/stage breakdowns surface through the existing query path
(`stage_breakdown`, `_aggregate_run_usage`, `status_snapshot`) with no new schema.

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
