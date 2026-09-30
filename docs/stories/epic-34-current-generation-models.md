# Epic 34: Current-Generation Model Alignment

> **Status: NOT STARTED (0/8)** — authored 2026-09-30. Thesis: the controller
> routes Claude work through the bare aliases `haiku` / `sonnet` / `opus`
> (`model_routing.py`) and lets the Claude CLI decide what they mean, so the
> ledger records whatever the CLI resolved that day and no run is reproducible
> by model. Around that alias layer everything has drifted a generation: the
> cost table (`cost_estimate.py`) prices Sonnet at $3/$15 and Opus at $5/$25
> (2026-07) while Sonnet 5 is $2/$10 and Opus 5.5 is $4/$20, so budgets
> over-refuse and every `$` on the dashboard overstates; tier detection
> (`build._model_tier`) matches substrings and drops any id that is not
> haiku/sonnet/opus; the Codex and OpenCode registry entries, adapter comments
> and `docs/harness-adapters.md` still name `gpt-5.5` / `gpt-5.4-codex` while
> FX's `~/.codex/config.toml` pins `gpt-6-astra`; and the stage prompts were
> tuned for a generation whose defaults (thinking off unless asked, `high`
> effort, forced `tool_choice`) the new models no longer share — Opus 5.5
> defaults to `medium` effort, cannot disable thinking, and rejects forced
> tool use.
>
> **Model facts this epic is written against**: Opus 5.5 = `claude-opus-5-5`
> ($4/$20, cache read $0.20), Sonnet 5 = `claude-sonnet-5` ($2/$10), Haiku
> 4.5 = `claude-haiku-4-5` ($1/$5), Opus 5 = `claude-opus-5` ($5/$25, the
> fallback). All three target ids were **probed live on FX's plan on
> 2026-09-30** (`claude -p ok --model <id>` answered and was billed as that
> id), so the entitlement fallback in 34.1-002 guards the next launch, a plan
> change or a mid-run outage — not a missing model today. **There is no
> Sonnet 5.5.** Fable 5.1 is out of scope (2.5x Opus, `refusal` stop reason,
> different prompting).
>
> **Decisions locked 2026-09-30** (FX): three tiers, Opus 5.5 on top, no
> Fable tier; explicit ids are the new default but `opus`/`sonnet`/`haiku`
> in per-repo overrides keep resolving; no default id flips until a live
> `claude -p --model <id>` / `codex exec --model <id>` probe succeeds on FX's
> plan, and an entitlement failure degrades to the previous tier, never
> aborts a run; OpenAI defaults move to `gpt-6-astra` with per-stage routing
> staying opt-in; ids + prices + OpenAI defaults ship first (MVP), effort map,
> prompt audit and benchmark gate follow; the epic stays at 8 stories —
> Fable, Bedrock/Vertex ids and Qwen/local models are explicitly out.

## Epic Overview

**Epic ID**: Epic-34
**Description**: Make the controller state, price and tune for the models it
actually runs. Four features: (34.1) every Claude tier resolves to an explicit
current-generation model id, verified live before it becomes a default and
degrading to the previous generation on an entitlement failure, with tier
detection and the registry hygiene that depends on it; (34.2) the notional
cost table is keyed by model id at today's list prices and reports its own
vintage; (34.3) the Codex and OpenCode harnesses default to `gpt-6-astra`
end-to-end in registry, adapters and docs; (34.4) routing gains a per-stage
effort dimension, the stage prompts are audited for patterns written for the
previous generation, and a benchmark run on the Epic-31 eval harness records
the delta before the new defaults ship.
**Business Value**: Reproducible runs — a ledger row names the model that did
the work, not an alias. Honest money — the budget gate and dashboard figures
stop overstating by 20–40% on Sonnet/Opus stages, so budgets stop refusing
work they could afford. Cheaper, better defaults — Opus 5.5 costs 20% less
than Opus 5 and Sonnet 5 a third less than Sonnet 4.6, and effort tuned per
stage is the single biggest quality/cost lever the new generation exposes.
One place to move when the next generation lands.
**Success Metrics**:
- 100% of stage attempts in the ledger after cutover carry a full model id
  (`claude-*-N` / `gpt-*`), zero bare aliases.
