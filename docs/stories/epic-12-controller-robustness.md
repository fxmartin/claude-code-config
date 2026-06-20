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

## Epic Scope

**Total Stories**: 5 | **Total Points**: 16 | **MVP Stories**: 0 (roadmap — primary defect is Must Have)

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

## Story Dependencies (within Epic-12)

```
12.1-001 (envelope recovery) ─┐  share build.py/CLI — serialize if run together
12.1-002 (preflight guard)   ─┘
12.2-001 (renderer integrity)   independent
12.2-002 (commit-msg lint)      independent
12.2-003 (auto-migrate at launch) independent
```

- **Cohort 1** (no cross-deps): 12.1-001, 12.2-001, 12.2-002, 12.2-003 can run concurrently;
  12.1-002 shares files with 12.1-001, so serialize those two (or run 12.1-002 after 12.1-001).
  (12.2-003 also touches `build.py`/CLI entry points — serialize with 12.1-001/12.1-002 if run together.)

## Epic Complete When

- A missing/malformed result envelope is recovered automatically in the common case; parking
  `NEEDS_ATTENTION` happens only after bounded recovery is exhausted, with every attempt
  logged.
- No controller run can hang itself: preflight/agent-added tests cannot recurse into
  `build`/`dashboard`, and runaway tests are timeout-bounded.
- The progress renderer preserves non-ledger history and re-renders idempotently.
- Agent-authored commits are commitlint-compliant before they reach a PR.
- Source issue [#72](https://github.com/fxmartin/claude-code-config/issues/72) can be closed.
