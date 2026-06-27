# Epic 20: Cross-Harness SDLC Portability — run the pipeline on any agent harness

> **Status: COMPLETE (18/18)** — the original 13 stories merged on `main` (2026-06-27): harness
> registry + adapter contract, pluggable output parsing, role→harness config, per-stage harness in
> the ledger, Codex build/QA adapter + review/QA routing, harness-neutral skill format +
> generator/transpiler + parity CI gate, capability probe + degradation matrix, and the "add a new
> harness" guide + in-process-agent boundary docs. Built across parallel runs 68e5a36c / e4f9976c
> (PRs #200-#212). **Re-opened 2026-06-27 with Feature 20.7 (5 stories)** after cross-harness usage
> testing found three gaps the original stories shipped incomplete: (1) per-role `--harness` routing
> was wired for preflight + ledger *label* only — the resolved harnesses were validated in `cli.py`
> and then discarded, so `--harness build=codex` *labelled* the ledger while `_dispatch_stage` still
> ran `claude` for every stage; (2) the Codex `build-stories` was still a hand-maintained native
> orchestrator in the mirror, never brought under the single-source generator (only the 7 utility
> skills were); (3) per-stage *model* routing (Epic-14's Balanced map) was Claude-only — a registry
> harness ignored the routed model. Feature 20.7 closes all three plus a per-repo default-harness
> override; **Story 20.7-001 wired the routing into real dispatch, so per-role `--harness` is now
> functional** (a codex-routed stage dispatches the Codex adapter, not just a ledger label). All five
> Feature 20.7 stories are now merged (20.7-001..005), and 20.7-003 documented the Codex-worker
> runtime and corrected the harness-support status.
> Created 2026-06-26. Triggered by FX's request to make the autonomous-sdlc
> framework run beyond Claude Code (Codex, opencode, pi, …) and to allow alternate-harness role
> assignment (e.g. Codex for review and QA while Claude builds). This epic generalizes the
> vendor-agnostic registry pattern that Epic-08 introduced for *adversarial review* into a
> first-class **harness abstraction** across the whole agent-dispatch path; it does NOT re-author
> the Codex mirror plugin by hand (that drift is exactly what this epic eliminates). The two
> in-process-subagent skills (`fix-issue`, `resume-build-agents`) stay Claude-only by design.

## Epic Overview

**Epic ID**: Epic-20
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

**Total Stories**: 18 | **Total Points**: 72 | **MVP Stories**: 0 (roadmap — Should Have)

## Out of Scope (Non-Goals)

- **In-process subagent portability.** `fix-issue` and `resume-build-agents` spawn Claude
  *in-process* agents (`subagent_type`/`model`/`isolation="worktree"`) with no CLI-harness
  equivalent; they remain Claude-only. This epic documents the boundary (Story 20.6-002) rather
  than abstracting it.
- **Concrete opencode / pi / gemini adapters.** Only **Codex** is built here. The framework +
  generic template + onboarding guide make those a config exercise; shipping/validating each one
  is future work.
- **Release & versioning.** CHANGELOG/semver stays owned by Epic-05; the Codex mirror plugin's
  version bump cadence is unchanged except where the generator replaces hand edits.
- **Re-implementing parallelism or the ledger.** Epic-17 owns concurrency; Epic-20 only *gates*
  it by capability. The SQLite ledger schema is extended (which harness ran a stage), not redesigned.
- **The OpenAI `codex` delegation plugin** (`codex-companion.mjs`, `codex-rescue`) — that is a
  separate Claude→Codex delegation path and is not modified, though the Codex adapter may reuse
  `codex exec`.

## Features in This Epic

### Feature 20.1: Harness adapter registry — the core abstraction

Generalize the dispatch seam and the adversarial-reviewers registry into one config-driven harness
abstraction the rest of the epic builds on.

#### Stories

##### Story 20.1-001: Define the harness registry and adapter contract
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

##### Story 20.1-002: Pluggable per-harness output parsing
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

**Dependencies**: 20.1-001
**Risk Level**: High

### Feature 20.2: Per-role harness routing — heterogeneous runs

The headline capability: assign build/coverage/review/qa/merge roles to different harnesses in one run.

#### Stories

##### Story 20.2-001: Role→harness assignment configuration
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

**Dependencies**: 20.1-001
**Risk Level**: Medium

##### Story 20.2-002: Record and surface per-stage harness in the ledger
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

**Dependencies**: 20.2-001, 20.1-002
**Risk Level**: Medium

### Feature 20.3: Codex as a first-class adapter

The one concrete non-Claude adapter, proving the abstraction end-to-end.

#### Stories

##### Story 20.3-001: Codex build/QA adapter
**User Story**: As FX, I want Codex to run build and coverage/QA agents through the registry so
that the whole pipeline can execute without Claude.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `harnesses.yaml` has a `codex` entry **When** a build dispatches a build/coverage agent
  to it **Then** Codex runs (via `codex exec` / the established Codex path), returns the
  `<<<RESULT_JSON>>>` contract, and the result validates.
- **Given** a Codex agent run **When** it completes **Then** its output is parsed by the Codex
  parser (Story 20.1-002) and the stage advances normally.
- **Given** a full run configured for Codex **When** it finishes **Then** zero `claude` processes
  were spawned (verified via the dispatch log).

**Technical Notes**: Reuse the shapes in `scripts/codex-adversarial-review.sh` and
`controller/src/sdlc/schemas/adversarial-reviewer-response.schema.json`. Do not modify the OpenAI
`codex` delegation plugin; this is a controller-side adapter.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: Codex adapter contract round-trip (mocked `codex exec`), zero-claude assertion
- [ ] Docs: Codex adapter section

**Dependencies**: 20.1-002
**Risk Level**: High

##### Story 20.3-002: Route review/QA roles to Codex through the unified registry
**User Story**: As FX, I want the `review` and `qa` roles to use Codex via the same registry that
build uses so that there aren't two competing Codex configurations.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the harness registry and `adversarial-reviewers.yaml` **When** the `review` role runs
  on Codex **Then** a single source of truth governs the Codex command (no duplicated/divergent
  config).
- **Given** `--harness review=codex,qa=codex,build=claude` **When** a run executes **Then** review
  and QA verdicts come from Codex and build artifacts from Claude, all recorded per Story 20.2-002.

**Technical Notes**: Decide and document whether `adversarial-reviewers.yaml` becomes a *view* over
`harnesses.yaml` or vice-versa; Epic-08 owns the reviewer-consensus semantics — preserve them.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: review-on-codex path, config de-duplication, consensus preserved
- [ ] Docs: reconcile adversarial-review.md with the harness registry

**Dependencies**: 20.2-001, 20.3-001
**Risk Level**: Medium

### Feature 20.4: Single-source skill authoring — kill mirror drift

Author each pipeline skill once; generate Claude and Codex skill files, with a parity gate.

#### Stories

##### Story 20.4-001: Harness-neutral skill definition format
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

##### Story 20.4-002: Skill generator/transpiler
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

**Dependencies**: 20.4-001
**Risk Level**: High

##### Story 20.4-003: Cross-harness parity CI gate
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

**Dependencies**: 20.4-002
**Risk Level**: Medium

### Feature 20.5: Capability gating and graceful degradation

Probe what each harness can do; degrade safely when it can't.

#### Stories

##### Story 20.5-001: Harness capability probe and preflight
**User Story**: As FX, I want the controller to know each harness's capabilities (worktree
isolation, parallel, JSON contract, usage tracking) before a run so that it can plan safely.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a harness declares capability flags in `harnesses.yaml` (and/or a probe command)
  **When** `sdlc build` preflights **Then** capabilities are resolved and logged.
- **Given** a requested run mode the harness can't support (e.g. `mode=parallel` but no worktree
  isolation) **When** preflight runs **Then** the controller warns and selects a safe alternative
  (Story 20.5-002), rather than failing mid-run.

