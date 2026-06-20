# Epic 14: Cost & Model Governance

> **Status: PLANNED** — created 2026-06-20; revised 2026-06-20 for the **Claude Max subscription**
> billing model. Inspired by the token-economics patterns in the external
> [affaan-m/ECC](https://github.com/affaan-m/ECC) `the-longform-guide.md` (model selection by task,
> thinking-token caps, early compaction). Distinct from Epic-11, which makes cost *observable*:
> this epic makes consumption *enforceable* and routes models by task.
>
> **Billing-model note**: The controller reads `total_cost_usd` verbatim from the
> `claude -p --output-format json` envelope (`dispatch.py:184`) — an **API list-price equivalent**
> computed from token usage. On a **Claude Max 20x subscription** (flat monthly fee, no per-token
> billing) that dollar figure is **notional**, not actual spend. So the real governance primitive
> here is **tokens** and the **Max plan's rate-limit windows** (5-hour rolling + weekly cap) — the
> thing that actually halts an overnight batch — not dollars.

## Epic Overview

**Epic ID**: Epic-14
**Description**: The controller tracks per-stage token usage and (notional) cost in the ledger and
surfaces it on the dashboard, but it never *acts* on consumption: there is no per-run ceiling, no
pause when usage runs away, no awareness of the subscription's rate-limit windows, and every stage
is dispatched to the same model regardless of difficulty (a trivial coverage top-up uses the same
model as an architecture-heavy build). This epic adds a **token-first** budget gate that
pauses/aborts a run when accrued tokens cross a ceiling, **rate-limit/quota awareness** for the Max
plan windows, a pre-dispatch usage estimate, model-tier routing per stage/complexity
(Haiku/Sonnet/Opus), and a thinking-token cap — turning consumption from a number on a dashboard
into a control the controller respects.

**Business Value**: FX runs long unattended overnight batches, increasingly several in parallel, on
a **Claude Max 20x subscription**. The real failure mode isn't a runaway *dollar* bill (there is
none — it's a flat fee) but **exhausting the plan's rate-limit quota mid-batch**, leaving the run
throttled or stalled overnight. A token-first ceiling plus rate-limit awareness keeps a stuck
bugfix loop or a runaway stage from burning the quota, and model routing stretches that quota
further by reserving the strongest model for the few stages that need it.

**Success Metrics**:
- A run with `--budget` (in **tokens**) set **never exceeds it**: the controller pauses or aborts
  (resumably) once accrued tokens cross the ceiling, verified by test. The dollar figure is shown
  only as a **labelled notional** reference ("API-equivalent, not billed on subscription").
- The controller **tracks consumption against the Max rate-limit windows** (5-hour rolling +
  weekly) and warns/pauses **before** throttling stalls a batch, rather than discovering it mid-run.
- Operators see a **pre-dispatch usage estimate** per stage and can skip/abort before spend.
- Simple stages route to a **cheaper model** and high-risk/complex stages to a stronger one,
  measurably stretching quota versus all-one-model on a representative run.
- The thinking-token cap is honored, reducing hidden per-request thinking tokens without degrading
  gate pass rates.

## Epic Scope

**Total Stories**: 6 | **Total Points**: 23 | **MVP Stories**: 0 (roadmap — Should Have)

## Out of Scope (Non-Goals)

- **Cost *display* / dashboards** — owned by Epic-11 (11.1-003 running token/cost, dashboard
  panels). This epic consumes that accrual; it does not re-render it.
- **Provider abstraction beyond Claude.** Routing chooses among Claude tiers (and any
  `SDLC_AGENT_CMD` override); building a multi-provider gateway is not in scope.
- **Changing the result contract.** Model selection and budgeting must work *with* the existing
  `<<<RESULT_JSON>>>` envelope and schema validation, unchanged.
- **Hard real-time billing accuracy.** Estimates are guidance; the authoritative figure remains
  the `--output-format` usage totals reconciled at stage completion.

## Features in This Epic

### Feature 14.1: Budget & Quota Enforcement

Make token consumption — and the subscription's rate-limit quota — a ceiling the controller
respects, not just a number it reports.

#### Stories

