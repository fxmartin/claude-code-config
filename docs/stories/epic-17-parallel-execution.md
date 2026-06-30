# Epic 17: True Parallel Story Execution

> **Status: COMPLETE (5/5)** — all stories merged on `main` (PRs #161-#165); validated end-to-end by the epic-13 parallel run (4 concurrent agents). Created 2026-06-20 from a live post-mortem of the epic-11 run: the
> controller computes dependency cohorts but the executor runs stories **strictly one at a time**.
> `mode=parallel` is a label written to the ledger, never acted on. This epic makes the executor
> actually run a cohort's stories concurrently, with the isolation and concurrency-safety that
> requires. Distinct from Epic-11 (which *displays* runs) — this epic produces the concurrency
> Epic-11's dashboard would finally have something true to show.

## Epic Overview

**Epic ID**: Epic-17
**Description**: `compute_cohorts` groups stories into dependency levels (a width-4 cohort means
four stories *could* run together), but the execution loop in `build.py` is
`for cohort in cohorts: for story in cohort: outcome = _run_story(...)` — a blocking call that
drives a story through all four stages (build → coverage → review → merge) before the loop
advances. There is **no** threading/asyncio/process pool in `build.py`, and the
`mode = "serial" if opts.sequential else "parallel"` value (`build.py:1174`) is only persisted to
the ledger and logged — nothing reads it back to fan out. So every run is sequential regardless of
`mode`, the dashboard only ever shows one active story, and `ps` shows a single `claude -p`.
Worse, the controller provides **no per-story isolation**: the build agent runs
`git checkout -b feature/{id}` in the *shared* repo root (`build.py:992`) and dispatch sets no
per-story cwd, so concurrent agents would collide in one working tree. This epic delivers genuine
parallelism: a bounded concurrent executor for each cohort, per-story git-worktree isolation,
concurrency-safe ledger writes, and a `mode`/`--concurrency` surface that the executor honors.

**Business Value**: FX runs long unattended batches and chose this framework partly for its
"parallel cohort" story. Today a width-4 cohort takes ~4× longer than it should — four
independent stories run back-to-back instead of together. Real concurrency cuts wall-clock for
multi-story cohorts toward the slowest single story, turning overnight batches from serial slogs
into genuine fan-out, and finally makes the "parallel" labelling true.

**Success Metrics**:
- A `mode=parallel` run executes up to **N stories of a cohort concurrently** (default N=5):
  verified by ≥2 simultaneous `claude -p` processes **and** ≥2 stories `ACTIVE` in the ledger at
  once.
- Wall-clock for a multi-story cohort approaches the **slowest single story**, not the sum — a
  measurable speedup versus the sequential baseline on a representative cohort.
- **No cross-story interference**: each concurrent story builds in its own worktree/branch, and
  concurrent ledger writes never raise "database is locked".
- `--sequential` (and `--concurrency=1`) **reproduce today's exact serial behavior** — no
  regression for users who want one-at-a-time.

## Epic Scope

**Total Stories**: 5 | **Total Points**: 19 | **MVP Stories**: 0 (roadmap — Should Have)

## Out of Scope (Non-Goals)

- **Continuous (no-barrier) scheduling.** We keep a barrier between cohorts (run cohort N
  concurrently, wait, then N+1). Starting a story the instant its individual deps finish is a
  deliberate future enhancement, not this epic.
- **Container-per-story isolation.** Worktree isolation is the mechanism here; containerized
  per-story execution is deferred to Epic-13's sandbox work.
- **Cross-run / multi-machine parallelism.** Epic-11's registry covers *displaying* multiple
  runs; this epic parallelizes stories *within a single run* on one host.
- **Changing the 4-stage pipeline or the `<<<RESULT_JSON>>>` contract.** Stages, schemas, and
  result parsing are unchanged; only the scheduling around `_run_story` changes.
- **Removing dependency ordering.** Cohorts still gate dependents; a story whose dependency
  failed is still marked `BLOCKED`.

## Features in This Epic

### Feature 17.1: Concurrent Cohort Executor

Replace the blocking inner loop with a bounded concurrent executor that honors `mode` and a
configurable cap — without losing dependency-blocking or failure isolation.

#### Stories

##### Story 17.1-001: Bounded concurrent execution of a cohort's stories
**User Story**: As FX running a `parallel` build, I want the controller to run a cohort's ready
stories concurrently up to a configurable cap so that independent stories finish together instead
of one after another.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `mode=parallel` (the default) **When** a cohort is executed **Then** its non-blocked
  stories are dispatched concurrently through a bounded worker pool (default **5**, overridable
  via `--concurrency=N`), each running the full `_run_story` build→coverage→review→merge sequence.
- **Given** a cohort with more ready stories than the cap **When** it runs **Then** at most N run
  at once; the rest start as workers free up; a barrier waits for the **whole cohort** before the
  next cohort begins (cohort-barrier scheduling).
- **Given** `--sequential` or `--concurrency=1` **When** the run executes **Then** behavior is
  byte-for-byte today's serial path (one story at a time), proven by test — no regression.
- **Given** a story whose dependency failed **When** the cohort runs **Then** it is still marked
  `BLOCKED` before dispatch (dependency-blocking preserved), and a story that raises mid-flight
  does not crash the pool — its outcome is recorded and the other workers continue (failure
  isolation).
- **Given** `resume` re-enters a partially-built run **When** it executes remaining cohorts
  **Then** it honors the same concurrency semantics as `build`.

**Technical Notes**: Replace the inner `for story in cohort` loop (`build.py:~1217`) with a
bounded `concurrent.futures.ThreadPoolExecutor` over `_run_story` — work is I/O-bound on agent
subprocesses, so threads suffice (the GIL is fine; `adversarial.py` already uses a thread pool).
Make the `mode` flag (`build.py:1174`) authoritative: the executor reads it (and `--concurrency`).
Keep the dependency-block check before submitting each story. Mirror the same executor in
`resume.py`. Depends on worktree isolation (17.2-001) and concurrency-safe writes (17.1-002) being
in place first, or it is unsafe.

**Definition of Done**:
- [ ] Cohort stories run via a bounded thread pool (default 5, `--concurrency=N`)
- [ ] Cohort barrier preserved; dependency-blocking and failure isolation intact
- [ ] `--sequential`/`--concurrency=1` reproduce the serial path (regression test)
- [ ] `resume` honors the same concurrency
- [ ] Tests with synthetic stories assert ≥2 concurrent and correct outcome aggregation
- [ ] Documented in `docs/controller-architecture.md`

**Dependencies**: 17.1-002, 17.2-001
**Risk Level**: High

##### Story 17.1-002: Concurrency-safe ledger writes
**User Story**: As the controller writing story/stage/event rows from several workers at once, I
want ledger writes to be concurrency-safe so that parallel stories never fail with "database is
locked".
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** multiple workers writing to the ledger concurrently **When** writes contend **Then**
  they succeed without raising "database is locked" — the write connection sets a `busy_timeout`
  (WAL is already enabled in the schema DDL) and/or writes are serialized through a single writer.
- **Given** concurrent stage transitions for different stories **When** persisted **Then** each
  story's rows are correct and isolated (no lost updates, no cross-story corruption).
- **Given** a single-threaded (`--sequential`) run **When** it writes **Then** behavior is
  unchanged.

**Technical Notes**: The read connection already sets `busy_timeout = 2000`; the write
`_connect` does not — add it there (and a sane retry) so contended writers wait rather than error.
WAL (`PRAGMA journal_mode = WAL`) is set in the DDL, supporting concurrent readers + one writer.
If a busy_timeout proves insufficient under the cap, serialize writes behind a process-level lock.
Touches the `Ledger` connection helpers in `build.py`.

**Definition of Done**:
- [ ] Write connection sets `busy_timeout` (and retry) so concurrent writers don't error
- [ ] Concurrent multi-story writes verified correct + isolated by test
- [ ] Serial path unchanged
- [ ] Documented

**Dependencies**: None
**Risk Level**: Medium

##### Story 17.1-003: Ready-queue dispatch — retire the cohort barrier
**User Story**: As FX running a `parallel` build, I want each story dispatched the moment its own
dependencies are satisfied — not when its whole wave finishes — so that workers never sit idle
behind an unrelated slow or human-gated story in the same cohort.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `mode=parallel` and a free worker **When** a story's dependencies are all done (or
  absent from the queue) **Then** it is dispatched immediately, regardless of which cohort/wave it
  sits in — there is no barrier between waves (continuous ready-queue / list scheduling).