- `cost_estimate` per-tier figures match the published list price ±0 for the
  three Claude tiers; the dashboard and `sdlc status` show the price-table
  vintage.
- A forced entitlement failure on `claude-opus-5-5` in a test lands the stage
  on `claude-opus-5` with a `warn` ledger event and the run finishes `DONE`.
- `rg 'gpt-5\.' controller/ scripts/ docs/` returns only CHANGELOG/story
  history.
- The benchmark gate (34.4-003) shows pass rate not below the pre-cutover
  baseline and blended cost per completed story at or below it, or the
  default flip is held with the delta recorded in `docs/optimization/BASELINE.md`.

## Epic Scope
**Total Stories**: 8 | **Total Points**: 24 | **MVP Stories**: 5 (14 pts)

## Features in This Epic

### Feature 34.1: Explicit Claude Model Identities

The tier ladder `HAIKU → SONNET → OPUS` (`model_routing.TIER_LADDER`) stays;
what each rung *means* becomes a controller decision recorded in one table,
not a CLI alias resolved at dispatch.

#### Stories

##### Story 34.1-001: Tier-to-model-id map with alias compatibility
**User Story**: As FX reading a ledger row, I want every routed stage to name
the exact model id it ran on (`claude-opus-5-5`, `claude-sonnet-5`,
`claude-haiku-4-5`) so that a run is reproducible by model and a price can be
attached to it, while my existing `.sdlc-model-routing.yaml` files that say
`opus` keep working.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the Balanced profile **When** a `build` stage dispatches **Then**
  `claude -p … --model claude-sonnet-5` is the argv and the ledger's
  `stage_attempts.model` reads `claude-sonnet-5`, not `sonnet`.
- **Given** a per-repo override `model_routing: {build: opus}` **When** the
  run resolves routing **Then** `opus` maps through the same table to
  `claude-opus-5-5` and the routing banner prints both (`build=opus →
  claude-opus-5-5`).