##### Story 14.1-001: Per-run token budget gate (with notional-$ display)
**User Story**: As FX leaving a batch running overnight on a Max subscription, I want to set a
**token** ceiling so that the controller pauses or aborts the run when accrued tokens cross it,
rather than burning unbounded quota on a stuck loop.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `sdlc build --budget=<tokens>` (or per-repo config) **When** the run's accrued tokens
  (from the Epic-11 11.1-003 ledger accrual) cross the ceiling **Then** the controller stops
  dispatching further stages and records the reason in the ledger. The **primary unit is tokens**;
  a `$`-denominated budget is accepted but treated as a convenience converted to the notional
  API-equivalent (not real spend on a subscription).
- **Given** the ceiling is hit mid-run **When** the controller stops **Then** in-flight work is
  finished or cleanly parked (no discarded committed work — R10 holds) and the run is
  **resumable** (`sdlc resume`) once the budget is raised.
- **Given** no `--budget` is set **When** a run executes **Then** behavior is unchanged from today
  (no ceiling).
- **Given** a `--budget` with a `pause` vs `abort` policy **When** the ceiling is hit **Then** the
  configured policy is honored (pause → NEEDS_ATTENTION-style hold; abort → terminal stop).
- **Given** the dollar figure is shown anywhere (status/dashboard) **When** rendered **Then** it is
  **labelled notional** — e.g. "$X (API-equivalent, not billed on subscription)" — so it is never
  mistaken for actual spend. (Dashboard renders the label; Epic-11 owns the rendering surface.)

**Technical Notes**: Reads the running token accrual the ledger exposes (Epic-11 11.1-003);
enforce in the `run_build` cohort loop in `controller/src/sdlc/build.py` between stage dispatches.
The `cost_usd` column already comes from `total_cost_usd` in the agent envelope
(`dispatch.py:184`) — keep storing it, but treat it as notional, not the budget primitive. Reuse
the resume machinery (Epic-10) so a budget pause resumes like any interruption.

**Definition of Done**:
- [ ] `--budget` token ceiling enforced between stage dispatches ($ accepted, converted to notional)
- [ ] Pause/abort policy honored; committed work preserved; run resumable
- [ ] No-budget path unchanged
- [ ] `$` shown as labelled-notional wherever surfaced (coordinate with Epic-11 rendering)
- [ ] Tests: ceiling crossed → stop + resume; policy variants
- [ ] Documented in `docs/controller-architecture.md`

**Dependencies**: Epic-11 11.1-003 (running token accrual); Epic-10 resume
**Risk Level**: Medium

##### Story 14.1-003: Max rate-limit / quota awareness with automatic resume
**User Story**: As FX running unattended overnight batches on a Claude Max 20x plan, I want the
controller to detect rate-limit exhaustion and **automatically resume when the window resets** —
without me restarting anything — so that hitting the 5-hour limit pauses the batch rather than
killing my night's work.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the controller is dispatching agents **When** the Max plan's rate-limit signal is
  available (rate-limit headers / `429` `retry-after` surfaced by `claude -p`, or a configured
  5-hour-rolling + weekly budget) **Then** the controller tracks remaining quota for the active
  window and surfaces it (status/ledger).
- **Given** the quota is exhausted or near a configurable threshold **When** the controller would
  dispatch the next stage **Then** it stops dispatching, records a distinct **`PAUSED` /
  `RATE_LIMITED`** state (NOT `NEEDS_ATTENTION` — this is waiting for *time*, not human attention),
  and computes the window-reset time.
