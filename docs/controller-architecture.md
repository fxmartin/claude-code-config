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
| `sdlc/parsers.py` | Pluggable per-harness output parsers — interpret an agent's stdout into a validated `AgentResult`, registered by id (Story 20.1-002). |
| `sdlc/harness.py` | Config-driven harness registry — declares each agent harness (claude, codex, …) and how to invoke it (Story 20.1-001); `dispatch_on_harness` runs an agent on a resolved harness (Story 20.3-001). |
| `sdlc/capability.py` | Harness capability resolution, optional CLI probe, and the preflight mode decision (Story 20.5-001). |
| `sdlc/degradation.py` | Centralized degradation matrix — maps capability gaps to safe fallbacks (parallel→serial, usage "unavailable", rate-limit skipped) (Story 20.5-002). |
| `sdlc/role_routing.py` | Per-role harness routing — maps build/coverage/review/merge/docs to harnesses, fails fast on unknown/disabled (Story 20.2-001). |
| `sdlc/portability.py` | The in-process-agent boundary — cross-harness vs Claude-only skills, with a fail-fast guard pointing at the boundary doc (Story 20.6-002). |
| `sdlc/discovery.py` | Reads stories from the markdown epic files into the queue. |
| `sdlc/contracts.py` | JSON-schema parse + validation (Story 7.2-001). |
| `sdlc/ledger_view.py` | DB-path resolution + markdown render hook. |
| `sdlc/resume.py` | Crash-resume: derives each story's resume point from the ledger and re-enters the loop (Story 10.1-001). |
| `sdlc/status.py` | Read-side `state` helpers — a greppable state-machine dump (Story 10.1-001). |
| `sdlc/rollback.py` | Returns a run to a prior checkpoint by resetting the later stories (Story 10.2-001). |
| `sdlc/reconcile.py` | Verifies parked stories against `origin/main` and recomputes the run terminal — shared by close-out and `sdlc reconcile` (Stories 12.3-001/12.3-002). |
| `sdlc/registry.py` | Host-level run registry — a cross-repo discovery cache for `sdlc runs`/dashboard (Story 11.2-001). |
| `sdlc/clean.py` | Safe workspace garbage collection — dry-run-by-default reclamation of orphan worktrees, merged branches, and stale transcript logs, registry/pid-aware (Story 15.3-001). |
| `sdlc/doctor.py` | Read-side health-check across install/ledger/runs/config/deps — powers `sdlc doctor` (Story 15.1-001). |

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
   The whole command is bounded by `--preflight-timeout` (default 600s), and when
   the project ships `pytest-timeout` the detected pytest command also gets a
   per-test bound (`--timeout=60 --timeout-method=thread`, `PER_TEST_TIMEOUT`) so
   a single hanging agent-added test fails fast instead of stalling the suite
   until the whole-command timeout (Story 12.1-002).

   **Recursion guard (`SDLC_IN_TEST` sentinel).** `default_preflight` runs the
   test command with `SDLC_IN_TEST=1` exported into the *child* environment only
   (the parent env is untouched). The guard then blocks **only the side-effecting
   real path**, never unit coverage (AC3):
   - `run_build` short-circuits (returns `BuildResult(skipped_in_test=True)`,
     reported by the verb as an exit-0 note) when `in_test_sentinel()` is true
     **and** it is a real run — i.e. no fake `dispatcher`/`preflight` was injected
     (`dispatcher is None and preflight is None`). Tests that inject a
     `FakeDispatcher`/stub preflight are deliberately exercising orchestration and
     run unblocked, even when the sentinel is set (the controller's own preflight
     case). Dry-run returns before the guard.
   - The `dashboard` verb short-circuits before `serve()` (after `--stop`/
     `--restart`, which only kill a server) so it never binds a socket under the
     sentinel.

   The net effect: a project test that invokes `sdlc build`/`sdlc dashboard` bare
   cannot recurse into real orchestration (pytest-within-pytest) or bind a server
   and hang the parent run, while the controller's own dry-run, arg/scope-error,
   stubbed-`run_build`, and fake-dispatcher tests all still pass with the sentinel
   set (Story 12.1-002).
2. **Discovery** — `discover_queue(scope)` parses `##### Story X.Y-NNN:` headers
   from `docs/stories/epic-*.md`, extracting priority, points, and intra-project
   dependencies.

   **Dependencies-line convention (Story 12.5-001).** Edge extraction reads only
   the *leading edge list* of a `**Dependencies**:` line — the run of bare
   `X.Y-NNN` ids before the first parenthetical (`(`), `;`, em/en-dash, or
   sentence-ending period (`_dependency_head`/`_parse_dependency_edges` in
   `discovery.py`). Story ids that appear only in parenthetical or sentence prose
   are **not** edges, and a line that leads with `None`/`N/A`/`TBD` resolves to
   zero dependencies. This stops a benignly-worded rationale (e.g.
   `12.3-001 (reconcile flips it once 12.3-004 lands)`) from minting a phantom
   edge to `12.3-004` and crashing `compute_cohorts` with a false cycle. **Authoring
   rule:** put real edges on the `**Dependencies**:` line as a leading
   comma/whitespace ID list (or `None`); put rationale/sequencing notes either in
   a trailing parenthetical/sentence or on a separate `**Sequencing**:` line — the
   parser ignores ids there, but a genuine intended cycle still fails fast with the
   story-named `compute_cohorts` error.
3. **Cohort scheduling** — `compute_cohorts` groups stories whose dependencies
   are all satisfied (already merged or in an earlier cohort). A cycle is a hard
   `ValueError`, never an infinite loop.

   **Concurrent cohort execution (Story 17.1-001).** A `parallel` run (the
   default) dispatches a cohort's *ready* stories through a bounded
   `ThreadPoolExecutor` — at most `effective_concurrency(opts)` at once (default
   **5**, set with `--concurrency=N`). Work is I/O-bound on the agent
   subprocesses, so threads suffice (the same model `adversarial.py` uses). The
   cohort loop is **cohort-barrier scheduled**: `_dispatch_cohort` blocks until
   *every* story in the cohort reaches a terminal/parked outcome before the next
   cohort begins, so dependency ordering is never violated. Within a cohort the
   dependency-block check still runs **before** a story is submitted — a story
   whose dependency did not cleanly finish is marked `BLOCKED` and never
   dispatched — and a worker that raises mid-flight is captured (failure
   isolation): its story is recorded `FAILED` while its peers run to completion.
   Outcomes are applied in cohort (submission) order so the end state is
   deterministic regardless of which worker finished first. `--sequential` (or
   `--concurrency=1`) forces an effective cap of **1**, taking a separate,
   byte-for-byte-unchanged serial path: the budget gate is re-checked before
   every story and a cost-gate/rate-limit park breaks mid-cohort, exactly as
   before. The budget gate on the parallel path is checked once at each cohort
   boundary (the pool cannot interleave a per-story check; the barrier bounds
   mid-cohort spend). `resume` mirrors the same executor and honours the
   persisted (or `--concurrency`-overridden) worker cap. Concurrent ledger writes
   are kept safe by the WAL + `busy_timeout` work in Story 17.1-002 (see
   *Concurrency-safe writes* below).
4. **Per-story execution** — each story walks `build → coverage → review →
   merge` (coverage is skipped under `--skip-coverage`). Each stage dispatches
   an agent and the response is validated against its schema *before* the next
   stage runs.

   **Branch isolation (Story 12.4-001).** The build prompt
   (`render_build_prompt`) cuts the story branch from a freshly-fetched remote
   base — `git fetch origin && git checkout -b feature/<id> origin/main` — not
   the base-less `git checkout -b feature/<id>`. In a real `parallel` run each
   story now builds in its **own git worktree** (see *Per-story worktree
   isolation* below), so concurrent agents no longer share a working dir; a
   `--sequential` run keeps the shared root for back-compat. The merge agent only
   returns HEAD to `main` on its **success** path; on a parked/blocked/conflict
   path it leaves
   the working dir on the story's feature branch. A base-less checkout would then
   stack the next story on that leftover branch, so a later successful merge would
   transitively land the earlier (parked) story's commits on `main` — leaving the
   ledger out of sync with reality. To close the gap on both ends, `run_build` and
   `run_resume` call `_reposition_head(root)` between stories (real runs only —
   injected fakes skip it, like the close-out reconcile guard): it lands HEAD on
   the **local** base branch (`main`/`master`) — stripping the `origin/` prefix
   `_base_ref` yields so the working dir ends on a branch, not detached at
   `origin/main` — regardless of where the merge agent left it. Repositioning is
   best-effort and **never** deletes a
   feature branch or its commits (R10): committed-but-unmerged work on a parked
   branch is preserved. The deliberate tension this accepts: branch-from-`main`
   means a genuinely-incomplete story now FAILS honestly instead of silently
   shipping, and close-out reconciliation (step 9) is what still rescues work that
   *truly* landed.
5. **Envelope re-ask** — a stage that exits cleanly but omits or malforms its
   `<<<RESULT_JSON>>>` block (a `ContractError`) usually means the agent did good
   work and failed only to wrap it. Before any heavier recovery, the controller
   issues a bounded **envelope-only re-ask** (`render_envelope_reask_prompt` →
   `_reask_envelope`): it re-prompts the *same* stage agent to inspect the branch
   it already built and emit just the result block — explicitly **not** to redo
   the work or create new commits (R10). On a schema-valid, success-reporting
   reply the stage is marked DONE and the run proceeds exactly as if the agent
   had emitted the block the first time. The attempt is recorded as a `reask`
   stage row and logged to the ledger `events` (Story 12.1-001).
6. **Bugfix loop** — a stage failure (agent FAILED status, dispatch error, or
   **schema-invalid output**), or an envelope re-ask that still fails, routes to
   the bugfix agent. A fix is confirmed only when `fix_status == FIXED` and
   `tests_passing` is true; the stage is then retried. Bounded to
   `MAX_BUGFIX_ATTEMPTS` (2) per story. Only once this bounded recovery is
   exhausted is the story parked: `NEEDS_ATTENTION` when committed work exists on
   `feature/<story>` (preserved for manual push/MR, R10), otherwise `FAILED`
   (`_exhausted_status`).

   **Awaiting human approval (Story 12.3-003).** A merge blocked *only* by the
   high-risk human-approval gate — a PR carrying `risk:high` (from `risk_gate.py`
   / `.github/workflows/risk-gate.yml`) with no `risk-approved` label or
   `risk-approver` review — is **not** a fixable failure: the bugfix loop cannot
   self-approve and would only exhaust into `FAILED`, misreporting a run that is
   honestly awaiting FX. The merge `merge_status` enum stays `MERGED|FAILED|SKIPPED`
   (re-enumerating it is a non-goal); the block is surfaced **additively** — the
   merge agent sets a documented `block_reason` field (extra properties are
   allowed) and/or names the marker in free text, which `_merge_awaiting_approval`
   recognizes and `_dispatch_stage` tags `kind="awaiting_approval"`. The schema's
   `merge_sha`/`merged_at` are required to be non-empty only when
   `merge_status == MERGED` (`if/then`), so a real blocked response — which has no
   SHA — passes validation and reaches this classification instead of being
   rejected as a contract error first. `_run_story`
   short-circuits that kind to `AWAITING_APPROVAL` **before** the bugfix loop,
   preserving the committed work and open PR (R10). Reconciliation
   (`reconcile_run`) flips the story to `DONE` once FX approves and the PR
   merges. `AWAITING_APPROVAL` is orthogonal to epic-14's `PAUSED`/`RATE_LIMITED`
   (waiting on a *person* vs. waiting on *time*); none of these is `FAILED`.