**Technical Notes**: Capability flags live next to the registry entry; an optional probe command
can confirm a CLI is installed/authenticated. Coordinate with Epic-17's `mode` authority.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: capability resolution, missing-capability preflight decision
- [ ] Docs: capability matrix

**Dependencies**: 20.1-001
**Risk Level**: Medium

##### Story 20.5-002: Degradation matrix and safe fallbacks
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

**Dependencies**: 20.5-001, 20.2-002
**Risk Level**: Medium

### Feature 20.6: Onboarding and boundaries

Make adding a harness a documented config exercise; record what stays Claude-only.

#### Stories

##### Story 20.6-001: "Add a new harness" guide + generic adapter template
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

**Dependencies**: 20.1-002, 20.5-001
**Risk Level**: Low

##### Story 20.6-002: Document the in-process-agent boundary
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

**Dependencies**: 20.6-001
**Risk Level**: Low

### Feature 20.7: Cross-Harness Completion

Finish what 20.2/20.3 began — and close two gaps the original epic shipped incomplete: route a
stage's worker to its assigned harness *for real*, make the Codex `build-stories` controller-driven
from a single source, give non-Claude harnesses per-stage model routing, and let a repo declare its
own default harness. Added 2026-06-27 after cross-harness usage testing.

#### Stories

