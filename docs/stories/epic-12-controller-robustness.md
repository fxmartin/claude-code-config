# Epic 12: Controller Robustness & Failure Recovery

> **Status: PLANNED** — created 2026-06-20 from [issue #72](https://github.com/fxmartin/claude-code-config/issues/72),
> a post-mortem of the `sdlc build epic-10` run (ledger run `a07a1855`). Distinct from
> Epic-10 (which *completed the stub CLI verbs*) and Epic-11 (realtime/multi-run
> *observability*): this epic makes the controller **resilient** — it should not strand
> recoverable work, hang CI, clobber history, or emit non-compliant commits when an
> autonomous run goes sideways.

## Epic Overview

**Epic ID**: Epic-12
**Description**: The first long autonomous controller run (epic-10) exposed that the
`sdlc` controller is brittle around failure. When the coverage agent for story 10.2-001
finished its work but omitted the `<<<RESULT_JSON>>>` envelope, the controller parked the
story `NEEDS_ATTENTION`, committed-but-did-not-push the branch, ran **no** bugfix/retry, and
exited 1 — stranding otherwise-good work that then needed full manual rescue (fix a hanging
test, resolve conflicts, rewrite non-compliant commits, push, PR, merge). This epic hardens
the four failure modes that surfaced: (1) a missing/malformed result envelope dead-ends a
stage instead of being retried; (2) `default_preflight` can recurse into the project test
command and hang; (3) the ledger renderer can clobber hand-maintained `.build-progress.md`
history; (4) agent-authored commits can violate commitlint and only fail at PR time.

A second post-mortem — the epic-11 run `877df2ab` and epic-12 run `ced08c0f`, both of which
finished marked **FAILED** while every story's work had actually merged to `main` and each
required **manual ledger reconciliation by hand** — exposed two further failure modes addressed
by Features 12.3/12.4: (5) run terminal status is a blind in-memory tally of self-reported agent
statuses that never verifies what landed on `origin/main` (and a merge blocked only by the
high-risk human gate is mistaken for a failure); and (6) branch-stacking — story branches are
cut base-less from whatever HEAD is checked out, so a parked story's commits ride a later story's
merge onto `main` transitively, leaving the ledger out of sync with reality.

**Business Value**: FX runs long unattended autonomous batches (increasingly several in
parallel). The value of autonomy collapses if a single malformed agent message — or a
self-inflicted hang — strands a night's work and demands a manual archaeology session the
next morning. Robustness is what makes "fire-and-forget overnight" actually safe.

**Success Metrics**:
- An agent stage that produces good work but a malformed/missing result envelope is
  **recovered automatically** (bounded re-ask/retry) in the common case, rather than parked
  for manual rescue. Manual `NEEDS_ATTENTION` rescues drop toward zero.
- No controller run **hangs**: preflight and agent-added tests cannot recurse into the
  controller's own orchestration; a runaway stage is bounded by a timeout.
- The ledger renderer **never destroys** pre-existing/non-ledger `.build-progress.md` history.
- **Zero** commitlint failures reach a PR from agent-authored commits.
- A run whose work merged to `origin/main` **reports DONE without manual intervention** — the
  number of by-hand ledger reconciliations drops to zero.
- A high-risk-gated merge is reported as `AWAITING_APPROVAL`, **never** FAILED, and a
  parked/failed story **never** lands transitively on `main`.

## Epic Scope

**Total Stories**: 12 | **Total Points**: 46 | **MVP Stories**: 0 (roadmap — primary defect is Must Have)

## Out of Scope (Non-Goals)

- **Realtime progress / streaming** — owned by Epic-11. This epic is about *recovery and
  integrity*, not visibility (though Epic-11's sub-stage events would surface these failures
  sooner).
- **New CLI verbs or orchestration surface** — Epic-07/Epic-10 own the surface; this epic
  hardens the existing pipeline only.
- **Changing the result-envelope contract or JSON schemas** (Epic-07). Recovery must work
  *with* the existing `<<<RESULT_JSON>>>` contract, not redefine it.
- **Unbounded retries.** Recovery stays within the existing bounded bugfix budget; the goal
  is to *attempt* recovery before parking, not to retry forever.

## Features in This Epic

### Feature 12.1: Resilient Agent-Result Handling

Stop a single bad agent message or a self-inflicted hang from dead-ending a run.

#### Stories

##### Story 12.1-001: Recover a missing or malformed result envelope before parking
**Status**: Done (run `ced08c0f`, shipped via #88)
**User Story**: As FX running an unattended build, I want the controller to attempt a bounded
recovery when an agent completes work but omits or malforms the `<<<RESULT_JSON>>>` envelope,
so that otherwise-good work is not stranded as `NEEDS_ATTENTION` and left for manual rescue.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** an agent stage exits cleanly but `_parse_envelope` finds no valid
  `<<<RESULT_JSON>>> … <<<END_RESULT>>>` block **When** the controller processes the result
  **Then** it issues a bounded **envelope-only re-ask** (re-prompt the same agent to emit just
  the result block for the work it already did) before taking any terminal action.
- **Given** the envelope re-ask still fails **When** retries within the bounded budget are
  exhausted **Then** the stage is routed through the existing bounded bugfix/retry path (the
  same recovery other agent failures receive), and only then parked `NEEDS_ATTENTION`.
- **Given** any of these recovery attempts **When** they run **Then** the R10 guarantee holds:
  committed work is never discarded, and each attempt is recorded in the ledger `events` log.
- **Given** a successful recovery **When** the envelope is obtained **Then** the run proceeds
  to the next stage exactly as if the agent had emitted it the first time (no manual step).

**Technical Notes**: Touches `controller/src/sdlc/dispatch.py` (`_parse_envelope`,
`dispatch_agent`) and `controller/src/sdlc/build.py` (`_run_story` — the path that today logs
"missing RESULT_JSON marker" and goes straight to `NEEDS_ATTENTION` with "no bugfix re-run").
Reuse the existing bugfix-iteration budget (`_MAX_BUGFIX…`) rather than adding a new unbounded
loop. An envelope-only re-ask is cheaper than a full stage re-run — try it first.

**Definition of Done**:
- [ ] Missing/malformed envelope triggers a bounded envelope re-ask, then the bugfix path,
      before `NEEDS_ATTENTION`
- [ ] R10 (never discard committed work) preserved; recovery attempts logged to the ledger
- [ ] Tests: synthetic agent outputs (no envelope, malformed envelope, recovered-on-re-ask,
      exhausted) drive each branch
- [ ] Docs updated (`docs/controller-architecture.md` failure-handling section)

**Dependencies**: None
**Risk Level**: Medium

##### Story 12.1-002: Guard preflight and agent-added tests against recursive hangs
**Status**: Done (run `ced08c0f`, shipped via #89)
**User Story**: As FX, I want the controller's preflight (and agent-added tests) to be unable
to recurse into the controller's own orchestration, so that a build cannot hang itself.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `default_preflight` runs the detected project test command **When** that test
  suite contains a test that invokes the CLI's `build` (or `dashboard`) verb bare **Then** the
  invoked verb short-circuits under a test/CI sentinel instead of launching the real
  preflight/orchestration (no pytest-within-pytest, no server bind).
- **Given** an agent adds tests during a build **When** the controller runs them **Then** they
  execute under a per-invocation timeout, so a hanging test fails fast with a clear message
  rather than stalling the run until the preflight timeout.
- **Given** the guard is in place **When** the normal test suite runs **Then** legitimate CLI
  tests still pass (the guard only blocks real orchestration side effects, not unit coverage).

**Technical Notes**: This surfaced as a real hanging test in the 10.2-001 build that the
coverage gate never caught (it could not run). Options: a `SDLC_IN_TEST`/CI env sentinel that
`build`/`dashboard` check before doing real work; and/or wrapping the controller's test
invocation with a timeout. Touches `default_preflight` and the `build`/`dashboard` command
entry points.

**Definition of Done**:
- [ ] `build`/`dashboard` short-circuit under the test sentinel (no real preflight/server)
- [ ] Controller-run tests are bounded by a timeout
- [ ] Regression test: a test that invokes `build` bare no longer hangs the suite
- [ ] Docs note the sentinel contract

**Dependencies**: None (shares `build.py`/CLI files with 12.1-001 — serialize the build if run in parallel)
**Risk Level**: Medium

### Feature 12.2: Build-Output Integrity

Protect the artifacts the controller writes: the progress view, the commits it authors, and the
ledger schema it depends on.

#### Stories

##### Story 12.2-001: Make the progress renderer non-destructive to non-ledger history
**Status**: Done (run `ced08c0f`, shipped via #88)
**User Story**: As FX, I want the `.build-progress.md` ledger renderer to preserve
hand-maintained / pre-existing build history, so that a controller run does not clobber the
logs of epics completed outside the SQLite ledger.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `.build-progress.md` contains build logs not represented in the ledger (epics done
  via the markdown workflow) **When** `scripts/sdlc-state.sh render` regenerates the file
  **Then** that pre-existing history is preserved, not overwritten.
- **Given** the renderer runs **When** it appends ledger-driven run data **Then** the result
  is deterministic and idempotent (re-rendering does not duplicate or drop sections).
- **Given** a fresh repo with no prior history **When** the renderer runs **Then** behavior is
  unchanged from today (no regression for the greenfield case).

**Technical Notes**: A "Historical" block already exists (frozen at story 4.2-001). Extend that
contract so any non-ledger content is preserved — e.g. a managed-region marker pair around the
auto-generated section, leaving everything outside it untouched; or make regeneration
opt-in/non-destructive by default. Touches `scripts/sdlc-state.sh` (render) and possibly
`controller/src/sdlc/ledger_view.py`.

**Definition of Done**:
- [ ] Renderer preserves non-ledger/pre-existing history (managed-region or equivalent)
- [ ] Idempotent re-render (bats/test proof)
- [ ] Greenfield behavior unchanged
- [ ] Documented in the ledger-view / source-control reference

**Dependencies**: None
**Risk Level**: Low

##### Story 12.2-002: Lint agent commit messages against commitlint at commit time
**Status**: Done (run `ced08c0f`, shipped via #87)
**User Story**: As FX, I want the controller to validate agent-authored commit messages
against the repo's commitlint rules at commit time, so that a non-compliant header never
reaches a PR and fails the commit-format CI job.
**Priority**: Should Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** a build/coverage/bugfix agent is about to commit **When** the commit message
  violates the repo's commitlint rules (e.g. header > 72 chars, non-lowercase subject,
  disallowed type) **Then** the controller detects it before/at commit time and re-asks the
  agent for a compliant message (bounded), rather than letting it surface only at PR CI.
- **Given** a repo with no commitlint config **When** an agent commits **Then** the check is a
  no-op (graceful — the controller does not invent rules).
- **Given** the check is in place **When** an agent emits a compliant message **Then** there is
  no behavior change.

**Technical Notes**: The 10.2-001 build commit was 84 chars + capitalized subject and only
failed at PR time, forcing a history rewrite. The controller can shell `commitlint` (or apply
the repo's `.commitlintrc.json` rules) against the proposed message in the commit step. Touches
the controller's commit step in `build.py` / the agent commit instructions.

**Definition of Done**:
- [ ] Commit messages linted against the repo's commitlint config at commit time
- [ ] Bounded re-ask on violation; graceful no-op when no config present
- [ ] Tests cover violation→re-ask and the no-config no-op
- [ ] Documented alongside the commit-format reference

**Dependencies**: None
**Risk Level**: Low

##### Story 12.2-003: Auto-apply pending ledger migrations at controller launch
**Status**: Done (run `ced08c0f`, shipped via #88)
**User Story**: As FX launching the controller in a repo whose ledger predates a schema change,
I want any pending migrations applied before a verb reads or writes the ledger, so that an
out-of-date DB (e.g. one missing 11.2-007's `wave`/`dependencies` columns) never crashes
`status`/`dashboard`/`resume`/`rollback` with a "no such column" error.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a repo whose ledger DB exists but is behind on migrations (lacks columns a later
  `_MIGRATIONS` entry adds) **When** any controller verb is launched (`build`, `status`,
  `dashboard`, `resume`, `rollback`) **Then** pending migrations are applied (via the existing
  `_apply_migrations`) **before** the verb reads or writes the ledger, so no query fails with
  "no such column".
- **Given** a read/recovery verb triggers the migration **When** it runs **Then** the migrate
  step uses a **writable** connection up front (a read-only connection cannot `ALTER TABLE`);
  subsequent reads may still use the read-only path, and re-launching is a no-op (idempotent).
- **Given** no ledger DB exists yet (never-built repo) **When** a read verb runs **Then**
  behavior is unchanged from today — it reports "no ledger / no runs" and does **not** create a
  spurious empty DB (migrate only when the DB already exists).
- **Given** two controller processes launch concurrently against the same ledger **When** both
  attempt the same pending migration **Then** it is safe (the `_migrations` version guard plus a
  SQLite busy timeout) — no corruption, no double-apply, no crash.
- **Given** a fresh build that creates the ledger from the current DDL **When** it runs **Then**
  applying migrations is a no-op (no regression from today).

**Technical Notes**: Today `Ledger.init()` (which runs `_SCHEMA_DDL` + `_apply_migrations`) is
called in exactly one place — inside `run_build` (`build.py:1172`). The read/recovery verbs
construct `Ledger(db_path)` and query via `_connect_ro` **without** migrating (`status`
`cli.py:177`, `dashboard` `dashboard.py:369`, `resume` `cli.py:350`, `rollback` `cli.py:443`).
Centralize an idempotent `Ledger.ensure_migrated()` that — only when `db_path` exists — opens a
writable connection and runs `_apply_migrations`, and call it at each verb's launch before any
read. Add a SQLite busy timeout for concurrent launches. Per this epic's non-goals, do **not**
add a new `sdlc migrate` verb — apply automatically at launch.

**Definition of Done**:
- [ ] Idempotent `ensure_migrated` (or equivalent) applied at launch by `build`, `status`, `dashboard`, `resume`, `rollback` before any ledger read/write
- [ ] Read verbs migrate via a writable connection, then read; no "no such column" on a stale ledger
- [ ] No-DB case unchanged (no spurious ledger created for read verbs)
- [ ] Concurrent-launch safety (busy timeout + `_migrations` guard) covered by a test
- [ ] Tests: an old-schema fixture DB is auto-migrated on each verb; fresh-DB no-op; no-DB read behavior preserved
- [ ] Docs updated (`docs/controller-architecture.md` ledger/migration section)

**Dependencies**: None (hardens the existing migration mechanism). Directly makes Epic-11 story
11.2-007's `wave`/`dependencies` migration safe on pre-existing ledgers.
**Risk Level**: Medium

##### Story 12.2-004: Generate compliant commit subjects
**User Story**: As FX, I want the controller to ensure every agent commit subject is
commitlint-compliant by construction — not dependent on an agent transcribing a long, Title-Case
story title — so that autonomous runs do not stall on commit-format re-asks and a malformed
re-ask cannot park otherwise-good work `NEEDS_ATTENTION`.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a build/coverage/bugfix agent prepares a commit **When** the controller commits it
  **Then** the subject is commitlint-compliant by construction (header ≤ 72 incl. the
  `type(scope):` prefix, lower-case subject, no trailing period) — the controller normalizes the
  proposed subject (lower-case the initial letter, trim to the length budget, strip a trailing
  period) rather than relying on the agent transcribing the story title verbatim.
- **Given** a story whose title is long or Title-Case (e.g. the Feature 12.3/12.4 titles)
  **When** its commit is created **Then** the resulting subject still passes commitlint (the raw
  title is never used as the subject).
- **Given** the common case **When** a build runs **Then** the commit passes the gate on the
  first attempt and no re-ask is dispatched (compliant by construction, not by retry).
- **Given** a commit-format re-ask is still needed **When** it is dispatched **Then** it routes
  through the same envelope-recovery path as other stages (12.1-001), so a malformed re-ask
  response (e.g. missing `branch_name`, as in run `7df64f19`) is recovered/retried rather than
  dead-ending the story into `NEEDS_ATTENTION`.
- **Given** an agent already emits a compliant subject **When** normalization runs **Then** it is
  left unchanged (no regression; idempotent).

**Technical Notes**: Story 12.2-002 added commitlint-at-commit with a bounded re-ask
(`controller/src/sdlc/commitlint.py`, `build.py`). The remaining gap: build agents derive the
subject from the story title (`render_build_prompt`), which is frequently > 72 chars and
Title-Case, so commitlint rejects it and the run depends on the re-ask succeeding — and the
re-ask can itself fail on a malformed envelope (run `7df64f19`: "commit-lint re-ask dispatch
failed: ... missing required field 'branch_name'"). Fix at the source: have the controller
normalize/derive a compliant subject before committing (and/or have the build prompt supply the
`type(scope):` prefix and mandate a short imperative lower-case subject), and route the
commit-format re-ask through `_reask`/envelope recovery (12.1-001) so a bad re-ask response is
recovered. References 12.2-002 (extends it) and 12.1-001 (envelope recovery).

**Definition of Done**:
- [ ] Commit subjects compliant by construction (length, case, no trailing period); raw story title never used as the subject
- [ ] Common case passes the gate first time (no re-ask needed)
- [ ] Commit-format re-ask routes through envelope recovery; a malformed re-ask response is recovered, not parked
- [ ] Already-compliant subjects unchanged (idempotent)
- [ ] Tests: long/Title-Case title → compliant subject; malformed re-ask envelope → recovered; idempotent on compliant input
- [ ] Docs updated (commit-format reference + failure-handling section)

**Dependencies**: 12.2-002
**Risk Level**: Medium

### Feature 12.3: Honest Run-Terminal Status

A run's terminal status must reflect what actually landed on `origin/main`, not an in-memory
tally of self-reported agent statuses. A run whose work shipped — even after a 429 abort or via
a stacked transitive merge — must report SUCCESS; a merge blocked only by the high-risk human
gate must report a distinct "awaiting human" state, not FAILED. (Post-mortem of the epic-11 run
`877df2ab` and epic-12 run `ced08c0f`, both of which finished FAILED while their work was fully
merged to `main`, and both of which required manual ledger reconciliation by hand.)

#### Stories

##### Story 12.3-001: Reconcile story status against origin/main
**Status**: Done (run `7df64f19`; reconcile core shipped as `73fdaf2` via PR #93)
**User Story**: As FX running unattended builds, I want the controller to verify against
`origin/main` whether each story's work actually landed before it computes the run's terminal
status, so that a run whose PRs genuinely merged reports DONE instead of FAILED/NEEDS_ATTENTION
from a stale in-memory tally.
**Priority**: Must Have
**Story Points**: 8

**Acceptance Criteria**:
- **Given** a story parked `NEEDS_ATTENTION`/`FAILED`/`BLOCKED`/`AWAITING_APPROVAL` whose
  `feature/<id>` work is provably present on `origin/main` **When** close-out reconciliation runs
  **Then** that story is reclassified `DONE`, its `merge` stage row is recorded/updated to `DONE`,
  and an audit event (`source="reconcile"`) names the winning signal and merge SHA.
- **Given** reconciliation needs the latest remote state **When** it starts **Then** it runs
  `git fetch origin` first, and a fetch failure (offline / no remote) degrades to a no-op skip —
  reconciliation never raises and never fails an otherwise-good run.
- **Given** landing detection across merge styles **When** it evaluates a story **Then** it treats
  the story as landed if **any** of: `git merge-base --is-ancestor feature/<id> origin/main`;
  `git cherry origin/main feature/<id>` reports nothing left to apply (patch-id equivalence —
  squash/rebase-resilient, catches transitive/stacked landings); `gh pr view <pr_number> --json
  state` is `MERGED`; or `origin/main` contains a commit whose message matches the mandated
  `(#<story_id>)` tag.
- **Given** a story already `DONE`/`SKIPPED` **When** reconciliation runs **Then** it is left
  untouched (no redundant work, no duplicate merge row).
- **Given** reconciliation finishes **When** the run terminal is computed **Then** it is computed
  from the reconciled per-story statuses, so a run whose every story landed reports `DONE`.
- **Given** reconciliation is re-run on an already-reconciled run **When** it executes **Then** it
  is idempotent — no status flips, no duplicate stage rows, only a "nothing to reconcile" event.

**Technical Notes**: New module `controller/src/sdlc/reconcile.py` exposing
`reconcile_run(ledger, run_id, root=None, fetch=True) -> ReconcileResult`. Reuse `_base_ref`,
`_git`, and the branch-existence guard pattern from `story_commit_exists` in `build.py`. Note
`story_commit_exists` only counts commits *ahead of* base (`rev-list --count base..branch`) and so
cannot detect already-landed work — that is the gap; the `--is-ancestor`/`git cherry`/`gh pr view`/
grep-tag combination closes it. Persist via existing `Ledger.set_story_status`,
`stage_start`/`stage_finish` (to synthesize the `merge` DONE row that `rollback._story_merged` and
`compute_resume_plan` key off), and `Ledger.event_log`. No new schema column. Wire the call in
`run_build` after the cohort loop and **before** the close-out tally.

**Definition of Done**:
- [ ] Combines `--is-ancestor` + `git cherry` patch-id + `gh pr view` state + `(#<id>)` tag-in-`origin/main`; any one ⇒ landed
- [ ] `git fetch` first; offline/no-remote degrades to no-op; never raises, never fails the run
- [ ] Reclassifies parked stories to `DONE`, synthesizes/updates the `merge` DONE row, logs an audit event with winning signal + SHA
- [ ] Run terminal recomputed from reconciled statuses at close-out
- [ ] Idempotent on re-run; already-`DONE`/`SKIPPED` stories untouched
- [ ] Tests: squash landing, fast-forward landing, transitive/stacked landing, PR-merged-but-branch-deleted, genuinely-unlanded (stays parked), offline no-op, idempotent re-run
- [ ] Docs updated (`docs/controller-architecture.md` close-out / reconciliation section)

**Dependencies**: None
**Sequencing**: shares `build.py` close-out with 12.3-004 / 12.4-001 — serialize (do not build concurrently)
**Risk Level**: High

##### Story 12.3-002: Add the sdlc reconcile recovery verb
**User Story**: As FX whose overnight run aborted (e.g. a 429) before its already-open PRs were
merged by hand the next morning, I want a `sdlc reconcile` command that re-checks the run against
`origin/main` and corrects the ledger, so that a run which truly shipped no longer shows FAILED
days later.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a finished or interrupted run **When** I invoke `sdlc reconcile [run] [--db PATH]`
  **Then** it runs the same `reconcile_run` algorithm as close-out (12.3-001), reclassifies any
  stories whose work landed, recomputes and re-stamps the run terminal, and prints a human summary
  (e.g. "reclassified 3 story(ies) to DONE; run ced08c0f FAILED → DONE").
- **Given** no run id is passed **When** `sdlc reconcile` runs **Then** it targets the most recent
  run (mirrors `rollback`'s default), and `ensure_migrated()` runs first so a stale ledger does not
  crash with "no such column".
- **Given** a run where nothing changed **When** I reconcile **Then** it reports "nothing to
  reconcile" and exits 0 (idempotent, safe to re-run).
- **Given** no ledger / no such run **When** I reconcile **Then** it reports cleanly (non-zero only
  on a genuinely unknown explicit run id), never creating a spurious empty DB.

**Technical Notes**: Add a `reconcile` Typer command in `controller/src/sdlc/cli.py` modeled on
`rollback`: construct `Ledger(db or default_db_path())`, `ledger.ensure_migrated()`, then call the
shared `reconcile_run` from 12.3-001 and print `ReconcileResult`. Per this epic's "no new CLI
verbs" non-goal: this is a **recovery verb** in the same spirit as the existing `resume`/`rollback`
recovery surface (the manual counterpart to the automatic close-out reconciliation), not new
orchestration — call that out so it does not read as scope creep.

**Definition of Done**:
- [ ] `sdlc reconcile [run] [--db]` calls the shared `reconcile_run`, defaults to latest run, migrates first
- [ ] Human summary of reclassifications + run-status transition; idempotent "nothing to reconcile"
- [ ] No-DB / unknown-run handled cleanly; no spurious ledger created
- [ ] Tests: 429-aborted-then-merged fixture reconciles FAILED → DONE; idempotent re-run; default-to-latest
- [ ] Docs: recovery-verbs reference notes `reconcile` alongside `resume`/`rollback`

**Dependencies**: 12.3-001 (reuses `reconcile_run`)
**Risk Level**: Medium

##### Story 12.3-003: Add the AWAITING_APPROVAL merge state
**User Story**: As FX, I want a merge blocked only by the high-risk human-approval gate to be
parked in a distinct `AWAITING_APPROVAL` state that does not burn the bugfix loop and does not
mark the run FAILED, so that a run waiting on my approval is reported honestly as awaiting-human
rather than as a failure.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the merge agent returns `BLOCKED_HIGH_RISK` (PR carries `risk:high` from
  `risk_gate.py`/`.github/workflows/risk-gate.yml`, no `risk-approved` label or `risk-approver`
  review) **When** the controller classifies the merge outcome **Then** it recognizes "blocked
  awaiting human approval" as a distinct outcome — **not** a generic stage failure — and does
  **not** enter the bugfix loop (which cannot self-approve and would only exhaust into FAILED).
- **Given** a story whose merge is high-risk-blocked **When** `_run_story` returns **Then** it
  returns `AWAITING_APPROVAL`, the story row is set `AWAITING_APPROVAL`, and the committed work /
  open PR is preserved (R10).
- **Given** a run whose only non-DONE stories are `AWAITING_APPROVAL` **When** close-out computes
  the run terminal **Then** the run is `AWAITING_APPROVAL` (a non-FAILED, non-DONE bucket), never
  `FAILED`; dependency-blocking follows the existing `NEEDS_ATTENTION` rule.
- **Given** the human later approves and the PR merges **When** `sdlc reconcile` (12.3-002) or a
  subsequent close-out runs **Then** the `AWAITING_APPROVAL` story reconciles to `DONE` and the run
  terminal is recomputed.
- **Given** epic-14 14.1-003's `PAUSED`/`RATE_LIMITED` **When** these states coexist **Then**
  `AWAITING_APPROVAL` is orthogonal (waiting for a *person* vs. waiting for *time*); neither is
  FAILED, and reconciliation never reclassifies a `PAUSED` run as FAILED.

**Technical Notes**: The merge schema enum is only `MERGED|FAILED|SKIPPED`
(`merge-agent-response.schema.json`) and the agent maps `BLOCKED_HIGH_RISK → FAILED` today, so the
signal is lost before the controller sees it. Two coordinated changes: (1) surface the block —
prefer an **additive** `block_reason` field / text-line detection in `_dispatch_stage` /
`_stage_failure_summary` returning a new `kind="awaiting_approval"`, over re-enumerating the schema
(per the epic's "don't redefine the result contract" non-goal); (2) in `_run_story`, short-circuit
that kind to `return "AWAITING_APPROVAL"` **before** the `MAX_BUGFIX_ATTEMPTS` path, bypassing
`_run_bugfix`. Add `AWAITING_APPROVAL` to `_TERMINAL_RUN_STATES` and the finalize logic; ensure
status-snapshot counts, `list_runs`, and the dashboard tolerate the new string.

**Definition of Done**:
- [ ] `BLOCKED_HIGH_RISK` recognized as a distinct outcome, not a generic stage failure
- [ ] No bugfix-loop entry for a high-risk block; story returns `AWAITING_APPROVAL`; work/PR preserved (R10)
- [ ] Run terminal supports `AWAITING_APPROVAL` (non-FAILED); added to `_TERMINAL_RUN_STATES` and finalize logic
- [ ] Composes with epic-14 `PAUSED`/`RATE_LIMITED` (no FAILED reclassification of paused runs); reconcile flips approved-and-merged back to DONE
- [ ] Status/dashboard render the new state
- [ ] Tests: high-risk block → AWAITING_APPROVAL (no bugfix dispatch); run terminal not FAILED; reconcile-after-approval → DONE
- [ ] Docs: failure-handling + states reference

**Dependencies**: 12.3-001 (reconcile flips it to DONE after approval). Coordinates run-state
vocabulary with epic-14 14.1-003 — reference, do not duplicate.
**Risk Level**: High

##### Story 12.3-004: Share one run-finalization helper
**User Story**: As a maintainer, I want the run-terminal computation and close-out to live in one
place so that the reconciliation step and the new `AWAITING_APPROVAL` state cannot drift between
the `build` and `resume` code paths.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the close-out logic duplicated in `build.py` and `resume.py` **When** this story lands
  **Then** both call a single `finalize_run(ledger, run_id, status_map, ...)` helper that computes
  the terminal (including `AWAITING_APPROVAL`), recomputes counts, logs the finish event, and
  stamps `run_update_status` — so the two paths can never diverge again.
- **Given** the shared helper **When** it runs **Then** it invokes reconciliation (12.3-001) at one
  defined point so both `build` and `resume` reconcile identically, and the existing
  `BuildResult`/`ResumeResult` shapes are unchanged.
- **Given** existing behavior **When** no stories landed unexpectedly **Then** terminal outcomes for
  already-passing runs are unchanged (pure refactor for the DONE/FAILED cases).

**Technical Notes**: Factor the identical close-out blocks (`build.py` terminal computation +
counts/event/status/registry; the mirror in `resume.py`) into one helper, likely in `build.py`
beside `_exhausted_status` or in the new `reconcile.py`. Keep `_registry_finish` callable from the
build path only (resume has no registry arg today) — parameterize it. This is the backward-compat
safety story: it removes the duplicated-finalization hazard.

**Definition of Done**:
- [ ] One `finalize_run` helper used by both `run_build` and `run_resume`
- [ ] Reconcile + `AWAITING_APPROVAL` handled once, no divergence
- [ ] Existing `BuildResult`/`ResumeResult` fields and DONE/FAILED outcomes unchanged for passing runs
- [ ] Tests assert build and resume produce identical terminals for the same final story map
- [ ] Docs note the single finalize point

**Dependencies**: 12.3-001, 12.3-003 (the new semantics it consolidates). Serialize with 12.3-001 (both touch `build.py` close-out).
**Risk Level**: Medium

### Feature 12.4: Branch Isolation

Fixing reconciliation makes the ledger *eventually* honest; fixing branch-stacking makes it
*immediately* honest, so a genuinely-incomplete story FAILS instead of silently riding a later
story's merge onto `main`.

#### Stories

##### Story 12.4-001: Cut story branches from origin/main
**User Story**: As FX, I want each story's `feature/<id>` branch to be cut from a fresh
`origin/main` and the working directory returned to `main` between stories, so that a parked/failed
story's commits never stack under the next story and transitively ship to `main` unnoticed —
making each story's outcome isolated and honest.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the build prompt instructs branch creation **When** it renders **Then** it cuts from
  the remote base — `git fetch origin && git checkout -b feature/<id> origin/main` — instead of the
  current base-less `git checkout -b feature/{id}`, so a branch never stacks on whatever HEAD
  happened to be.
- **Given** a story finishes (DONE, FAILED, NEEDS_ATTENTION, AWAITING_APPROVAL) and the agent left
  the working dir on a feature branch (the merge agent only returns to `main` on its success path)
  **When** the controller moves to the next story/cohort **Then** the controller repositions HEAD to
  `main`/`origin/main` (the run and resume loops do this today — they currently never touch HEAD).
- **Given** a story that genuinely did not complete **When** the run finishes **Then** its work is
  **not** present on `origin/main` (no transitive landing), so reconciliation (12.3-001) correctly
  leaves it parked and the run honestly reflects the incomplete story — explicitly accept this
  tension: branch-from-main means real failures now FAIL rather than silently ship, and
  reconciliation is what still rescues work that *truly* landed.
- **Given** committed-but-unmerged work on a parked feature branch **When** HEAD is repositioned
  **Then** that branch and its commits are preserved (R10) — repositioning never deletes a feature
  branch or its commits.
- **Given** the shared-working-dir model (dispatch has no `cwd`/worktree today) **When** this ships
  **Then** it works without worktrees; full per-story worktree isolation is deferred to epic-17
  17.2-001/17.2-002 and referenced, not duplicated here.

**Technical Notes**: Edit `render_build_prompt` in `build.py` to `git fetch origin` +
`git checkout -b feature/{story.id} origin/main`. Add a controller-side HEAD reposition (a
`_git(root, "checkout", base)` using `_base_ref`/`_git`) between stories in the `run_build` cohort
loop and the `run_resume` loop, best-effort and non-fatal. Root cause recap: agents share one
working dir (no `cwd` in `dispatch.py`); the merge agent exits on the feature branch for
parked/blocked/conflict paths, so the next story's base-less `git checkout -b` stacks on the
leftover branch and a later successful merge transitively lands the earlier parked commits — which
the ledger never reconciled (the gap 12.3-001 also covers).

**Definition of Done**:
- [ ] Build prompt cuts `feature/<id>` from `origin/main` after a fetch
- [ ] Controller repositions HEAD to `main` between stories/cohorts in both `run_build` and `run_resume`
- [ ] Parked/failed feature branches and their commits preserved (R10); no branch deletion
- [ ] Regression test: a story that fails before merge does not appear on `origin/main`; a later successful story does not transitively land it
- [ ] References epic-17 17.2-001/002 for worktree isolation as the follow-on
- [ ] Docs: branch-model / source-control reference updated

**Dependencies**: None to ship; pairs with 12.3-001 (reconcile preserves truly-landed work once
stacking is fixed). Shares `build.py` with 12.3-001/12.3-004 — serialize. Forward-references
epic-17 17.2-001/002.
**Risk Level**: High

### Feature 12.5: Discovery & Scheduling Robustness

Protect the controller's *input* path — parsing epic markdown into the build
queue — so a benignly-worded story file cannot crash scheduling.

#### Stories

##### Story 12.5-001: Parse only intended dependency edges
**User Story**: As FX authoring epic stories, I want discovery to read only the intended
dependency edges from a story's `**Dependencies**:` line, so that story IDs mentioned in
explanatory prose are not parsed as edges and cannot create a phantom dependency cycle that
crashes cohort scheduling and aborts the build.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a `**Dependencies**:` line that lists real edge IDs followed by parenthetical or
  sentence prose mentioning other story IDs (e.g. `**Dependencies**: 12.3-001 (reconcile flips
  it once 12.3-004 lands)`) **When** discovery parses it **Then** only the leading edge id(s)
  are extracted (`12.3-001`), and IDs inside the prose (`12.3-004`) are ignored.
- **Given** a line beginning with `None` followed by prose containing IDs (e.g.
  `None (shares build.py with 12.3-004 and 12.4-001)`) **When** parsed **Then** the story has
  zero dependencies — no prose IDs become edges.
- **Given** the existing terse Dependencies lines across every current epic file **When** parsed
  **Then** each story's resolved edge set is unchanged (regression-locked by a fixture test that
  parses all `docs/stories/epic-*.md`).
- **Given** the verbose Dependencies lines that crashed `sdlc build epic-12` (the 12.3/12.4
  stories, used as real fixtures) **When** parsed and scheduled **Then** `compute_cohorts`
  resolves with no phantom cycle.
- **Given** a Dependencies line whose *intended* edges genuinely form a cycle or reference an
  out-of-queue story **When** `compute_cohorts` runs **Then** the controller still fails fast
  with the existing clear, story-named error — the parser fix must not mask real cycles.

**Technical Notes**: Root cause in `controller/src/sdlc/discovery.py`: `_DEPENDENCIES`
(`^\*\*Dependencies\*\*:\s*(.+?)\s*$`) captures the whole line and `_DEP_ID.findall(...)`
extracts every `X.Y-NNN` anywhere on it (regexes ~lines 16-18; used ~lines 108-111). Constrain
edge extraction to the intended list — e.g. only scan the segment before the first `(` or
sentence delimiter, treat a leading `none`/`n/a`/`tbd` as empty, and accept only a leading
comma/whitespace-separated run of bare IDs. Keep it permissive enough not to regress today's
terse lines. Add unit tests using the actual verbose 12.3-001 / 12.4-001 prose as fixtures, plus
a guard test that parses every `docs/stories/epic-*.md` and asserts no story resolves an edge to
an ID that only appears in prose. Document the convention in the story-authoring reference
("Dependencies line = leading ID list or `None`; put rationale on a separate `Sequencing` line").
This is the root-cause fix for the symptom hot-patched in PR #92 (which only terse-ified the one
offending 12.3-001 line). Keep/strengthen the `cohort.py` cycle error message (it already names
members) — but the parser is the root cause.

**Definition of Done**:
- [ ] Edge extraction limited to the intended leading ID list; prose IDs ignored; leading `None` → no deps
- [ ] Terse Dependencies lines across all existing epics parse to unchanged edge sets (regression test)
- [ ] The verbose 12.3/12.4 lines parse and schedule without a phantom cycle (fixture test)
- [ ] Genuine cycles / out-of-queue edges still fail fast with the story-named error
- [ ] Tests in the controller suite (90% gate); discovery parser branches covered
- [ ] Story-authoring convention documented (Dependencies line vs Sequencing note)

**Dependencies**: None
**Risk Level**: Low

## Story Dependencies (within Epic-12)

```
12.1-001 (envelope recovery) ─┐  share build.py/CLI — serialize if run together
12.1-002 (preflight guard)   ─┘
12.2-001 (renderer integrity)   independent
12.2-002 (commit-msg lint)      independent
12.2-003 (auto-migrate at launch) independent
12.2-004 (compliant commit subjects) extends 12.2-002 (shipped) — independent to build

12.3-001 (reconcile core) ──┬─ 12.3-002 (sdlc reconcile verb)   reuses reconcile_run
                            ├─ 12.3-003 (AWAITING_APPROVAL)      reconcile flips to DONE post-approval
                            └─ 12.3-004 (shared finalize)        consolidates 001+003 close-out
12.4-001 (branch-from-main) ── pairs with 12.3-001; independent to ship

12.5-001 (dependency-line parser) ── independent, no deps
```

- **Cohort 1** (no cross-deps): 12.1-001, 12.2-001, 12.2-002, 12.2-003 can run concurrently;
  12.1-002 shares files with 12.1-001, so serialize those two (or run 12.1-002 after 12.1-001).
  (12.2-003 also touches `build.py`/CLI entry points — serialize with 12.1-001/12.1-002 if run together.)
- **Feature 12.3/12.4**: 12.3-001 has no deps; 12.3-002/003/004 depend on it. 12.3-001, 12.3-004,
  and 12.4-001 all touch `build.py` close-out / run loop — **serialize them** (do not build
  concurrently). 12.3-003 coordinates run-state vocabulary with **epic-14 14.1-003**
  (`PAUSED`/`RATE_LIMITED`); 12.4-001 forward-references **epic-17 17.2-001/002** (worktree isolation).

## Epic Complete When

- A missing/malformed result envelope is recovered automatically in the common case; parking
  `NEEDS_ATTENTION` happens only after bounded recovery is exhausted, with every attempt
  logged.
- No controller run can hang itself: preflight/agent-added tests cannot recurse into
  `build`/`dashboard`, and runaway tests are timeout-bounded.
- The progress renderer preserves non-ledger history and re-renders idempotently.
- Agent-authored commits are commitlint-compliant before they reach a PR.
- A run whose work actually landed on `origin/main` reports DONE — automatically at close-out and
  via `sdlc reconcile` after the fact — instead of FAILED requiring manual ledger reconciliation.
- A merge blocked only by the high-risk human gate reports `AWAITING_APPROVAL`, not FAILED, and
  never burns the bugfix loop; it reconciles to DONE once approved and merged.
- Story branches are cut from `origin/main` and HEAD is repositioned between stories, so a
  parked/failed story never transitively lands on `main` — failures are isolated and honest.
- The build and resume paths share one run-finalization helper, so terminal-status logic cannot
  drift between them.
- A story's `**Dependencies**:` line cannot create a phantom cycle from prose-mentioned IDs —
  discovery parses only the intended edges, so a benignly-worded story file never crashes
  cohort scheduling.
- Source issue [#72](https://github.com/fxmartin/claude-code-config/issues/72) can be closed.