7. **Commit-message lint** — after any **commit-authoring** stage succeeds
   (build, coverage, a confirmed bugfix, and an *envelope-recovered* build/
   coverage stage), the controller lints the HEAD commit of `feature/<story>`
   against the repo's commitlint rules
   (`load_commitlint_config` → `lint_commit_message`) so a non-compliant header
   never reaches a PR and fails the `commit-format` CI job. On a violation it
   issues a bounded **message-only re-ask** (`render_commit_lint_reask_prompt` →
   `_lint_stage_commit`): the *same* stage agent `git commit --amend`s the header
   into a compliant form — explicitly **not** changing code or adding commits
   (R10) — bounded to `MAX_COMMITLINT_REASK` (2), with the re-ask validated
   against that stage's own schema. It is a graceful no-op when the repo has no
   commitlint config, the commit can't be read, or the message is already
   compliant. If the message is **still** non-compliant once the bounded re-asks
   are exhausted, the story is parked `NEEDS_ATTENTION` (the build/coverage
   success gate) rather than advancing a known-non-compliant commit to
   review/merge/PR — committed work is preserved on the branch (R10), upholding
   the epic's "zero commitlint failures reach a PR" guarantee. (The mid-loop
   lint of a *bugfix* commit is best-effort and does not park: that stage is
   about to be retried, and the retry's own success-time lint is the terminal
   gate.) Each attempt is a `commitlint` stage row, logged to the ledger
   `events` (Story 12.2-002).

   **Compliant by construction (Story 12.2-004).** The lint above is the
   backstop, not the first line of defence. The build prompt no longer asks the
   agent to transcribe the (often long, Title-Case) story title as the commit
   subject — which routinely blew `header-max-length`/`subject-case` and made the
   run depend on the re-ask succeeding. Instead every commit-authoring prompt
   (`render_build_prompt` → `feat`, `render_coverage_prompt` → `test`,
   `render_bugfix_prompt` → `fix`) supplies an already-compliant header built by
   `build_commit_header`
   (`commitlint.compliant_subject`): the subject is lower-cased, stripped of a
   trailing period, and trimmed on a word boundary to the `header-max-length`
   budget left after the fixed `type(scope): ` prefix and the `(#<id>)` tag (which
   reconciliation keys off and is always preserved intact). An already-compliant
   subject is left unchanged (idempotent), so the common case passes the gate on
   the first attempt with **no** re-ask dispatched. When a commit-format re-ask
   *is* still needed and its reply omits or malforms the result envelope (e.g. the
   missing `branch_name` of run `7df64f19`), `_lint_stage_commit` routes that
   contract error through the same **envelope-only recovery** as other stages
   (`_reask_envelope`, step 5) rather than dead-ending the story into
   `NEEDS_ATTENTION` — the amend itself usually landed, so the recovered envelope
   lets the re-lint see the now-compliant message. Only when that recovery *also*
   fails is the story parked.
