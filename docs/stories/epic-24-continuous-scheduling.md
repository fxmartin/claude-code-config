# Epic 24: Continuous Ready-Queue Scheduling

> **Status: PLANNED (0/1)** — created 2026-06-30 from a live observation of the epic-23 run.
> Epic-17 delivered concurrent cohort execution but deliberately kept a **barrier between
> cohorts**, listing the alternative as an explicit Non-Goal: *"starting a story the instant its
> individual deps finish is a deliberate future enhancement, not this epic."* This epic is that
> enhancement — replace the wave barrier with continuous ready-queue dispatch so a story starts the
> moment its own dependencies finish, not when its whole wave does.

## Epic Overview

**Epic ID**: Epic-24
**Description**: The controller computes dependency cohorts and runs each cohort concurrently
(Epic-17, Story 17.1-001), but `_dispatch_cohort` is a **barrier** — it returns only once every
story in the wave finishes before the next wave begins. So a story whose own dependency finished
early still waits for unrelated, slower (or human-gated) siblings in the same wave. Observed on the
epic-23 run: `23.2-001`'s only dependency (`23.1-001`) completed early, yet it sat idle until an
unrelated same-wave story (`23.6-001`) finished. This epic replaces the wave barrier with
continuous ready-queue (list) scheduling: maintain a ready set (deps ⊆ done), dispatch to any free
worker, and recompute readiness on each completion — no wave boundary — while keeping
`compute_cohorts` as the dashboard's wave grouping.

**Business Value**: Higher worker utilization on real DAGs, where waves have uneven story durations
or a parked/approval-gated story. Cuts wall-clock for multi-wave epics toward the critical path
rather than the sum of per-wave slowest stories — directly visible on long unattended batches like
epics 20/22/23.

**Success Metrics**:
- A story is dispatched within one scheduler tick of its last dependency completing, regardless of
  wave — verified by a test where a slow wave-1 story does not delay a ready wave-2 story.
- Worker utilization on a representative DAG is measurably higher than cohort-barrier scheduling
  (fewer idle worker-seconds), with the same correctness.
- No regression: `--sequential`/`--concurrency=1` reproduce today's serial path; dependency
  blocking, failure isolation, and parked/rate-limit handling are unchanged.

## Epic Scope

**Total Stories**: 1 | **Total Points**: 5 | **MVP Stories**: 0 (roadmap — Should Have)

## Out of Scope (Non-Goals)

- **Changing the 4-stage pipeline or the result contract.** Only the scheduling around
  `_run_story` changes; stages, schemas, and `<<<RESULT_JSON>>>` parsing are untouched.
- **Removing dependency ordering.** Dependents still wait for their deps; a story whose dependency
  failed or is parked is still `BLOCKED`.
- **Cross-run / multi-machine scheduling.** This stays within a single run on one host (as Epic-17).
- **Priority / weighted / critical-path scheduling.** Ready stories tie-break by ascending id only;
  cost-aware or critical-path-first ordering is a later concern.

## Features in This Epic

### Feature 24.1: Continuous Dispatch

Retire the cohort barrier and dispatch on per-story readiness, while preserving every safety
property Epic-17 established (dependency-blocking, failure isolation, concurrency-safe writes,
serial-path parity).

#### Stories

##### Story 24.1-001: Ready-queue dispatch — retire the cohort barrier
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
bounded `ThreadPoolExecutor` (17.1-001) as workers free, and on each `as_completed` future
recompute readiness from the updated done/blocked sets. `compute_cohorts` stays as the *display*
grouping (the dashboard's wave view) but no longer gates execution. Care points: a parked story
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