- **Given** a per-repo override pinning a full id (`build: claude-opus-4-8`)
  **When** the run resolves routing **Then** the id passes through untouched
  (today's escape hatch is preserved).
- **Given** cheap-first escalation (Story 14.2-003) **When** a stage retries
  one tier up **Then** it climbs the ladder by tier and dispatches the mapped
  id.

**Technical Notes**: Add `TIER_MODEL_IDS: dict[str, str]` beside `TIER_LADDER`
in `model_routing.py` as the single source of truth; resolve at the point the
tier leaves the routing layer (`build._resolved_stage_model` /
`fix_issue` model selection), so the profiles keep speaking in tiers. The
`rate_limit_probe` in `harnesses.yaml` (`claude -p ok --model haiku`) should
read the same table. `docs/controller-architecture.md` Epic-14 section and
the README routing table state the map and the vintage date.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: tier→id resolution, alias override, full-id passthrough,
      escalation climbs by tier, ledger row carries the id
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: None
**Risk Level**: Medium

##### Story 34.1-002: Live entitlement probe with previous-generation fallback
**User Story**: As FX, I want the controller to prove an id works on this
host before it becomes a run's default and to fall back to the previous
generation when it does not, so that the next model launch, a plan change or
a mid-run outage never aborts a run.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `claude -p ok --model claude-opus-5-5` returns non-zero with a
  model-not-found/entitlement error at preflight **When** the run starts
  **Then** the opus tier resolves to `claude-opus-5`, a `warn` `harness`
  event names the substitution, the routing banner shows it, and the run
  proceeds.
- **Given** the probe succeeds **When** stages dispatch **Then** the argv also
  carries `--fallback-model claude-opus-5` so a mid-run 404/overload degrades
  in place instead of failing the stage.
- **Given** a probe error that is a *rate limit* (Story 14.1-003 wording)
  **When** preflight evaluates it **Then** it is not read as an entitlement
  failure — the RATE_LIMITED path owns it.
- **Given** `sdlc doctor` **When** run **Then** a finding lists each tier's
  id and whether the probe succeeded, cached per host for 24h.

**Technical Notes**: Reuse the Story 31.1-001 harness preflight seam
(`harness_preflight_model_pin_unsupported`) rather than a new probe path; the
fallback table is `TIER_FALLBACK_IDS` next to `TIER_MODEL_IDS`
(`claude-opus-5-5 → claude-opus-5`, `claude-sonnet-5 → claude-sonnet-4-6`,
`claude-haiku-4-5 → claude-haiku-4-5`). Probe result cached under
`~/.local/state/sdlc/` so parallel cohorts do not each spend a call. Tests
inject the probe; never call the CLI.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: entitlement failure degrades and warns; rate-limit wording is not
      misclassified; `--fallback-model` present; doctor finding
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 34.1-001
**Risk Level**: Medium

##### Story 34.1-003: Tier detection and registry hygiene for explicit ids
**User Story**: As FX reading the dashboard and `sdlc status`, I want a full
model id to classify into its tier everywhere the controller groups by tier
(usage averages, cost estimates, escalation base), so that explicit ids do
not silently fall through as "unknown" and inflate the opus-equivalent
default.
**Priority**: Must Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** `build._model_tier("claude-sonnet-5")` **When** called **Then** it
  returns `sonnet`; `claude-fable-5-1` returns `fable` (not `opus`, not the
  raw id); a registry id (`gpt-6-astra`) returns the id unchanged.
- **Given** the historical usage average (`build.py` ~2770) **When** keyed by
  tier **Then** rows recorded under `claude-opus-4-8` and `claude-opus-5-5`
  fold into the same `opus` bucket.
- **Given** `templates/skill-template.md` and `templates/command-template.md`
  **When** read **Then** their `model:` example is `claude-sonnet-5`.

**Technical Notes**: Replace the substring loop with a lookup that tries
`TIER_MODEL_IDS` and `TIER_FALLBACK_IDS` values first, then the substring
rule as the compatibility tail. Keep the function's contract (unknown ids
pass through) so Codex ids are unaffected.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: each id class; templates asserted by a bats or pytest string check
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 34.1-001
**Risk Level**: Low

### Feature 34.2: Cost Table at Current List Prices

#### Stories

##### Story 34.2-001: Price table keyed by model id, stamped with its vintage
**User Story**: As FX deciding whether a budget refusal is real, I want the
notional `$` figures to use today's list price for the exact model that ran
and to say which price table produced them, so that Sonnet 5 and Opus 5.5
stages stop reading 20–50% too expensive.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `cost_estimate.MODEL_USD_PER_MILLION_TOKENS` **When** read **Then**
  it is keyed by model id with separate input/output rates
  (`claude-opus-5-5`: 4/20, `claude-sonnet-5`: 2/10, `claude-haiku-4-5`: 1/5,
  `claude-opus-5`: 5/25, `claude-sonnet-4-6`: 3/15) and carries a
  `PRICE_TABLE_VINTAGE = "2026-09-30"` constant; tier aliases resolve through
  34.1-001's map.
- **Given** a stage usage row with input/output/cache-read tokens **When**
  costed **Then** input and output are priced separately (cache reads at the
  published read rate) instead of the blended average.
- **Given** the dashboard run header and `sdlc status` **When** they show a
  `$` figure **Then** the vintage is shown beside it once (`$0.231 · prices
  2026-09-30`).
- **Given** an id with no price entry **When** costed **Then** the opus
  fallback applies and `sdlc doctor` warns once per id (`WARN Model pricing —
  gpt-6-astra has no list price; $ figures use the opus default`).

