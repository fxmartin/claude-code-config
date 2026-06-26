# Epic 19: Cross-Harness SDLC Portability — run the pipeline on any agent harness

> **Status: PLANNED** — created 2026-06-26. Triggered by FX's request to make the autonomous-sdlc
> framework run beyond Claude Code (Codex, opencode, pi, …) and to allow alternate-harness role
> assignment (e.g. Codex for review and QA while Claude builds). This epic generalizes the
> vendor-agnostic registry pattern that Epic-08 introduced for *adversarial review* into a
> first-class **harness abstraction** across the whole agent-dispatch path; it does NOT re-author
> the Codex mirror plugin by hand (that drift is exactly what this epic eliminates). The two
> in-process-subagent skills (`fix-issue`, `resume-build-agents`) stay Claude-only by design.

## Epic Overview

**Epic ID**: Epic-19
**Description**: The `autonomous-sdlc` framework is Claude-Code-native. The controller's single
dispatch seam (`controller/src/sdlc/dispatch.py`) defaults to `["claude","-p","--output-format",
"stream-json",…]` and, although `SDLC_AGENT_CMD` can override the command, the surrounding code
parses Claude-specific output shapes (stream-json frames, `api_error_status: 429`/`resetsAt`
rate-limits, `MAX_THINKING_TOKENS`, "prompt is too long" context overflow). Skill distribution to
Codex is a hand-maintained mirror plugin under `nix-install/plugins/autonomous-sdlc/` with a
separate manifest and version, so the two harnesses drift. This epic introduces (1) a config-driven
**harness registry** generalizing `controller/config/adversarial-reviewers.yaml`, (2) **pluggable
output parsing** per harness, (3) **per-role harness routing** so build/coverage/review/qa/merge can
each run on a different harness in one run, (4) **Codex as the first concrete non-Claude adapter**
for build+qa+review, (5) a **single-source skill-authoring** layer that generates Claude and Codex
skill files from one definition (extending the `shared-skills` SSOT + `sdlc sync-check` parity gate),
(6) **capability probing with graceful degradation**, and (7) an **"add a new harness" onboarding
guide**. `fix-issue`/`resume-build-agents` remain Claude-only and that boundary is documented.

**Business Value**: Removes single-vendor lock-in for FX and the five LTM colleagues: the pipeline
keeps running when Claude is rate-limited or unavailable, lets cheaper/faster harnesses take
mechanical roles (coverage, QA) while a stronger harness builds, and turns "support a new harness"
from a multi-day hand-port into a config + wrapper-script change. It also collapses the
Claude/Codex skill-mirror maintenance burden into a single source of truth.

**Success Metrics**:
- A full `sdlc build` run completes end-to-end with **zero `claude` invocations** when configured
  for an alternate harness (Codex), producing the same ledger/contract artifacts.
- A single run executes with **build on Claude and review+qa on Codex**, with the ledger recording
  which harness ran each story/stage.
- Adding a hypothetical new harness requires **no Python changes** — only a `harnesses.yaml` entry
  + a wrapper script + (optional) parser declaration; proven by the onboarding guide's worked example.
- **Zero hand-maintained skill mirrors**: Claude and Codex skill files are generated from one
  source and the parity gate fails CI on drift.
- Capability gaps degrade safely: a harness lacking worktree isolation runs serial (not crash),
  with an explicit log line; missing usage data is recorded as "unavailable", not zero.

## Epic Scope

**Total Stories**: 13 | **Total Points**: 52 | **MVP Stories**: 0 (roadmap — Should Have)

## Out of Scope (Non-Goals)

- **In-process subagent portability.** `fix-issue` and `resume-build-agents` spawn Claude
  *in-process* agents (`subagent_type`/`model`/`isolation="worktree"`) with no CLI-harness
  equivalent; they remain Claude-only. This epic documents the boundary (Story 19.6-002) rather
  than abstracting it.
- **Concrete opencode / pi / gemini adapters.** Only **Codex** is built here. The framework +
  generic template + onboarding guide make those a config exercise; shipping/validating each one
  is future work.
- **Release & versioning.** CHANGELOG/semver stays owned by Epic-05; the Codex mirror plugin's
  version bump cadence is unchanged except where the generator replaces hand edits.