- **Given** a wave whose stories have uneven durations (or one parked `AWAITING_APPROVAL`/rate-limit
  story) **When** an earlier-wave story a next-wave story depends on finishes first **Then** the
  next-wave story starts straight away rather than waiting for the slow sibling — measurably higher
  worker utilization than cohort-barrier scheduling.
- **Given** a story whose dependency is `FAILED`, `BLOCKED`, or parked (not done) **When** the
  scheduler evaluates readiness **Then** it is held `BLOCKED`/`TODO` and never dispatched on a
  partially-satisfied graph (dependency-blocking preserved exactly as today).
- **Given** the ready set has more entries than free workers **When** dispatching **Then** ties are
  broken by ascending story id so the schedule stays deterministic and reproducible.
- **Given** `--sequential` or `--concurrency=1` **When** the run executes **Then** it is
  byte-for-byte today's serial path (one story at a time, ascending id) — proven by test, no
  regression.
- **Given** `resume` re-enters a partially-built run **When** it continues **Then** it rebuilds the
  ready set from the ledger and honors the same dispatch semantics.

**Technical Notes**: Replace the cohort-barrier loop (`_dispatch_cohort` in `build.py`, which today
*"returns only once every story finishes"* before the next cohort begins) with a single continuous
scheduler: maintain a `ready` set (deps ⊆ done) plus an in-flight set, submit ready stories to the
bounded `ThreadPoolExecutor` (17.1-001) as workers free, and on each `as_completed` future recompute
readiness from the updated done/blocked sets. `compute_cohorts` stays as the *display* grouping (the
dashboard's wave view) but no longer gates execution. Care points: a parked story
(`AWAITING_APPROVAL`, rate-limit) is terminal-for-run for *its dependents'* blocking decision (they
become `BLOCKED`, not eligible) but must not stall *independent* branches; preserve failure
isolation and the concurrency-safe ledger writes (17.1-002); keep the rate-limit park/wake loop
working under the flatter scheduler.

