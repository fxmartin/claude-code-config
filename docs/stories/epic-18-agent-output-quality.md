# Epic 18: Agent Output Quality — Evaluation & Simplicity

> **Status: PLANNED** — created 2026-06-20. Inspired by the external
> [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) YAGNI toolkit — but only
> the parts we *don't* already have. Ponytail's philosophy (smallest-diff, "best code is the code
> you never wrote") is already baked into our `CLAUDE.md` (Surgical Changes + Complexity Check,
> adapted from Karpathy). What we lack, and this epic adds, is: (1) a way to **measure** agent
> output quality, and (2) a way to **enforce** simplicity in the *autonomous* pipeline where no
> human applies the complexity-check.

## Epic Overview

**Epic ID**: Epic-18
**Description**: We change agent prompts, swap models (Epic-14 routing), add skills, and tweak
schemas, but we have **no way to know whether any of it helped or hurt** — there is no evaluation
harness, no A/B, no regression baseline (the gap flagged during the ECC analysis). Separately, our
`CLAUDE.md` preaches "smallest reasonable diff / would a senior engineer say this is
overcomplicated?", but that check assumes a human reviewer — in an unattended `sdlc build` there
is none, and agents reliably over-build. This epic delivers two complementary capabilities: a
**reproducible evaluation/benchmark harness** that scores agent output (LOC, tokens, cost, time,
quality) on real tickets, and an **over-engineering review lens** that flags/strips over-built
code on each story's diff inside the pipeline.

**Business Value**: Evaluation turns prompt/model/skill changes from guesswork into measured
decisions — including validating that Epic-14's cheaper-model routing doesn't quietly degrade
quality, and catching agent-quality regressions before they ship across unattended batches. The
simplicity lens cuts the over-build tax (ponytail measured LOC −54% / tokens −22% / cost −20% on
an over-build-prone agentic benchmark) exactly where it bites: autonomous runs with no human to
say "that's too much."

**Success Metrics**:
- A single command reproduces an **agentic eval** that scores agent output on a fixed ticket set
  (LOC, tokens, cost, time, and a quality/safety check) and emits a comparable scoreboard.
- Two variants (prompt A vs B, or model tier A vs B) can be **compared on the same tickets**, and
  a stored **baseline flags regressions** when a change makes output worse.
- The over-engineering lens, on a representative diff, produces an accurate **delete-list** of
  over-built code with a low false-positive rate (it stays quiet on already-minimal code).
- Running the simplicity lens in the autonomous pipeline measurably **reduces net LOC** on
  over-build-prone stories without lowering gate pass rates.

## Epic Scope

**Total Stories**: 4 | **Total Points**: 18 | **MVP Stories**: 0 (roadmap — Should Have)

## Out of Scope (Non-Goals)

- **Importing ponytail's ruleset / philosophy prose or cross-harness adapters.** Our `CLAUDE.md`
  already encodes the YAGNI/surgical-diff/complexity-check principles; we are not duplicating them.
- **A general ML/experimentation platform.** The harness is a lightweight, inspectable eval over
  real tickets (promptfoo-style), not a hosted experiment-tracking service.
- **Blocking merges on raw LOC.** Fewer lines is a signal, not a hard gate — the lens flags
  over-engineering for routing/simplification, it does not fail a build for being "too long".
- **Re-litigating the result contract or stages.** Both capabilities work *with* the existing
  `<<<RESULT_JSON>>>` contract and 4-stage pipeline.

## Features in This Epic

### Feature 18.1: Evaluation & Benchmark Harness

Make agent-output quality measurable, comparable, and regression-guarded.

#### Stories

##### Story 18.1-001: Reproducible agentic eval harness
**User Story**: As FX tuning the framework, I want a one-command eval that scores agent output on a
fixed set of real tickets so that I can see the LOC/token/cost/time/quality impact of a change
instead of guessing.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a fixed ticket set against a real sample repo **When** the eval runs (e.g.
  `npx promptfoo eval -c <config>` or an `sdlc eval` wrapper) **Then** it drives the agent
  headlessly, scores each result on `git diff` (LOC delta), token usage, cost (notional),
  wall-time, and a quality/safety check (tests pass / no obvious breakage), and emits a scoreboard.
