# Epic 25: Reliable High-Risk-Block Recognition

> **Status: PLANNED (0/1)** — created 2026-06-30 from a live observation of the epic-23 resume run.
> Story 12.3-003 added the `AWAITING_APPROVAL` terminal state so a merge blocked *only* by the
> high-risk human-approval gate is reported honestly (parked, work preserved) instead of burning the
> bugfix loop and reading FAILED. That detection fired correctly on the first epic-23 run (23.3-001
> and 23.5-001 → `AWAITING_APPROVAL`) but **not** on the resume: 23.4-001 hit the same gate yet was
> classified as a generic merge-error, ran the bounded bugfix loop, and ended **FAILED**. This epic
> makes the recognition deterministic across the `build` and `resume` paths.

## Epic Overview

**Epic ID**: Epic-25
**Description**: The merge stage classifies its outcome from the merge agent's response and the PR's
check state. When a PR is blocked solely by the high-risk approval gate (`risk:high` present, no
`risk-approved` label / `risk-approver` review), Story 12.3-003 maps that to `AWAITING_APPROVAL` — a
non-FAILED parked state that skips the bugfix loop. Observed on the epic-23 resume (run `0541804d`):
23.4-001's merge was blocked by exactly that gate (PR #423 had every check green *except* "High-risk
file approval gate"), yet the controller recorded `merge=merge-error → bugfix=ENV_ISSUE → FAILED`,
while the same gate on the first run parked 23.3-001/23.5-001 as `AWAITING_APPROVAL`. The signal is
being lost on (at least) the resume path. This epic makes `BLOCKED_HIGH_RISK` recognition reliable:
a merge blocked only by the approval gate always parks as `AWAITING_APPROVAL`, never enters the
bugfix loop, and never reads FAILED — on a fresh build and on a resume alike.

**Business Value**: A human-gated story masquerading as FAILED is misleading and noisy — it burns
the bounded bugfix loop on something no agent can fix (self-approval is impossible), inflates the
failed count, and makes a clean "awaiting your approval" look like a defect that needs
investigation. Honest run-terminal status is the entire point of Feature 12.3.

**Success Metrics**:
- A merge blocked solely by the high-risk gate parks as `AWAITING_APPROVAL` on both `build` and
  `resume` — verified by a test that drives the gated-merge outcome through each entry point.
- Such a story never enters the bugfix loop (no wasted retries); the run terminal is
  `AWAITING_APPROVAL`, not FAILED, when it is the only non-DONE story.
- After the human approves and the PR merges, `sdlc reconcile` flips it to DONE (unchanged from
  12.3-003).

## Epic Scope

**Total Stories**: 1 | **Total Points**: 5 | **MVP Stories**: 0 (roadmap — Should Have)

## Out of Scope (Non-Goals)

- **Changing the gate itself.** The risk-gate workflow and the `risk-approved`/`risk-approver`
  approval paths are unchanged; only the controller's *recognition* of a gate-blocked merge changes.
- **Auto-approving high-risk changes.** The controller still never bypasses the gate;
  `AWAITING_APPROVAL` is a parked state awaiting a human, not an auto-merge.
- **Other merge-failure causes.** A genuine merge conflict, a failing test, or a non-gate CI red
  stays a real merge failure routed to the bugfix loop; only the gate-*only* block is reclassified.

## Features in This Epic

### Feature 25.1: Deterministic Gate-Block Recognition

Recognize a merge blocked solely by the high-risk approval gate as `AWAITING_APPROVAL` on every code
path, not just the fresh-build happy path.

#### Stories

##### Story 25.1-001: A gate-blocked merge parks as AWAITING_APPROVAL, never FAILED
**User Story**: As FX, I want a merge blocked only by the high-risk approval gate to be parked as
`AWAITING_APPROVAL` on both a fresh build and a resume, so that a story waiting on my approval never
burns the bugfix loop or reads as FAILED.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a PR whose only red check is the high-risk approval gate (`risk:high`, no
  `risk-approved`/`risk-approver`) **When** the merge stage evaluates the outcome on a fresh `build`
  **Then** it classifies `BLOCKED_HIGH_RISK` and the story is `AWAITING_APPROVAL` (work/PR
  preserved), not `merge-error`.
- **Given** the identical situation **When** the story is reached on `resume` **Then** the same
  classification fires — `AWAITING_APPROVAL`, byte-for-byte the build path (the exact gap observed on
  epic-23 run `0541804d` for 23.4-001).
- **Given** a `BLOCKED_HIGH_RISK` outcome **When** the controller routes it **Then** it does **not**
  enter the bounded bugfix loop (which cannot self-approve and only exhausts into FAILED), and the
  run terminal is `AWAITING_APPROVAL` when that is the only non-DONE story.
- **Given** a merge blocked by something **other** than the gate (a real conflict, a failing test, a
  non-gate CI red) **When** evaluated **Then** it is still a real merge failure routed to the bugfix
  loop — no false-positive parking.
- **Given** the human later approves and the PR merges **When** `sdlc reconcile` runs **Then** the
  `AWAITING_APPROVAL` story reconciles to DONE (unchanged from 12.3-003).

**Technical Notes**: 12.3-003 surfaces the block via an additive `block_reason` / text detection in
the merge outcome (`_dispatch_stage` / `_stage_failure_summary`) returning `kind="awaiting_approval"`,
short-circuited in `_run_story` *before* the `MAX_BUGFIX_ATTEMPTS` path. The epic-23 resume shows the
detection is path- or timing-dependent: confirm whether the merge agent's response or the PR
check-state read differs on resume (e.g. the gate check still *pending* vs *failed* when evaluated,
or the resume re-dispatch not carrying the same merge-outcome shape), and make the gate-only-block
detection robust to it — prefer reading the PR's **check rollup** for `risk:high` + the gate job's
conclusion over parsing free text. Add coverage that drives both the `build` and `resume` entry
points and a negative (real-failure) case.

**Definition of Done**:
- [ ] A gate-only-blocked merge parks `AWAITING_APPROVAL` on both `build` and `resume`
- [ ] Such a story never enters the bugfix loop; the run terminal is `AWAITING_APPROVAL`, not FAILED
- [ ] A non-gate merge failure still routes to the bugfix loop (no false-positive parking)
- [ ] reconcile-after-approval → DONE preserved (12.3-003)
- [ ] Tests cover the build path, the resume path, and the negative case
- [ ] Documented in the failure-handling / run-states reference

**Dependencies**: 12.3-003
**Risk Level**: High
