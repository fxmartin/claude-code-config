# Epic 26: Agent Process Discipline (Superpowers-Inspired)

> **Status: COMPLETE (4/4)** — all 4 stories implemented, tested, and merged (26.1-001 → 26.3-001,
> released v2.12.0–v2.15.0). Created 2026-07-11 from a comparative analysis of
> [obra/superpowers](https://github.com/obra/superpowers) (MIT, Jesse Vincent). Superpowers keeps
> *orchestration* in markdown skills an LLM follows — the model Epic-07 deliberately replaced with
> the deterministic controller — so its execution machinery is explicitly **not** imported. What it
> has that we lack is *agent discipline*: battle-tested prompt patterns that stop a dispatched agent
> from rationalizing its way around process (root-cause-first debugging, review-reception rigor,
> distrust of self-reported success), plus a TDD methodology for proving a skill actually changes
> agent behavior. This epic imports those four patterns as prompt/schema/test hardening. The
> analysis also *cleared* one candidate: superpowers guards its worktree detection against
> submodule confusion because it uses the `git-dir != git-common-dir` heuristic — our worktree
> machinery uses `git worktree list --porcelain` throughout and does not share the flaw.

## Epic Overview

**Epic ID**: Epic-26
**Description**: The controller owns the state machine deterministically (Epic-07), validates every
agent response against a JSON-schema contract, and retries failures through a bounded bugfix loop
with model escalation (14.2-003). But *within* a dispatched agent's turn, process quality rests
entirely on the prompt — and the current prompts state rules without defending them against the
rationalizations agents actually produce under pressure. Three concrete gaps, each with a proven
pattern in superpowers: (1) bugfix agents receive failure output and freedom — nothing forces a
root-cause investigation before a fix, so a symptom-patch that survives CI burns an escalation
cycle and ships a masked bug; (2) review findings routed to the bugfix loop are implemented
blindly — a wrong finding gets faithfully "fixed" with no verification step and no channel to
dispute it; (3) reviewers receive the implementer's self-report as context with no instruction to
treat it as unverified claims. A fourth, meta-level gap: the repo bats-tests its shell scripts
thoroughly but has no behavioral test that any *skill* changes agent behavior — skills ship
untested against the failure modes they exist to prevent.

**Business Value**: The bugfix loop is bounded (and each retry escalates to a costlier model), so
every wasted cycle has a direct cost and pushes a story toward FAILED/NEEDS_ATTENTION — which then
costs FX attention, the scarcest resource in a one-operator autonomous pipeline. Root-cause
discipline and finding-verification each convert a class of wasted cycles into productive ones.
Behavioral skill tests protect the whole discipline layer from silently regressing as prompts are
edited — the same "evidence before claims" principle the pipeline already applies to code, applied
to its own process documentation.

**Success Metrics**:
- Every bugfix-agent response carries a schema-enforced root-cause statement; a response without
  one fails contract validation and routes as malformed (never silently propagates).
- A deliberately wrong review finding injected into the bugfix loop is disputed with technical
  reasoning rather than implemented — verified by test.
- Adversarial/review prompts instruct the reviewer to verify implementer claims against the diff;
  the instruction is asserted by a bats test so it cannot be edited away unnoticed.
- At least two high-value skills have RED/GREEN behavioral test cases (baseline misbehavior
  documented without the skill, compliance demonstrated with it) runnable on demand.

## Epic Scope

**Total Stories**: 4 | **Total Points**: 13 | **MVP Stories**: 0 (roadmap)

## Out of Scope (Non-Goals)

- **Superpowers' orchestration layer.** `executing-plans` / `subagent-driven-development` are an
  LLM-followed markdown state machine with a markdown progress ledger — the architecture Epic-07
  replaced with the deterministic controller, SQLite ledger, and schema contracts. Not imported.
- **`verification-before-completion`.** Already covered structurally: the codex stop-time review
  gate and the `/verify` skill enforce evidence-before-claims at session boundaries.
- **Multi-harness plugin packaging** (`.codex-plugin`, `.cursor-plugin`, …). We already run Codex
  as a *worker harness* (Epic-20/21), which is the axis that matters here.
- **Brainstorming-flow changes.** Superpowers' per-feature design gate solves a different problem
  than our PM-interview → REQUIREMENTS.md → epics pipeline; the story pipeline already enforces
  planning structurally.
- **Worktree submodule guard.** Investigated and cleared — see the status note above.
- **Per-stage model routing changes.** Superpowers' "least powerful model per role" validates the
  existing cheap-first escalation (14.2-003) and Epic-20's per-stage routing; no new work needed.

## Features in This Epic

### Feature 26.1: Root-Cause-First Bugfix Discipline

The bugfix loop's value depends on fixes being *fixes*, not symptom patches. Import superpowers'
`systematic-debugging` core — "no fixes without root-cause investigation first" — into both bugfix
agent prompts, and make the root cause a schema-enforced field of the bugfix contract so the
discipline is validated by the controller, not merely requested by the prompt.

#### Stories

##### Story 26.1-001: Bugfix agents must state a root cause before a fix, enforced by contract
**User Story**: As FX, I want every bugfix agent forced to identify and report the root cause
before proposing a fix, so that a bounded, cost-escalating retry loop is never spent on a
symptom-patch that masks the real defect.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the bugfix agent prompts (`plugins/autonomous-sdlc/skills/build-stories/bugfix-agent-prompt.md`
  and `plugins/autonomous-sdlc/skills/fix-issue/bugfix-agent-prompt.md`) **When** a bugfix agent is
  dispatched **Then** the prompt requires a root-cause investigation phase before any fix is
  attempted, including a rationalization table naming the evasions to refuse (e.g. "the fix is
  obvious", "just add a guard and see if CI passes", "no time to investigate — retry budget is
  low").
- **Given** `controller/src/sdlc/schemas/bugfix-agent-response.schema.json` **When** a bugfix
  response is validated **Then** a `root_cause` field (what broke and why — not a restatement of
  the symptom) is required, and a response missing it fails contract validation and routes as
  malformed, exactly like any other schema violation today.
- **Given** the contract change **When** docs are regenerated/updated **Then** `docs/contracts.md`
  documents the new field, and existing controller tests for the bugfix contract are extended to
  cover both the present-and-valid and missing-field cases.
- **Given** the single-source skill generator (Epic-20) **When** the prompt changes **Then** the
  Codex-side bugfix prompt receives the same discipline (regenerated, not hand-copied).

**Technical Notes**: Schema lives inside the installed wheel (`importlib.resources`, Epic-21) — a
schema change is a controller change, shipped by `scripts/deploy.sh`. Keep the required field
minimal (a single string) so the contract does not over-constrain agent output format;
`sdlc validate bugfix <file>` gives instant local verification. Pattern source:
superpowers `skills/systematic-debugging` (four-phase protocol, "iron law", rationalization table).

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: None
**Risk Level**: Low

### Feature 26.2: Review-Loop Rigor — Verify Findings Both Ways

Two symmetric hardenings of the review⇄bugfix loop. Downstream: an agent *receiving* findings must
verify each against the codebase before implementing, with a schema channel to dispute findings it
can refute — review findings are claims, not orders. Upstream: an agent *producing* a review must
treat the implementer's self-report as unverified claims and judge the diff, not the narrative.

#### Stories

##### Story 26.2-001: Bugfix agents verify review findings and can dispute them
**User Story**: As FX, I want a bugfix agent that receives review findings to verify each one
against the actual code before implementing, and to dispute findings it can technically refute, so
that a wrong finding is never blindly "fixed" into the codebase.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a bugfix agent dispatched with review findings **When** it processes them **Then** the
  prompt requires per-finding verification against the codebase before implementation, and forbids
  performative agreement (implement-without-checking), following superpowers'
  `receiving-code-review` reception pattern (read → restate → verify → evaluate → respond →
  implement).
- **Given** the bugfix response contract **When** the agent reports **Then** each finding carries a
  disposition — implemented, or disputed with concrete technical reasoning — so a dispute is
  structured data the controller can see, not prose lost in a log.
- **Given** one or more disputed findings **When** the controller processes the response **Then**
  the dispute is surfaced (ledger event + visible in `sdlc status`/dashboard recent-events) and
  never silently swallowed; the story does not falsely report the finding as fixed.
- **Given** a test that injects a deliberately wrong finding (e.g. flags correct code as buggy)
  **When** the loop runs against it **Then** the finding is disputed with reasoning rather than
  implemented — the acceptance test for the whole story.

**Technical Notes**: Live precedent from the deploy-script session of 2026-07-10: the stop-gate
review was right twice, and the correct response each time was *evaluation* (reproduce, then fix),
not compliance — the same discipline this story installs in the pipeline's unattended path. Keep
routing conservative: a dispute is information for FX and the ledger, not a new state-machine
branch; whether a disputed finding blocks the story stays with existing review-stage semantics.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 26.1-001 (both stories touch the bugfix contract; land the schema change once)
**Risk Level**: Medium

##### Story 26.2-002: Reviewers treat the implementer's report as unverified claims
**User Story**: As FX, I want review/adversarial prompts to explicitly instruct the reviewer to
distrust the implementer's self-report and verify its claims against the diff, so that an
optimistic or inaccurate report cannot launder a defect through review.
**Priority**: Should Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** the adversarial review path (`scripts/codex-adversarial-review.sh` +
  `controller/src/sdlc/config/adversarial-reviewers.yaml`) and any pipeline prompt that hands an
  implementer's summary to a reviewer **When** a review is dispatched **Then** the prompt contains
  an explicit "do not trust the report" instruction: implementer claims — including design
  rationales like "kept it simple per YAGNI" — are unverified until checked against the diff.
- **Given** the reviewer's scope **When** it needs context beyond the diff **Then** the prompt
  bounds exploration the way superpowers' task-reviewer does: inspect outside the diff only for a
  concrete named risk, and name both the risk and what was checked in the report — keeping reviews
  focused and cheap without forbidding legitimate cross-cutting checks.
- **Given** the instruction is prompt text **When** prompts are later edited **Then** a bats test
  asserts its presence (same pattern as existing prompt-content tests), so the hardening cannot be
  silently dropped.

**Technical Notes**: Pattern source: superpowers
`skills/subagent-driven-development/task-reviewer-prompt.md` ("Do Not Trust the Report"; named-risk
exploration budget). Survey where implementer self-reports actually flow into review context before
editing — the instruction belongs wherever a report crosses into a reviewer's prompt.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: None
**Risk Level**: Low

### Feature 26.3: Behavioral Skill Testing (Skill TDD)

Superpowers' `writing-skills` treats a skill as production code: run the scenario *without* the
skill and document the agent's failure (RED), add the skill, verify compliance (GREEN), then close
the loopholes the agent finds. Our repo bats-tests every shell script but has no test that any
skill changes agent behavior. Establish the methodology on the discipline prompts this epic
ships — which need exactly this kind of proof — and leave a reusable harness pattern behind.

#### Stories

##### Story 26.3-001: RED/GREEN pressure-tests for the discipline prompts
**User Story**: As FX, I want behavioral test cases proving that the Epic-26 discipline prompts
actually change agent behavior under pressure, so that the discipline layer is evidence-based at
birth and protected against silent regression as prompts evolve.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the root-cause discipline (26.1-001) **When** its pressure scenario runs without the
  discipline text (RED baseline) **Then** the agent's symptom-patch behavior is captured and
  committed as the baseline record; **When** run with the discipline **Then** the agent
  investigates before fixing (GREEN) — both runs scripted and repeatable, not anecdotal.
- **Given** the finding-verification discipline (26.2-001) **When** its scenario presents a
  deliberately wrong review finding **Then** RED shows blind implementation and GREEN shows a
  reasoned dispute.
- **Given** the harness **When** a test case is added for any other skill **Then** the structure is
  reusable (scenario + baseline expectation + compliance expectation) and documented well enough
  that the next case costs an hour, not a design session; evaluate `claude plugin eval`
  (`evals/**/case.yaml` + graders) as the runner before building anything custom.
- **Given** these tests invoke live agents (cost, nondeterminism) **When** CI is considered
  **Then** they are explicitly **not** wired into the PR gate — on-demand execution documented,
  with CI integration deferred to Epic-18's eval-harness work.

**Technical Notes**: Complementary to Epic-18, not overlapping: 18 scores *output quality* on real
tickets (LOC/tokens/cost/quality); this verifies *process compliance* of a skill under pressure.
If Epic-18's harness lands first, build these as eval cases inside it. Pattern source: superpowers
`skills/writing-skills` (TDD mapping table; "if you didn't watch an agent fail without the skill,
you don't know if the skill teaches the right thing").

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 26.1-001, 26.2-001 (tests target the prompts those stories ship)
**Risk Level**: Medium

## Dependencies Between Stories

```
26.1-001 ──▶ 26.2-001 ──▶ 26.3-001
                26.2-002 ──┘
```

26.1-001 and 26.2-002 are independent and can run in parallel; 26.2-001 lands after 26.1-001 to
touch the bugfix schema once; 26.3-001 closes the epic by pressure-testing what the others shipped.

## Attribution

Patterns adapted from [obra/superpowers](https://github.com/obra/superpowers) (MIT License,
Copyright (c) 2025 Jesse Vincent): `systematic-debugging`, `receiving-code-review`,
`subagent-driven-development/task-reviewer-prompt.md`, and `writing-skills`. Adapted as
prompt/schema/test hardening for the deterministic controller architecture, not ported verbatim.