8. **Dependency blocking** — if a dependency ends FAILED/BLOCKED/SKIPPED/
   NEEDS_ATTENTION/AWAITING_APPROVAL, the dependent story is marked BLOCKED and
   never dispatched. These all count as not-done: the dependency's work is
   committed but unmerged (parked for manual push/MR, a commit-message fix, or
   FX's high-risk approval), so a dependent built on top of it would race
   incomplete work.
9. **Close-out reconciliation** — after the cohort loop and **before** the
   terminal tally, the shared `finalize_run` helper (real runs only — injected
   fakes skip it, like the recursion guard) calls `reconcile.reconcile_run` to
   re-check every parked story against the remote. The in-memory tally can lag
   reality: a PR that merged after a 429, by hand the next morning, or
   transitively as part of a stacked PR leaves a story parked even though its
   work shipped. Reconciliation verifies the truth on `origin/main` and corrects
   the ledger so the run terminal reports DONE instead of a stale
   FAILED/NEEDS_ATTENTION. See
   [Reconciliation](#reconciliation-against-originmain) below.
10. **Run terminal** — the **one** shared `finalize_run` helper (Story 12.3-004,
    in `build.py`) is the single close-out point for both `run_build` and
    `run_resume`: it runs reconciliation (step 9) at one defined point, recomputes
    the counts, logs the finish event, stamps the run terminal via the shared
    `_run_terminal` helper, and finishes the host registry (build path only —
    parameterized). Because both paths route through it, the terminal computation
    and the `AWAITING_APPROVAL` handling can never drift between `build` and
    `resume`. `_run_terminal` maps per-story outcomes to one run terminal: any
    `FAILED`/`BLOCKED` story ⇒ `FAILED`; else any
    `NEEDS_ATTENTION` ⇒ `NEEDS_ATTENTION` (the more-urgent "work is stuck" signal
    wins so a mix never hides it); else any `AWAITING_APPROVAL` ⇒
    `AWAITING_APPROVAL` (Story 12.3-003 — a non-FAILED, non-DONE bucket); else
    `DONE`. The CLI reports an `AWAITING_APPROVAL` run honestly (not a failure)
    but exits non-zero, since FX must still act. `reconcile._compute_terminal`
    mirrors this so a standalone `sdlc reconcile` over a not-yet-approved run
    keeps the `AWAITING_APPROVAL` signal rather than downgrading it.

## Reconciliation against origin/main

`reconcile.reconcile_run(ledger, run_id, root=None, fetch=True)` reconciles a
run's parked stories against the remote and recomputes its terminal. It is the
shared engine behind both automatic close-out (above) and the manual recovery
verb (`sdlc reconcile`, Story 12.3-002 — the counterpart to `resume`/`rollback`).

- **Scope.** Only stories parked `NEEDS_ATTENTION`/`FAILED`/`BLOCKED`/
  `AWAITING_APPROVAL` are candidates; already-`DONE`/`SKIPPED` stories are left
  untouched (no redundant work, no duplicate `merge` row). When no story is
  parkable, it is a no-op and never touches the network.
- **Fetch first, degrade offline.** It runs `git fetch origin` before inspecting
  refs. A fetch failure (offline / no remote) degrades to a **no-op skip** — it
  never raises and never fails an otherwise-good run, because stale local refs
  can't be trusted to reflect what landed.
- **Landing detection** treats a story as landed if **any** signal fires
  (complementary across merge styles, closing the gap that `story_commit_exists`
  — which only counts commits *ahead of* base — cannot):
  - `git merge-base --is-ancestor feature/<id> origin/main` (fast-forward /
    merge-commit landings);
  - `git cherry origin/main feature/<id>` reports nothing left to apply
    (patch-id equivalence — rebase / single-commit squash / transitive-stacked);
  - `gh pr view <pr> --json state` is `MERGED` (PR merged, branch deleted);
  - `origin/main` carries a commit whose message holds the mandated
    `(#<story_id>)` tag (multi-commit squash, where patch-id no longer matches).
- **Effect.** A landed story is set `DONE`, a DONE `merge` stage row is
  recorded/updated (the signal `rollback._story_merged` and `compute_resume_plan`
  key off), and a `source="reconcile"` audit event names the winning signal and
  merge SHA. The run terminal is then recomputed from the reconciled per-story
  statuses.
- **Idempotent.** A re-run over an already-reconciled run produces no status
  flips and no duplicate rows — only a "nothing to reconcile" event.

## Why schema validation is the safety boundary

Every agent returns a `<<<RESULT_JSON>>> … <<<END_RESULT>>>` block. The
controller parses and validates it against the schemas bundled in the `sdlc`
package (`controller/src/sdlc/schemas/`). A missing or
malformed block raises a `ContractError` — the next stage never runs on garbage.
This is the deterministic-control-flow guarantee Epic-07 was created for. Rather
than dead-end such a stage, the state machine first attempts the bounded
envelope re-ask described above (step 5) and then the bugfix loop, so a single
malformed agent message recovers automatically in the common case instead of
stranding otherwise-good work for manual rescue (Story 12.1-001).

## The ledger

`Ledger` writes the Epic-04 SQLite schema (`state/schema.sql`) using stdlib
`sqlite3`. Every stage transition is persisted **before** the next stage begins,
so a crash leaves a resumable state. The DDL is embedded in `build.py` so a
standalone `uv tool install` works with no repo checkout. The markdown
read-model (`docs/stories/.build-progress.md`) is regenerated from the ledger
via `sdlc-state.sh render` when that script is present (best-effort — a render
failure never fails an otherwise-good build).

### Concurrency-safe writes (Story 17.1-002)

The DDL opens the ledger in **WAL** mode (`PRAGMA journal_mode = WAL`), which
allows concurrent readers alongside a single writer. Under a `parallel` run
(Epic-17), several cohort workers drive `_run_story` on separate threads and
write story/stage/event rows at the same instant, so two writers can contend for
the one WAL write lock. To keep that contention from surfacing as a
"database is locked" error, the write connection (`Ledger._connect`) sets an
**explicit** `busy_timeout` — `LEDGER_BUSY_TIMEOUT_MS` (5000ms), mirrored on the
`sqlite3.connect(timeout=…)` argument — so a contended writer retries internally
for that window and waits the brief lock out rather than failing. It is set
explicitly (not left to Python's implicit `sqlite3.connect` default) so the
guarantee can never be silently dropped by a later change to the connect call,
and is at least as generous as the read connection's 2000ms (`_connect_ro`).
Because each write method runs a single-statement transaction, writers never
hold the lock long, so the timeout is never approached in practice. A
single-threaded (`--sequential` / `--concurrency=1`) run sees identical
behaviour — the same effective timeout it always had.

### Schema migrations (auto-applied at launch)

The schema evolves additively: each `_MIGRATIONS` entry in `build.py` is
`(version, name, table, columns, create_sql)`. Most entries add missing columns
to an existing ledger via `ALTER TABLE`, guarded by `PRAGMA table_info` so a
fresh DB built from the current DDL is a no-op, and record their version in the
`_migrations` table so they run at most once per DB (`_apply_migrations`). An
entry whose `create_sql` is set instead runs a `CREATE TABLE IF NOT EXISTS` to
bring a *whole new table* onto a pre-existing ledger (the column-add path cannot,
since `PRAGMA table_info` is empty for an absent table) — used by Migration 7 for
the Epic-22 `story_inventory` cross-backlog cache. The new-table DDL lives in a
standalone constant (`_STORY_INVENTORY_DDL`) embedded into `_SCHEMA_DDL` *and*
referenced by its migration, so the fresh-create and upgrade paths are identical.
A fresh `build` gets all of this for free — `Ledger.init()` runs the DDL then
`_apply_migrations`.

The read/recovery verbs, however, open the ledger **read-only** (`_connect_ro`),
and a read-only connection cannot `ALTER TABLE`. So a ledger that predates a
migration (e.g. one missing the columns a later entry adds) would otherwise
crash `status`/`state`/`dashboard`/`resume`/`rollback` with a "no such column"
error. `Ledger.ensure_migrated()` (Story 12.2-003) closes that gap: every verb
calls it at launch, **before** any read or write. It is idempotent and:

- **No-op when the DB does not exist** — a read verb against a never-built repo
  reports "no run" and does **not** materialise a spurious empty ledger; migrate
  only an already-present DB.
- **Uses a writable connection up front** — it opens the writable path to run
  `_apply_migrations`; subsequent reads still take the read-only path.
- **Concurrent-launch safe** — a SQLite busy timeout makes a second controller
  wait out the brief writer lock, and `BEGIN IMMEDIATE` takes that lock *before*
  the version check so two launchers cannot both `ALTER` the same column; the
  `_migrations` version guard then makes the loser's pass a no-op.

The dashboard covers both modes: single-`--db` mode migrates the one ledger, and
registry-discovery mode (`sdlc dashboard` with no `--db`) migrates *every*
discovered run's own ledger up front (`_migrate_registry_ledgers`), best-effort
— a missing/corrupt ledger is skipped rather than failing startup, since the
read paths already tolerate an unreachable ledger.

Per Epic-12's non-goals there is no `sdlc migrate` verb — migrations apply
automatically at launch.

The render is **non-destructive** (Story 12.2-001). The auto-generated document
is fenced between a managed-region marker pair:

```
<!-- BEGIN SDLC LEDGER (auto-generated by sdlc-state render — do not edit this region) -->
... regenerated from SQLite ...
<!-- END SDLC LEDGER -->
```

On `render --out <file>` the renderer only ever rewrites the bytes *between*
those markers, so any hand-maintained or markdown-workflow history outside the
region (e.g. epics tracked before the SQLite ledger existed) survives a
re-render. Three cases:

- **File already has the markers** → the managed block is spliced in place;
  everything before `BEGIN` and after `END` is preserved verbatim.
- **File exists but has no markers** (legacy / hand-maintained) → the existing
  content is preserved and a fresh managed region is appended below it, so the
  next render is a clean in-place splice.
- **No file yet** (greenfield) → the full managed document is written as-is.

Re-rendering the same ledger state is byte-identical (idempotent): the marker
pair is never nested or duplicated. Plain `render` to stdout emits the same
marker-fenced document.

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
  `.build-progress.md`. **Truthful mode + concurrency (Story 17.3-001):** the
  run's `mode` is derived from `authoritative_mode(opts)` — the same
  `effective_concurrency` figure the executor dispatches through — so a
  `--concurrency=1` run is labelled `serial`, never `parallel`. The snapshot's
  `run.concurrency` block carries `{limit, active}` (the worker cap and how many
  workers are busy now), and every `IN_PROGRESS` story is in `stories[]` — so a
  parallel run surfaces *all* active stories at once, not just one. The CLI
  renders this as `N/M workers busy`; the Epic-11 dashboard reads the same
  block. A `resume` re-stamps `runs.mode` and the persisted `concurrency` from
  `authoritative_mode(opts)` after applying any `--concurrency` override, so a
  resumed run reports the worker cap *it* runs with rather than the original
  run's stale figure. This epic produces the truth; rendering stays in Epic-11
  (no double-implementation).
- **`sdlc status --markdown [--write <file>]`** (`format_markdown` in
  `sdlc/status.py`, Story 15.1-002) renders a **portable handoff** — a single
  self-contained markdown document a colleague can paste into an issue or chat
  when asking for help. It reuses the `status_snapshot` and the `doctor` report
  (15.1-001) to cover readiness, install health, the active/recent run and its
  stages, and pending risk-gate approvals (stories parked `AWAITING_APPROVAL`).
  Home paths are scrubbed to `~` (the only PII the snapshot/doctor strings carry)
  so the export is secret-free. `--markdown` is an *added* format: plain `status`
  and `status --json` are byte-for-byte unchanged.
- **`sdlc state`** (`sdlc/status.py` + `Ledger.state_rows`) dumps every stage
  row (story id, stage, status, attempt, harness, PR, branch) in a stable,
  greppable format for debugging. The `HARNESS` column (Story 20.2-002) records
  which harness ran each stage so a heterogeneous `--harness` run is auditable;
  the ledger's nullable `stages.harness` column defaults to `claude` for rows
  that predate harness routing (an existing pre-migration ledger still loads via
  the additive Migration 6).

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

## Reconcile (recovery verb)

`sdlc reconcile` is the **manual** counterpart to the automatic close-out
reconciliation — a recovery verb in the same family as `resume`/`rollback`, not
new orchestration. Its job is to rescue a run that aborted (e.g. a 429) before
its already-open PRs were merged by hand the next morning, so the ledger no
longer shows FAILED days after the work actually shipped.

- **`sdlc reconcile [run] [--db PATH]`** (`sdlc/cli.py`) runs `ledger.ensure_migrated()`
  and then the shared `reconcile.reconcile_run` (see
  [Reconciliation](#reconciliation-against-originmain)) — the identical algorithm
  close-out runs — and prints a human summary of the reclassifications and the
  run-status transition (e.g. `reconciled 3 story(ies) to DONE; run ced08c0f
  FAILED → DONE`).
- **Defaults to the latest run** (mirrors `rollback`) when no run id is passed.
- **Idempotent.** A run with nothing to reconcile reports `nothing to reconcile`
  and exits 0; offline / no-remote degrades to a clean skip notice.
- **Clean absence.** No ledger / no runs reports cleanly and never materialises a
  spurious empty ledger; only a genuinely-unknown *explicit* run id exits
  non-zero (CLI exit 2).

## Clean (workspace garbage collection)

`sdlc clean` makes build-leftover cleanup a safe, repeatable verb instead of a
manual `git worktree`/`git branch` ritual. It promotes the orphan-worktree sweep
(`hooks/sweep-orphan-worktrees.sh`) and the merged-worktree teardown into a
first-class controller command and pairs with `sdlc doctor` (15.1-001 *detects*
the cruft → `clean` *fixes* it).

- **`sdlc clean [--force/--yes] [--db PATH] [--json]`** (`sdlc/cli.py` →
  `clean.run_clean`). **Dry-run by default**: it reports what it *would* remove
  and removes nothing until `--force`/`--yes` is passed.
- **Three classes of cruft** (`clean.plan_clean`):
  - **Orphan `agent-*` worktrees** — a registered worktree under
    `.claude/worktrees/` is reclaimable only when it is **not dirty** and its
    owning run is terminal or its pid is dead; untracked `agent-*` debris dirs a
    crash left behind are swept too. The main worktree and the cwd are always
    spared.
  - **Squash-merged `feature/<id>` branches** — "merged" is decided by the
    ledger (`status=DONE`) **and** the PR's merge state (`gh pr list --head … --state
    merged`), **not** `git branch --merged`, which misreports squash-merged
    branches as unmerged (the observed 0-of-18 case). Deletion is via
    `git branch -D`, so the tip stays reachable via reflog (recoverable).
  - **Stale transcript logs** — `.sdlc-state.db.logs/<run_id>/` dirs whose run is
    terminal; a live run's transcripts are kept.
- **Safe beside a live build.** Every candidate is cross-checked against the host
  run registry + a live-pid probe (`registry.pid_alive`): a worktree or branch an
  `IN_PROGRESS` run owns — here or in another session/clone sharing this repo path
  — is never touched. This registry-awareness is the differentiator over the blunt
  hook sweeper. A crashed run (ledger still `IN_PROGRESS` but dead pid) is *not*
  live, so its leftovers are reclaimable.
- **Never mutates the remote.** `clean` only reads git, the ledger, the registry,
  and a read-only `gh pr list`; it never pushes or fetches. Each removal is logged
  to stdout; `--json` emits the full `{candidates, protected, errors}` plan. Exits
  0.

## Doctor (health-check verb)

`sdlc doctor` (`sdlc/doctor.py`, Story 15.1-001) is a **read-side** self-service
diagnostic: one command that answers "is my install healthy and is anything
stuck?" and prints a concrete remedy for each problem, so a colleague resolves
common breakages without pinging FX. It never mutates the ledger or the install
— a behind-on-migrations DB is *reported*, not migrated (Epic-12 12.2-003 fixes
it on the next real run).

- **Checks** (each yields a `CLEAN` / `WARN` / `FAIL` `Finding` with a remedy):
  - **Install integrity** — every managed `~/.claude` symlink/file from
    `install/core.sh` is present and resolves (a dangling link counts as broken).
    Missing/broken → `FAIL`, remedy `./install.sh --core`.
  - **Ledger schema + integrity** — the ledger opens read-only and every
    `_MIGRATIONS` version is applied. Behind / pre-migration-framework → `WARN`
    (auto-migrates on the next `sdlc` verb); unreadable/corrupt → `FAIL`. No
    ledger yet is `CLEAN` ("nothing built").
  - **Stuck / stale runs** — reuses the registry's pid logic
    ([the run registry](#the-run-registry-cross-repo-discovery)): an
    `IN_PROGRESS` run whose pid is dead derives `DEAD` → `FAIL` (remedy
    `sdlc reconcile` then `sdlc runs --prune`); a ledger run `IN_PROGRESS` with
    no registry liveness and no activity for >6h → `WARN`.
  - **Config validity** — the packaged JSON schemas and the managed
    `settings.json` parse. A malformed file → `FAIL`.
  - **Dependencies** — one finding per external tool (`gh`, `claude`, `semgrep`,
    `osv-scanner`), each probed via `<tool> --version`. A missing tool is a `WARN`
    (it degrades a feature, not the install) with an install remedy.
- **Overall status** is the worst finding (`worst_status`: CLEAN < WARN < FAIL).
- **Exit code.** Defaults to **0** so it is safe to run anywhere; `--exit-code`
  makes a WARN exit 1 and a FAIL exit 2 so a wrapping script can gate on health.
- **`--json`** emits `{status, findings:[{check, name, status, detail, remedy}]}`
  for tooling and the markdown handoff (Story 15.1-002).
- **Overrides.** `--db`, `--claude-dir`, and `--repo-root` make every check
  point at an explicit location (used by tests and for diagnosing a non-default
  install).

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

## Run-logging CLI for non-controller pipelines (Story 11.2-013)

The `fix-issue` skill is a **markdown skill** (bash + `Agent` sub-agents), not the
controller, so it cannot call `Ledger`/`Registry` in-process the way `run_build`
does. To let a `fix-issue` session show up in the same dashboard as a `sdlc build`
run, the controller exposes a **minimal run-logging CLI** (`sdlc/runlog.py`) that
the skill shells out to per phase. It deliberately reuses the existing storage —
no new tables, no new render path — so the multi-run dashboard surfaces these runs
as-is:

- **`sdlc run-open --scope issue-N [--db --repo --mode --story-id --title --pid]`** —
  `run_open` calls `Ledger.init` + `run_create` (mode defaults to `fix-issue`),
  seeds **one** synthetic story (the issue itself, so stages have a FK parent),
  and `Registry.register`s the run. It prints the new `run_id` on stdout (or
  `{run_id, db, story_id}` with `--json`) for the skill to capture and thread into
  later calls. **`--pid` is the long-lived orchestrator's pid**, whose liveness
  stands in for the run's so `derive_state` can flag a crash as `DEAD`. A markdown
  skill must pass its `$PPID` (the Claude session): the `sdlc run-open` subprocess
  exits the instant it returns, so registering its *own* pid — the default — would
  make the registry derive a still-running fix `DEAD`. The in-process default
  (`os.getpid()`) only suits a long-running caller, as `sdlc build` is.
- **`sdlc run-stage <start|finish> --run ID --stage NAME [--db --story-id --attempt --status --failure-category]`**
  — `run_stage` calls `Ledger.stage_start` / `stage_finish` against the run's sole
  story (resolved from the ledger when `--story-id` is omitted). The skill emits
  one per `fix-issue` phase; the `build`/`coverage`/`review`/`merge` stage names
  line up with the dashboard's pipeline columns (`_STAGES`), while extra phases
  (`investigate`, `e2e`) live in the run's stage history.
- **`sdlc run-close --run ID [--db --status --completed --story-id]`** —
  `run_close` stamps the run (and its story) terminal via `Ledger.run_update_status`
  and mirrors it into the registry with `Registry.mark_finished`. A session that
  crashes before closing is detected as `DEAD` by dead pid, exactly like a
  controller run.

**Best-effort by construction.** Every verb swallows ledger/registry IO errors
(`run_open` returns `None`, the others return `False`) and the skill suffixes each
call with `2>/dev/null || true`, so a missing `sdlc`, an unwritable ledger, or a
corrupt registry never blocks or fails the fix — the pipeline still completes; the
dashboard simply doesn't show that run. `sdlc build` runs are untouched: the CLI is
additive and shares the same storage read by `status_snapshot`. Scope is
**fix-issue-only** for now; `build-stories` and future skills can adopt the same
verbs later.

## Live dashboard transport (Story 11.2-003)

The dashboard updates itself — no manual reload — over a **Server-Sent Events**
stream (`/api/stream`) served by the same stdlib `http.server`, so the
dependency footprint stays at zero web frameworks.

- **Change detection.** `Ledger.change_token()` returns an opaque digest that
  moves whenever any **dashboard-visible** field does. `MAX(events.id)` alone is
  insufficient: the dashboard also renders per-stage status/usage and per-story
  status/PR, which are set by in-place `UPDATE`s (`stage_finish`,
  `stage_set_usage`, `set_story_status`, `set_story_pr`) that write no event row
  — and the ledger runs in **WAL** mode, so the file mtime is no proxy and
  SQLite's `PRAGMA data_version` is meaningless across the fresh read-only
  connections we poll with. So the token is a `blake2b` digest over the mutable
  fields of `runs`/`stories`/`stages` plus the event high-water mark (row counts
  are tens per run, so it stays cheap to poll sub-second). `_change_token(server)`
  wraps it: single-`--db` mode returns the one ledger's token; registry-discovery
  mode digests every run's `id`/derived-state/token so the stream also fires when
  a run appears, finishes, or changes across repos. An unreachable ledger
  contributes `"0"` rather than breaking the stream.
- **The stream.** `_serve_stream` polls the token on a short interval
  (`_SSE_POLL_INTERVAL`, ~1 s ⇒ under the 2 s latency target) and pushes an SSE
  `change` event carrying the new token only when it moves; otherwise it emits a
  bare `: heartbeat` comment every `_SSE_HEARTBEAT_INTERVAL` to keep the
  connection (and any proxy) alive. Idle ⇒ no `change` traffic, negligible CPU.
  Each connection runs in its own `ThreadingHTTPServer` thread, so multiple
  browser tabs each get an independent stream; the loop exits the moment a write
  fails (client gone).
- **Client.** The page subscribes with `EventSource` and, on each pushed
  `change`, refetches `/api/runs` + `/api/status` — the same idempotent whole-
  snapshot render it always did. `EventSource` reconnects on its own, and because
  the render replaces (never appends) DOM, a dropped-and-resumed connection never
  duplicates rows. Browsers without `EventSource` fall back to gentle polling.

## GitHub repo health on the dashboard (Story 11.2-006)

Each run's external GitHub state — open/closed issues, open/closed PRs, and the
latest **default-branch** CI conclusion — is shown alongside the ledger-driven
view: a **compact badge on every overview row** and a **full panel in the
selected run's detail view**. The work lives in `sdlc/github_stats.py`; the
dashboard only wires it in.

- **Slug resolution.** `dashboard.repo_slug(root)` reuses `git_project_url`
  (and thus the shared `_SCP_REMOTE`/`_URL_REMOTE` regexes) to derive the run's
  `owner/repo` slug from its git remote. No remote ⇒ `None` ⇒ the "unavailable"
  state. The repo comes from the registry's `RunRecord.repo` (registry mode) or
  the ledger's parent dir (single-`--db` mode), so stats are per-repo with no
  cross-repo bleed.
- **Fetch.** `github_stats.fetch_stats(slug)` reads counts via the search API
  (`gh api search/issues` `total_count` for each `type:issue|pr state:open|closed`
  — closed PRs include merged) and the latest default-branch run via
  `gh run list --branch <default>`. `gh` talks to **GitHub's** rate limit, not
  the Claude Max window. Every call degrades to `None` on failure; when *all*
  fail the repo is reported `unavailable` (`gh` absent/unauthenticated/
  rate-limited), and a partial result (counts but no CI run yet) still renders.
- **Cache, off the request path.** `GitHubStatsCache` keys entries by repo slug
  with a ~60 s TTL. A `get()` never drives `gh` on the request path: a fresh
  entry is served as-is; a stale/absent one returns the last-known value (or a
  muted pending sentinel) and schedules a **background** refresh, deduped per
  slug. So N runs in one repo — and any number of polling tabs — cost one fetch
  per TTL window. The overview view (`_registry_runs_view`) further dedups the
  read **per repo** within a single render.
- **Wiring.** `/api/github?run=<id>` (`_github_stats`) serves the detail panel;
  `_registry_runs_view` enriches each overview row with a compact `github`
  summary. The client's existing ~2.5 s poll fetches `/api/github` alongside
  `/api/runs` + `/api/status` and reads the cache — it never itself drives `gh`,
  and a missing/unavailable summary degrades to a muted "GitHub unavailable"
  badge/panel without ever throwing or blocking the ledger-driven dashboard.

## Story status lifecycle and dashboard labels (Story 11.2-009)

A story row's ledger status tracks its actual progress, and the dashboard maps
that status to a human-facing label:

- **Start.** `_run_story` sets the story `IN_PROGRESS` the moment its first
  pending stage begins (Story 11.1-002), *before* any stage dispatch. Both
  scheduling paths inherit this: `run_build` and `resume` drive a story through
  `_run_story_rate_limited` → `_run_story`, so neither leaves a story on `TODO`
  while its stages run. Without this a story went `TODO` → terminal with no
  in-flight window, so `sdlc status`, the dashboard, and `counts.in_progress`
  never reflected work happening mid-run.
- **Terminal.** Once `_run_story` returns, the caller stamps the terminal status
  (`set_story_status(outcome)`): `DONE`, `FAILED`, `NEEDS_ATTENTION`, `BLOCKED`
  (unmet dependency), `RATE_LIMITED`, or `AWAITING_APPROVAL`. Stories skipped as
  already-merged are seeded `SKIPPED`.
- **Display labels.** The dashboard renders the status via a small label map
  (`LABELS` in the embedded page): `IN_PROGRESS` → **STARTED**, so the status
  column reads `TODO → STARTED → DONE/FAILED`. Only `IN_PROGRESS` is remapped —
  `BLOCKED`, `NEEDS_ATTENTION`, `SKIPPED`, `RATE_LIMITED`, and
  `AWAITING_APPROVAL` keep their own distinct labels so no real state is hidden.
  The badge's CSS class stays the **raw** status (e.g. `IN_PROGRESS`), so colours
  and the `counts.*` summary chips are unchanged; `.STARTED` shares the
  `.IN_PROGRESS` style. The shared `badge()` helper applies the map everywhere a
  status is rendered (the per-story status column, the run badges, and the
  summary chips), so an in-progress run reads `STARTED` consistently. This is
  foundational for Epic-17 17.3-001, which extends "started" to *multiple*
  concurrently-active stages.

## In-dashboard transcript viewer (Story 11.2-010)

Each story row carries a **"view session"** control that opens a modal listing
that story's stage transcripts — build, coverage, review, merge, and any bugfix
retries — and renders each one inline, so FX reads what every `claude -p`
session did without hunting for `.log` files or leaving the page. The work is in
`sdlc/dashboard.py`: a small JSON endpoint plus the embedded modal.

- **Endpoint.** `/api/logs?run=<id>&story=<id>` (`_serve_logs`) resolves the
  run's ledger (the registry record in discovery mode, the latest run in
  single-`--db` mode), reads `Ledger.stage_breakdown(run)`, and for the story
  enumerates **every** stage attempt — so a bugfix retry shows as its own
  transcript, not just the latest attempt the stage-pipeline view collapses to.
  Each entry is `{stage, attempt, status, path, exists, content}`.
- **Path confinement, shared with `/log`.** Transcript content is read through
  `_read_confined(root, output_path)`, which applies the same
  `Path.resolve().relative_to(logs_root)` guard as the `/log` route (factored
  into the shared `_logs_root(run)` helper). A ledger row whose `output_path`
  escapes the logs root — or points at a file that was never written — yields
  `exists: False` with empty content, so a bad/missing path can never leak a
  file outside the logs tree and the viewer shows a **placeholder, not an
  error**. An unknown/blank story or unreachable ledger returns an empty list
  (HTTP 200, never 404/500).
- **Rendering + graceful degradation.** The client's `renderTranscriptContent`
  shows plain-text transcripts (today) verbatim and readably. Once Epic-11
  11.1-001 streaming lands and logs become **stream-json** (one JSON object per
  line), it collapses each event to a compact `type: message` line and falls
  back to the raw line for anything that is not valid JSON — so a mixed or
  future format degrades rather than breaking.
- **Fallback preserved.** The existing new-tab `/log` link is kept per
  transcript (and on the stage badges), so the previous behaviour is a
  no-regression fallback. The modal closes on its backdrop, its × button, or
  `Esc`; the per-story control is bound by delegation on the `#stories`
  container, which the live transport re-renders each tick. Transcripts stay on
  disk (an Epic-11 non-goal: not in SQLite); the localhost-only surface means
  raw agent output is acceptable here and dovetails with Epic-13 sanitization.

## Stable-height live regions (Story 11.2-011)

The live transport (11.2-003) and the live detail view (11.2-004) re-render the
auto-updating regions with a full `innerHTML` swap on **every** tick. Their
content is variable-height — the run summary `#head` is 1–3 lines (run line +
optional config line + optional usage line), the per-story `.substage` activity
line can wrap, and `#updated` is a single clock line — so a swap that changed a
region's box height would reflow everything below it and make the page jump (and
shift content under a scrolled cursor). This is fixed in CSS only (**no backend
change**), in `sdlc/dashboard.py`'s embedded stylesheet:

- **`#head` reserves its 3-line maximum** (`min-height: 4.5em`), so toggling
  between 1, 2 and 3 lines never changes its height — the bar/chips/stories
  below it stay put.
- **`#updated` reserves one line** (`min-height: 1.5em`).
- **The `.substage` activity content clamps to a single line** via the
  line-clamp idiom (`.act { display: -webkit-box; -webkit-box-orient: vertical;
  -webkit-line-clamp: 1; overflow: hidden }`, full message on hover via
  `title`), so a long milestone is clipped rather than growing the row.
  `activityRow` wraps its content in that `.act` element. `white-space: nowrap`
  + `text-overflow: ellipsis` is deliberately **not** used here: the activity
  rides in a `colspan` cell of an auto-layout table, where a nowrap cell widens
  the column (and scrolls `.main` sideways) instead of ellipsizing. Line-clamp
  keeps normal wrapping — so the cell stays at the table width — then clips to
  one line. Verified live with a long message: the row stays one line and the
  table does not overflow horizontally.

Scroll position is preserved for free: `renderMain` only swaps the `innerHTML`
of individual child regions inside the `.main` scroll container, so a fixed-box
swap leaves `.main.scrollTop` untouched — there is no jump-to-top and no shift.

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

### Harness registry (Story 20.1-001)

`sdlc/harness.py` generalizes the dispatch seam and the
`sdlc/config/adversarial-reviewers.yaml` pattern into one config-driven **harness
abstraction**: a `sdlc/config/harnesses.yaml` keyed by harness name (`claude`,
`codex`, …) where each entry declares a **command template**, **invocation
flags**, **capability flags** (`worktree_isolation`, `parallel`, `json_contract`,
`usage_tracking`, `rate_limit_aware`), and an **output-parser id**. The file is
validated against `schemas/harness-registry.schema.json` (draft 2020-12) on load;
`load_harnesses_config` raises `HarnessError` with an actionable, field-named
message on any malformed entry. Command templates reuse the
`{pr_number}`/`{pr_url}`/`{story_id}` placeholder style of the reviewer registry.

`resolve_harness(name=None, *, config_path=None)` returns a `HarnessConfig` and is
the seam the rest of Epic-20 builds on. Its **default slot** (`name` is `None` or
`"claude"`) is deliberately backward-compatible:

- **No registry wired and no `SDLC_AGENT_CMD`** → the built-in Claude harness
  (`source="builtin"`), whose argv resolves through the *existing*
  `resolve_agent_cmd`, so it is **byte-identical** to today's `DEFAULT_AGENT_CMD`
  path (deny baseline + routed `--model` decoration included).
- **`SDLC_AGENT_CMD` set** → that override is **re-expressed as an ad-hoc registry
  entry** (`source="env"`), not removed; its argv is again `resolve_agent_cmd`'s
  env path, and the escape hatch owns its own model.

A **named non-default** harness (e.g. `codex`) is resolved from the registry and
renders its own template; an absent `config_path` or an unknown name fails fast
with `HarnessError` rather than half-running. The default slot does **not** consult
`harnesses.yaml` for its argv, so the shipped registry is a reference + the
foundation for parsers (Story 20.1-002) and role routing (Story 20.2-001) without
changing today's dispatch behaviour. `resolve_agent_argv(...)` is the convenience
wrapper returning the launch argv directly.

### Harness capability probe and preflight (Story 20.5-001)

`sdlc/capability.py` turns the registry's declared capability flags into a
**preflight decision** so a run plans safely *before* dispatching. It is the seam
the degradation matrix (Story 20.5-002) centralizes on.

- **`resolve_capabilities(harness)`** returns the full canonical capability map.
  Declared flags win; any **undeclared** canonical key defaults to `False` — an
  undeclared capability is assumed *absent* (conservative), so a harness only
  earns a capability it explicitly claims. Extra, non-canonical flags are
  preserved.
- **`probe_harness(harness)`** runs the optional `probe` command from the
  registry entry to confirm the CLI is installed/authenticated. A zero exit is
  `available`, a non-zero exit is `unavailable` (with the captured detail), and a
  harness with **no** `probe` command is `unknown` — never probed, no subprocess.
- **`preflight_harness(harness, requested_mode=...)`** resolves capabilities,
  probes, and decides the **effective run mode**. A `parallel` request requires
  both the `parallel` and `worktree_isolation` capabilities; when either is
  missing the controller **degrades to `serial`** and records a warning rather
  than failing mid-run (the safe alternative — Story 20.5-002 owns the full
  matrix). `serial` is always supportable. The returned `HarnessPreflight` is
  immutable and yields `log_lines()` for the ledger/stderr.

`run_build` calls this in preflight (after the run row is created) for the
default-slot harness and writes each line to the `harness` event source — `info`
normally, `warn` on any downgrade. For the built-in Claude harness this is purely
additive logging (all capabilities `true`, no probe), so dispatch is unchanged.

**Capability matrix** (canonical flags; the shipped `harnesses.yaml` values):

| Capability | Meaning | `claude` | `codex` |
|---|---|---|---|
| `worktree_isolation` | Can run each story in its own git worktree | ✅ | ❌ |
| `parallel` | Can fan a cohort across concurrent workers | ✅ | ❌ |
| `json_contract` | Emits the `<<<RESULT_JSON>>>` contract | ✅ | ✅ |
| `usage_tracking` | Reports token usage / cost | ✅ | ❌ |
| `rate_limit_aware` | Surfaces 429 / reset semantics for backoff | ✅ | ❌ |

A `parallel` request on `codex` (no `worktree_isolation`, no `parallel`) degrades
to `serial` with an explicit warning; its missing `usage_tracking` /
`rate_limit_aware` are recorded as "unavailable" rather than fabricated
(Story 20.5-002, below).

### Degradation matrix and safe fallbacks (Story 20.5-002)

`sdlc/degradation.py` is the **single, testable decision point** for what the
controller does when a harness lacks a capability — so a capability gap never
crashes a run and never degrades silently. `evaluate_degradations(harness,
capabilities, *, requested_mode=...)` returns an immutable `DegradationPlan`
listing every fallback applied (each a `Degradation` with a stable `kind`, the
`missing` capability flag(s), and a human-readable `message`).

| Missing capability | Requested | Fallback (`DegradationKind`) | Effect |
|---|---|---|---|
| `parallel` or `worktree_isolation` | `parallel` | `parallel_to_serial` | the cohort runs **serially** (the safe alternative), one explicit log line |
| `usage_tracking` | any | `usage_unavailable` | cost/usage recorded as **"unavailable"**, not fabricated as zero (the `PlainResultParser` returns `usage_available=False`) |
| `rate_limit_aware` | any | `rate_limit_skipped` | **rate-limit backoff is skipped** — no fabricated 429 handling (a non-zero exit is a plain dispatch error) |

A **fully capable** harness (the built-in Claude harness — every flag `true`)
yields an **empty plan**, so wiring this in is purely additive for the default
path; nothing is recorded and dispatch is unchanged.

`capability.preflight_harness` (Story 20.5-001) **delegates** its mode decision to
this matrix — it surfaces only the `parallel_to_serial` fallback as a preflight
warning, while the build flow records the full plan. `run_build` calls
`_record_degradations` right after the capability preflight: it resolves the
default-slot harness, evaluates the matrix for the run's mode, and writes one
`warn` event per fallback to the `degradation` event source — so any degradation
is auditable in the run summary (AC3). `DegradationPlan.to_records()` yields the
structured rows (`harness`, `kind`, `missing`, `message`, `requested_mode`,
`effective_mode`) the ledger/summary persists.

### Per-role harness routing (Story 20.2-001)

`sdlc/role_routing.py` maps each **pipeline role** to a harness so that, for
example, Claude builds while Codex reviews and QAs in one run. The role catalog is
the controller's dispatch stages:

| Role | Runs | Notes |
|------|------|-------|
| `build` | the build agent (story implementation + PR) | |
| `coverage` | the coverage/QA gate agent | `qa` is an accepted alias |
| `review` | the adversarial review slot | bridged to `adversarial-reviewers.yaml` |
| `merge` | the merge agent | |
| `docs` | the doc-currency agent | |

The map is supplied with `sdlc build --harness ROLE=NAME,…` (space- or
`--harness=`-joined), e.g.:

```
# Claude builds; Codex reviews and QAs:
sdlc build epic-20 --harness build=claude,review=codex,qa=codex

# Everything on the default (claude) — equivalent to no flag at all:
sdlc build epic-20
```

`parse_role_harness_map` canonicalises role names (`qa` → `coverage`), rejects an
unknown role or a malformed entry at parse time, and refuses two different
harnesses for the same role (`coverage=claude,qa=codex`). `resolve_role_routing`
then resolves **every** role to a `HarnessConfig`: a role absent from the map
collapses to the default harness (today's behaviour), and a role mapped to a
non-default harness is resolved from `sdlc/config/harnesses.yaml`. An **unknown** or
**disabled** harness, or a missing registry, raises `RoleRoutingError` so the CLI
**fails fast in preflight** (exit 2, no half-run) before any stage runs.

`check_review_bridge` is the Epic-08 coordination point: when the `review` role is
routed to a harness that *also* appears as a reviewer in
`adversarial-reviewers.yaml`, that reviewer must be **enabled** there — otherwise
the two configs would conflict (route review to a harness the reviewer registry has
switched off). A review harness absent from the reviewer registry, or a missing
reviewers file, is a no-op. Epic-08 still owns the reviewer-consensus semantics;
this bridge only prevents the registries from disagreeing.

**Wiring routing into actual dispatch (Story 20.7-001).** Resolving the map is only
half the job — until Story 20.7-001 the resolved harnesses were validated in `cli.py`
and then **discarded**, so `--harness build=codex` *labelled* the ledger but every
stage still ran on `claude`. The routing is now wired through the build loop:
`_dispatch_stage` (and the recovery re-dispatches: envelope re-ask, commitlint amend,
bugfix) call `_harness_dispatch_kwargs(stage, opts, model)`, which resolves the
stage's harness via the same `_stage_harness` role lookup the ledger records and
returns the dispatch kwargs that route it — `agent_cmd=harness.to_argv(model=model)`
and `parser=(None if harness.source in ("builtin","env") else harness.parser)` — into
the existing `dispatch` seam. So a role mapped to `codex` **actually runs** the Codex
adapter argv with the `codex-exec` parser, and the ledger `harness` column matches
what ran. With **no** `--harness` map the helper returns an empty dict, so the
dispatch passes no `agent_cmd`/`parser` and is byte-identical to today's default path
(the `_resolve_dispatch` thinking-cap/sandbox binding and test injection are
preserved). A registry harness owns its own argv, so the routed `--model` decorates
only the built-in/`env` Claude slots — a codex stage ignores it.

### Codex build/QA adapter (Story 20.3-001)

The `codex` registry entry is the first concrete **non-Claude** adapter, proving
the abstraction end-to-end: a build or coverage/QA agent runs on Codex through the
same registry the default Claude slot uses.

- **Wrapper.** `scripts/codex-build-adapter.sh` receives the controller-assembled
  prompt on **stdin**, runs Codex headlessly via `codex exec`, and **forwards
  Codex's stdout verbatim** so the agent's `<<<RESULT_JSON>>>` block round-trips
  untouched. `--self-test` emits a schema-valid `build` block with no real Codex,
  and `HARNESS_AGENT_CMD` overrides the underlying command (e.g. to add
  `--full-auto`). Because the wrapper only ever runs `codex`, a run routed here
  spawns **zero `claude` processes** (AC3).
- **Dispatch seam.** `harness.dispatch_on_harness(harness, agent_type, prompt, …)`
  expresses the contract for running an agent on a resolved `HarnessConfig`: render
  the harness's own argv (`to_argv`), select its declared parser, and hand both to
  `dispatch_agent`. A registry harness (codex) uses its declared `codex-exec` parser;
  the built-in/`env` Claude slots keep the stream-json parser (`parser=None`), so the
  default path is unchanged. The build loop applies that same contract inline via
  `_harness_dispatch_kwargs` (Story 20.7-001, see *Per-role harness routing* above) so
  the routed `agent_cmd`/`parser` flow through the existing `dispatch` seam — keeping
  its thinking-cap/sandbox binding and test injection — rather than bypassing it.
- **Parsing.** Codex output is interpreted by the `codex-exec` parser
  (`PlainResultParser`, Story 20.1-002): it reads the result block straight from
  stdout, records usage as **unavailable** (never fabricated as zero), and treats
  any non-zero exit as a plain dispatch failure (no fabricated 429). The stage then
  advances exactly like a Claude stage.

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
and hands it, with the accumulated stdout, to a shared `_interpret` step. That
step is harness-neutral: it builds a `CollectedOutput` and delegates to the
harness's **output parser** (see below), which owns `<<<RESULT_JSON>>>`
extraction, `usage`/`total_cost_usd`/`session_id` capture, and schema validation.
Unknown / non-JSON stream lines are teed to the transcript but ignored for
control flow.

Any command **without** `stream-json` (a custom `SDLC_AGENT_CMD`, or an older
agent) takes the **captured** path (`_dispatch_captured`, the original
`subprocess.run` with `capture_output=True`). A streamed run whose terminal
`result` event never arrives degrades gracefully: `_interpret` falls back to
parsing the accumulated stdout exactly as the captured path would, so a
malformed stream never fails the run on its own. The streaming path keeps the
verbatim stream in the transcript (it is the live view); the captured envelope
path still rewrites the transcript to the readable agent text.

### Pluggable output parsers (Story 20.1-002)

The Claude-specific *interpretation* (envelope unwrapping, usage/cost capture,
429/`resetsAt` rate-limit and "prompt is too long" context-overflow recognition)
used to live inline in `dispatch._interpret`. It is now an **output-parser
interface** (`sdlc/parsers.py`) so a non-Claude harness gets proper handling
instead of the lossy plain-stdout fallback. The split is *collection* vs
*interpretation*:

- **Collection** stays in `dispatch.py`: running the subprocess, streaming vs
  captured I/O, the kill-switch / heartbeat. It hands the result to a parser as a
  harness-neutral `CollectedOutput` (agent type, stdout/stderr, returncode,
  transcript path, the optional Claude `result` envelope, and any stream-captured
  reset epoch).
- **Interpretation** is per-harness, owned by an `OutputParser` resolved **by id**
  through `get_parser(parser_id)`. Each `harnesses.yaml` entry declares its parser
  id; `None` selects the built-in default. An **unregistered id fails fast** with
  `UnknownParserError` (the typo + the registered ids), never a silent mis-parse.

Two parsers ship:

- **`claude-stream-json`** — `ClaudeStreamJsonParser`, the built-in default. The
  former `_interpret` body, preserved **verbatim**, so the Claude path is
  byte-for-byte today's: the `<<<RESULT_JSON>>>` contract, `usage` /
  `total_cost_usd` / `session_id`, structured-429 / `resetsAt` rate-limits, and
  context-overflow detection all behave exactly as before.
- **`codex-exec`** — `PlainResultParser`, for a harness with a JSON contract but
  **no usage / rate-limit semantics**. It reads the harness-neutral
  `<<<RESULT_JSON>>>` block straight out of stdout and validates it against the
  *same* contract schema, but it does **not** unwrap a Claude envelope and has no
  rate-limit / context-overflow recognition — a non-zero exit is always a plain
  `AgentDispatchError`, never a fabricated 429. Usage is recorded as
  **unavailable** (`AgentResult.usage_available=False`, `usage=None`) rather than
  fabricated as zero, so cost tracking skips the stage and the run still advances.

The `<<<RESULT_JSON>>>` contract (`sdlc.contracts.parse_and_validate`) is the
harness-neutral seam both parsers validate against. `dispatch_agent(…, parser=…)`
threads the id from the resolved harness; with no id the default keeps today's
Claude behaviour, so the change is fully backward compatible. Adding a harness
that reuses an existing parser shape needs **no new Python** — only a
`harnesses.yaml` entry naming a registered parser id.

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

### The in-process-agent boundary (Story 20.6-002)

Epic-20 makes the controller's dispatch path **cross-harness**: `build-stories`
assembles a prompt and shells out through the dispatch seam (`sdlc/dispatch.py`),
so a role can be routed to any registry harness (`claude`, `codex`, …) via
`--harness`. Two skills deliberately stay **Claude-only**: `fix-issue` and
`resume-build-agents` spawn their sub-agents **in-process** with the Claude Code
`Agent` tool (`subagent_type` / `model` / `isolation="worktree"`). That tool is a
Claude Code primitive with **no CLI-harness equivalent** — there is no prompt to
hand to `codex exec`, so there is nothing to port. This is a design boundary, not
a gap to be closed later.

| Skill | Dispatch mechanism | Harness support |
|-------|--------------------|-----------------|
| `build-stories` | controller dispatch seam (`sdlc/dispatch.py`) | **Any registry harness** (`--harness build=claude,review=codex,…`) |
| `fix-issue` | in-process `Agent` tool (`subagent_type` / `isolation`) | **Claude only** |
| `resume-build-agents` | in-process `Agent` tool (`subagent_type` / `isolation`) | **Claude only** |

`sdlc/portability.py` encodes this matrix as the single source of truth
(`CROSS_HARNESS_SKILLS`, `CLAUDE_ONLY_SKILLS`, `support_matrix()`) and exposes a
fail-fast guard: `assert_harness_supports_skill(skill, harness)` raises
`HarnessError` pointing back at this section when a Claude-only skill is asked to
run on a non-Claude harness, so a misrouted invocation fails before doing half a
run rather than crashing deep inside a sub-agent call.

### Kill-switch and heartbeat dead-man (Story 13.4-001)

A dispatched agent that hangs or loops must never hold an unattended run (or the
machine) hostage, so `_dispatch_streaming` bounds *any* stall and kills cleanly:

- **Process-group kill, not just the parent.** The agent is launched with
  `start_new_session=True` so it leads its own session / process group. When it
  must be terminated, the controller signals the whole **group**
  (`os.killpg(os.getpgid(pid), …)`) so a tool subprocess the agent spawned cannot
  be orphaned and survive the kill. `_signal_process_group` falls back to
  signalling just the direct child when the group cannot be reached (no pid yet,
  already reaped, or a platform without POSIX process groups).
- **Graceful then hard (`SIGTERM` → grace → `SIGKILL`).**
  `_terminate_process_group` sends `SIGTERM` to the group first so a well-behaved
  agent can flush and exit, waits `_TERM_GRACE_S` (10 s), then escalates to
  `SIGKILL` so a runaway is terminated for certain. A child that exits on
  `SIGTERM` is never `SIGKILL`-ed.
- **Two independent kill triggers.** The existing wall-clock `timeout` (default
  `DEFAULT_TIMEOUT_S`, 3600 s) is joined by a **heartbeat dead-man**: an
  output-idle monitor thread that kills the agent after `stall_timeout` seconds
  with no stream line (`DEFAULT_STALL_TIMEOUT_S`, 300 s; `None` disables it). The
  monitor re-arms on every line received, so slow-but-productive work is never
  killed, while a genuine hang is caught far inside the wall-clock bound. The
  raised `AgentDispatchError` names which trigger fired (`stalled: no output for
  Ns` vs `timed out after Ns`).
- **Quarantine + ledger.** On a kill the partial transcript is copied to a
  `<transcript>.killed` sibling (`_quarantine_transcript`) so the event can be
  reviewed without the next run overwriting the live log, and the quarantine path
  is included in the error message — which the build loop records as the failed
  stage's `event_log` entry, so the kill is captured in the ledger through the
  existing dispatch-error path (no new schema).

The **captured** path (a non-streaming `SDLC_AGENT_CMD`) also launches with
`start_new_session=True` for group isolation, but relies on
`subprocess.run(timeout=…)` reaping the direct child; full graceful
process-group escalation and the heartbeat live on the streaming path, which is
the default real-agent dispatch. This complements Epic-12 story 12.1-002
(preflight hang guard, which prevents recursive self-invocation): 12.1-002 bounds
the test command, this bounds the agent subprocess itself.

### Per-story working directory (Story 17.2-001)

`dispatch_agent(…, cwd=…)` is the seam that lets the controller run an agent in
a directory other than its own cwd. It threads straight onto the `subprocess`
call of **both** dispatch paths (`subprocess.run(cwd=…)` /
`subprocess.Popen(cwd=…)`); `cwd=None` inherits the parent's working directory,
so the no-isolation path is byte-for-byte today's.

### Deny baseline under the permission bypass (Story 13.1-001)

Every dispatched agent runs `claude -p … --dangerously-skip-permissions`: with no
human to approve tool calls, the bypass is what lets a headless agent actually
write files, commit, and call `gh` instead of being silently denied. That same
bypass, though, gives the agent the blast radius of the whole machine. The deny
baseline narrows it back down **without** reintroducing prompts (which would
break unattended runs).

The flag suppresses the permission *prompt*; it does **not** disable an explicit
deny list supplied on the command surface. `settings.json`'s `permissions.deny`
*is* bypassed by the flag — so it is the wrong enforcement point — but
`--disallowedTools` on the `claude` invocation is honoured. So `resolve_agent_cmd`
appends the baseline as `--disallowedTools "<rule>,<rule>,…"` to the **built-in
default command only**. The default baseline (`DENY_BASELINE` in
`dispatch.py`) is:

| Rule | Blocks |
|------|--------|
| `Read(~/.ssh/**)` / `Write(~/.ssh/**)` | reading or tampering with SSH keys |
| `Read(~/.aws/**)` | reading AWS credentials |
| `Read(**/.env*)` | reading `.env` secret files anywhere in the tree |
| `Bash(curl * \| bash)` | "pipe the internet into a shell" remote execution |
| `Bash(ssh *)` | outbound SSH egress |

The rules are deliberately narrow — they block only the listed secret paths and
egress shells, so ordinary development (editing repo files, running the test
command) is unaffected.

Precedence mirrors model routing (Story 14.2-001): the baseline decorates only the
built-in default. A `SDLC_AGENT_CMD` or an explicit `agent_cmd` is the escape
hatch and **owns its own permission posture**, so no deny rules are appended to it
— an operator taking over the command can set their own `--disallowedTools`.

**Per-repo override.** Set `SDLC_DENY_BASELINE` to a comma-separated rule list to
*replace* the baseline for one repo without editing controller code, or to the
empty string to opt out entirely (the flag is then omitted and the default is
byte-for-byte its pre-13.1 form). Whitespace is trimmed and blank entries
dropped, so `"Read(~/.aws/**), , Bash(ssh *)"` resolves to two rules. Unset → the
built-in baseline, so the secure default needs no configuration.

The stronger option for genuinely untrusted repos is the opt-in container sandbox
(Story 13.4-002); the deny baseline is the host-path floor that always applies.

### Container sandbox for untrusted repos (Story 13.4-002)

The deny baseline narrows what a dispatched agent can touch on the **host**. For a
genuinely untrusted repo — one you're reviewing or building but don't trust — the
stronger, **recommended** option is to give the agent no host or network reach at
all: run it inside a container. This is opt-in (`--sandbox`, or `SDLC_SANDBOX=1`
per repo); trusted local runs stay on the host, where the deny baseline is the
floor.

When enabled, `dispatch_agent` resolves the agent command exactly as on the host
path (default, `SDLC_AGENT_CMD`, deny baseline, routed model — all unchanged) and
then **wraps** it in a hardened `<runtime> run` invocation (`_apply_sandbox` /
`sandbox_wrap` in `dispatch.py`). The wrap is transparent: the prompt still
arrives on stdin and the agent's `stream-json` / `<<<RESULT_JSON>>>` envelope
still streams out on stdout, so usage extraction, schema validation, the branch,
and the commits are **byte-for-byte the host path's** — the result contract is
unchanged. The worktree is bind-mounted, so commits the agent makes land back in
the host worktree exactly as before.

The container is locked down:

| Flag | Effect |
|------|--------|
| `--network none` | **no egress** — a compromised agent can reach neither the host nor the internet (default) |
| `--cap-drop ALL` | every Linux capability dropped |
| `--security-opt no-new-privileges` | no privilege escalation inside the container |
| `--user <uid>:<gid>` | runs as the **host operator's non-root uid/gid**, so mounted files stay owned by you |
| `-v <worktree>:/workspace:Z` + `-w /workspace` | the per-story worktree is the only mount; the agent runs there |
| `--rm` | the container is discarded after the stage |

**Fail-fast (AC3).** If `--sandbox` is requested but no container runtime is on
`PATH`, dispatch raises `SandboxUnavailableError` **before any agent launches** —
it never silently falls back to an unsandboxed host run. Runtime is auto-detected
(`podman`, then `docker`) or forced with `SDLC_SANDBOX_RUNTIME`.

**Configuration knobs** (all optional):

| Env var | Default | Purpose |
|---------|---------|---------|
| `SDLC_SANDBOX` | unset (off) | per-repo opt-in equivalent of `--sandbox`; also covers resumed runs |
| `SDLC_SANDBOX_IMAGE` | `sdlc-agent-sandbox:latest` | the image the agent runs in (must already contain `claude` + toolchain; the controller never builds it) |
| `SDLC_SANDBOX_RUNTIME` | auto (`podman`→`docker`) | force a specific runtime |
| `SDLC_SANDBOX_NETWORK` | `none` | egress mode — point at a locked-down filtering network only for a stage that genuinely needs the API ("explicit allowlist only if a stage needs it") |

Because egress is off by default, an agent inside the sandbox cannot reach the
Anthropic API unless the operator opts into a filtering egress network via
`SDLC_SANDBOX_NETWORK` (and mounts/forwards credentials accordingly). Building
that proxy is out of scope; the hook is the network knob. The `--sandbox` flag is
bound onto the real dispatch seam in `_resolve_dispatch` and persisted per run, so
a resumed run keeps the same isolation; `SDLC_SANDBOX` is honoured directly at the
dispatch boundary and so covers runs the flag never threaded through.

## Per-story worktree isolation (Story 17.2-001)

Until concurrency, every story built in the **shared repo root**: the build
agent ran `git checkout -b feature/<id>` in one working tree. That is unsafe the
moment two stories run at once — they would fight over one index and working
tree. So a real `parallel` run now gives each story its **own git worktree**.

`_prepare_story_workdir(opts, story, ledger, run_id, real_run=…)` decides, per
story, what cwd its agent runs in:

- **Real `parallel` run** → `create_story_worktree(root, story_id, run_id)` makes
  a checkout at `<root>/.claude/worktrees/agent-<run>-<story>`. The path follows
  the `agent-*` convention the orphan-sweeper (`hooks/sweep-orphan-worktrees.sh`)
  and the worktree-bootstrap hook (`hooks/forge-worktree-bootstrap.sh`) already
  key off, and `.claude/worktrees/` is gitignored so the checkouts never show in
  `git status`. The worktree is checked out **detached at the base ref**
  (`origin/main` when set, else `HEAD`), so the build agent cuts its own
  `feature/<id>` branch **inside** it exactly as on the shared-root path — the
  build prompt is unchanged. Concurrent stories therefore land on separate
  worktrees and separate branches over one **shared object store**, with no
  shared index or working-tree contention.
- **`--sequential` (or `--concurrency=1`)** → `None`: one story at a time cannot
  collide, so the shared root is kept for byte-for-byte back-compat.
- **A fake-dispatcher (test) run** (`real_run=False`) → `None`: orchestration is
  exercised without touching the real repo, exactly like the `_reposition_head`
  and close-out reconcile guards.
- **Worktree creation fails** (no repo, colliding path) → it logs the reason and
  falls back to the shared root: a `WorktreeError` is recoverable, **never**
  fatal to the build.

The chosen path is recorded on the story row (`stories.worktree_path`, added by
migration 5) so teardown (Story 17.2-002) and observability can locate the
checkout. `_run_story` binds the worktree onto the dispatch seam as `cwd` via
`functools.partial`, so **every** dispatch for that story — each stage, the
envelope re-ask, the commit-lint amend, and the bugfix loop — runs inside the
isolated checkout.

## Per-story worktree teardown (Story 17.2-002)

Creating a worktree per concurrent story (17.2-001) is only safe if each one is
cleaned up again — otherwise a long `parallel` run leaks `agent-*` checkouts on
disk, or a crash/resume trips a duplicate `git worktree add`. Story 17.2-002
closes the lifecycle, reusing the merged-worktree-removal semantics of
`hooks/worktree-gc.sh` and the registration-aware safety of
`hooks/sweep-orphan-worktrees.sh`.

- **Close-out (AC1).** When a story reaches a **terminal** outcome — `DONE`,
  `FAILED`, or a per-story `NEEDS_ATTENTION` (including the failure-isolation
  `FAILED` of a worker that raised) — `_teardown_story_workdir` looks up the
  story's recorded `worktree_path` and calls `remove_story_worktree`, which runs
  `git worktree remove --force` then `git worktree prune`. The `feature/<id>`
  branch and its commits are **never** touched: the branch/PR is the deliverable,
  so committed work survives (R10); `--force` only discards the worktree's own
  expendable working-tree state. Teardown is **keyed by the story's own recorded
  path**, so it can never race or remove a peer worker's in-flight checkout — and
  it runs *after* the cohort barrier on the parallel path (single-threaded there)
  for belt-and-braces. It is best-effort and never fatal: a removal failure is
  logged (the orphan-sweeper will later reclaim it), not raised.
- **Resumable holds keep their worktree.** A rate-limit park (`RATE_LIMITED`) or
  cost-gate pause `break`s/`continue`s *before* teardown, so the in-flight story's
  worktree is preserved for re-entry rather than torn down mid-flight.
- **Orphan sweep (AC2).** A crash or abort leaves the worktree behind. The
  existing `hooks/sweep-orphan-worktrees.sh` (6-hour `agent-*` sweep) reclaims it,
  and crucially **never removes a checkout `git worktree list` still tracks** —
  the same registration guard the controller uses via
  `_worktree_registered_paths`, so an in-flight story's worktree is safe even
  while the sweeper runs.
- **Resume re-attach (AC3).** `create_story_worktree` is deterministic on
  re-entry: if the target path is **still a live registered worktree** it is
  re-attached (returned as-is, preserving the resumed branch and committed work)
  rather than re-added; if a **stale directory** git no longer tracks is found, it
  is cleared and pruned before a fresh `worktree add`. Either way a `resume` never
  hits an "already exists"/"already registered" failure.
- **Resume close-out of end-crash stories.** A story whose stages are all `DONE`
  but whose status was never finalised ("end-crash") owns no stage to dispatch,
  yet it is **not** nothing-to-resume — it is closed out (`DONE`, no dispatch) and
  its worktree torn down, both in the cohort loop *and* when it is the only kind
  of work left. (Previously an end-crash-only run short-circuited as a no-op,
  stranding the run `IN_PROGRESS` and leaking its worktree — tearing the worktree
  down while leaving the run resumable would be an incoherent half-state, so the
  story is finalised instead.) `run_resume` therefore treats a run as
  nothing-to-resume only when it has neither dispatchable work nor an end-crash
  story to finalise.

`--sequential` / `--concurrency=1` and fake-dispatcher (test) runs record no
worktree, so teardown is a no-op and the shared-root path stays byte-for-byte
unchanged.

## Per-run token budget gate (Story 14.1-001)

`sdlc build --budget=<N>` sets a **token** ceiling the controller respects
between stories. Tokens — not dollars — are the governance primitive: on a
Claude Max subscription the `total_cost_usd` the agent envelope reports is an
**API-list-price equivalent** computed from usage, never real spend on the flat
monthly fee. A `$`-denominated budget (`--budget=$5`, `--budget=30usd`) is
accepted as a convenience and converted to a notional token ceiling via
`usd_to_notional_tokens` (rate `NOTIONAL_USD_PER_MILLION_TOKENS`, a documented
≈$15/Mtok constant — guidance, not a billing fact); the original dollars are
kept only for display.

The gate reads the live accrual from `Ledger.run_usage_totals(run_id)` — the
same per-stage token columns the 11.1-003 streaming path writes — and is checked
in the `run_build` cohort loop **after each story finishes**, never mid-stage.
Because the just-finished story's stages are already committed and the unbuilt
stories are still `TODO`, no committed work is ever discarded (R10 holds). When
accrued tokens cross the ceiling, `_budget_close_out` applies the
`--budget-policy`:

- **`pause`** (default): records a NEEDS_ATTENTION-style reason and leaves the
  run `IN_PROGRESS`, so `Ledger.latest_resumable_run` — and therefore
  `sdlc resume` — picks it up once the budget is raised.
- **`abort`**: records the reason and stamps the run `ABORTED` (terminal); not
  auto-resumable.

The budget (and policy) is persisted in the run's config event, and `run_resume`
**re-enforces** it: the same pre-dispatch `_budget_exceeded` check runs in the
resume loop, and because the accrual carried in the ledger already counts the
pre-pause spend, resuming a paused run *without* raising the ceiling re-pauses
immediately (dispatching nothing) rather than continuing unbounded.
`sdlc resume --budget=<N>` raises the ceiling so the run can finish — that is
what "resumable once the budget is raised" means. The pause/abort close-out is
shared between `run_build` and `run_resume` via `apply_budget_stop`, so a resume
halts identically to a fresh build.

Either way the reason logged to the ledger — and the `sdlc build` summary line —
renders any dollar figure through `notional_cost_label`, e.g.
`$0.62 (API-equivalent, not billed on subscription)`, so the `$` is never
mistaken for actual spend (the dashboard, owned by Epic-11, renders the same
label). With no `--budget` the path is byte-for-byte today's behaviour (the gate
is skipped; `0` means "no ceiling", never "ceiling of zero").

## Pre-dispatch cost estimate and warning (Story 14.1-002)

Before each stage is dispatched the controller computes a lightweight **usage
estimate**, records it on the stage row, and surfaces it — so an operator sees
roughly what a stage will spend *before* it runs, and can gate expensive work.
This is guidance only; the authoritative figure remains the post-stage
`--output-format` usage envelope reconciled at completion.

The estimator lives in `sdlc/cost_estimate.py`. `estimate_stage(stage, prompt,
*, historical_tokens)` returns a `StageEstimate` (prompt tokens, projected total
tokens, notional `$`, and a `calibrated` flag):

- **Heuristic.** Prompt tokens are `len(prompt) // ~4` chars; the projected total
  is `prompt_tokens × stage_factor`, where the per-stage factor (`build` 12×,
  `merge` 3×, …) reflects how much a stage amplifies its prompt into output +
  tool round-trips. The projection is floored at the prompt's own tokens.
- **Calibration.** When the ledger already holds recorded usage for that stage,
  `Ledger.historical_stage_tokens(stage)` (the average of past DONE attempts that
  recorded usage) overrides the crude factor — the estimate self-improves as the
  ledger fills, and the event is tagged `[calibrated from history]`.
- **Notional `$`.** Tokens convert to dollars at the same notional rate as the
  budget gate and render through `notional_cost_label`, so the `$` is never
  mistaken for real spend on the subscription.

`_estimate_stage_cost` (in `build.py`, called from `_run_story` before
`_dispatch_stage`) renders the prompt, estimates, writes the estimate to the
stage row via `Ledger.stage_set_estimate`, and logs a `pre-dispatch estimate`
event. It is **best-effort**: any failure degrades to `None` and the stage
dispatches exactly as today.

**Threshold gate.** `sdlc build --cost-threshold=<N>` sets a per-stage token
ceiling for the *estimate* (a `$` value is accepted and converted, like
`--budget`). When an estimate crosses it (`_over_cost_threshold`):

- in `--auto` the controller **warns and proceeds** (the warning names the
  estimate, the threshold, and the notional `$`);
- **interactively** (no `--auto`) it **gates before any spend** — the stage row
  is finished `SKIPPED` with category `cost-gate`, the gated story is parked
  `NEEDS_ATTENTION`, and no agent is dispatched (R10: no work started, nothing
  discarded).

A threshold of `0` (the default) means "no gate": the estimate is still computed
and recorded, but never warns or gates — behaviour is otherwise unchanged.

**Resumable, not a silent bypass.** The interactive gate pauses the *run*
resumably — mirroring the budget pause, it raises an internal `_CostGatePause`
that the cohort loop turns into a `cost_gated` close-out which leaves the run
`IN_PROGRESS` (never a terminal status `latest_resumable_run` couldn't surface).
The threshold is **persisted in the run config**, so `sdlc resume`
**re-enforces** the same gate rather than rebuilding options with `threshold=0`
and silently dispatching the stage the original run gated. To continue a gated
story, raise or clear the gate on resume: `sdlc resume <scope> --cost-threshold=0`
(disable) or `--cost-threshold=<higher>`. The persisted threshold + the SKIPPED
(not DONE) gated-stage row mean the resumed run re-attempts exactly that stage.
`--auto` is persisted alongside the threshold too, so a resumed auto run keeps
its **warn-and-proceed** posture and never flips to interactive and wrongly gates
a stage the original auto run would have run straight through.

**Estimate-vs-actual reconciliation.** On a stage's successful completion,
`_reconcile_estimate` compares the recorded estimate against the authoritative
token total from the agent envelope and logs an `estimate reconciled: est ~X vs
actual Y tokens (±Z%)` event. The persisted reconciliation is the
`estimated_tokens`/`estimated_cost_usd` columns sitting alongside the actual
usage columns on the same stage row (ledger Migration 3), which is also what
feeds the next run's historical calibration.

## Per-task model routing (Story 14.2-001)

Not every stage needs Opus. `sdlc build --model-routing=<profile>` dispatches
each pipeline stage on a model matched to its cognitive load, cutting quota burn
where quality is not at stake while pinning the strong tier where it is. The map
lives in `sdlc/model_routing.py` as a frozen `ModelRoutingConfig` (per-stage map
+ escalation policy), and `select_model(stage, config, *, points, high_risk)` is
the single chooser.

The shipped default profile is **Balanced**:

| Stage | Default | Escalates to Opus when… |
|---|---|---|
| `discovery` | Haiku | — (structured extraction) |
| `build` | Sonnet | points ≥ threshold (8) — see the resume-determinism note below |
| `coverage` | Sonnet | — (tests need correctness) |
| `review` | Sonnet | high-risk (`risk_gate`) **or** points ≥ threshold |
| `adversarial` | **Opus** | **pinned — never downgraded, in any profile** |
| `merge` | Haiku | — (mechanical) |

Two documented alternatives ship alongside it: **Quality-first** (Opus
everywhere) and **Quota-max** (cheapest everywhere, with the adversarial skeptic
still pinned to Opus and a higher escalation bar). A per-repo
`.sdlc-model-routing.yaml` additively overrides the chosen profile's stage map,
points threshold, or escalation model — mirroring `risk_gate.py`'s
`.sdlc-risk-config.yaml` convention. A missing file is a silent no-op; a
malformed one is a hard error.

**Where the `--model` is applied.** Every dispatch this controller makes routes:
the four pipeline stages via `_dispatch_stage`, plus the two recovery agents —
the envelope-only re-ask (`_reask_envelope`, on the map's `reask` tier) and the
bugfix agent (`_run_bugfix`, on the `bugfix` tier; per-attempt escalation on
retry is layered on by Story 14.2-003 — see below). Each calls
`_select_stage_model(stage, story, opts)` and threads
the result into `dispatch_agent(model=…)`, which appends `--model <model>` to the
**default** command only. `_ROUTABLE_STAGES` — the stages a `--model-<stage>`
override is accepted for — is exactly this routed set (`build`, `coverage`,
`review`, `merge`, `bugfix`, `reask`); `discovery` and `adversarial` are
dispatched outside this pipeline, so an override for them is a hard error rather
than a silent no-op (the profile map still defines their tiers for `select_model`
and the adversarial Opus pin). Precedence, highest first:

1. an explicit per-stage `--model-<stage>=<model>` flag (the escape hatch);
2. a `SDLC_AGENT_CMD` override — the custom command owns its own model, so the
   routed model never decorates it (`resolve_agent_cmd` returns it untouched);
3. the routing profile's `select_model`;
4. routing off (`--model-routing` unset / `off`) → **no `--model`**, so the CLI
   default (Opus today) stands and behaviour is byte-for-byte unchanged.

The high-risk signal (`_story_high_risk`) matches the story branch's changed
files against the Epic-08 risk-gate patterns, best-effort (any git/import error
→ `False`). It is consulted **only for `review`** (`_RISK_AWARE_STAGES`), where
the branch is already pushed so the same diff — and the same verdict — is seen on
the original run and on a resume. `build` deliberately ignores it and escalates
on **points** alone: build's branch does not exist when its model is chosen on a
fresh run, so a live-git lookup would return `False` on first build but `True` on
a resume (branch now present), silently changing the routed model across the
resume. Routing build off points (a spec-derived, resume-stable signal) keeps it
deterministic. The resolved config is memoized on `opts` so the override file is
read at most once per run. The profile and per-stage overrides are persisted in
the run's config event and rehydrated by `run_resume`, so a resumed run routes
identically. The `<<<RESULT_JSON>>>`
contract and schema validation are untouched — routing changes only *which model*
runs a stage, never how its output is parsed.

### Cheap-first dispatch with model escalation on retry (Story 14.2-003)

Routing makes the *common* path cheap; cheap-first makes the *stuck* path strong
without paying for it up front. A stage runs on its mapped (cheaper) tier on the
first pass — the passing path, which is the common one. Only when a stage **fails
into the bugfix loop** does it climb: each bugfix attempt escalates the model one
tier up the ladder (`TIER_LADDER = (haiku, sonnet, opus)`), capped at the
strongest tier, rather than retrying on the model that just failed. So Opus is
paid for exactly when a stage is actually stuck — not on every build.

`escalate_model(base, steps)` in `sdlc/model_routing.py` is the chooser: it bumps
`base` up the ladder by `steps`, and is a deliberate no-op when `steps <= 0` (the
cheap first pass), when `base is None` (routing off — no tier to climb), when
`base` is already at the top tier (escalating Opus does nothing), or when `base`
is a custom / pinned model id the ladder cannot reason about (returned verbatim,
never silently rewritten). `_select_stage_model(…, escalation_steps=N)` applies it
*after* the base map selection — and an explicit `--model-<stage>` pin is returned
*before* it, so an operator's pin is never escalated.

The escalation level is the **count of bugfix attempts already spent on this
stage**, threaded through `_run_story`'s loop: `0` on the first dispatch, `+1` per
retry. Both the retried stage (`_dispatch_stage(…, escalation_steps=bugfix_attempts)`)
and the bugfix agent itself (`_run_bugfix(…, escalation_steps=bugfix_attempts)`)
climb together. The existing `MAX_BUGFIX_ATTEMPTS` budget is unchanged, so a stage
already mapped to Opus simply re-runs on Opus (escalation no-op) without extra
attempts. The model chosen per attempt is recorded as a ledger event
(`info`, `controller`, tagged `Story 14.2-003`) for both the retry dispatch and
the bugfix agent, so the Epic-18 eval harness can measure cheap-first's
success rate. Worked example on **Balanced** (`build` = Sonnet): first build on
Sonnet fails → bugfix and the build retry both escalate to Opus. On **Quota-max**
(`build` = Haiku) two failures walk the full ladder Haiku → Sonnet → Opus.

The climb survives a resume. The escalation level is `start_escalation +
bugfix_attempts`, where `start_escalation` is the resumed stage's prior
FAILED-attempt count reconstructed from the ledger (`compute_resume_plan`). So a
stage that had already climbed to a stronger tier before an interruption resumes
on that tier rather than dropping back to its cheap base — mirroring the
resume-determinism guarantee routing established in 14.2-001. Only **FAILED**
attempts count: a crashed or rate-limited `IN_PROGRESS` attempt never escalated,
so it never inflates the level. This is a routing offset only — the bounded
`MAX_BUGFIX_ATTEMPTS` budget is untouched, keeping its existing per-resume reset
semantics.

## Thinking-token cap and early-compaction config (Story 14.2-002)

Extended thinking is a hidden per-request cost: every dispatched agent may spend
thinking tokens the result envelope never surfaces as a stage knob. On a long
overnight batch that cost compounds. `sdlc build --thinking-cap=<N>` bounds it.

**How the cap is applied.** The cap is an *environment* knob, not a CLI flag.
`dispatch.py` exports it as **`MAX_THINKING_TOKENS`** on a copy of the current
environment (`_dispatch_env`), handed to both the streamed `Popen` and the
captured `subprocess.run`. Because it rides the environment rather than the
agent's argv, the same cap applies to the built-in default command *and* any
`SDLC_AGENT_CMD` / explicit override — `claude -p` honours `MAX_THINKING_TOKENS`
regardless of the rest of the command. With no cap (`--thinking-cap` unset or
`0`) `_dispatch_env` returns `None`, the subprocess inherits the parent
environment unchanged, and the agent keeps its default thinking budget — the
no-cap path is byte-for-byte today's.

**One bind, every stage.** The cap is a per-run constant, so it is bound once
onto the real dispatch seam in `_resolve_dispatch` (a `functools.partial` over
`dispatch_agent`) rather than threaded through each `dispatch(...)` call site.
That single bind reaches every routed stage (build / coverage / review / merge /
bugfix / reask). An *injected* dispatcher (the orchestration tests' fake) is
returned untouched — it owns its own signature — so the cap is bound only on the
real `dispatcher is None` path.

**Recorded per run, re-applied on resume.** The cap is persisted in the run's
`config` event (`thinking_cap`) so the dashboard can show it and `sdlc resume`
re-applies the same bound (`_options_from_config` carries it; legacy runs without
the field default to no cap).

**Early compaction.** Auto-compaction is left at Claude Code's default (enabled,
`autoCompactEnabled`): the controller never sets `DISABLE_AUTO_COMPACT`, so a long
run keeps compacting context near the limit. There is no documented env var to
lower the compaction *threshold*, so "early compaction" here means **honouring —
not disabling — the built-in behaviour** while the thinking cap does the bounding
of hidden per-request cost.

## Rate-limit / quota awareness with automatic resume (Story 14.1-003)

On a Claude Max subscription the real overnight failure mode is not dollars (they
are flat) but **finite quota**: a 5-hour rolling window and a weekly cap. Without
handling, a limit hit surfaced as a non-zero `claude -p` exit → `AgentDispatchError`
→ the bugfix loop re-dispatched (also throttled) → the story parked
`NEEDS_ATTENTION`/`FAILED` and the night's run died. Story 14.1-003 replaces that
with detect → pause → auto-wait-or-park → resume.

**Detection.** `sdlc/rate_limit.py` (`detect_rate_limit`) scans the agent's
stderr / error-envelope text for a throttle signal — a `429`, a `rate limit` /
`rate_limit_error` / `usage limit reached`, or an explicit `Retry-After` / reset
epoch. `dispatch.py` raises a distinct **`RateLimitError`** (a subclass of
`AgentDispatchError`, so any older `except AgentDispatchError` still degrades
gracefully) carrying a `RateLimitSignal`. When **no** signal is present the
exit is the ordinary `AgentDispatchError` and behaviour is exactly today's (AC7).

**A throttle is never a stage failure.** `_dispatch_stage` (and the reask /
bugfix / commit-lint dispatch sites) re-raise `RateLimitError` *before* the
generic `AgentDispatchError` handling, so a 429 never records a `FAILED` attempt
nor burns a bugfix attempt. `_run_story` (given an `rl_ctx`) absorbs it: the
interrupted attempt's `IN_PROGRESS` row is left as a crashed attempt and the
*same* stage is retried as a fresh attempt — preserving the PR/bugfix state and
any committed work (R10).

**Auto-wait vs durable park.** The reset time is computed by `seconds_until_reset`
(explicit `retry-after` → absolute reset epoch → else a full `--window`, the
documented approximate heuristic). If it is within the configurable
**`--rate-limit-max-wait`** cap (default ≈ one window, ~5h), the controller
**waits in-process** (`_rate_limit_wait`, flagging the run `RATE_LIMITED` with a
periodic countdown event) and **auto-resumes the same run** — no manual
`sdlc resume`. The per-agent dispatch timeout is unaffected: it bounds the agent
subprocess, not this controller-side wait. If the reset is **beyond** the cap
(e.g. a weekly cap days away), the controller does not hold the process: it
durably parks the run **`RATE_LIMITED`** and exits.

**`RATE_LIMITED` is a distinct, resumable state** — deliberately *not*
`NEEDS_ATTENTION` (the run waits for *time*, not human attention) and *not*
terminal. `Ledger.latest_resumable_run` matches it alongside `IN_PROGRESS`, so
`sdlc resume` (or a scheduled wake) continues it once the window reopens; an
interrupted (machine sleep/crash) paused run resumes the same way from the
committed ledger state. It renders with its own badge in `sdlc status` /
the dashboard (Epic-11 11.2-009).

**Resume honours the parked reset time.** The park persists the approximate
reset epoch into the run config (`rate_limit_reset_at`), and `_honor_parked_reset`
gates the resume on it *before* dispatching anything: a resume that arrives
**before** the window reopens waits in-process (when the remaining time is within
the cap) or durably re-parks (beyond it), so an early resume can never dispatch
into a still-closed window and blow the quota. Once the reset has passed, the
window is treated as freshly reopened — the `WindowQuota` baseline is seeded with
the run's *current* accrual (not 0), so the resumed run makes forward progress
rather than re-parking forever on pre-park spend.

**Configured window budget (proactive).** When no live rate-limit header is
available, a configured per-window token budget gates dispatch instead:
`--window-budget=<N|$>` (tokens, or a `$` convenience converted via the same
notional rate as 14.1-001) tracked over a `--window=<s>` rolling window via the
11.1-003 accrual, pausing at `--rate-limit-threshold` (default `1.0`; `<1` pauses
*near* the limit). `WindowQuota` measures usage from a baseline captured at the
window's open; `_run_story_rate_limited` checks it *before* dispatching each story
and waits/parks identically.

All four knobs are persisted in the run's config event, so `run_resume`
re-enforces the same cap and window budget (carried via `_options_from_config`);
the wait/park is shared between `run_build` and `run_resume` (`_make_rate_limit_context`,
`_run_story_rate_limited`, `apply_rate_limit_park`) so a resume reacts identically
to a fresh build. The `clock` / `sleep_fn` are injectable so the in-process wait
is deterministic and instant under test.

## Documentation-currency lens (Story 18.3-001)

The four stages never used to tell an agent to touch user-facing docs, so docs
drifted from behaviour across unattended batches and FX hand-ran `/sync-progress`
after every run. `doc_currency.py` closes that gap at two surgical touch-points —
no new stage:

1. **Build prompt + story-template DoD.** When the lens is enabled (the default),
   `render_build_prompt` appends an instruction to update the affected user-facing
   docs (`README.md`, `docs/`, usage/help text) **in the same commit** as the code
   change — scoped to what the diff actually touches, never the CHANGELOG (the
   Epic-05 release workflow owns that). The matching DoD line ships in the
   `create-epic` / `generate-epics` story templates.
2. **Review dimension.** `render_review_prompt` appends a documentation-currency
   dimension, and `doc_currency.analyze_diff` / `analyze_paths` are the
   deterministic core: they parse a `git diff`, classify each touched path against
   a conservative behaviour heuristic — a CLI verb/flag (`controller/src/sdlc/cli.py`),
   a skill (`skills/**`), a hook (`hooks/**`), or an installer step
   (`setup.sh` / `bootstrap*.sh` / `lib/*.sh`) — and emit one finding per category
   (which doc looks stale + a one-line why) **only** when a behaviour-changing diff
   ships with no doc update. Any doc touch in the same diff suppresses all findings
   (the low-false-positive choice), test/fixture files never count as behaviour,
   and a CHANGELOG bump neither satisfies currency nor is ever flagged.

**Policy.** `SDLC_DOC_CURRENCY_POLICY` selects `advisory` (default — record on the
PR, never blocks shipping) or `route_to_bugfix` (hand the gap to the bounded
bugfix loop). `DocCurrencyResult.route_to_bugfix` is true only when the policy
routes *and* there is a finding to route.

**Disable switch.** `SDLC_DOC_CURRENCY` set to `0`/`false`/`no`/`off` reverts to
today's behaviour — the build prompt and review prompt drop their docs language,
and the lens returns no findings. Default is on.

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
- **Per-stage observability is ledger-based.** Stage transitions are written to
  the ledger `events` table and surfaced via the markdown render hook. Run
  lifecycle milestones are mirrored to Telegram via `notify.py`.
- **`current_stage` is not written.** The column exists in the schema for the
  resume story (4.3-001); the build state machine does not populate it yet.