- **Given** the same inputs **When** the eval is re-run **Then** results are reproducible within
  expected model variance (config + ticket set + seed/`n` runs are versioned in-repo).
- **Given** the eval **When** it executes **Then** it runs against a sample target (not the
  framework repo itself) and never mutates `main` or opens PRs — it scores diffs in isolation.

**Technical Notes**: Model on ponytail's `benchmarks/promptfooconfig.yaml` pattern (real headless
agent editing a template repo, scored on `git diff`, n runs). Reuse the controller's token/cost
extraction (`dispatch.py` envelope `usage`/`total_cost_usd`) so eval metrics match ledger metrics.
Keep the harness in a `benchmarks/` or `eval/` dir with the ticket set + config versioned.

**Definition of Done**:
- [ ] One-command agentic eval scoring LOC/tokens/cost/time/quality on a fixed ticket set
- [ ] Versioned config + tickets + sample target; reproducible within model variance
- [ ] Runs in isolation (no `main` mutation / PRs)
- [ ] Scoreboard output (table/JSON); documented in a `docs/evaluation.md`
- [ ] Tests for the scoring/aggregation logic (not the live model)

**Dependencies**: None
**Risk Level**: Medium

##### Story 18.1-002: Variant comparison and regression baselines
**User Story**: As FX, I want to compare two variants (prompt A vs B, or model tier A vs B) on the
same tickets and store a baseline so that I can tell whether a change is an improvement and get
alerted when a later change regresses quality.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** two variants (different prompt/skill/model) **When** the eval runs both on the same
  ticket set **Then** it produces a side-by-side delta (per metric) and a clear better/worse/neutral
  verdict per ticket and overall.
- **Given** a committed **baseline** scoreboard **When** a new eval runs **Then** it flags metrics
  that regressed beyond a configurable tolerance (e.g. quality down, or tokens/LOC up materially).
- **Given** a variant that wins **When** results are reviewed **Then** the comparison is recorded
  (so a prompt/model decision is backed by data, not vibes) — directly usable to validate Epic-14
  model routing (does Haiku-on-coverage hold quality?).

**Technical Notes**: A thin layer over 18.1-001: parameterize the harness by variant, diff two
scoreboards, persist a baseline file in-repo. Keep "quality" measurable (tests/gates pass), not a
subjective LLM-judge unless explicitly added and itself validated.

**Definition of Done**:
- [ ] A/B comparison on a shared ticket set with per-metric deltas + verdict
- [ ] Baseline file; regression flagging beyond tolerance
- [ ] Comparison output persisted; documented
- [ ] Tests for delta + regression-flag logic

**Dependencies**: 18.1-001
**Risk Level**: Medium

##### Story 18.1-003: Wire a lightweight eval into CI for prompt/skill/schema changes
**User Story**: As FX, I want a small eval to run in CI when agent prompts, skills, or schemas
change so that a quality regression is caught on the PR, not in an overnight batch.
**Priority**: Could Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a PR that touches agent prompts/skills/schemas **When** CI runs **Then** a bounded
  eval subset (few tickets, low `n`) runs and reports the scoreboard vs the baseline.
- **Given** a material regression vs baseline **When** CI evaluates **Then** the job flags it
  (warn or fail, configurable) so the change is reviewed before merge.
- **Given** an unrelated PR **When** CI runs **Then** the eval is skipped (path-filtered) to keep
  CI fast and cheap.

**Technical Notes**: Reuse the Epic-02 CI patterns. Keep the CI subset tiny (cost/quota-aware —
this spends real quota on Max); the full eval stays a manual/local command. Path-filter to
agent-affecting changes only.