**Technical Notes**: The blended-average simplification dates from #427; the
ledger already stores in/out/cache tokens separately (Story 11.1-002), so
this is a table shape change plus one costing function. Budget-gate priors
(`DEFAULT_USD_PER_MILLION_TOKENS = 15.0`) stay for the routing-off path.
Document in `docs/controller-architecture.md` Epic-14 cost section.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: per-id pricing, alias resolution, cache-read rate, unknown-id
      fallback + doctor finding, vintage rendered on dashboard and status
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 34.1-001
**Risk Level**: Low

### Feature 34.3: OpenAI Harness Defaults

#### Stories

##### Story 34.3-001: Codex and OpenCode default to gpt-6-astra end-to-end
**User Story**: As FX routing a stage to Codex, I want the harness registry,
adapters, docs and the commented per-stage example to name the model I am
entitled to and run today (`gpt-6-astra`) so that a copied config works
first time and nothing in the repo still points at `gpt-5.x`.
**Priority**: Must Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** `controller/src/sdlc/config/harnesses.yaml` **When** read **Then**
  the codex comments and the commented `models:` example use `gpt-6-astra`;
  the opencode entry's `provider/model` example reads `openai/gpt-6-astra`.
- **Given** the default codex `command` (no `{model}` placeholder) **When** a
  stage dispatches **Then** behaviour is unchanged — Codex uses
  `~/.codex/config.toml`'s model; per-stage routing remains opt-in.
- **Given** a repo that opts in (`--model {model}` + `models:`) **When**
  preflight runs **Then** the Story 31.1-001 model-pin probe runs
  `echo hi | codex exec --model gpt-6-astra` once and refuses with the
  existing `harness_preflight_model_pin_unsupported` message on failure.
- **Given** `rg 'gpt-5\.[0-9]' controller/ scripts/ docs/` excluding
  `CHANGELOG.md` and `docs/stories/` **When** run **Then** it is empty.