- **Given** a run is `PAUSED` for rate limits and the reset is within a **configurable max-wait
  cap (default ≈ one window, ~5h)** **When** the controller is still running **Then** it **waits
  in-process** (sleeping with a periodic countdown log) and **automatically resumes the same run**
  when the window reopens — no manual `sdlc resume` required. The per-agent dispatch timeout does
  not interfere (it bounds the agent subprocess, not the controller's wait).
- **Given** the reset is **beyond** the max-wait cap (e.g. a weekly cap that resets days away)
  **When** the controller pauses **Then** it does **not** hold the process indefinitely — it
  leaves the durable `PAUSED` state and exits, so `sdlc resume` (or a scheduled wake) continues it
  later.
- **Given** the process is interrupted (machine sleep, crash) while `PAUSED` **When** `sdlc resume`
  runs after the window has reset **Then** the run continues cleanly from where it paused
  (committed work preserved — R10).
- **Given** a hard `429` mid-stage **When** it occurs **Then** the controller honors `retry-after`
  as a short backoff and treats it as a recoverable pause, **not** a stage `FAILED` (so a throttle
  never burns a bugfix attempt).
- **Given** no rate-limit signal is available (API-key auth, or signal absent) **When** a run
  executes **Then** behavior degrades gracefully to today's (no quota gating), with a logged note.

**Technical Notes**: This is the **real** overnight failure mode on a subscription — dollars are
flat, quota is finite. Today there is **zero** rate-limit handling: a limit hit surfaces as a
non-zero `claude -p` exit (or the 1h dispatch timeout) → `AgentDispatchError` → the bugfix loop
re-dispatches (also throttled) → the story is parked `NEEDS_ATTENTION`/`FAILED` and the run dies.
This story replaces that with: detect the signal (rate-limit headers / `429` `retry-after` from the
agent envelope or stderr in `dispatch.py`, else a configured window budget tracked from the
11.1-003 accrual); introduce a `PAUSED`/`RATE_LIMITED` run+story state distinct from
`NEEDS_ATTENTION`; auto-wait-and-continue within the cap, else durably park for `sdlc resume`.
Reuse the Epic-10 resume machinery for the park-and-resume path. The new state must render in
`sdlc status` and the dashboard (coordinate with Epic-11 11.2-009 status labels). Window reset
times are approximate — document the heuristic; exact Max limit values are not hardcoded
(configurable, sane defaults).

**Definition of Done**:
- [ ] Quota tracked against a rolling window (rate-limit signal or configured budget)
- [ ] Distinct `PAUSED`/`RATE_LIMITED` state (separate from `NEEDS_ATTENTION`), surfaced in status + dashboard
- [ ] In-process auto-wait + auto-resume when reset is within the configurable max-wait cap
- [ ] Beyond the cap: durable park; `sdlc resume`/scheduled wake continues; survives process death
- [ ] `429`/`retry-after` = short backoff, never a stage `FAILED`/bugfix burn
- [ ] Graceful no-signal degradation, logged
- [ ] Tests: synthetic limit → auto-wait+resume within cap; beyond-cap park+resume; interrupted-while-paused resume
- [ ] Documented in `docs/controller-architecture.md`

**Dependencies**: Epic-11 11.1-003 (token accrual); Epic-10 resume machinery; coordinates with Epic-11 11.2-009 (status rendering)
**Risk Level**: Medium

##### Story 14.1-002: Pre-dispatch cost estimate and warning
**User Story**: As FX, I want an estimated cost per stage before it is dispatched so that I can
see (and optionally gate) expensive work before spending on it.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a stage is about to be dispatched **When** the controller prepares it **Then** it
  computes an estimate (from prompt size + the stage's typical usage) and records/surfaces it.
- **Given** an estimate exceeds a configured per-stage threshold **When** the stage would run
  **Then** the controller warns and (in `--auto`) proceeds or (interactively) can gate.
- **Given** the stage completes **When** the actual usage is known **Then** estimate-vs-actual is
  reconciled in the ledger for future calibration.

**Technical Notes**: Lightweight heuristic (token count of the assembled prompt × stage factor);
calibrate against historical per-stage usage already in the ledger. No external pricing calls —
use a configurable price table.

**Definition of Done**:
- [ ] Per-stage pre-dispatch estimate computed and recorded
- [ ] Over-threshold warning; gate option in interactive mode
- [ ] Estimate-vs-actual reconciliation persisted
- [ ] Tests for estimate + threshold behavior
- [ ] Documented

**Dependencies**: None (improves with 14.1-001)
**Risk Level**: Low

### Feature 14.2: Model Routing

Match the model to the task instead of paying top-tier for everything.

#### Stories

##### Story 14.2-001: Per-task model routing (Balanced default map)
**User Story**: As FX, I want each pipeline task dispatched to a model matched to its cognitive
load — not Opus for everything — so that I cut quota burn dramatically without lowering quality
where it actually matters.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the controller dispatches a stage **When** routing is on **Then** it passes a
  `--model` chosen from a configurable per-stage map whose **Balanced default** is:

  | Task / stage | Default model | Notes |
  |---|---|---|
  | `discovery` (parse epics → queue) | **Haiku** | structured extraction |
  | `build` | **Sonnet** | → **Opus** if the story is high-risk (`risk_gate`) or large (points ≥ threshold) |
  | `coverage` | **Sonnet** | tests need correctness |
  | `review` (senior-code-reviewer) | **Sonnet** | → **Opus** if high-risk |
  | `adversarial` (Epic-08 skeptic) | **Opus** | pinned — never downgraded |
  | `merge` | **Haiku** | mechanical: fetch/resolve/PR/merge |
  | `bugfix` (retry) | **escalates** | see 14.2-003 |

- **Given** a story flagged high-risk (Epic-08 `risk_gate`) or large (points ≥ a configurable
  threshold) **When** the `build`/`review` model is chosen **Then** it escalates that stage to
  Opus, while low-risk small stories stay on Sonnet/Haiku.
- **Given** the `adversarial` reviewer **When** it is dispatched **Then** it is **always** Opus
  (or the configured strong vendor) regardless of profile — the skeptic is never cheapened.
- **Given** a `SDLC_AGENT_CMD` override or an explicit per-stage `--model` flag **When** set
  **Then** it wins over the map (escape hatch preserved).
- **Given** routing is off / unconfigured **When** a run executes **Then** behavior is unchanged
  from today (CLI default model for all stages — i.e. all-Opus on the current setup).
- **Given** routing is in effect **When** results return **Then** the `<<<RESULT_JSON>>>` contract
  and schema validation are unchanged.

**Technical Notes**: Add a `--model` argument to the dispatch command in
`controller/src/sdlc/dispatch.py` (today `DEFAULT_AGENT_CMD` sets none → CLI default = Opus 4.8),
selected per stage in `build.py` (`_dispatch_stage`/`_render_stage_prompt` already key off the
stage name). Reuse the Epic-08 `risk_gate.py` signal and `story.points` from `discovery.py` for
the build/review escalation. The map + risk/points thresholds live in controller config with a
per-repo override; ship **Balanced** as the default profile (with Quality-first / Quota-max as
documented alternatives). Validate with the Epic-18 eval harness that Sonnet-on-build holds quality
versus all-Opus before trusting it on high-stakes repos.

**Definition of Done**:
- [ ] `--model` selected per stage from the configurable map; Balanced is the shipped default
- [ ] `build`/`review` escalate to Opus on `risk_gate` match or points ≥ threshold; `adversarial` pinned Opus
- [ ] `SDLC_AGENT_CMD`/per-stage `--model` override precedence preserved
- [ ] Routing-off = unchanged (CLI default) behavior
- [ ] Result contract unchanged (test)
- [ ] Tests for map selection + risk/points escalation + adversarial pin; documented in `docs/controller-architecture.md`

**Dependencies**: None (escalation uses Epic-08 risk signal; validate with Epic-18 eval)
**Risk Level**: Medium

##### Story 14.2-002: Thinking-token cap and early-compaction config
**User Story**: As FX, I want a configurable thinking-token cap (and earlier compaction) on
dispatched agents so that hidden per-request thinking cost is bounded on long runs.
**Priority**: Could Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** a configured thinking-token cap **When** an agent is dispatched **Then** the cap is
  applied (e.g. `MAX_THINKING_TOKENS`), and the value is recorded for the run.
- **Given** no cap is configured **When** an agent runs **Then** behavior is unchanged (default
  thinking budget).
- **Given** the cap is applied **When** stages run **Then** gate pass rates are not measurably
  degraded on a representative run (sanity check, not a hard gate).

**Technical Notes**: Surface the env/flag through `dispatch.py`. Pair with an early-compaction
setting where applicable. Smallest story in the epic.

**Definition of Done**:
- [ ] Thinking-token cap configurable and applied at dispatch; recorded per run
- [ ] No-cap path unchanged
- [ ] Tests for cap application
- [ ] Documented

**Dependencies**: None
**Risk Level**: Low

##### Story 14.2-003: Cheap-first dispatch with model escalation on retry
**User Story**: As FX, I want a stage that fails on its default (cheaper) model to be retried on a
stronger tier so that I pay for Opus only when a stage is actually stuck — not on the common
passing path.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a stage runs at its mapped (cheaper) tier and **fails** into the bugfix loop **When**
  the controller retries **Then** each bugfix attempt **escalates the model one tier**
  (e.g. Haiku→Sonnet→Opus), capped at the strongest tier, rather than retrying on the same model
  that just failed.
- **Given** a stage that passes first time **When** the run proceeds **Then** no escalation
  occurs — the cheap path is the common path (this is where the quota saving comes from).
- **Given** a stage already mapped to Opus **When** it fails **Then** escalation is a no-op
  (already top tier) and the existing bounded bugfix budget (`MAX_BUGFIX_ATTEMPTS`) is unchanged.
- **Given** the escalation **When** it happens **Then** the model used per attempt is recorded
  (ledger/transcript) so the eval harness (Epic-18) can see cheap-first's success rate.

**Technical Notes**: Hooks the bugfix loop in `build.py` (`_run_story` / `_run_bugfix`) — the
retry path already exists (`MAX_BUGFIX_ATTEMPTS`, bugfix-seq); this adds a tier bump to the model
chosen for the retry dispatch. Pairs with 14.2-001 (the base map) and complements Epic-12 12.1-001
(envelope re-ask) — an envelope re-ask can also escalate. This is the highest-leverage quota lever:
common path stays cheap, Opus power arrives exactly when needed.

**Definition of Done**:
- [ ] Bugfix retry escalates the model one tier per attempt, capped at top tier
- [ ] First-pass success uses the cheap tier (no escalation); bugfix budget unchanged
- [ ] Per-attempt model recorded for eval visibility
- [ ] Tests: fail-then-escalate, top-tier no-op, pass-no-escalation
- [ ] Documented

**Dependencies**: 14.2-001 (base map); complements Epic-12 12.1-001
**Risk Level**: Medium

## Story Dependencies (within Epic-14)

```
14.1-001 (token budget gate) ── needs Epic-11 11.1-003 (token accrual) + Epic-10 resume
14.1-003 (rate-limit/quota)  ── needs Epic-11 11.1-003; shares pause/resume path with 14.1-001
14.1-002 (usage estimate)      independent (improves with 14.1-001)
14.2-001 (per-task routing)  ── independent (build/review escalation uses Epic-08 risk_gate)
14.2-003 (retry escalation)  ── needs 14.2-001; hooks the bugfix loop (complements Epic-12 12.1-001)
14.2-002 (thinking cap)        independent
```

- **Cohort 1** (no intra-epic deps): 14.1-001, 14.1-002, 14.1-003, 14.2-001, 14.2-002 — all touch
  `dispatch.py`/`build.py` (and 14.1-001/14.1-003 share the pause/resume path), so serialize the
  ones that edit the same dispatch builder.
- **Cohort 2**: 14.2-003 (needs the 14.2-001 base map).

## Epic Complete When

- A `--budget` (token) run never exceeds its ceiling: it pauses/aborts resumably with committed
  work preserved; any `$` shown is labelled notional.
- The controller tracks Max rate-limit/quota consumption and, on exhaustion, enters a distinct
  `PAUSED`/`RATE_LIMITED` state and **auto-resumes when the window resets** (in-process within a
  configurable cap, else durably parked for `sdlc resume`); `429`/`retry-after` is a backoff, never
  a stage failure.
- Operators see per-stage usage estimates before dispatch and can gate expensive work.
- Stages route to model tiers by a Balanced per-task map (Haiku merge/discovery, Sonnet
  build/coverage/review, Opus on high-risk/large builds + the pinned adversarial skeptic),
  stretching quota on simple work, with the result contract unchanged.
- Cheap-first dispatch escalates the model one tier per bugfix retry, so Opus is paid for only when
  a stage is actually stuck.
- The thinking-token cap is honored on a representative run without degrading gate outcomes.