**Definition of Done**:
- [ ] CI job runs a bounded eval on prompt/skill/schema changes only
- [ ] Regression vs baseline flagged (warn/fail configurable)
- [ ] Path-filtered; cost/quota-bounded
- [ ] Documented in the CI + evaluation docs

**Dependencies**: 18.1-001, 18.1-002
**Risk Level**: Low

### Feature 18.2: Simplicity Enforcement

Operationalize the `CLAUDE.md` complexity-check inside the autonomous pipeline, where no human
applies it.

#### Stories

##### Story 18.2-001: Over-engineering review lens on each story's diff
**User Story**: As FX running unattended builds, I want a review pass that flags over-built code on
each story's diff so that the autonomous pipeline enforces "smallest reasonable diff" instead of
only preaching it in `CLAUDE.md`.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a story's diff at the review stage **When** the over-engineering lens runs **Then** it
  returns a structured **delete-list** — speculative abstractions, unused params/branches,
  hand-rolled code a stdlib/existing-dep/one-liner would cover, premature generality — each with a
  file/line and a one-line "why".
- **Given** findings exist **When** the controller processes them **Then** per policy it either
  **routes them to the simplify/bugfix path** (agent applies the cuts, gates re-run) or **records
  them as advisory** on the PR — configurable, defaulting to advisory so it never blocks shipping.
- **Given** an already-minimal diff **When** the lens runs **Then** it stays quiet (low
  false-positive rate) — no nitpicking code that is already lean.
- **Given** the lens is disabled (config/off) **When** a build runs **Then** behavior is unchanged
  from today.

**Technical Notes**: Implement as a review *dimension*, not a new stage — fold into the existing
review stage or the Epic-08 adversarial reviewer slot (it's a natural adversarial lens). Mirrors
ponytail's `/ponytail-review` "delete-list" output and our built-in `simplify`/`roast` skills, but
runs *in* the autonomous pipeline. Default advisory (don't gate shipping on style); the
route-to-simplify mode reuses the bounded bugfix loop. The eval harness (18.1) is how we'd verify
the lens actually reduces LOC without hurting gate pass rates.

**Definition of Done**:
- [ ] Over-engineering lens produces a structured delete-list (file/line + reason) on a story diff
- [ ] Policy: advisory (default) or route-to-simplify via the bounded bugfix path
- [ ] Low false-positive on already-minimal diffs (tested with lean + over-built fixtures)
- [ ] Disable switch; off = unchanged behavior
- [ ] Tests for finding extraction + policy routing; documented

**Dependencies**: None (verified by 18.1; may reuse the Epic-08 adversarial slot)
**Risk Level**: Medium

## Story Dependencies (within Epic-18)

```
18.1-001 (eval harness) ──> 18.1-002 (variant compare + baselines) ──> 18.1-003 (CI eval)
18.2-001 (over-engineering lens)   independent (verified by 18.1; may reuse Epic-08 slot)
```

- **Cohort 1** (no deps): 18.1-001, 18.2-001
- **Cohort 2**: 18.1-002 (needs 18.1-001)
- **Cohort 3**: 18.1-003 (needs 18.1-001 + 18.1-002)

> Cross-epic: 18.1 is the measurement layer for **Epic-14** (does cheaper-model routing hold
> quality?), **Epic-16** (do learned "instincts" help?), and any prompt change. 18.2 complements
> the built-in `simplify`/`roast` skills and the **Epic-08** adversarial gate.

## Epic Complete When

- A one-command agentic eval scores agent output (LOC/tokens/cost/time/quality) reproducibly on a
  fixed ticket set, with variant comparison and a regression-flagging baseline.
- A lightweight eval runs in CI on agent-affecting changes and flags regressions before merge.
- An over-engineering lens flags over-built code on each story's diff (advisory by default, or
  route-to-simplify), staying quiet on already-minimal code — and the eval harness confirms it cuts
  LOC without hurting gate pass rates.