##### Story 20.7-001: Wire per-role `--harness` routing into actual dispatch
**User Story**: As FX, I want `--harness build=codex` to actually run Codex workers (not just label
the ledger) so that per-role cross-harness routing works as Epic-20 intended.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `--harness build=codex,coverage=codex,review=codex,merge=codex` **When** a build runs
  **Then** every dispatched worker uses the codex adapter argv (zero `claude` processes) and the
  `codex-exec` parser — asserted against a recording dispatcher in the **build loop**, not
  `harness.to_argv()` in isolation.
- **Given** a mixed map `build=claude,review=codex` **When** a build runs **Then** the build stage
  argv is claude and the review stage argv is the codex adapter, and the ledger `harness` column
  matches what actually ran.
- **Given** no `--harness` flag and no `SDLC_AGENT_CMD` **When** a build runs **Then** dispatch is
  byte-identical to today (the default path passes no `agent_cmd`/`parser`).
- **Given** codex's capabilities (`worktree_isolation:false, parallel:false`) **When** a parallel
  build routes a stage to it **Then** it degrades to serial with the existing warn log, not a crash.

**Technical Notes**: Thread the per-stage harness through `_dispatch_stage` (`build.py:4286`) using
the existing `_stage_harness` (`build.py:4184`) + `resolve_harness`/`default_registry_path()`
(`role_routing.py:278`); on the opt-in path pass `agent_cmd=h.to_argv(model=model)` and
`parser=(None if h.source in ("builtin","env") else h.parser)` into the existing `dispatch` seam
(preserves `_resolve_dispatch`'s thinking-cap/sandbox binding and test injection). `dispatch_agent`
already accepts both (`dispatch.py:633`). Root cause of the gap: `dispatch_on_harness`/
`resolve_agent_argv` (`harness.py:228,240`) have no callers and `cli.py:170` discards
`resolved_harnesses`. Replace the misleading `test_full_codex_run_spawns_zero_claude` (asserts on
`harness.to_argv()` only) with a real build-loop assertion.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] TDD build-loop tests (zero-claude, mixed-harness, default-unchanged, degradation)
- [ ] `docs/controller-architecture.md` routing section corrected

**Dependencies**: 20.1-001, 20.1-002, 20.2-001, 20.2-002, 20.3-001 (completes their intent)
**Risk Level**: High

##### Story 20.7-002: Bring `build-stories` under the single-source skill generator
**User Story**: As FX maintaining the pipeline, I want the Codex `build-stories` generated from one
neutral source as a thin `sdlc build` wrapper so that it stops being a hand-maintained native
orchestrator and can't drift from the Claude side.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a new `shared-skills/neutral/build-stories.skill.md` (thin wrapper, `{{ARGUMENTS}}`)
  **When** `scripts/generate-skills.sh generate` runs **Then** it emits both the Claude
  `plugins/autonomous-sdlc/skills/build-stories/SKILL.md` and the Codex mirror copy.
- **Given** the regenerated Claude skill **When** loaded **Then** it is functionally identical to the
  current hand-written thin wrapper (preserves `allowed-tools: Bash`, `disable-model-invocation`,
  `argument-hint`; runs `sdlc build $ARGUMENTS`) — golden comparison.
- **Given** the regenerated Codex skill **When** loaded **Then** it runs `sdlc build` via the
  controller (an honest wrapper, not the "native port" orchestrator) and invokes as
  `Use build-stories …`.
- **Given** a generated file is hand-edited **When** the parity gate runs in CI **Then** it fails
  with a diff + regenerate command; in-sync passes.