- **Re-implementing parallelism or the ledger.** Epic-17 owns concurrency; Epic-19 only *gates*
  it by capability. The SQLite ledger schema is extended (which harness ran a stage), not redesigned.
- **The OpenAI `codex` delegation plugin** (`codex-companion.mjs`, `codex-rescue`) — that is a
  separate Claude→Codex delegation path and is not modified, though the Codex adapter may reuse
  `codex exec`.

## Features in This Epic

### Feature 19.1: Harness adapter registry — the core abstraction

Generalize the dispatch seam and the adversarial-reviewers registry into one config-driven harness
abstraction the rest of the epic builds on.

#### Stories

##### Story 19.1-001: Define the harness registry and adapter contract
**User Story**: As FX, I want a single config file that declares each available harness and how to
invoke it so that the controller can run agents on any of them without code changes.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a new `controller/config/harnesses.yaml` keyed by harness name (`claude`, `codex`, …)
  **When** the controller loads it **Then** each entry resolves a command template, invocation
  flags, capability flags, and an output-parser id, validated against a JSON schema.
- **Given** no `harnesses.yaml` and no env override **When** a build runs **Then** behavior is
  byte-identical to today's `DEFAULT_AGENT_CMD = ["claude","-p",…]` (backward compatible default).
- **Given** `SDLC_AGENT_CMD` is set **When** a build runs **Then** it still works (the override is
  re-expressed as an ad-hoc registry entry, not removed).

**Technical Notes**: Generalize the seam in `controller/src/sdlc/dispatch.py`; model the loader on
how `controller/config/adversarial-reviewers.yaml` is read. Reuse the `{pr_number}`/`{story_id}`
placeholder-templating style already used by the reviewer registry. Add the schema under
`controller/src/sdlc/schemas/`.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: registry load/validate, default fallback, env-override path
- [ ] `docs/controller-architecture.md` updated with the registry/seam

**Dependencies**: None
**Risk Level**: High

##### Story 19.1-002: Pluggable per-harness output parsing
**User Story**: As FX, I want each harness's output (success JSON, errors, rate-limits, context
overflow) parsed by a harness-specific parser so that non-Claude harnesses get proper handling
instead of the lossy plain-stdout fallback.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the Claude parser **When** it processes stream-json output **Then** it still extracts
  the `<<<RESULT_JSON>>>` contract, usage, 429/`resetsAt` rate-limits, and "prompt is too long"
  context-overflow exactly as today.
- **Given** a harness declaring a different parser id **When** its agent returns **Then** the
  declared parser is used and its result validates against the same `controller/src/sdlc/schemas`
  contract.
- **Given** a harness with no usage/rate-limit semantics **When** its agent returns **Then** usage
  is recorded as "unavailable" (not fabricated) and the run still advances.

**Technical Notes**: Extract the Claude-specific parsing currently inline in `dispatch.py` into a
parser interface; register parsers by id from `harnesses.yaml`. Keep the `<<<RESULT_JSON>>>`
contract harness-neutral.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: Claude parser parity (golden output), alt-parser path, unavailable-usage path
- [ ] Docs updated

**Dependencies**: 19.1-001
**Risk Level**: High

### Feature 19.2: Per-role harness routing — heterogeneous runs

The headline capability: assign build/coverage/review/qa/merge roles to different harnesses in one run.

#### Stories

##### Story 19.2-001: Role→harness assignment configuration
**User Story**: As FX, I want to map each pipeline role to a harness so that, for example, Claude
builds while Codex reviews and QAs in the same run.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a role→harness map (config + `sdlc build` flag, e.g. `--harness build=claude,review=codex,qa=codex`)
  **When** a build runs **Then** each role dispatches to its assigned harness from the registry.
- **Given** no map **When** a build runs **Then** all roles default to a single harness (today's
  behavior — `claude`).
- **Given** a role mapped to an unknown/disabled harness **When** the build starts **Then** it
  fails fast in preflight with a clear message (no half-run).

**Technical Notes**: Roles are the existing controller stages (build, coverage/qa, review, merge,
docs). Bridge with `adversarial-reviewers.yaml` so the `review` role and the reviewer registry
agree rather than conflict (Epic-08 coordination point).

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: per-role routing, default collapse, unknown-harness preflight failure
- [ ] Docs: role catalog + routing examples

**Dependencies**: 19.1-001
**Risk Level**: Medium