**Definition of Done**:
- [ ] Cohort barrier retired; stories dispatch on per-story readiness across wave boundaries
- [ ] Worker utilization improved (test: a slow wave-1 story does not delay a ready wave-2 story)
- [ ] Dependency-blocking, failure isolation, and parked/rate-limit handling intact
- [ ] Deterministic tie-break by story id; `--sequential`/`--concurrency=1` reproduce the serial path
- [ ] `resume` rebuilds the ready set from the ledger and honors the same semantics
- [ ] `compute_cohorts` retained for the dashboard wave view; execution decoupled from it
- [ ] Documented in `docs/controller-architecture.md`

**Dependencies**: 17.1-001, 17.1-002
**Risk Level**: High

### Feature 17.2: Per-Story Worktree Isolation

Give each concurrent story its own checkout so agents cannot collide in a shared working tree.

#### Stories

##### Story 17.2-001: Controller-owned git worktree per story
**User Story**: As FX running stories concurrently, I want each story's agent to operate in its
own git worktree so that simultaneous `git checkout`/edits/commits never clobber each other.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a story is about to be dispatched **When** the controller prepares it **Then** it
  creates a dedicated git worktree (`git worktree add`) for that story and dispatches the agent
  with its working directory set to that worktree, recording the worktree path in the ledger.
- **Given** two stories run concurrently **When** each builds **Then** they operate in separate
  worktrees on separate `feature/{id}` branches sharing the repo's object store — no shared
  index, no cross-story file contention.
- **Given** a non-claude `SDLC_AGENT_CMD` or `--sequential` mode **When** dispatched **Then** the
  worktree path is still honored (or, in sequential mode, may reuse the root for back-compat) and
  the result contract is unchanged.

**Technical Notes**: Dispatch currently sets **no** per-story cwd (`dispatch.py`), and the build
agent runs `git checkout -b feature/{id}` in the shared root (`build.py:992`). The controller must
own worktree lifecycle: create under a temp/agents area (compatible with the existing
`agent-*` orphan-sweeper and `forge-worktree-bootstrap` conventions), pass cwd into `dispatch`,
and adjust the agent prompt so it builds inside the worktree. Keep the `feature/{id}` branch model.

**Definition of Done**:
- [ ] Controller creates a per-story worktree and dispatches the agent with cwd in it
- [ ] Worktree path recorded in the ledger; concurrent stories isolated (test)
- [ ] `dispatch` accepts/propagates a per-story working directory
- [ ] Result contract unchanged; sequential back-compat preserved
- [ ] Documented in `docs/controller-architecture.md`

**Dependencies**: None
**Risk Level**: High

