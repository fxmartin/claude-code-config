# Epic 27: Performance & Token Optimization

> **Status: COMPLETE (12/12)** — all 12 stories implemented, tested, and merged 2026-07-15/16
> (27.0-001 → 27.3-004, PRs #465-#476). Created 2026-07-11 from a data-verified analysis of past runs: live
> ledgers (nix-install clone + local-code-bench), 212 stage logs with usage/cost, and 472 session
> transcripts since 2026-06-10. Headline findings: the controller path is already well-routed
> (≈ $9/story, build 58% of cost), but the **interactive/Agent-tool path runs 94% of its token
> traffic on Opus** (21.6M output / 4,268M cache-read tokens in a month) because `fix-issue`
> hardcodes Opus and every repo agent inherits the interactive default. Elapsed time and tokens are
> coupled: quota exhaustion triggers 5-min rate-limit backoff loops that turned ~20-min stories into
> multi-hour outliers (one run averaged 503 min/coverage stage). Full baseline + reproduction
> queries land in `docs/optimization/BASELINE.md` (Story 27.0-001).

## Epic Overview
**Epic ID**: Epic-27
**Description**: Cut the framework's token consumption and wall-clock time without weakening its quality gates. Three levers, ranked by measured impact: (1) kill the silent Opus default on the interactive/Agent-tool path (fix-issue hardcoded models, agent frontmatter, prompt dedup/shrink); (2) risk-tier the gates so docs-only/low-risk changes stop paying full-price coverage and Opus adversarial review; (3) structural controller optimizations that remove agent work a deterministic script can do (coverage pre-check, controller-owned PR creation, story-section injection, pre-baked review packet, stall telemetry).
**Business Value**: Lower per-story cost (baseline ≈ $9/story on the controller path; Opus dominates the interactive path), higher effective quota (fewer rate-limit stalls → faster unattended batches), and a committed measurement baseline that makes every future optimization verifiable.
**Success Metrics**: Opus share of interactive token traffic materially down from 94%; per-story controller cost down vs the $9 baseline; merge-gate pass rate and bugfix-loop rate not regressed; no multi-hour stage-duration outliers attributable to quota backoff on comparable batch sizes.

## Epic Scope
**Total Stories**: 12 | **Total Points**: 37 | **MVP Stories**: 0 (post-MVP roadmap epic)

## Non-Goals
- Migrating `fix-issue` orchestration into the controller (`sdlc fix`) — deferred to a future epic, tracked in [#436](https://github.com/fxmartin/claude-code-config/issues/436).
- Repairing the eval harness result-contract (`evaluate.py`) — deferred, tracked in [#435](https://github.com/fxmartin/claude-code-config/issues/435); this epic uses ledger/transcript measurements instead.
- Any weakening of gates for high-risk or normal code changes: escalation to Opus stays for flagged risk, the adversarial slot keeps its Opus floor on high-risk, and the coverage criterion is never lowered — only enforced deterministically.

## Features in This Epic

### Feature 27.0: Measurement Baseline

#### Stories

##### Story 27.0-001: Commit the measured performance baseline
**User Story**: As FX, I want the measured token/time baseline committed with its reproduction queries so that every optimization in this epic (and after it) is verified against real numbers, not guesses
**Priority**: Must Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** the repo **When** I open `docs/optimization/BASELINE.md` **Then** it contains the per-stage cost/token/duration table from the 50-story stage-log aggregation, the interactive model-mix table (Opus 94%), and the healthy-vs-outlier stage-duration comparison
- **Given** the doc **When** I follow its reproduction section **Then** each table can be regenerated: the ledger duration SQL, the stage-log stream-json usage aggregation script, and the transcript model-mix scan
- **Given** the doc **When** I read its "success criteria" section **Then** the epic's exit measurements (Opus share, $/story, gate pass rates) are stated

**Technical Notes**: Data sources: `.sdlc-state.db` (`stages` table has timestamps only — tokens come from `.sdlc-state.db.logs/<run>/<story>-<stage>-<n>.log` stream-json `result` lines), `~/.claude/projects/<project>/*.jsonl` for the interactive model mix. No production code.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing (N/A — documentation-only; reproduction scripts must run cleanly)
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: None
**Risk Level**: Low

### Feature 27.1: Kill the Silent Opus Default (Model Routing & Prompt Hygiene)

#### Stories

##### Story 27.1-001: Align fix-issue models with the Balanced profile
**User Story**: As FX, I want fix-issue to dispatch build/review/bugfix agents on Sonnet by default with risk-based Opus escalation so that a typical issue stops paying Opus prices on its three heaviest phases
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the investigation agent's structured result **When** it completes Phase 3 **Then** it includes a `COMPLEXITY: LOW|MEDIUM|HIGH` field (prompt + result-block contract updated in `investigation-agent-prompt.md`)
- **Given** a LOW/MEDIUM-complexity issue with no high-risk/security label **When** Phases 4/6/8 dispatch **Then** the build, review, and bugfix agents run on **sonnet**
- **Given** a HIGH-complexity issue or one carrying a high-risk/security label **When** those phases dispatch **Then** they run on **opus** (escalation preserved)
- **Given** the skill file **When** I read the inline `Agent(...)` examples (~lines 306-383) **Then** they match the new routing (no stale hardcoded `model="opus"`)

**Technical Notes**: `plugins/autonomous-sdlc/skills/fix-issue/SKILL.md` lines ~118-217 (phase instructions) and ~306-383 (inline examples); mirrors the controller's Balanced profile (`controller/src/sdlc/model_routing.py:66-79`) which already proves Sonnet-with-escalation holds quality. Merge/summary stay haiku.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: None
**Risk Level**: Medium

##### Story 27.1-002: Explicit model frontmatter on repo code agents
**User Story**: As FX, I want the repo's code agents to declare `model: sonnet` explicitly so that interactive Agent-tool dispatches stop silently inheriting my Opus session default
**Priority**: Must Have
**Story Points**: 1

**Acceptance Criteria**:
- **Given** `agents/*.md` **When** I inspect `qa-engineer`, `senior-code-reviewer`, `backend-typescript-architect`, `python-backend-engineer`, `ui-engineer`, `bash-zsh-macos-engineer` **Then** each declares `model: sonnet` in frontmatter
- **Given** a high-risk task **When** the orchestrator passes an explicit `model` at dispatch **Then** it overrides the frontmatter (escalation path documented in each agent file's header comment or WORKFLOW-v2.md)
- **Given** `podman-container-architect` **When** inspected **Then** it is unchanged (already pins sonnet)

**Technical Notes**: Frontmatter-only change; personal/* research agents are out of scope (WebSearch-heavy, low volume).

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing (N/A — config frontmatter; verified by dispatch smoke in 27.1-001's verification)
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: None
**Risk Level**: Low

##### Story 27.1-003: Single-source shared gate prompts and shrink the two largest
**User Story**: As FX, I want the gate-prompt templates duplicated between `fix-issue/` and `build-stories/` single-sourced and the two largest prompts cut by ≥40% so that every gate dispatch stops re-tokenizing redundant instructions
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the plugin **When** I search for coverage-gate/bugfix-agent/doc-update templates **Then** each exists exactly once (e.g. under `plugins/autonomous-sdlc/skills/_shared/`) and both skills resolve it (via `${CLAUDE_PLUGIN_ROOT}`; fallback in-plugin symlinks if the variable does not resolve in dispatched prompts — verify first)
- **Given** `build-stories/coverage-gate-prompt.md` (12k) and `merge-update-prompt.md` (9k) **When** shrunk **Then** each is ≥40% smaller by bytes with all gate criteria, result-block contracts, and failure-path instructions preserved
- **Given** a full `sdlc build` smoke on one story **When** the coverage and merge stages run **Then** their result blocks still validate against the existing schemas

**Technical Notes**: Shrink by cutting restated context and collapsing repeated result-block boilerplate — never by removing gate criteria. The controller loads these at dispatch time, so byte count is a direct per-dispatch token saving.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: None
**Risk Level**: Medium

##### Story 27.1-004: Split fix-issue SKILL.md into core + batch-mode reference
**User Story**: As FX, I want fix-issue's single-issue path to stop loading the parallel-batch orchestration and duplicated boilerplate so that every invocation halves its skill-prompt tokens
**Priority**: Should Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** `fix-issue/SKILL.md` **When** measured **Then** it is ≤ ~1,500 words (from ~3,022), with the parallel-batch mode (old lines 295-417) moved to `batch-mode.md` and an explicit instruction to read it only for `all` / `next --limit=N` arguments
- **Given** the five skills carrying the identical run-logging + Telegram boilerplate **When** inspected **Then** the boilerplate lives in one shared snippet referenced by each
- **Given** a single-issue `fix-issue` smoke **When** run **Then** all phases execute unchanged; **Given** a batch invocation **Then** batch-mode.md is loaded and the parallel path works

**Technical Notes**: Behavior-preserving refactor of prompt text only. Keep the Context Budget Rules (old lines 419-422) in the core file — they are what keeps the orchestrator lean.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 27.1-001 (edits the same file; land alignment first)
**Risk Level**: Low

### Feature 27.2: Risk-Tiered Gates

#### Stories

##### Story 27.2-001: Change-class detection + docs-only gate skip in the controller
**User Story**: As FX, I want the controller to deterministically classify a story's change class after build and skip the coverage dispatch and adversarial slot for docs-only/chore changes so that documentation stories stop paying code-gate prices
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a completed build stage **When** the changed files all match docs patterns (`*.md`, `docs/**`, plus a configurable allowlist) **Then** the story is classified `docs-only` and the coverage dispatch and adversarial slot are skipped
- **Given** a skipped gate **When** I inspect the ledger and `sdlc status`/dashboard **Then** the skip is recorded with a `skip_reason` (never displayed as a passed gate)
- **Given** any changed file outside the docs patterns **When** classified **Then** the full gate chain runs unchanged
- **Given** the review stage **When** a docs-only story reaches it **Then** the (non-adversarial) review still runs

**Technical Notes**: TDD in `controller/tests/`. Classification from the build stage's reported diff (or `git diff --name-only` in the worktree — deterministic, not agent-reported). Touches `controller/src/sdlc/build.py` stage flow; ledger write via existing stage-status machinery.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: None
**Risk Level**: Medium

##### Story 27.2-002: Adversarial slot risk tiering
**User Story**: As FX, I want the adversarial reviewer to run on Sonnet for low-risk stories while keeping its Opus floor for high-risk/large ones so that the most expensive pinned dispatch is paid only where the risk justifies it
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a low-risk story (points below threshold, no high-risk flag) **When** the adversarial slot dispatches a Claude reviewer **Then** it runs on **sonnet**
- **Given** a high-risk or large story **When** the slot dispatches **Then** it runs on **opus** (floor preserved — tiering can never downgrade high-risk)
- **Given** the slot resolves to the Codex CLI backend (`config/adversarial-reviewers.yaml`) **When** tiering applies **Then** the decision is skip/keep per 27.2-001's classification (no model choice exists for external reviewers)
- **Given** `sdlc status` **Then** the tier used is visible per story

**Technical Notes**: `controller/src/sdlc/model_routing.py` (adversarial pin becomes a floor tierable downward for low-risk only), `role_routing.py:261-438`, `adversarial.py`. TDD in `controller/tests/`.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 27.2-001 (consumes the change-class/risk classification)
**Risk Level**: Medium

##### Story 27.2-003: fix-issue mirror — docs-only issues skip coverage and E2E
**User Story**: As FX, I want fix-issue to skip the coverage and E2E phases for docs-only issues so that the interactive path applies the same risk tiering as the controller
**Priority**: Should Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** an issue whose build phase touched only docs-pattern files **When** the gate chain runs **Then** Phases 5 (coverage) and 7 (E2E) are skipped with the skip recorded in the run log and summary
- **Given** any code file in the diff **When** the gate chain runs **Then** all phases execute unchanged
- **Given** the skip **Then** the review phase (Phase 6) still runs

**Technical Notes**: Deterministic file-pattern check in the orchestrator instructions (`git diff --name-only` on the issue branch), using the same patterns as 27.2-001 — reference one shared pattern list, don't duplicate it.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 27.2-001 (shared docs-pattern definition), 27.1-001/27.1-004 (same file)
**Risk Level**: Low

### Feature 27.3: Structural Controller Optimizations

#### Stories

##### Story 27.3-001: Deterministic coverage pre-check + controller-owned PR creation
**User Story**: As FX, I want the controller to run the coverage check itself and open the PR deterministically so that the coverage agent (18% of story cost) is dispatched only when there is a real gap to fill
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a successful build stage **When** the controller runs the project's test + coverage command in the worktree **Then** a result of tests-green AND coverage ≥ threshold (default 90%) skips the coverage-agent dispatch, recorded in the ledger with `skip_reason`
- **Given** coverage below threshold or failing tests **When** pre-checked **Then** the coverage agent dispatches exactly as today, with the pre-check numbers included in its prompt
- **Given** any story (skipped or not) **When** the coverage stage completes **Then** the PR/MR exists — created by deterministic controller code (`gh pr create` / `glab` equivalent via the Epic-22/23 adapter), no longer by the agent
- **Given** the gate criterion **Then** it is unchanged: 90% threshold, enforced deterministically

**Technical Notes**: TDD in `controller/tests/`. Coverage command resolution must reuse the existing per-repo config the coverage prompt already references. PR creation via the code-host adapter (Epic-22) keeps GitLab parity. `build.py` stage flow + `coverage-gate-prompt.md` trimmed of PR-creation instructions.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 27.1-003 (edits coverage-gate prompt; land the shared/shrunk version first)
**Risk Level**: High

##### Story 27.3-002: Story-section injection into build/coverage prompts
**User Story**: As FX, I want the controller to embed the story's own markdown section into the build prompt so that every build agent (58% of story cost) stops burning turns re-reading and searching the full epic file
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the discovery stage has parsed the epics **When** `render_build_prompt` renders **Then** the story's own section (title through Risk Level) is embedded verbatim, replacing the "Read {epic_file} and find the story" instruction
- **Given** a story section exceeding a size cap **When** rendered **Then** the renderer falls back to today's read-it-yourself instruction (no truncated specs ever injected)
- **Given** the coverage prompt **When** rendered **Then** it receives the same treatment
- **Given** the full controller test suite **Then** prompt-renderer tests cover both the injection and fallback paths

**Technical Notes**: `controller/src/sdlc/build.py:2825-2914` (`render_build_prompt`, replacing the instruction at `:2905`) and the coverage renderer. Section extraction should reuse the discovery-stage epic parsing rather than re-parsing.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: None
**Risk Level**: Medium

##### Story 27.3-003: Pre-baked review packet
**User Story**: As FX, I want a deterministic `sdlc review-packet` artifact (PR meta, changed files, diff, test/coverage output) embedded into review prompts so that reviewers stop re-deriving their inputs with `gh pr view/diff/checkout` round-trips
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a story at the review stage **When** the controller dispatches the reviewer **Then** the prompt embeds a packet built by new `controller/src/sdlc/review_packet.py` (also exposed as a Typer verb), size-capped with fallback to today's fetch-it-yourself instructions
- **Given** the packet **When** the adversarial slot dispatches **Then** it reuses the same packet
- **Given** `review-gate-prompt.md` (both build-stories and fix-issue variants) **Then** the `gh pr view/diff/checkout` instructions are replaced by packet consumption (fallback path retained)
- **Given** a review smoke on a real PR **Then** the reviewer's verdict schema validates and the transcript shows no `gh pr diff`/`checkout` calls on the happy path

**Technical Notes**: TDD in `controller/tests/`. Use the code-host adapter for GitHub/GitLab parity. Cap the embedded diff; oversized diffs fall back rather than truncate silently.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 27.1-003 (shared review prompt); should land before or with 27.2-002 (adversarial reuses the packet)
**Risk Level**: Medium

##### Story 27.3-004: Ledger stall telemetry + workflow doc alignment
**User Story**: As FX, I want rate-limit backoff time recorded as its own ledger dimension so that stage durations stop being polluted by quota stalls and outlier runs are diagnosable at a glance
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the controller enters a rate-limit wait (`_rate_limit_wait`) **When** the run proceeds **Then** the waited seconds are recorded per stage/story in the ledger (event or column) distinct from agent runtime
- **Given** `sdlc status`/dashboard **When** a run had stalls **Then** stall time is visible separately from stage duration
- **Given** `WORKFLOW-v2.md` **Then** the gating description reflects Epic-27's tiered gates and the concurrency doc/code drift is fixed (doc says 3, code default is 5 at `build.py:482`)

**Technical Notes**: TDD in `controller/tests/`. Hook into the existing `_rate_limit_wait`/`sleep_fn` path (`build.py:597-728`) — injection-friendly by design. Ledger migration via the `_migrations` table.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: None
**Risk Level**: Low

## Verification & Exit Measurements

After all stories land (and a comparable batch has run):
1. Re-run the three BASELINE.md measurements (ledger durations, stage-log usage aggregation, transcript model-mix scan).
2. Success: Opus share of interactive tokens materially down from 94%; controller cost/story down vs $9; merge-gate pass rate and bugfix-loop rate not regressed; docs-only stories show `skip_reason` entries instead of coverage/adversarial dispatches.
3. Smokes during the epic: one docs-only story (gate skips), one small code story (coverage pre-check + review packet + controller-opened PR), one LOW-complexity `fix-issue` run (sonnet routing visible in the transcript).