##### Story 19.2-002: Record and surface per-stage harness in the ledger
**User Story**: As FX reviewing a run, I want the ledger and status to show which harness ran each
story/stage so that heterogeneous runs are auditable.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a heterogeneous run **When** a stage completes **Then** the SQLite ledger records the
  harness that ran it.
- **Given** `sdlc status` / the ledger view **When** rendered **Then** the harness per stage is
  visible.
- **Given** an existing (pre-migration) ledger **When** opened **Then** it still loads (harness
  column defaults to `claude`/`unknown`).

**Technical Notes**: Extend the ledger schema with a nullable `harness` column; update
`status.py`/`ledger_view.py`. Coordinate with Epic-11 (observability *renders* what this records).

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: ledger write/read, backward-compat migration, status rendering
- [ ] Docs updated

**Dependencies**: 19.2-001, 19.1-002
**Risk Level**: Medium

### Feature 19.3: Codex as a first-class adapter

The one concrete non-Claude adapter, proving the abstraction end-to-end.

#### Stories

##### Story 19.3-001: Codex build/QA adapter
**User Story**: As FX, I want Codex to run build and coverage/QA agents through the registry so
that the whole pipeline can execute without Claude.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `harnesses.yaml` has a `codex` entry **When** a build dispatches a build/coverage agent
  to it **Then** Codex runs (via `codex exec` / the established Codex path), returns the
  `<<<RESULT_JSON>>>` contract, and the result validates.
- **Given** a Codex agent run **When** it completes **Then** its output is parsed by the Codex
  parser (Story 19.1-002) and the stage advances normally.
- **Given** a full run configured for Codex **When** it finishes **Then** zero `claude` processes
  were spawned (verified via the dispatch log).

**Technical Notes**: Reuse the shapes in `scripts/codex-adversarial-review.sh` and
`controller/src/sdlc/schemas/adversarial-reviewer-response.schema.json`. Do not modify the OpenAI
`codex` delegation plugin; this is a controller-side adapter.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: Codex adapter contract round-trip (mocked `codex exec`), zero-claude assertion
- [ ] Docs: Codex adapter section

**Dependencies**: 19.1-002
**Risk Level**: High

##### Story 19.3-002: Route review/QA roles to Codex through the unified registry
**User Story**: As FX, I want the `review` and `qa` roles to use Codex via the same registry that
build uses so that there aren't two competing Codex configurations.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the harness registry and `adversarial-reviewers.yaml` **When** the `review` role runs
  on Codex **Then** a single source of truth governs the Codex command (no duplicated/divergent
  config).
- **Given** `--harness review=codex,qa=codex,build=claude` **When** a run executes **Then** review
  and QA verdicts come from Codex and build artifacts from Claude, all recorded per Story 19.2-002.

**Technical Notes**: Decide and document whether `adversarial-reviewers.yaml` becomes a *view* over
`harnesses.yaml` or vice-versa; Epic-08 owns the reviewer-consensus semantics — preserve them.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: review-on-codex path, config de-duplication, consensus preserved
- [ ] Docs: reconcile adversarial-review.md with the harness registry

**Dependencies**: 19.2-001, 19.3-001
**Risk Level**: Medium

### Feature 19.4: Single-source skill authoring — kill mirror drift

Author each pipeline skill once; generate Claude and Codex skill files, with a parity gate.

#### Stories

##### Story 19.4-001: Harness-neutral skill definition format
**User Story**: As FX maintaining the pipeline, I want one neutral definition per skill so that I
don't hand-maintain separate Claude and Codex copies.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a neutral skill source (body + harness-agnostic metadata) **When** validated **Then**
  it captures everything both the Claude `SKILL.md` (frontmatter, `allowed-tools`, `argument-hint`,
  `disable-model-invocation`) and the Codex `.codex-plugin` manifest/`Use <skill>` form need.
- **Given** the existing `shared-skills/` SSOT **When** the format is defined **Then** the 7
  shared skills are expressible in it without loss (proves the schema).

**Technical Notes**: Extend the `shared-skills/` + ADR-002 model to the full pipeline. Capture the
Claude-only constructs (`${CLAUDE_SKILL_DIR}`, `$ARGUMENTS`, the `` !`…` `` preprocessor) as
harness-tagged blocks so the generator can translate or omit per target.