##### Story 17.2-002: Safe worktree integration and teardown under concurrency
**User Story**: As FX, I want each story's worktree integrated and cleaned up safely after it
finishes so that concurrent runs don't leak worktrees or leave half-merged state.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a story completes (DONE/FAILED/NEEDS_ATTENTION) **When** the controller closes it out
  **Then** its branch/PR is preserved and its worktree is removed (`git worktree remove`), reusing
  the existing merged-worktree cleanup semantics — committed work is never discarded (R10 holds).
- **Given** a crash or abort mid-cohort **When** the run is later resumed or swept **Then**
  orphaned per-story worktrees are detected and cleaned (the `agent-*` orphan-sweeper handles
  them) without removing a worktree that an in-flight story still needs.
- **Given** `resume` re-enters a run **When** a story must continue **Then** its worktree is
  re-attached or recreated deterministically (no duplicate `git worktree add` failure).

**Technical Notes**: Reuse `cmux-stop.sh` merged-worktree removal and
`sweep-orphan-worktrees.sh` (the 6-hour `agent-*` sweep that already checks `git worktree list`).
Ensure teardown is keyed per story and never races a concurrent worker's worktree.

**Definition of Done**:
- [ ] Per-story worktree removed on close-out; branch/PR + committed work preserved
- [ ] Orphan worktrees from crash/abort swept safely (no removal of in-flight worktrees)
- [ ] `resume` re-attaches/recreates worktrees deterministically
- [ ] Tests for close-out, orphan sweep, and resume re-attach
- [ ] Documented

**Dependencies**: 17.2-001
**Risk Level**: Medium

### Feature 17.3: Truthful Mode & Concurrency Observability

Make the run's state reflect real concurrency so tooling stops lying about it.

#### Stories

##### Story 17.3-001: Make `mode` authoritative and surface concurrent activity
**User Story**: As FX watching a parallel run, I want `status`/the ledger to show multiple stories
active at once so that the tooling reflects reality instead of showing a single story while
several run.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a `parallel` run with several stories executing **When** `sdlc status` (and the
  ledger) are queried **Then** they show **all** currently-active stories and the run's effective
  concurrency (e.g. "3 of 5 workers busy"), not just one.
- **Given** the `mode` value **When** a run executes **Then** it is authoritative — the displayed
  mode matches actual behavior (no "parallel" label on a serial execution).
- **Given** the Epic-11 dashboard **When** it reads this run **Then** it has accurate multi-active
  data to render (this epic produces the truth; Epic-11 renders it — no double-implementation).

**Technical Notes**: Small surface change once 17.1-001 lands: `status.py` already reads active
stages; ensure it returns all `ACTIVE` rows and an effective-concurrency figure. This resolves the
original confusion ("the dashboard only shows one"). Coordinate with Epic-11 11.2-004 (live
detail) so rendering stays in Epic-11.

**Definition of Done**:
- [x] `status`/ledger expose all active stories + effective concurrency
- [x] Displayed `mode` matches real execution
- [x] No rendering logic duplicated from Epic-11 (data only)
- [x] Tests for the multi-active snapshot
- [x] Documented

**Dependencies**: 17.1-001
**Risk Level**: Medium

## Story Dependencies (within Epic-17)

```
17.1-002 (ledger-safe writes) ─┐
17.2-001 (worktree per story) ─┼─> 17.1-001 (concurrent executor) ─> 17.3-001 (truthful status)
                               └─> 17.2-002 (worktree teardown)
```

- **Cohort 1** (no deps): 17.1-002, 17.2-001
- **Cohort 2**: 17.1-001 (needs 17.1-002 + 17.2-001); 17.2-002 (needs 17.2-001)
- **Cohort 3**: 17.3-001 (needs 17.1-001)

> Cross-epic: Epic-13's kill-switch/heartbeat (13.4-001) must terminate **all** concurrent agents,
> not one — call out when building it. Epic-11's live detail (11.2-004) renders the concurrency
> this epic produces. Epic-12's per-story recovery is unaffected (operates within `_run_story`).

## Epic Complete When

- A `parallel` run runs up to `--concurrency` stories of a cohort at once (default 5), with ≥2
  agents and ≥2 `ACTIVE` ledger rows observed simultaneously.
- Multi-story cohort wall-clock approaches the slowest single story, beating the sequential
  baseline measurably.
- Each concurrent story is isolated in its own worktree/branch; concurrent ledger writes never
  error; worktrees are cleaned up safely (including after crash/resume).
- `--sequential`/`--concurrency=1` reproduce today's serial behavior exactly.
- `status` and the ledger reflect real concurrency, and `mode` is authoritative.
