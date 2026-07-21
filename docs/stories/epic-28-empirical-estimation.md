# Epic 28: Empirical Estimation and Telemetry Integrity

> **Status: PLANNED (0/7)** created 2026-07-19 from the 2026-07-19 cost dataset: a
> 76-story priced dataset built from this controller's own ledgers on local-code-bench
> (June 25 to July 18, 2026). Thesis: the factory should stop *estimating* from story
> points and start *calibrating* from its own ledger history. Points stay as a
> human-readable scope label; routing, budgets, and batch planning move to
> ledger-calibrated predictions.
>
> **Evidence (and its limits)**: On this dataset, story points do not predict cost. The
> medium (3 to 4 pts) and large (5 to 8 pts) bands differ by only 8 percent at the
> median ($9.13 vs $9.91), the single most expensive story ($43.24) was a 3-pointer, and
> 172 of 193 story-builds were assigned 3 or 5 by the discovery agent, so points carry
> almost no information. Cost is tail-driven, not size-driven: the expensive stories are
> the ones that hit review retries, bugfix loops, or long stalls, so rework probability
> matters more than scope. Yet points are load-bearing today: Epic-14 model routing
> escalates build/review to Opus on points at or above a threshold, and pre-dispatch
> estimates and budget gates consume the same signal, so routing keys model choice to
> noise. This is **n=76, single repo** evidence: strong enough to stop trusting points,
> not strong enough to ship a heavy model. Feature 28.2 starts crude (historical means by
> band + a few features) and earns complexity only as the calibrated error justifies it.
>
> **Telemetry note (what already shipped, and what this epic still owes)**: The two acute
> meter bugs this dataset surfaced were **already fixed in this repo before this epic was
> authored**. The re-ask no longer overwrites the original stage attempt's usage and
> output_path (Issue #480 defect 1, PR #482), and the recovery-row model column is now
> written, including the registry-harness case (Issue #480 defect 3 and Issue #483, PRs
> #482 and #484). So this epic does **not** re-fix those. What remains is the harder
> integrity work the dataset implied but those point-fixes did not deliver: a
> reconciliation job that backfills spend from the session logs where the ledger and logs
> still disagree (crashed and interrupted sessions, tracked in Issue #481, and any history
> orphaned before PR #482 landed), a ledger-vs-logs agreement check in `sdlc doctor`, and
> verification that the model column now populates for **every** stage on fresh runs with
> the historical NULLs backfilled or explicitly flagged. Calibration on top of
> unreconciled telemetry would train on lies, so these stories come first.

## Epic Overview

**Epic ID**: Epic-28
**Description**: The controller assigns each story a point value and then feeds that value
into decisions it is not fit to make: model-tier escalation (Epic-14 14.2-001), the
pre-dispatch cost estimate (14.1-002), and the batch/budget posture (14.1-001, 14.1-003).
The 2026-07-19 dataset shows points do not predict cost on this factory's own work, while
the real cost driver (rework: review retries, bugfix loops, stalls) is never predicted at
all. This epic replaces point-keyed estimation with **ledger-calibrated prediction**. It
first hardens the meter the predictor will train on (a reconciliation backfill from the
session logs, a ledger-vs-logs agreement doctor check, and verified per-attempt model
recording), then computes a per-story predicted-token and predicted-rework-probability
signal from ledger history, and finally switches the consumers (model routing, the budget
gate, pre-dispatch warnings, and the batch planner) from raw points to the calibrated
prediction. Points remain, demoted to descriptive scope metadata. A fourth concern runs
alongside: model routing must actually engage by default and announce itself when it does
not. The Balanced profile Epic-14 shipped silently defaulted to off (an unset profile
meant CLI-default-for-everything), so the entire prediction-to-routing chain is moot if
routing never turns on, and a cost-governance control that fails silent-and-expensive is
not a control.

**Business Value**: FX runs long unattended batches on a Claude Max subscription where the
binding constraint is the rate-limit window, and the stories that exhaust it are the
tail-risk ones, not the nominally large ones. Routing Opus by points spends the strongest
model on cheap work and starves the genuinely risky stories that a rework signal would
have caught. Calibrated prediction stretches the same quota further (Opus arrives when
predicted rework is high, not when a noisy point estimate is high), makes the budget gate
and batch planner honest about what a run will actually consume, and gives every future
optimization a trustworthy meter to measure against. The integrity work also restores the
~17 percent of spend the ledger was under-reporting, so cost dashboards and eval
comparisons (Epic-11, Epic-18) stop working from numbers that are quietly low.

**Success Metrics**:
- **Ledger-vs-logs agreement**: after the reconciliation backfill, the ledger's summed
  per-stage usage agrees with the session-log ground truth for at least a stated
  threshold of stage attempts (target: the residual disagreement is only genuinely
  unrecoverable cases, e.g. crashed sessions with no per-turn usage), and `sdlc doctor`
  reports the agreement rate so drift is visible going forward.
- **Model column populated**: on fresh runs, the `stages.model` column is non-NULL for
  every dispatched stage (primary and recovery), verified by test and by a doctor check;
  historical NULL rows are backfilled from the logs where possible and otherwise flagged,
  not silently zero.
- **Prediction quality measured honestly**: the predictor's error is reported as the
  **median absolute error of predicted vs actual tokens** (and the calibration curve for
  predicted rework probability) on a held-out slice of ledger history, tracked over time.
  The epic does not claim the predictor is "accurate"; it claims the error is measured and
  improving.
- **Routing no longer keyed to raw points**: model-tier escalation and the budget or
  batch decisions consume the calibrated prediction, and no consumer reads `story.points`
  as an escalation input (points remain only as descriptive metadata), verified by test.
- **Honest about power**: every metric above is reported with its sample size and the
  single-repo caveat, so the factory never mistakes n=76 calibration for a general law.

## Epic Scope

**Total Stories**: 7 | **Total Points**: 34 | **MVP Stories**: 0 (post-MVP roadmap epic)

## Out of Scope (Non-Goals)

- **Re-fixing the two point-defects.** The re-ask usage overwrite (Issue #480 defect 1,
  PR #482) and the recovery-row model column (Issue #480 defect 3 and Issue #483, PRs #482
  and #484) already shipped. This epic consumes those fixes; it does not redo them.
- **A heavyweight ML model.** Feature 28.2 is deliberately crude first (historical means
  by band + a small feature set + a reconciled residual). Gradient-boosted trees, learned
  embeddings, or any external training pipeline are out of scope until the crude
  predictor's measured error justifies more.
- **Removing story points.** Points stay as a human-readable scope label on stories,
  issues, and the dashboard. This epic removes points only as a *decision input* to
  routing/budget/estimation, not as a label.
- **Cost display and dashboards.** Rendering is owned by Epic-11. This epic corrects the
  accrual those panels read and adds prediction columns to the ledger; it does not
  re-render the dashboard beyond surfacing the new fields.
- **Changing the result contract.** Prediction, reconciliation, and model recording work
  with the existing `<<<RESULT_JSON>>>` envelope and schema validation, unchanged.
- **Provider abstraction beyond Claude tiers and the configured harness registry.**
  Routing still chooses among the Epic-14 tiers and Epic-20 harness registry.

## Features in This Epic

### Feature 28.1: Telemetry Integrity

Harden the meter before anything trains on it. The acute overwrite and model-column
point-defects already shipped (PRs #482, #484); these stories close the gap those fixes
left: reconcile the ledger against the session logs, make the agreement a health check,
and verify per-attempt model recording end to end.

#### Stories

##### Story 28.1-001: Ledger-vs-logs reconciliation backfill and doctor agreement check
**User Story**: As FX trusting the ledger as the cost record of truth, I want a
reconciliation pass that backfills per-stage usage from the session logs wherever the
ledger and logs disagree, and a `sdlc doctor` check that reports the agreement rate, so
that the meter the predictor trains on matches what actually ran instead of the roughly 17
percent it was under-reporting.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a completed run whose stage attempts have session logs under
  `.sdlc-state.db.logs/<run>/<story>-<stage>-<attempt>.log` **When** the reconciliation
  pass runs **Then** for each stage row it compares the ledger usage against the
  authoritative `{"type":"result"}` line in that attempt's log and, where the log carries
  usage the ledger is missing or the two disagree, writes the log-derived usage onto the
  correct attempt row (the original expensive session, not a recovery attempt).
- **Given** a crashed or interrupted session whose log has **no** terminal `result` line
  (Issue #481) **When** the pass runs **Then** it recovers the per-turn token counts by
  summing the streamed `usage` fields, records them as **tokens with cost flagged as
  unavailable** (never a fabricated dollar figure), and marks the row as log-recovered so
  the source is auditable.
- **Given** a row the pass has already reconciled **When** the pass runs again **Then** it
  is idempotent: no double-counting, no re-summing, and in-progress rows are skipped.
- **Given** the pass has run **When** I invoke `sdlc doctor` **Then** it reports the
  ledger-vs-logs **agreement rate** (share of stage attempts whose ledger usage matches
  the log ground truth within a tolerance) and lists the residual disagreements with their
  reason (log-recovered, no-log, still-divergent), so drift is visible on every health
  check.
- **Given** a repo with no session logs on disk (logs pruned) **When** the pass or the
  doctor check runs **Then** it degrades gracefully to "unverifiable" for those rows and
  says so, rather than reporting false agreement.

**Technical Notes**: Absorbs Issue #481 (crash-session recovery) rather than depending on
it — #481 specifies the same `usage_reconcile.py` sweep, the same tokens-only/no-fabricated-cost
rule, the same `reask`/`bugfix`/`commitlint` matching and the same idempotency guarantee, and this
story is a strict superset of it (adding completed-session backfill and the doctor agreement
rate). Building both would collide on the same new module. It also completes the
reconciliation the PR #482 overwrite-fix implied but did not deliver for pre-fix history.
Session logs are raw `stream-json`; the terminal `result` event is the only authoritative
cost line (per-turn events carry tokens but no cost), so cost is recoverable only for
sessions that completed. The reconcile primitive is a new module (for example
`controller/src/sdlc/usage_reconcile.py`) rather than an extension of `reconcile.py`,
which today only reclassifies parked story *status* and has no usage awareness. Writes go
through the existing `Ledger.stage_set_usage` in `controller/src/sdlc/build.py`; matching
must handle `reask`/`bugfix`/`commitlint` recovery rows, keyed by
`(run, story, stage, attempt)`. Surface the agreement metric through
`controller/src/sdlc/doctor.py`. This is the only story that both restores lost history
and prevents future drift, so it precedes all calibration.

**Definition of Done**:
- [ ] Reconciliation pass backfills log-derived usage onto the correct attempt row for
      completed sessions; recovers tokens (cost flagged unavailable) for crashed sessions
- [ ] Idempotent and skips in-progress rows; no double-count on re-run
- [ ] `sdlc doctor` reports the ledger-vs-logs agreement rate and enumerates residual
      disagreements with reasons; pruned-log rows report as unverifiable
- [ ] Tests: overwrite-era history reconciled, crash-session token recovery, idempotency,
      no-log degradation
- [ ] Documented in `docs/controller-architecture.md` and the `sdlc doctor` help

**Dependencies**: none — Issue #481 is absorbed into this story, not a prerequisite (see
Technical Notes); consumes PR #482 (overwrite fix)
**Risk Level**: Medium

##### Story 28.1-002: Verified per-attempt model recording and NULL backfill
**User Story**: As the person building a predictor and reading cost by model, I want the
`stages.model` column to be non-NULL for every dispatched stage on fresh runs and the
historical NULLs backfilled or flagged, so that model attribution is a fact in the ledger
rather than something re-derived by parsing logs.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a fresh run under model routing (Epic-14) and any harness (Epic-20) **When**
  each stage completes **Then** its `stages.model` value is non-NULL and equals the model
  the dispatch actually used, for **every** stage type: build, coverage, review, merge,
  and the recovery rows (reask, bugfix). (The recovery-row fix shipped in PRs #482 and
  #484; this story adds the end-to-end verification and closes any remaining primary-path
  or harness gap the 2026-07-19 dataset, which showed all 374 rows NULL, exposed.)
- **Given** the existing ledger with NULL `model` on historical rows **When** the backfill
  runs **Then** rows whose session log records a `modelUsage` model id are backfilled from
  the log, and rows with no recoverable model are left NULL but **counted and reported**
  (not silently coerced), so the coverage of the column is known.
- **Given** `sdlc doctor` **When** it runs **Then** it reports the share of stage rows with
  a populated `model` for runs after the migration, flagging any regression to NULL on
  fresh runs as a defect.
- **Given** a stage dispatched with an explicit `--model` override or a registry harness
  whose model differs from the Claude tier alias **When** it is recorded **Then** the row
  captures the model that actually ran (consistent with `_resolved_stage_model` and the
  recovery-row resolution added in #484), not a placeholder.

**Technical Notes**: The `model` column was added in schema v11 (Story 14.2-003) but the
2026-07-19 dataset found it NULL on all 374 rows. The recovery-path cause was fixed in PRs
#482 and #484; this story verifies the primary `build`/`coverage`/`review`/`merge` path
also lands a non-NULL model on fresh runs (the primary write via `stage_start(model=...)`
exists after Issue #427, so the story is mostly a regression test plus a backfill), and
adds a doctor coverage check so a future regression is caught. The `modelUsage` key in the
`stream-json` result envelope (`controller/src/sdlc/dispatch.py`) is the backfill source
for history. No schema change (the column exists); backfill writes through the ledger.

**Definition of Done**:
- [ ] Regression test: every stage type (primary + recovery) records non-NULL `model` on a
      fresh run, across the built-in Claude slot and a registry harness
- [ ] Historical NULL rows backfilled from `modelUsage` where present; unrecoverable rows
      counted and reported, not coerced
- [ ] `sdlc doctor` reports post-migration `model` coverage and flags fresh-run NULLs
- [ ] Documented in `docs/controller-architecture.md`

**Dependencies**: 28.1-001 (shares the log-parsing and doctor surface); consumes PRs #482, #484
**Risk Level**: Low

### Feature 28.2: Calibrated Prediction

Compute a per-story predicted-token and predicted-rework signal from reconciled ledger
history. Crude first, honest about error, reconciled after every run so it improves.

#### Stories

##### Story 28.2-001: Extend discovery output with predictor features; demote points to metadata
**User Story**: As the person building the predictor, I want the discovery agent to emit
the story features the predictor needs (acceptance-criteria count, dependency depth, and a
scope proxy) alongside the points value, with points recorded as descriptive metadata
rather than a decision input, so that the predictor has real features to key on instead of
a near-constant point label.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the discovery agent parses an epic into a story queue **When** it emits a story
  **Then** the story record carries the predictor features (acceptance-criteria count,
  dependency depth from the dependency graph, and a scope proxy such as expected files or
  areas touched where the epic states it), in addition to the existing points value.
- **Given** a story record **When** it is written to the ledger and the story docs **Then**
  `points` is preserved and shown as a **descriptive scope label**, explicitly documented
  as no longer an escalation or budget input.
- **Given** the discovery output schema changes **When** a downstream consumer reads a
  story **Then** the result contract and existing story fields remain backward compatible
  (new fields are additive; absent features are recorded as unknown for the predictor to
  treat as missing).
- **Given** an epic that does not state enough to compute a feature **When** discovery runs
  **Then** the feature is recorded as unknown (not zero), so the predictor can treat it as
  missing rather than as a real low value.

**Technical Notes**: Touch `controller/src/sdlc/discovery.py` (points assignment today) to
emit the additional features, and thread them through the story record the ledger and
story renderer persist. Keep the change additive so Epic-22/Epic-11 story consumers are
unaffected. This is the story that operationalizes "points demoted to metadata": the label
survives everywhere it is human-facing, but the machine reads features instead. It has no
intra-epic dependency (a discovery-side data change), so it can land early, in parallel
with Feature 28.1, and it **must precede** Story 28.2-002, whose predictor keys on these
features.

**Definition of Done**:
- [ ] Discovery emits acceptance-criteria count, dependency depth, and a scope proxy per
      story; unknown features recorded as unknown, not zero
- [ ] Points preserved and documented as descriptive-only; additive, backward-compatible
      schema
- [ ] Tests: feature extraction, missing-feature handling, backward compatibility
- [ ] Documented in `docs/controller-architecture.md`

**Dependencies**: None (a discovery-side data change; produces the feature set 28.2-002 consumes)
**Risk Level**: Low

##### Story 28.2-002: Per-story token and rework-probability predictor from ledger history
**User Story**: As FX, I want each story to carry a predicted token cost and a predicted
rework probability computed from the factory's own reconciled history and the discovery
features, recorded before the run and reconciled against actuals after, so that downstream
decisions rest on a measured-and-improving signal instead of a story-point guess.
**Priority**: Should Have
**Story Points**: 8

**Acceptance Criteria**:
- **Given** reconciled ledger history (Feature 28.1) and the discovery features (Story
  28.2-001) **When** a story is about to run **Then** the controller computes and records,
  before dispatch, a **predicted token cost** and a **predicted rework probability**
  (probability the story enters a review retry or bugfix loop) from a crude, inspectable
  model: per-stage historical means keyed by points band and risk flag, adjusted by the
  discovery features (acceptance-criteria count, dependency depth, and a files-touched or
  diff-size proxy where available).
- **Given** a story completes **When** its actual tokens and actual rework are known
  **Then** the prediction-vs-actual is reconciled and persisted (extending the estimate-
  vs-actual reconciliation already persisted by Story 14.1-002), building the training set
  for the next prediction.
- **Given** a run of predictions and their reconciled actuals **When** I ask for
  prediction quality **Then** the controller reports the **median absolute error of
  predicted vs actual tokens** and a calibration summary for the rework probability, each
  with its sample size, so quality is measured, not asserted.
- **Given** insufficient history for a story's band or feature combination, or a discovery
  feature recorded as unknown **When** the predictor runs **Then** it falls back to the
  global historical mean and marks the prediction **low-confidence**, rather than emitting
  a confident number from no data.
- **Given** the predictor is disabled or unconfigured **When** a run executes **Then**
  behavior degrades to today's point-keyed estimate (14.1-002), with a logged note, so the
  epic is safe to roll out incrementally.

**Technical Notes**: Extend `controller/src/sdlc/cost_estimate.py` (the 14.1-002 heuristic
already has a calibration hook and persists estimate-vs-actual, the natural training
store). Training data reads the reconciled `stages` usage and the run/story history in the
`Ledger` (`build.py`: `stage_set_usage`, `run_usage_totals`, per-stage historical
averages), keyed by the discovery features from Story 28.2-001. Keep the model inspectable
(means + adjustments), version the predictor so a recalibration is auditable, and record
predictions on the story/run rows so post-run reconciliation is a join, not a
re-computation. Honor the n=76 caveat: report error with sample size and never suppress the
low-confidence flag.

**Definition of Done**:
- [ ] Predicted tokens + predicted rework probability computed and recorded pre-run from
      reconciled history and the discovery features, with the documented crude model
- [ ] Prediction-vs-actual reconciled and persisted post-run (extends 14.1-002 store)
- [ ] Prediction quality reported as median absolute error (tokens) + rework calibration,
      each with sample size
- [ ] Low-confidence fallback to global mean on thin history or unknown features; disabled
      path unchanged
- [ ] Tests: prediction record/reconcile round-trip, thin-history fallback, error metric
- [ ] Documented in `docs/controller-architecture.md`

**Dependencies**: 28.1-001, 28.1-002 (reconciled telemetry to train on); 28.2-001 (the discovery features it keys on)
**Risk Level**: Medium

### Feature 28.3: Consumers Switch to Predictions

Move the decisions that key on points today (model escalation, the budget gate,
pre-dispatch warnings, and batch planning) onto the calibrated prediction. The Epic-08
risk signal and the Epic-14 tier map are preserved; only the points-keyed inputs change.

#### Stories

##### Story 28.3-001: Route model escalation on predicted tokens and rework risk, not raw points
**User Story**: As FX, I want build/review model escalation to key on predicted token cost
and predicted rework probability instead of raw story points, so that Opus arrives on the
stories a calibrated signal says are actually risky, not on the ones a noisy point estimate
inflated.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** routing is on and the predictor is available **When** the controller chooses
  the `build` or `review` model **Then** it escalates to the stronger tier on a configurable
  threshold of **predicted tokens or predicted rework probability**, not on `story.points`.
- **Given** the Epic-08 `risk_gate` signal **When** a stage is high-risk **Then** its
  behavior is unchanged: high-risk still escalates and the adversarial slot keeps its Opus
  floor (this story replaces the *points* input, not the risk input).
- **Given** the predictor is low-confidence or disabled **When** the model is chosen
  **Then** routing falls back to today's Epic-14 behavior (points-keyed or CLI default),
  logged, so the switch is safe to roll out.
- **Given** no consumer of the routing decision **When** the code is reviewed **Then**
  `story.points` is no longer read as an escalation input anywhere in the routing path
  (verified by test and search); points remain only as metadata.
- **Given** the escalation threshold **When** it is configured **Then** it is a documented,
  per-repo-overridable value expressed in predicted tokens or predicted rework probability,
  with a sane default derived from the dataset.

**Technical Notes**: Change the escalation input in
`controller/src/sdlc/model_routing.py` and `role_routing.py` (the points-keyed escalation
today) to consume the 28.2-002 prediction; preserve the Epic-14 Balanced tier map and the
Epic-08 `risk_gate.py` high-risk path unchanged. This directly retires Non-Goal-adjacent
finding #4 from the dataset (routing about to be enabled would key model choice to noise).
Validate with the Epic-18 eval harness that prediction-keyed escalation holds quality
versus the points-keyed baseline before trusting it broadly.

**Definition of Done**:
- [ ] Build/review escalation keys on predicted tokens or rework probability, configurable
      threshold, per-repo override, documented default
- [ ] Epic-08 risk escalation and adversarial Opus floor unchanged
- [ ] Low-confidence/disabled fallback to Epic-14 behavior, logged
- [ ] No routing-path read of `story.points` as an escalation input (test + search)
- [ ] Tests: prediction-keyed escalation, risk path preserved, fallback; eval-harness check
- [ ] Documented in `docs/controller-architecture.md`

**Dependencies**: 28.2-002 (prediction signal); preserves Epic-08 risk_gate, Epic-14 map
**Risk Level**: Medium

##### Story 28.3-002: Budget gate, pre-dispatch warnings, and batch planner consume the prediction
**User Story**: As FX planning an overnight batch against a finite rate-limit window, I
want the budget gate, pre-dispatch warnings, and the batch planner to use the calibrated
per-story prediction instead of a point-based estimate, so that the run's projected
consumption and the batch's fit against the window are honest.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a pre-dispatch check (Story 14.1-002) **When** a stage or story is about to run
  **Then** the warning and any interactive gate use the 28.2-002 predicted tokens, and the
  surfaced estimate is labelled with its confidence.
- **Given** the token budget gate (Story 14.1-001) **When** a run executes **Then** its
  projected-remaining computation uses summed predictions for not-yet-run stories, so a
  run heading for the ceiling is flagged earlier than a purely accrued-so-far view would.
- **Given** a batch of ready stories and the Max rate-limit window (Story 14.1-003) **When**
  the batch planner sequences them **Then** it uses the **summed predicted tokens** against
  the window budget to order and pace dispatch, rather than summed points, and reports the
  projected window fit with its confidence.
- **Given** the prediction is low-confidence or disabled **When** any of these consumers
  runs **Then** it falls back to the Epic-14 point-based estimate, logged, so the batch
  never blocks on a missing prediction.
- **Given** the actuals after the run **When** the batch's projected-vs-actual window fit is
  reconciled **Then** the miss is recorded, feeding the 28.2-002 error metric so planning
  accuracy is tracked over time.

**Technical Notes**: Wire the 28.2-002 prediction into the budget/estimate path in
`controller/src/sdlc/cost_estimate.py` and the `run_build` budget enforcement in
`build.py` (Story 14.1-001), and into the rate-limit-window planning of Story 14.1-003.
Keep the notional-dollar labelling from Epic-14 (the primitive stays tokens on a Max
subscription). This story closes the loop the epic opens: the consumers that made points
load-bearing now read the calibrated signal, and their misses feed back into the
predictor's measured error.

**Definition of Done**:
- [ ] Pre-dispatch warning/gate uses predicted tokens with a confidence label
- [ ] Budget gate projects remaining using summed predictions for pending stories
- [ ] Batch planner paces against summed predicted tokens vs the rate-limit window and
      reports projected fit with confidence
- [ ] Low-confidence/disabled fallback to Epic-14 estimate, logged
- [ ] Projected-vs-actual window fit reconciled and fed to the 28.2-002 error metric
- [ ] Tests: gate/warning prediction path, batch pacing, fallback, reconciliation
- [ ] Documented in `docs/controller-architecture.md`

**Dependencies**: 28.2-002 (prediction signal); coordinates Epic-14 14.1-001, 14.1-002, 14.1-003
**Risk Level**: Medium

### Feature 28.4: Routing Engagement and Visibility

Make the Balanced routing from Epic-14 (Story 14.2-001) actually engage by default and be
impossible to leave silently off. This is the prerequisite that makes Feature 28.3
meaningful: keying escalation on predictions is moot if routing never turns on. A
cost-governance control that fails silent-and-expensive is not a control.

#### Stories

##### Story 28.4-001: Engage Balanced routing by default and surface the routing state
**User Story**: As FX, who believed model routing was on for months while every stage
silently ran the CLI default, I want the Balanced profile to be the effective default when
no profile is configured and the resolved routing state printed and ledger-logged at run
start, so that a cost-governance control cannot fail silent-and-expensive and I can see
which profile governed any run.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** no `model_profile` is configured **When** a run starts **Then** the Balanced
  map is the effective default (Haiku discovery/merge, Sonnet build/coverage/review, Opus
  adversarial and high-risk escalation), a startup banner names the resolved profile and
  prints the per-stage model map after all overrides plus the escalation thresholds in
  effect, and each dispatched stage carries the mapped `--model`, verifiable in the ledger
  stage rows and the session logs.
- **Given** `model_profile: off` (an explicit opt-out value, never an absent key) **When** a
  run starts **Then** all stages use the CLI default model AND the banner and a ledger event
  state this loudly, for example "MODEL ROUTING OFF: CLI default <model> used for ALL
  stages", so the state is visible live and post-hoc.
- **Given** an existing explicit override (`SDLC_AGENT_CMD` or a per-stage `--model`)
  **When** routing resolves **Then** the override keeps precedence exactly as today, and the
  banner shows the effective per-stage model after the override is applied.
- **Given** any completed run **When** I view `sdlc status` or the dashboard **Then** the
  resolved routing config that governed the run is shown (the profile name and its effective
  per-stage map after overrides, persisted on the run row), and each stage's chosen model is
  shown (persisted on the stage row by Story 28.1-002, surfaced here, not re-implemented).
- **Given** routing is off AND the invocation looks unattended (a batch size above one, or a
  `--budget` flag set) **When** `sdlc doctor` or the run preflight executes **Then** a
  warning is raised: doctor warns strongly, or fails, when routing-off meets an
  unattended-looking run.
- **Given** any run **When** it starts **Then** its **fully-resolved routing config** (the
  profile name plus the effective per-stage model map and escalation thresholds after all
  overrides) is resolved once and snapshotted on the run row, and every subsequent
  `sdlc resume` **replays that snapshot** rather than re-resolving against the current config,
  default, or overrides, so routing is identical across the original run and all of its
  resumes (the Epic-10/Epic-12 resume identically contract).
- **Given** a run is resumed after `.sdlc-model-routing.yaml`, an `SDLC_AGENT_CMD`, or a
  per-stage `--model` override has changed since the run was created **When** the resume
  dispatches its remaining stages **Then** they use the routing from the run's frozen
  snapshot, not the changed config, so an unrelated edit between a run and its resume can
  never alter that run's routing. A deliberate routing change on resume is out of scope for a
  plain resume: it would require an explicit re-resolve flag, documented as such.
- **Given** a run created **before** this change, whose persisted state predates the default
  flip (an empty or absent `model_profile` meant routing off under the old semantics) **When**
  it is resumed after the flip **Then** it continues to route exactly as it originally did
  (routing off, CLI default for every stage), never silently upgraded to Balanced. A ledger
  migration stamps such legacy run rows with an explicit `off` so their routing is frozen at
  their original behavior.
- **Given** the default flip **When** the change ships **Then** the behavior change is called
  out in the changelog and `docs/controller-architecture.md`.

**Technical Notes**: The 14.2-001 implementation made an empty `model_profile` mean "routing
off, CLI default for every stage", and empty was the state of every run. The 2026-07-19
dataset (336 session logs, 374 stage attempts) contains **zero Sonnet sessions**; merge ran
37 times on Opus 4.8 and 33 on Fable 5 (premium models doing Haiku-grade mechanical work,
11.6 percent of spend); and the model tracked the CLI default of the day (all Opus in June,
all Fable 5 in July), the signature of no `--model` flag being passed. Flip the default in
profile resolution (`controller/src/sdlc/model_routing.py`, `role_routing.py`) so an unset
`model_profile` resolves to Balanced and only an explicit `off` disables routing; keep the
`SDLC_AGENT_CMD` and per-stage `--model` precedence unchanged. Emit the banner from the
`run_build` cohort loop in `build.py` before the first dispatch (resolved profile name,
per-stage map after overrides, escalation thresholds) and write it to the run's events via
`event_log` so `status.py` and `dashboard.py` can surface it post-hoc; persist the fully-resolved routing
config (profile name plus the effective per-stage map and thresholds after overrides) as a
snapshot on the run row, and rely on Story 28.1-002 for the per-stage `model` column rather
than duplicating that write. Add the check in `doctor.py`. The `--model` plumbing already
exists in `dispatch.py`. The escalation thresholds shown in the banner reflect the Epic-14
points/risk basis today and the prediction basis once Story 28.3-001 lands.

**Resume identity is the subtle part of the default flip.** The controller guarantees a
resumed run behaves identically to the original (Epic-10 resume machinery, Epic-12 resume
discipline), so the flip must never reach an in-flight run. Two rules enforce this. First,
**resolve-and-freeze the whole config**: the fully-resolved routing config (profile name plus
the effective per-stage map and thresholds after overrides) is resolved once at run creation
and snapshotted on the run row (the same write the visibility half already needs), and
`run_resume` replays that snapshot rather than reloading `.sdlc-model-routing.yaml` or
re-reading the overrides. Freezing only the profile *name* would be insufficient: a resume
that re-derived the map from the name could still pick up an edited config file or a changed
override and route differently from the original run. Second, **legacy migration**: runs
created before this change persist `model_profile: ""`,
which meant routing off under the old semantics but would re-resolve to Balanced under the
new default. A versioned ledger migration (auto-applied at launch, the Epic-12 pattern)
backfills existing run rows, stamping any pre-change run's resolved profile as `off` so its
resume is frozen at its original routing-off behavior. New runs stamp their resolved profile
at creation, so only the one legacy backfill is ever needed.

**Definition of Done**:
- [ ] Unset `model_profile` resolves to Balanced (effective default); only explicit
      `model_profile: off` disables routing; `SDLC_AGENT_CMD` and per-stage `--model`
      precedence unchanged
- [ ] Startup banner (printed and written as a ledger event) names the resolved profile, the
      per-stage model map after overrides, and the escalation thresholds; the off state
      prints loudly
- [ ] Fully-resolved routing config (profile name + effective per-stage map + thresholds)
      snapshotted on the run row; per-stage chosen model persisted via Story 28.1-002
      (coordinated, not duplicated); both surfaced in `sdlc status` and the dashboard
- [ ] Routing config resolved once and snapshotted on the run row at creation; `run_resume`
      replays the snapshot and never reloads the config file or overrides, so routing is
      identical across a run and all of its resumes
- [ ] Versioned ledger migration (auto-applied at launch) stamps pre-change run rows as
      routing `off`, freezing legacy runs at their original routing-off behavior
- [ ] `sdlc doctor` / run preflight warns when routing is off, and warns strongly or fails
      when routing-off meets an unattended-looking invocation (batch size above one or
      `--budget` set)
- [ ] Tests: default-on resolution, explicit off, override precedence unchanged, banner
      content, snapshot persistence, a **pre-change run resumes routing-off (not Balanced)**
      after the flip, and a **run resumed after its config or override changed replays the
      frozen snapshot** (routing unchanged)
- [ ] Changelog entry stating the default flip and why (cite: zero Sonnet sessions across 374
      attempts while the operator believed routing was on); `docs/controller-architecture.md`
      updated (routing states, banner, doctor check, the resume/migration rule)

**Dependencies**: 28.1-002 (persists the per-stage chosen model in the v11 column; this story
surfaces it, does not re-implement it). Reuses the Epic-10 resume machinery and the Epic-12
resume-migration pattern for resolve-and-freeze and the legacy backfill (both COMPLETE, so
context not a blocking intra-epic dependency). Prerequisite for 28.3-001 (routing must engage
before its escalation can key on predictions). Fixes the Epic-14 Story 14.2-001 default.
**Risk Level**: Medium (an intended behavior change: the default flip alters model selection
for every run; mitigated by the loud banner, the doctor check, the changelog note, unchanged
override precedence, and resolve-and-freeze plus the legacy migration so in-flight and
pre-change runs resume identically)

## Story Dependencies (within Epic-28)

```
28.1-001 (reconcile backfill + doctor)  absorbs Issue #481; consumes PR #482. FIRST.
28.1-002 (verified model recording)     needs 28.1-001 (shared log-parse/doctor); consumes PRs #482, #484
28.2-001 (discovery features)           independent (a discovery-side data change; can run alongside Feature 28.1)
28.2-002 (predictor)                    needs 28.1-001 + 28.1-002 (reconciled telemetry) + 28.2-001 (discovery features)
28.4-001 (routing engages + visible)    needs 28.1-002 (surfaces the per-stage model column); fixes Epic-14 14.2-001
28.3-001 (routing on prediction)        needs 28.2-002 + 28.4-001 (routing must be engaged before it can key on predictions); preserves Epic-08 risk_gate + Epic-14 map
28.3-002 (budget/batch on prediction)   needs 28.2-002; coordinates Epic-14 14.1-001/002/003
```

- **Cohort 1 (first wave, no intra-epic dependencies)**: 28.1-001 and 28.2-001 both start
  immediately, and 28.1-002 follows 28.1-001. 28.1-001/28.1-002 are the integrity stories
  that **gate every telemetry consumer**: calibration on top of corrupted telemetry would
  train on lies, so no story that reads the meter (the predictor 28.2-002 and the Feature
  28.3 consumers) starts until the meter agrees with the logs and the model column is
  verified. 28.2-001 (discovery features) shares this wave for a different reason: it has no
  intra-epic dependency and touches no telemetry, so it runs alongside the integrity work
  rather than waiting behind it.
- **Cohort 2 (prediction and routing engagement)**: 28.2-002 (the predictor) and 28.4-001
  (routing engages and becomes visible) run in parallel. Each needs only Cohort 1 outputs
  and not the other: 28.2-002 needs the reconciled telemetry (28.1-001/28.1-002) and the
  discovery features (28.2-001); 28.4-001 needs the per-stage model column from 28.1-002.
- **Cohort 3 (consumers)**: 28.3-002 depends only on 28.2-002; 28.3-001 depends on both
  28.2-002 and 28.4-001 (routing must be engaged before its escalation can key on
  predictions). They touch different consumers (routing vs budget/batch) and can run in
  parallel once their inputs land.

## Epic Complete When

- The ledger agrees with the session logs after a reconciliation backfill, crashed-session
  spend is recovered as tokens (cost flagged unavailable, never fabricated), the pass is
  idempotent, and `sdlc doctor` reports the ledger-vs-logs agreement rate so drift stays
  visible.
- Every dispatched stage on a fresh run records a non-NULL `stages.model` (primary and
  recovery, across the Claude slot and a registry harness), historical NULLs are backfilled
  or explicitly flagged, and a doctor check guards against regression.
- Each story carries a predicted token cost and a predicted rework probability computed
  from reconciled history and reconciled against actuals, with prediction quality reported
  as median absolute error (tokens) plus a rework calibration summary, each with its sample
  size and the single-repo caveat.
- The discovery agent emits the predictor's features and points is demoted to a descriptive
  scope label, no longer read as a decision input.
- Model routing engages by default: an unset `model_profile` resolves to the Balanced map,
  only an explicit `off` disables it, every run prints and ledger-logs a routing banner
  (resolved profile, per-stage map after overrides, escalation thresholds), the off state
  prints loudly, the governing profile is visible in `sdlc status` and the dashboard, and
  `sdlc doctor` warns when routing is off on an unattended-looking run.
- Model escalation keys on predicted tokens or rework probability (Epic-08 risk and the
  Epic-14 adversarial Opus floor unchanged), and the budget gate, pre-dispatch warnings,
  and batch planner consume the calibrated prediction, with a logged fallback to Epic-14
  behavior whenever the prediction is low-confidence or disabled.
- No consumer in the routing, budget, or batch path reads `story.points` as a decision
  input, verified by test and search.