**Definition of Done**:
- [ ] Format/schema defined and peer reviewed
- [ ] Tests: round-trip the 7 shared skills through the schema
- [ ] `docs/adr/` updated (supersede/extend ADR-002)

**Dependencies**: None
**Risk Level**: Medium

##### Story 19.4-002: Skill generator/transpiler
**User Story**: As FX, I want a generator that emits Claude and Codex skill files from the neutral
source so that both harnesses stay in lock-step automatically.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a neutral skill source **When** the generator runs **Then** it emits the Claude
  `plugins/autonomous-sdlc/skills/<name>/SKILL.md` and the Codex
  `nix-install/plugins/autonomous-sdlc/skills/<name>/SKILL.md` (+ committed slash-command symlinks
  where applicable).
- **Given** the generated Claude output **When** loaded by Claude Code **Then** it behaves
  identically to the current hand-written skill (golden comparison on at least one skill).
- **Given** the generated Codex output **When** loaded by Codex **Then** it carries the correct
  `.codex-plugin` manifest schema and `Use <skill>` invocation.

**Technical Notes**: Drive distribution via `scripts/` (alongside `sync-shared-skills.sh`); reuse
`controller/src/sdlc/sync.py` machinery where possible.

**Definition of Done**:
- [ ] Generator implemented and peer reviewed
- [ ] Tests: golden Claude + Codex output for representative skills
- [ ] Docs: how to author + regenerate skills

**Dependencies**: 19.4-001
**Risk Level**: High

##### Story 19.4-003: Cross-harness parity CI gate
**User Story**: As FX, I want CI to fail when generated skill files drift from their source so that
the harnesses can never silently diverge again.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a committed skill file that no longer matches its neutral source **When** the parity
  job runs **Then** CI fails with a diff and the regenerate command.
- **Given** everything in sync **When** the job runs **Then** it passes.

**Technical Notes**: Extend `sdlc sync-check` (`controller/src/sdlc/sync.py`) and
`scripts/sync-shared-skills.sh verify` to cover all generated outputs, not just the 7 shared skills.

**Definition of Done**:
- [ ] Gate implemented and wired into CI
- [ ] Tests: drift-detected (fail) and in-sync (pass) cases
- [ ] Docs updated

**Dependencies**: 19.4-002
**Risk Level**: Medium

### Feature 19.5: Capability gating and graceful degradation

Probe what each harness can do; degrade safely when it can't.

#### Stories

##### Story 19.5-001: Harness capability probe and preflight
**User Story**: As FX, I want the controller to know each harness's capabilities (worktree
isolation, parallel, JSON contract, usage tracking) before a run so that it can plan safely.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a harness declares capability flags in `harnesses.yaml` (and/or a probe command)
  **When** `sdlc build` preflights **Then** capabilities are resolved and logged.
- **Given** a requested run mode the harness can't support (e.g. `mode=parallel` but no worktree
  isolation) **When** preflight runs **Then** the controller warns and selects a safe alternative
  (Story 19.5-002), rather than failing mid-run.

**Technical Notes**: Capability flags live next to the registry entry; an optional probe command
can confirm a CLI is installed/authenticated. Coordinate with Epic-17's `mode` authority.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: capability resolution, missing-capability preflight decision
- [ ] Docs: capability matrix

**Dependencies**: 19.1-001
**Risk Level**: Medium

##### Story 19.5-002: Degradation matrix and safe fallbacks
**User Story**: As FX, I want capability gaps to degrade predictably (parallel→serial, drop usage
metrics, skip worktrees) so that an alternate harness never crashes a run.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a harness without worktree isolation **When** a parallel build is requested **Then**
  the cohort runs serially with one explicit log line explaining the downgrade.
- **Given** a harness without usage/rate-limit semantics **When** stages run **Then** cost/usage is
  recorded as "unavailable" and rate-limit backoff is skipped (no fabricated 429 handling).
- **Given** any degradation **When** it occurs **Then** it is recorded in the ledger/run summary.

**Technical Notes**: Centralize the degradation decisions so they're testable; document the matrix
in `docs/controller-architecture.md`.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: each degradation path
- [ ] Docs: degradation matrix published

**Dependencies**: 19.5-001, 19.2-002
**Risk Level**: Medium

### Feature 19.6: Onboarding and boundaries