**Technical Notes**: Extend `controller/src/sdlc/skill_format.py` + `skill_generator.py`
(`generate_claude_skill`/`generate_codex_skill`) + `neutral-skill.schema.json` to carry pipeline
frontmatter (the 7 utility skills don't use `allowed-tools`/`argument-hint`); adjust the codex
template's hardcoded "Codex-native port" preamble for a controller wrapper. Add `build-stories` to the
generated-parity set (`controller/src/sdlc/sync.py`, `scripts/sync-shared-skills.sh
verify-generated`). Overwrites the mirror's hand-written orchestrator; bump the `nix-install`
submodule. Aligns with `portability.py` `CROSS_HARNESS_SKILLS = {"build-stories"}`.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Golden Claude + Codex output tests
- [ ] Parity gate red-on-drift / green-in-sync
- [ ] ADR-002/003 updated if the format scope is extended

**Dependencies**: 20.4-001, 20.4-002, 20.4-003 (extends the generator to a pipeline skill); 20.7-001
(so the generated wrapper actually routes)
**Risk Level**: Medium

##### Story 20.7-003: Document Codex-worker runtime and correct the harness-support status
**User Story**: As an LTM colleague, I want clear docs on running a Codex-worker build and an honest
epic status so that I don't hit auth/sandbox dead-ends or trust a misleading `COMPLETE`.
**Priority**: Should Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** `docs/harness-adapters.md` **When** a reader sets up a codex-worker run **Then** it
  states: pre-authenticate codex; `HARNESS_AGENT_CMD="codex exec --full-auto"` for non-interactive
  write/exec; do **not** combine with controller `--sandbox` (claude-only, no-egress image); run on
  the host path (worker `gh` ops need network/auth that codex's `workspace-write` sandbox blocks).
- **Given** `STORIES.md` and the epic file **When** Epic-20 status is read **Then** it reflects that
  per-role routing was label-only until 20.7-001 and is now functional.

**Technical Notes**: Update `docs/harness-adapters.md`, `docs/controller-architecture.md` (if wording
needs), and the Epic-20 status lines. Capture the routing-gap root cause as a one-line provenance note.

**Definition of Done**:
- [ ] Codex-worker runtime documented in `docs/harness-adapters.md`
- [ ] Epic-20 status lines corrected (epic file + STORIES.md)
- [ ] Reviewed

**Dependencies**: 20.7-001, 20.7-002
**Risk Level**: Low

##### Story 20.7-004: Per-harness, per-stage model routing
**User Story**: As FX, I want each non-Claude harness to map pipeline stages to its own models
(e.g. Codex: build=`gpt-5.4-codex`, merge=a cheaper model, adversarial=a stronger one) so that
cost/capability tuning works on every harness, not just Claude — the OpenAI analog of Epic-14's
Balanced map.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a registry harness whose command template carries a `{model}` placeholder and a
  per-harness stage→model map **When** a stage routes to it **Then** `to_argv` substitutes the mapped
  model for that stage (registry harnesses no longer ignore `model`).
- **Given** the `codex` harness with a stage→model map (build/coverage/review/merge/adversarial)
  **When** a build runs **Then** each stage's codex worker launches with its mapped OpenAI model.
- **Given** a harness/stage with no model mapping **When** a stage routes to it **Then** it falls back
  to the harness's default command (today's single fixed model) — no regression.
- **Given** the Claude harness **When** a build runs **Then** Epic-14's Haiku/Sonnet/Opus routing is
  unchanged.

**Technical Notes**: Extend `HarnessConfig.to_argv` (`harness.py:102`) so a registry entry with a
`{model}` placeholder receives the routed model (today it's ignored for registry, `harness.py:109`);
add a per-harness model map to `harnesses.yaml` (the `codex` command gains `--model {model}` + a
stage→model table, analogous to `ModelRoutingConfig`/`BALANCED` in `model_routing.py`); reuse
`select_model`/`escalate_model` for the stage *role*, then resolve the harness-specific id. Epic-14's
`haiku`/`sonnet`/`opus` aliases are Claude-only — codex needs its own id set (`gpt-5.4-codex`
variants). `codex-build-adapter.sh` must pass the model through (`codex exec --model …`).

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests (registry model substitution, codex per-stage models, no-map fallback, claude unchanged)
- [ ] `docs/harness-adapters.md` model-map section

**Dependencies**: 20.7-001 (routing must actually run codex first); relates to 20.2-001 (role map)
and Epic-14 (model-routing concepts)
**Risk Level**: Medium

##### Story 20.7-005: Per-repo default harness override
**User Story**: As FX, I want a repo to declare its default harness (and optional per-role defaults)
in a root override file so that I don't pass `--harness` on every `sdlc build` in that repo.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a consumer repo with a root `.sdlc-harness.yaml` declaring `default: codex` (and an
  optional per-role map) **When** `sdlc build` runs with no `--harness` flag **Then** every unmapped
  role routes to the file's default.
- **Given** an explicit `--harness` flag **When** a build runs **Then** the flag wins over the file
  (precedence: CLI flag > repo file > registry `default:`).
- **Given** no file and no flag **When** a build runs **Then** behaviour is today's (registry
  `default: claude`).
