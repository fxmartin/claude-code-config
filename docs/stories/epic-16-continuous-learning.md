# Epic 16: Continuous Learning / "Instincts"

> **Status: PLANNED (EXPERIMENTAL)** — created 2026-06-20. Inspired by the external
> [affaan-m/ECC](https://github.com/affaan-m/ECC) "instincts" system (`/evaluate-session`
> extracts patterns at session end, confidence-scores them, `/evolve` promotes high-confidence
> ones into skills). Scoped deliberately as a **spike + thin, human-gated MVP** — novel but with
> soft ROI and real complexity. Lowest priority; sequence after Epics 13–15.

## Epic Overview

**Epic ID**: Epic-16
**Description**: Every autonomous run produces signal the framework currently discards: which
failure categories recurred, which bugfixes worked, which stages stalled, which dependencies bit
repeatedly. ECC captures this as "instincts" — confidence-scored learned patterns promoted into
reusable skills. This epic explores bringing a *bounded, human-gated* version of that to our
controller: a run post-mortem that mines the ledger (`events`, stage outcomes, failure categories)
plus git history for recurring patterns and emits confidence-scored candidate "learnings", then a
promotion step that turns high-confidence learnings into a **reviewable** skill/doc draft —
never auto-installed.

**Business Value**: A framework that quietly learns from its own runs gets better without FX
hand-authoring every lesson. If even a few recurring failure modes become codified guidance
("this dependency conflict was fixed this way before"), future runs avoid repeating the same
dead-ends. Because it is experimental, the value is *validated by the spike* before any larger
commitment.

**Success Metrics**:
- The spike (16.1-001) demonstrably extracts ≥1 non-trivial, correct recurring pattern from real
  ledger history of a past run, with a confidence score — or concludes, with evidence, that the
  signal is too thin to be worth more investment.
- Promotion (16.1-002) produces a human-reviewable draft; **nothing is auto-installed** into the
  active skill set without explicit approval.
- No regression to build behavior: learning capture runs at run end and never blocks or alters a
  build.

## Epic Scope

**Total Stories**: 2 | **Total Points**: 8 | **MVP Stories**: 0 (roadmap — experimental, Could Have)

## Out of Scope (Non-Goals)

- **Auto-installing learned skills.** Promotion is always human-gated. A learning never becomes an
  active skill/agent without explicit approval.
- **Altering a running build.** Capture is a post-run step; it must not influence dispatch,
  gating, or scheduling within the run that produced it.
- **A learning marketplace / import-export of others' instincts.** ECC has this; out of scope here
  until the local loop proves valuable.
- **ML/embedding infrastructure.** The spike uses simple, inspectable heuristics over ledger + git
  history, not a trained model or vector store, unless the spike explicitly justifies more.

## Features in This Epic

### Feature 16.1: Learning Capture & Promotion

Mine completed runs for recurring patterns; promote the strong ones under human review.

#### Stories

##### Story 16.1-001: (Spike) Run post-mortem pattern extraction
**User Story**: As FX, I want a spike that mines a completed run's ledger and git history for
recurring patterns and emits confidence-scored candidate learnings, so that I can judge whether
continuous learning is worth building out.
**Priority**: Could Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a completed run's ledger (`events`, stage outcomes, `failure_category`) and the run's
  git history **When** the post-mortem runs **Then** it produces a list of candidate "learnings"
  (recurring failure modes, repeated fixes, chronic stalls), each with a short description, the
  evidence it was derived from, and a confidence score.
- **Given** the candidate list **When** reviewed **Then** entries are inspectable (the evidence is
  shown, not a black box) and stored in a learnings store (file-based, outside the repo working
  tree like the ledger).
- **Given** the spike concludes **When** results are assessed **Then** there is a written finding
  on whether the extracted signal justifies 16.1-002 and any further investment.
- **Given** the post-mortem **When** it runs **Then** it executes only after a run completes and
  never blocks or alters the build.

**Technical Notes**: Read-only over the ledger (`controller/src/sdlc/` ledger access) + `git log`
for the run's branches. Heuristic extraction (group by `failure_category`, count repeats, detect
fix patterns); confidence = frequency × consistency. Keep it simple and inspectable — this is a
spike to validate signal, not a production learner. Store under the agent-data home (e.g. a
`learnings/` dir alongside ledger state), never committed.

**Definition of Done**:
- [ ] Post-mortem extracts confidence-scored candidate learnings from a real past run's ledger+git
- [ ] Each candidate carries its evidence; stored in a file-based learnings store (not in-repo)
- [ ] Runs only post-completion; never blocks/alters a build
- [ ] Written go/no-go finding on whether to build 16.1-002
- [ ] Tests over a synthetic ledger fixture

**Dependencies**: None
**Risk Level**: High

##### Story 16.1-002: Human-gated promotion to a skill/doc draft
**User Story**: As FX, I want to promote a high-confidence learning into a reviewable skill or doc
draft so that proven lessons become reusable guidance — only after I approve them.
**Priority**: Could Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a high-confidence learning (from 16.1-001) **When** I promote it **Then** the system
  generates a **draft** skill/doc (e.g. a `SKILL.md` or a best-practices note) for review — it is
  never auto-installed into the active skill set.
- **Given** a draft **When** I approve it **Then** it follows the normal authoring path (the
  existing `create-skill` generator / a PR), so review and CI gate it like any other change.
- **Given** a learning below the confidence threshold **When** promotion is attempted **Then** it
  is refused with the reason (keeps low-signal noise out of the skill set).

**Technical Notes**: Reuse the existing `create-skill` generator for the draft shape. Promotion is
a deliberate operator action, not automatic. Gate on the confidence score from 16.1-001.

**Definition of Done**:
- [ ] Promotion produces a reviewable draft (never auto-installed)
- [ ] Approved drafts flow through the normal authoring/PR path
- [ ] Sub-threshold learnings refused with a reason
- [ ] Tests for promote + refuse paths

**Dependencies**: 16.1-001
**Risk Level**: Medium

## Story Dependencies (within Epic-16)

```
16.1-001 (spike: extraction) ──> 16.1-002 (human-gated promotion)
```

- **Cohort 1**: 16.1-001 (spike — gates the rest of the epic on its go/no-go finding)
- **Cohort 2**: 16.1-002 (needs 16.1-001 and a "go" decision)

## Epic Complete When

- The spike has produced a clear, evidence-backed go/no-go on continuous learning.
- If "go": high-confidence learnings can be promoted into human-reviewed skill/doc drafts that
  flow through the normal authoring/PR path, with nothing auto-installed.
- Learning capture runs only post-completion and never affects a running build.