Make adding a harness a documented config exercise; record what stays Claude-only.

#### Stories

##### Story 19.6-001: "Add a new harness" guide + generic adapter template
**User Story**: As FX (or a colleague), I want a step-by-step guide and a generic adapter template
so that wiring opencode/pi/gemini is a config + wrapper change, not a code change.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the guide **When** followed for a hypothetical harness **Then** a worked example shows
  the `harnesses.yaml` entry, a wrapper-script template, parser declaration, and capability flags —
  with no Python edits required.
- **Given** the generic CLI adapter template **When** copied **Then** it round-trips the
  `<<<RESULT_JSON>>>` contract out of the box.

**Technical Notes**: Place under `docs/` (e.g. `docs/harness-adapters.md`); reference the Codex
adapter as the canonical worked example. List opencode/pi/gemini as candidate future targets.

**Definition of Done**:
- [ ] Guide + template written and reviewed
- [ ] Template validated against the contract schema
- [ ] Linked from README harness matrix

**Dependencies**: 19.1-002, 19.5-001
**Risk Level**: Low

##### Story 19.6-002: Document the in-process-agent boundary
**User Story**: As FX, I want it written down that `fix-issue`/`resume-build-agents` stay
Claude-only so that no one wastes time trying to run them on a CLI harness.
**Priority**: Should Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** the docs **When** a reader checks harness support **Then** a clear matrix shows the
  controller-driven `build-stories` path is cross-harness while `fix-issue`/`resume-build-agents`
  are Claude-only (they use the in-process `Agent` tool with `subagent_type`/`isolation="worktree"`,
  which has no CLI equivalent).
- **Given** `fix-issue` invoked under a non-Claude harness **When** it starts **Then** it fails
  fast with a message pointing to the boundary doc (if such invocation is even reachable).

**Technical Notes**: Update `docs/controller-architecture.md` and the README harness/support matrix.

**Definition of Done**:
- [ ] Boundary documented in architecture + README
- [ ] Matrix added
- [ ] Reviewed

**Dependencies**: 19.6-001
**Risk Level**: Low

## Story Dependencies (within Epic-19)

```
19.1-001 (registry) ─┬─> 19.1-002 (parsers) ─┬─> 19.3-001 (codex adapter) ─> 19.3-002 (codex review/qa)
                     │                        └─> 19.2-002 (ledger) ───────────────┐
                     ├─> 19.2-001 (role routing) ─> 19.2-002 ─> 19.5-002 (degrade) ┘
                     └─> 19.5-001 (capability) ─┬─> 19.5-002
                                                └─> 19.6-001 (guide) ─> 19.6-002 (boundary)

19.4-001 (skill format) ─> 19.4-002 (generator) ─> 19.4-003 (parity gate)
```

- **Cohort 1** (no deps): 19.1-001, 19.4-001
- **Cohort 2**: 19.1-002 (needs 19.1-001), 19.2-001 (needs 19.1-001), 19.5-001 (needs 19.1-001),
  19.4-002 (needs 19.4-001)
- **Cohort 3**: 19.3-001 (needs 19.1-002), 19.2-002 (needs 19.2-001, 19.1-002), 19.4-003 (needs
  19.4-002), 19.6-001 (needs 19.1-002, 19.5-001)
- **Cohort 4**: 19.3-002 (needs 19.2-001, 19.3-001), 19.5-002 (needs 19.5-001, 19.2-002), 19.6-002
  (needs 19.6-001)

> Cross-epic: Epic-08 *owns* the adversarial-reviewer registry this epic generalizes (19.3-002 must
> preserve its consensus semantics). Epic-11 *renders* the per-stage harness this epic *records*
> (19.2-002). Epic-17 *owns* parallelism; this epic's capability gating (19.5) *respects* it. Epic-05
> keeps release/versioning.

## Epic Complete When

- A full `sdlc build` run completes on Codex with zero `claude` invocations.
- A single run builds on Claude and reviews+QAs on Codex, with per-stage harness in the ledger.
- Adding a new harness needs no Python changes (proven by the onboarding guide's worked example).
- Claude and Codex skill files are generated from one source and CI fails on drift.
- Capability gaps degrade safely (parallel→serial, usage "unavailable") with explicit logging.
- The Claude-only boundary for `fix-issue`/`resume-build-agents` is documented with a support matrix.