- **Given** the file names an unknown/disabled harness **When** a build starts **Then** it fails fast
  in preflight — the same path as the CLI flag.

**Technical Notes**: Add an additive per-repo override at the consumer repo root, mirroring
`.sdlc-model-routing.yaml` (`model_routing.py:OVERRIDE_FILENAME`) and `.sdlc-risk-config.yaml`
(`risk_gate.py`). Resolve in `_stage_harness` (`build.py:4184`) / `resolve_role_routing`
(`role_routing.py`) as the fallback below the `--harness` map and above the registry `default:`.
Reuse the existing preflight validation (`resolve_role_routing`/`check_review_bridge`/
`reconcile_reviewer_registry`, `cli.py:170`). YAML to match the existing `.sdlc-*.yaml` convention.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests (file default applied, CLI overrides file, no-file unchanged, per-role map honoured,
  invalid-harness fails fast)
- [ ] `docs/harness-adapters.md` + a sample `.sdlc-harness.yaml`

**Dependencies**: 20.7-001 (routing must run); relates to 20.2-001 (role map)
**Risk Level**: Low

## Story Dependencies (within Epic-20)

```
20.1-001 (registry) ─┬─> 20.1-002 (parsers) ─┬─> 20.3-001 (codex adapter) ─> 20.3-002 (codex review/qa)
                     │                        └─> 20.2-002 (ledger) ───────────────┐
                     ├─> 20.2-001 (role routing) ─> 20.2-002 ─> 20.5-002 (degrade) ┘
                     └─> 20.5-001 (capability) ─┬─> 20.5-002
                                                └─> 20.6-001 (guide) ─> 20.6-002 (boundary)

20.4-001 (skill format) ─> 20.4-002 (generator) ─> 20.4-003 (parity gate)

# Feature 20.7 (cross-harness completion) — gates on the original routing/skill/model work:
20.7-001 (real routing) ─┬─> 20.7-002 (build-stories single-source) ─┐
                         ├─> 20.7-004 (per-stage model routing) ─────┼─> 20.7-003 (runtime docs + status)
                         └─> 20.7-005 (per-repo default harness) ────┘
```

- **Cohort 1** (no deps): 20.1-001, 20.4-001
- **Cohort 2**: 20.1-002 (needs 20.1-001), 20.2-001 (needs 20.1-001), 20.5-001 (needs 20.1-001),
  20.4-002 (needs 20.4-001)
- **Cohort 3**: 20.3-001 (needs 20.1-002), 20.2-002 (needs 20.2-001, 20.1-002), 20.4-003 (needs
  20.4-002), 20.6-001 (needs 20.1-002, 20.5-001)
- **Cohort 4**: 20.3-002 (needs 20.2-001, 20.3-001), 20.5-002 (needs 20.5-001, 20.2-002), 20.6-002
  (needs 20.6-001)
- **Feature 20.7** (re-opened completion work): 20.7-001 is the unblocker (needs the original
  20.1/20.2/20.3); 20.7-002, 20.7-004, 20.7-005 each build on 20.7-001; 20.7-003 (docs + status)
  closes out after 20.7-001/002.

> Cross-epic: Epic-08 *owns* the adversarial-reviewer registry this epic generalizes (20.3-002 must
> preserve its consensus semantics). Epic-11 *renders* the per-stage harness this epic *records*
> (20.2-002). Epic-17 *owns* parallelism; this epic's capability gating (20.5) *respects* it. Epic-05
> keeps release/versioning.

## Epic Complete When

- A full `sdlc build` run completes on Codex with zero `claude` invocations.
- A single run builds on Claude and reviews+QAs on Codex, with per-stage harness in the ledger.
- Adding a new harness needs no Python changes (proven by the onboarding guide's worked example).
- Claude and Codex skill files are generated from one source and CI fails on drift.
- Capability gaps degrade safely (parallel→serial, usage "unavailable") with explicit logging.
- The Claude-only boundary for `fix-issue`/`resume-build-agents` is documented with a support matrix.
- **(Feature 20.7)** `--harness build=codex` *actually* dispatches Codex workers (not just a ledger
  label); the Codex `build-stories` is generated from the single neutral source; each harness can
  route models per stage; and a repo can set its default harness via `.sdlc-harness.yaml`.
