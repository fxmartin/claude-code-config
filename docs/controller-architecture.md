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
| `sdlc/reconcile.py` | Verifies parked stories against `origin/main` and recomputes the run terminal — shared by close-out and `sdlc reconcile` (Stories 12.3-001/12.3-002). |
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
4. **Per-story execution** — each story walks `build → coverage → review →
   merge` (coverage is skipped under `--skip-coverage`). Each stage dispatches
   an agent and the response is validated against its schema *before* the next
   stage runs.

   **Branch isolation (Story 12.4-001).** The build prompt
   (`render_build_prompt`) cuts the story branch from a freshly-fetched remote
   base — `git fetch origin && git checkout -b feature/<id> origin/main` — not
   the base-less `git checkout -b feature/<id>`. Agents share one working dir
   (`dispatch.py` has no per-call `cwd`/worktree yet — full worktree isolation is
   deferred to epic-17 17.2-001/17.2-002), and the merge agent only returns HEAD
   to `main` on its **success** path; on a parked/blocked/conflict path it leaves
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

### Schema migrations (auto-applied at launch)

The schema evolves additively: each `_MIGRATIONS` entry in `build.py` adds
missing columns to an existing ledger via `ALTER TABLE`, guarded by
`PRAGMA table_info` so a fresh DB built from the current DDL is a no-op, and
records its version in the `_migrations` table so it runs at most once per DB
(`_apply_migrations`). A fresh `build` gets this for free — `Ledger.init()`
runs the DDL then `_apply_migrations`.

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