**Technical Notes**: `docs/harness-adapters.md` has seven `gpt-5.5` mentions
and one `gpt-5.4-codex` (the ChatGPT-account 400 anecdote — keep the lesson,
update the id). `scripts/opencode-build-adapter.sh` line ~60 names
`openai/gpt-5.6`. A bats test pins the registry example so the next
generation is a one-line diff. Pricing for OpenAI ids is out of scope
(34.2-001's unknown-id fallback covers them).

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: registry/adapter string assertions; grep gate in bats
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: None
**Risk Level**: Low

### Feature 34.4: Behaviour Tuning for the New Generation

#### Stories

##### Story 34.4-001: Per-stage effort map in model routing
**User Story**: As FX paying for tokens, I want each stage to run at an effort
level chosen for its job (mechanical stages low, build high, review/merge
xhigh) and to be able to override it per repo like the model map, so that
Opus 5.5's `medium` default does not silently under-think review and Haiku
stages do not over-spend.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the Balanced profile **When** a stage dispatches on the claude
  harness **Then** the argv carries `--effort <level>` from a per-stage map
  (`discovery: low, docs: low, coverage: medium, build: high, review: xhigh,
  merge: medium, bugfix: high`) and the ledger `stage_attempts` row records
  the effort.
- **Given** `.sdlc-model-routing.yaml` with `effort: {review: max}` **When**
  routing resolves **Then** the override wins for that stage and the routing
  banner prints `review=claude-opus-5-5@max`.
- **Given** a tier whose model does not support the level (Haiku 4.5 has no
  `effort`) **When** dispatching **Then** the flag is omitted and a `debug`
  event says why — never a CLI error.
- **Given** cheap-first escalation **When** a retry climbs a tier **Then**
  effort climbs one level too (capped at `max`), and the escalation event
  names both.

**Technical Notes**: `claude -p` accepts `--effort <level>`; the Codex adapter
maps to `model_reasoning_effort` only if `codex exec` exposes it — otherwise
the flag is dropped for non-claude harnesses (capability declared in
`harnesses.yaml`, mirroring `rate_limit_aware`). Thread the value the same
way the routed `--model` decoration reaches `dispatch.resolve_agent_cmd`.
Levels per model from the Anthropic thinking/effort table; Fable excluded.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: default map, override, unsupported-level omission, escalation,
      ledger column, non-claude harness drop
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 34.1-001
**Risk Level**: Medium

##### Story 34.4-002: Prompt audit of stage prompts and plugin skills
**User Story**: As FX, I want the build/coverage/review/merge/bugfix prompts
and the plugin skills audited for instructions written for the previous model
generation, so that the new models are not steered by rules that now reduce
output quality (over-prescriptive step lists, thinking-tag rules, forced
tool-choice assumptions, "do not reason" clauses).
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the `/claude-api prompt-audit` procedure **When** run over
  `render_*_prompt` in `build.py`/`fix_issue.py`, `contracts._result_wrapper`
  and `plugins/autonomous-sdlc/skills/*/SKILL.md` **Then** a report in
  `docs/optimization/PROMPT-AUDIT-2026-09.md` lists each finding with
  `file:line`, pattern, why obsolete and confidence.
- **Given** the report **When** the diff is applied **Then** every existing
  prompt-contract test still passes and each removed rule is either covered
  by a keep-list entry or by a test proving the behaviour it protected
  survives (the Epic-26 RED/GREEN pressure tests).
- **Given** a rule that exists because of a recorded incident (e.g. the
  b8fdbc71 background-wait, the #740 no-pipeline wait) **When** audited
  **Then** it is kept and cited, never removed as "dated".

**Technical Notes**: The audit is a reading exercise, not a rewrite: expect
most of the wrapper's process rules to stay. Watch for the new generation's
known shifts — thinking always on (no "think step by step"), forced
`tool_choice` gone (result-block extraction must not rely on it), Opus 5.5
verbosity/progress-update behaviour between tool calls.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: prompt-contract suite green; new tests for any rule replaced by
      behaviour
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 34.1-001
**Risk Level**: Medium

##### Story 34.4-003: Benchmark gate before the defaults flip
**User Story**: As FX, I want the old and new tier maps measured on the same
eval ticket set before the new defaults ship, so that "cheaper and better" is
a recorded number, not an assumption.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `controller/eval/eval-config.yaml` **When** `sdlc eval` runs once
  with the pre-epic map (`--model-profile` pinned to the previous ids) and
  once with the new map + effort defaults **Then** both scoreboards land in
  `controller/eval/results/` and a comparison section in
  `docs/optimization/BASELINE.md` reports pass rate, bugfix rounds, blended
  cost per completed ticket and wall-clock, with `n`.
- **Given** the comparison **When** pass rate is below baseline or cost per
  completed ticket is above it **Then** the default flip PR is held and the
  section records the decision and the retuning tried.
- **Given** a green comparison **When** the flip merges **Then**
  `controller/eval/baseline.json` is updated to the new map's numbers.

**Technical Notes**: Epic-31 owns the harness (`GO-NO-GO-31.3-001.md` is the
template for the write-up). Keep `--n` small (2–3 per ticket) — this is a
regression check, not a study; state the power honestly as Epic-28 did.
Runs on FX's Max plan share the window with live builds — schedule when the
queue is idle.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: none beyond the eval harness's own; the write-up is the artefact
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 34.1-002, 34.2-001, 34.4-001
**Risk Level**: Low

## Non-Goals

- A Fable 5.1 tier (cost, `refusal` handling and prompting differ enough to
  be their own epic).
- Bedrock / Vertex / Foundry model ids and partner pricing.
- Qwen / local models and the OpenCode adapter beyond the id rename.
- Real spend accounting — both harnesses bill by subscription; `$` stays the
  notional API-equivalent signal Epic-14 defined.
- OpenAI per-stage routing on by default (stays opt-in per 34.3-001).
