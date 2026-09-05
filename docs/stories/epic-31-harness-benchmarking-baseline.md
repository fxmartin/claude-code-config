# Epic 31: Harness Benchmarking & Local-Model Baseline

> **Status: NOT STARTED (0/7)** — authored 2026-09-05, from FX's decision to
> baseline local agentic coding (OpenCode + a local dense Qwen model) against
> the hosted Claude harness on **quality, token consumption, and time**.
> Thesis: the controller already measures all three, but not *comparably*. The
> ledger records `started_at`/`finished_at` per stage attempt alongside
> `harness` and `model` (`build.py:221`), and `sdlc eval` already scores the
> exact tuple FX wants — `quality_pass_rate`, `tokens_mean`, `cost_mean`,
> `wall_mean`, `loc_net_mean` (`eval_compare.py:45`) — with an A/B differ and a
> verdict that refuses to call a quality drop "better". The gap is that the one
> tool built for A/B **cannot vary the harness**: `EvalConfig`
> (`evaluate.py:61`) carries `model`, `agent_type`, `n`, `seed` and no
> `harness`, so today it can only compare two models on the same harness.
>
> Three further holes make a naive cross-harness comparison actively
> misleading, and each is a story here: (1) the `qwen` harness declares
> `usage_tracking: false` (`harnesses.yaml:104`) — its token axis is empty, so
> a comparison silently puts a number beside a blank; (2) rate-limit stalls are
> recorded as their own dimension (`stall_seconds`, Story 27.3-004,
> `build.py:3026`) but are **not** subtracted from `duration_seconds`, and only
> the hosted harness ever stalls — biasing wall-clock *against* Claude; (3)
> cost is a notional $/Mtok constant (`DEFAULT_USD_PER_MILLION_TOKENS`), which
> is meaningless for a local model whose marginal cost is electricity.
>
> **Relationship to Epic-29**: Epic-29 makes the local harnesses *run*
> (29.2-001 registers OpenCode at all; 29.2-003 mines its JSON event stream for
> real usage; 29.1-001/29.2-002 verify parallelism). This epic makes them
> *measurable against each other*. Epic-29 is a hard dependency for the
> OpenCode arm of the baseline — it does not exist in the registry today.

## Epic Overview

**Epic ID**: Epic-31
**Description**: FX is about to spend real time evaluating whether local
inference can carry agentic coding work. That decision is only as good as the
measurement behind it, and the measurement rig is one field short of usable.
`sdlc eval` materialises a fresh git baseline per run, dispatches a fixed
ticket set `n` times in isolated workspaces, scores each result against the
ticket's `quality_cmd` (exit 0 = pass), and times it with `time.monotonic()`
(`evaluate.py:532`) — a genuinely sound rig, already versioned in-repo
(`controller/eval/eval-config.yaml`, `baseline.json`). It reaches the agent
through `dispatch_agent` (`dispatch.py:666`), whose `agent_cmd` + `parser`
keywords are exactly the pair the harness registry resolves a harness name
into. So harness selection is a threading exercise, not new machinery. This
epic (1) threads it, end to end, and records it in the scoreboard so a result
is attributable; (2) closes the three comparability holes so time, tokens and
cost mean the same thing on both arms — or are explicitly, visibly absent
rather than silently zero; and (3) executes and records the actual baseline,
with a written method so the numbers survive contact with a skeptical reader.
**Business Value**: Turns "should we run agentic coding on local models?" from
an impression into evidence, at the moment FX is deciding it. A credible
local-model arm is what unblocks the fully-offline loop Epic-30 is building
toward (its own success metric concedes that agent/model traffic stays cloud
"until Epic-29 local harnesses land"), and it puts a number on the cost and
rate-limit escape hatch that motivates harness diversification in the first
place. The metric-integrity work is reusable beyond this baseline: every
future harness or model change gets an honest A/B instead of a fresh argument.

**Success Metrics**:
- `sdlc eval --harness <name>` runs the identical ticket set on claude, qwen
  and opencode, and `sdlc eval-compare` produces a per-metric delta and verdict
  across two harnesses — the comparison FX cannot run today.
- Every scoreboard states the harness, model, adapter version and host that
  produced it; a scoreboard missing that provenance is rejected by
  `eval-compare` rather than silently compared.
- Wall-clock on the hosted arm is reported **excluding** rate-limit stall time,
  and the excluded amount is shown beside it — a quota-throttled Claude run and
  an unthrottled local run are compared on agent time, not on FX's quota.
- A harness that cannot report usage yields an explicit "unavailable" in the
  scoreboard and a refusal-to-compare on the token axis — never a `0` that
  reads as "free".
- A recorded baseline exists: claude vs local Qwen (and OpenCode, subject to
  Epic-29) over ≥3 runs per ticket, with quality, tokens, stall-adjusted time,
  and a written go/no-go on local inference for real story work.

**Out of Scope**:
- Registering or fixing the OpenCode adapter, qwen parallel/worktree flags, and
  OpenCode usage telemetry — all Epic-29 (29.2-001, 29.1-001/29.2-002,
  29.2-003). This epic consumes them and must degrade cleanly when absent.
- Model serving infrastructure (llama.cpp / Ollama / MLX / vLLM choice,
  quantisation, context length tuning). The harness adapter is the seam; how
  the model is served is FX's environment, recorded as provenance, not
  controlled here.
- Changing the ledger's timestamp resolution. `CURRENT_TIMESTAMP` is
  whole-second UTC; that is adequate for minute-scale stages and a schema
  migration is not worth it. Sub-second timing stays the eval harness's
  `time.monotonic()`.
- A budget/spend model for local inference. 31.2-003 makes cost *honest* (not
  fabricated); Epic-14/Epic-28 own cost governance and calibration.
- Benchmarking non-coding tasks, or any published/comparative claim about
  vendor models. This is FX's internal decision input on his own workload.

## Features in This Epic

### Feature 31.1: Harness-Aware Eval Rig

Give the eval harness the one axis it lacks, and make every scoreboard say what
produced it. Without provenance an A/B is folklore.

#### Stories

##### Story 31.1-001: Harness selection in the eval config and CLI
**User Story**: As FX baselining local inference, I want `sdlc eval` to accept
a harness — in the versioned config and as a `--harness` override — resolved
through the harness registry to the adapter command and parser, so that the
same ticket set can be run on claude, qwen and opencode and the results are
comparable by construction.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** an eval config declaring `harness: qwen` **When** `sdlc eval` runs
  **Then** every ticket dispatch goes through that harness's registry entry
  (command + parser), and the scoreboard records the harness name.
- **Given** `--harness opencode` on the command line **When** the config also
  names one **Then** the CLI flag wins, mirroring the model-override precedence
  and the wider CLI/env > file > default rule.
- **Given** a config naming **no** harness **When** the eval runs **Then**
  behaviour is byte-identical to today (the built-in `claude` default via
  `DEFAULT_HARNESS`) — the field is purely additive, per the Epic-20 registry
  pattern.
- **Given** a harness name absent from the registry, or present but
  `enabled: false` **When** the eval starts **Then** it aborts before any
  dispatch with a one-line actionable error naming the registry file — never
  mid-ticket, never after spending tokens.
- **Given** a harness whose `probe` fails (binary missing on this machine)
  **When** the eval starts **Then** it aborts at preflight with the probe's
  own diagnostic, so a missing `qwen`/`opencode` binary is distinguishable from
  a bad config.
- **Given** the model pin (`model:`, Issue #435) **When** combined with a
  harness **Then** the model still resolves and is passed to the adapter's
  `{model}` placeholder where the harness supports it, and a harness that
  cannot take a model pin says so at preflight rather than silently ignoring it.

**Technical Notes**: `EvalConfig` (`evaluate.py:61`) gains `harness: str |
None`, pinned in `__post_init__` exactly as `model` is (resolve the default
rather than leave None, so a scoreboard never says "whatever the CLI happened
to default to"). The dispatch seam already fits: `dispatch_agent`
(`dispatch.py:666`) takes `agent_cmd` and `parser`, which is precisely what
the registry resolves a harness into — thread the resolved pair through the
`Dispatcher` protocol (`evaluate.py:452`, already injectable for tests) rather
than reaching into the registry from inside the run loop. Keep
`controller/eval/eval-config.yaml` and `ci-config.yaml` working untouched
(no `harness:` key = claude), since CI consumes them.

**Definition of Done**:
- [ ] `harness` in `EvalConfig` + `--harness` CLI override with documented precedence
- [ ] Registry resolution to command+parser threaded through the dispatcher seam
- [ ] Preflight aborts: unknown harness, disabled harness, failed probe, unsupported model pin
- [ ] Existing configs run unchanged on the claude default (regression test)
- [ ] Tests: per-harness dispatch with a fake dispatcher, precedence, each abort path
- [ ] Documented in the eval config's own comments and the controller docs

**Dependencies**: none (Epic-29 needed only for the *opencode* arm to exist)
**Risk Level**: Medium

##### Story 31.1-002: Scoreboard provenance and refusal to compare unlike runs
**User Story**: As FX reading a scoreboard weeks later, I want every result to
carry the harness, model, adapter/CLI version, host and timestamp that produced
it, and `eval-compare` to refuse (not silently proceed) when two scoreboards
disagree on something that invalidates the comparison, so that a number can
always be traced to the conditions that made it.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** any completed eval **Then** the scoreboard JSON records harness,
  resolved model, harness binary version (from the registry `probe`), host
  identifier, ticket-set identity (config name + seed + ticket ids), `n`, and a
  UTC timestamp.
- **Given** two scoreboards built from **different ticket sets** (differing
  config name/seed/ticket ids) **When** `eval-compare` runs **Then** it refuses
  with a clear message — comparing different work is not an A/B.
- **Given** two scoreboards from the same ticket set but different harnesses
  **When** compared **Then** the comparison proceeds and the output states both
  harnesses in its header, so the delta is never read as model-only.
- **Given** a legacy scoreboard predating provenance **When** compared **Then**
  it is accepted with an explicit "provenance unknown" warning rather than
  rejected — existing baselines stay usable.
- **Given** `--force` **When** the ticket sets differ **Then** the comparison
  proceeds with the mismatch printed as a warning, for the deliberate case.

**Technical Notes**: Extend `scoreboard_to_dict` and the baseline shape
(`eval_compare.py:331` `load_scoreboard`, `:348` `save_scoreboard`); keep the
addition backward-compatible so `controller/eval/baseline.json` still loads —
`BaselineError` is for malformed files, not for merely older ones. Host
identifier should be coarse (hostname/arch), not a fingerprint. The version
string comes from running the registry's declared `probe`, so it is the same
source of truth the preflight uses.

**Definition of Done**:
- [ ] Provenance block written by every eval; documented field-by-field
- [ ] `eval-compare` refuses mismatched ticket sets; `--force` escape hatch
- [ ] Cross-harness comparisons state both harnesses in the output header
- [ ] Legacy scoreboards load with a warning, not an error
- [ ] Tests: provenance round-trip, refusal, force, legacy acceptance

**Dependencies**: 31.1-001
**Risk Level**: Low

### Feature 31.2: Cross-Harness Metric Integrity

Make time, tokens and cost mean the same thing on both arms — or be visibly
absent. Every story here removes a specific way the current numbers lie when
a hosted harness is compared to a local one.

#### Stories

##### Story 31.2-001: Stall-adjusted wall-clock
**User Story**: As FX comparing a rate-limited hosted harness against a local
model that never throttles, I want reported time to exclude quota-backoff
stalls, with the excluded amount shown beside it, so that the comparison
measures agent speed rather than the state of FX's Claude quota.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a run that waited on rate limits **When** duration is reported (eval
  scoreboard, `sdlc status`, dashboard) **Then** an agent-time figure excluding
  `stall_seconds` is available, and the stalled amount is displayed alongside —
  neither figure replaces the other silently.
- **Given** a run that never stalled **Then** agent time equals wall-clock and
  the stall figure renders as absent (the existing None-not-zero convention),
  so nothing changes visually for the common case.
- **Given** an eval dispatch that stalls mid-ticket **When** the ticket is
  scored **Then** the stall is attributed to that ticket and excluded from its
  `wall_s`, so a single throttled ticket cannot skew `wall_mean`.
- **Given** `eval-compare` across two harnesses **Then** the wall metric it
  compares is the stall-adjusted one, and the report states that explicitly.
- **Given** existing ledger rows with no stall records **Then** adjusted time
  degrades to raw wall-clock (no migration, no retroactive claims).

**Technical Notes**: The data already exists — `record_rate_limit_stall`
(`build.py:3026`) writes stalls as their own dimension precisely so durations
stay diagnosable, and the snapshot surfaces `stall_seconds` per story
(`build.py:4121`) and per run (`build.py:4156`). What is missing is the
subtraction at the reporting edge and the equivalent capture inside the eval
harness, whose `time.monotonic()` spans (`evaluate.py:532`, `:558`) currently
include any in-process backoff. Do the subtraction in the derived-duration
helpers (`_duration_seconds` / `_story_duration_seconds`, `build.py:3911`),
not by mutating stored timestamps — the ledger stays a record of what happened.
Note the asymmetry to document: `qwen` declares `rate_limit_aware: false`, so
the local arm has no stall concept at all; absent is correct there, not zero.

**Definition of Done**:
- [ ] Stall-adjusted duration in the derived helpers; raw timestamps untouched
- [ ] Eval harness attributes and excludes stalls per ticket
- [ ] CLI/dashboard/scoreboard show agent time with stalled time beside it
- [ ] `eval-compare` compares the adjusted metric and says so
- [ ] Tests: stalled vs unstalled runs, per-ticket attribution, legacy fallback

**Dependencies**: none
**Risk Level**: Medium

##### Story 31.2-002: Honest token accounting for harnesses without usage telemetry
**User Story**: As FX comparing token consumption across harnesses, I want a
harness that cannot report usage to produce an explicit "unavailable" that
propagates into the scoreboard and blocks a token verdict, so that a local
arm's missing telemetry can never be read as "used no tokens".
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a harness declaring `usage_tracking: false` **When** an eval or
  build stage completes **Then** the token figure is recorded as unavailable
  (None), never 0, everywhere it surfaces — ledger, scoreboard, dashboard.
- **Given** a scoreboard whose token metric is unavailable **When**
  `eval-compare` runs against one that has it **Then** the token and cost rows
  report "not comparable" with the reason, and the overall verdict is computed
  from the metrics that *are* comparable, stating which were excluded.
- **Given** a harness that grows usage telemetry later (Epic-29's 29.2-003 for
  OpenCode) **When** it reports usage **Then** the token axis becomes
  comparable with no change to this machinery — capability-driven, not
  hardcoded per harness.
- **Given** the estimation fallback (`estimated_tokens` /
  `estimated_cost_usd`) **When** it is the only figure available **Then** it is
  labelled as an estimate in every surface and never silently compared against
  a measured figure from the other arm.
- **Given** a local model where an approximate token count *is* obtainable from
  the serving layer **Then** the design leaves a documented seam for supplying
  it, without this story committing to any particular server.

**Technical Notes**: `capability.py`'s conservative default (undeclared means
absent) is the right basis — drive this off `usage_tracking` rather than a
harness-name check. `_sum_tokens` (`build.py:3955`) already returns None rather
than 0 "so the dashboard renders '—' rather than a misleading zero"; extend
that discipline through the scoreboard and the comparator, which currently
assume every metric is a float (`_metric_value`, `eval_compare.py:162`). The
verdict logic (`ticket_verdict`, `:131`) must tolerate a partially-comparable
metric set without letting an excluded axis look like a pass. This is the
single highest-risk hole in the baseline: without it the local arm looks free.

**Definition of Done**:
- [ ] Unavailable-vs-zero enforced end to end for `usage_tracking: false` harnesses
- [ ] `eval-compare` marks token/cost not-comparable and excludes them from the verdict, naming the exclusions
- [ ] Estimates labelled as estimates and never compared against measurements
- [ ] Capability-driven (no per-harness special cases); documented seam for external token counts
- [ ] Tests: false-telemetry harness, mixed comparison, estimate labelling, verdict with excluded axes

**Dependencies**: 31.1-001; complements Epic-29 29.2-003
**Risk Level**: High

##### Story 31.2-003: A cost figure that means something for local inference
**User Story**: As FX weighing local inference against hosted API spend, I want
the cost metric to distinguish metered spend from local inference (whose
marginal cost is not $/token), so that "cost" in a cross-harness comparison is
either a real number or an explicitly labelled non-comparison.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a hosted harness **When** cost is reported **Then** behaviour is
  unchanged: metered or notional $/Mtok as today.
- **Given** a local harness **When** cost is reported **Then** it is not a
  $/Mtok extrapolation; it is either an explicitly configured local rate
  (default: unset) or "not metered", and the scoreboard says which.
- **Given** a cross-harness comparison where one arm is not metered **When**
  `eval-compare` runs **Then** the cost row is a labelled non-comparison
  (reusing 31.2-002's not-comparable machinery), never a delta implying the
  local arm saved a specific dollar amount.
- **Given** a configured local rate (e.g. an energy-per-hour figure) **Then**
  it is recorded as provenance so the assumption travels with the number.
- **Given** Epic-14/28 cost governance paths **Then** they are unaffected — a
  not-metered harness must not break budget gates or calibration, and the
  behaviour when one is encountered is documented.

**Technical Notes**: `notional_cost` / `DEFAULT_USD_PER_MILLION_TOKENS`
(`cost_estimate.py`, consumed at `evaluate.py:298`) encodes a hosted-API
assumption that simply does not hold for a model running on FX's own hardware;
the fix is to make the assumption explicit and optional, not to invent an
energy model. Keep this deliberately thin — the honest answer for the baseline
is "tokens are the currency on the local arm, dollars on the hosted one".

**Definition of Done**:
- [ ] Metered vs not-metered distinction in the cost path, defaulting to today's behaviour
- [ ] Optional configured local rate, recorded as provenance
- [ ] Cost row rendered as a labelled non-comparison when arms differ
- [ ] Budget/calibration paths verified unaffected by a not-metered harness
- [ ] Tests: hosted unchanged, local not-metered, configured rate, mixed comparison

**Dependencies**: 31.2-002 (shares the not-comparable machinery)
**Risk Level**: Low

### Feature 31.3: The Baseline

Run it, record it, and write down the method — so the conclusion outlives the
session that produced it.

#### Stories

##### Story 31.3-001: Recorded claude-vs-local baseline
**User Story**: As FX deciding whether local agentic coding is viable, I want a
recorded head-to-head over the fixed ticket set — claude versus the local Qwen
model, and OpenCode where Epic-29 has landed it — with quality, tokens and
stall-adjusted time, so that the decision rests on my own workload rather than
on published benchmarks.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the rig from 31.1/31.2 **When** the baseline runs **Then** each arm
  executes the identical ticket set at the same `n` (≥3), on the same machine,
  and each produces a provenance-complete scoreboard committed under
  `controller/eval/`.
- **Given** the arms differ in parallel capability (`qwen` declares
  `parallel: false` and `worktree_isolation: false`) **When** the baseline runs
  **Then** every arm runs serially, and the report states this — a parallel
  Claude cohort must never be compared on wall-clock against a serial local run.
- **Given** the scoreboards **When** `eval-compare` runs **Then** the recorded
  output includes the per-metric deltas, the verdict, and any axes excluded as
  not-comparable, verbatim.
- **Given** a local arm that fails outright on some tickets (dispatch failure,
  malformed result envelope) **Then** those are reported as failures with their
  category, not dropped — a harness that cannot complete the work is a finding,
  not missing data.
- **Given** the results **Then** a short go/no-go records which stages (if any)
  FX would route to local inference, and what would have to change for the rest.

**Technical Notes**: The existing `strutils-baseline` ticket set
(`controller/eval/eval-config.yaml`: three small, well-scoped edits each gated
by `pytest -q`) is the right starting instrument — small enough to run `n≥3`
across three harnesses without absurd spend, and already versioned. Expect the
local arm to expose adapter-level failures before it exposes quality
differences; that is a legitimate result. Record the model-serving setup
(server, quantisation, context length) as provenance — it dominates the numbers
and is not otherwise captured. Watch the eval's per-dispatch timeout
(`evaluate.py:30`): a slower local model may need a raised ceiling, and a
timeout must be recorded as a timeout, not as a quality failure.

**Definition of Done**:
- [ ] Provenance-complete scoreboard per arm, committed under `controller/eval/`
- [ ] Serial-vs-serial discipline enforced and stated
- [ ] Recorded `eval-compare` output with deltas, verdict, and exclusions
- [ ] Failures/timeouts reported by category, not silently dropped
- [ ] Written go/no-go on routing real stages to local inference

**Dependencies**: 31.1-001, 31.1-002, 31.2-001, 31.2-002; Epic-29 29.2-001 for
the OpenCode arm (the qwen arm can proceed without it)
**Risk Level**: Medium

##### Story 31.3-002: Benchmarking method and re-run runbook
**User Story**: As FX (or a colleague) re-running this in three months against
a newer local model, I want the method written down — what is measured, what is
deliberately not comparable, and the exact commands — so that a re-run is a
repeat rather than a re-derivation.
**Priority**: Should Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** the doc **Then** it states each metric, its source, its precision
  (ledger whole-second vs eval `time.monotonic()`), and why the two are never
  mixed in one table.
- **Given** the doc **Then** it lists the known non-comparabilities and their
  causes — telemetry-less harnesses, not-metered cost, stall asymmetry,
  serial-vs-parallel — so a reader cannot innocently misread a scoreboard.
- **Given** the doc **Then** it gives the literal command sequence to reproduce
  a baseline for a new harness or model, including the provenance to capture
  about the serving setup.
- **Given** a future harness added to the registry **Then** the doc states what
  must be true before it can join a comparison (probe passes, capabilities
  declared, telemetry status known).

**Technical Notes**: Belongs beside the controller architecture docs, linked
from the eval config's comments so it is found from the artifact rather than
only from the docs tree. Keep it a method note, not a results archive —
31.3-001's scoreboards are the results and they carry their own provenance.

**Definition of Done**:
- [ ] Method doc: metrics, sources, precision, non-comparabilities
- [ ] Literal re-run command sequence including serving-setup provenance
- [ ] Admission criteria for adding a harness to a comparison
- [ ] Linked from the eval config and the controller docs

**Dependencies**: 31.3-001
**Risk Level**: Low

## Epic Sequencing

31.1-001 is strictly first — nothing else can be measured cross-harness until
the eval rig can select one. 31.1-002 follows immediately, because an
unattributable scoreboard is worthless the moment there is more than one arm.
31.2-001 and 31.2-002 are independent of each other and can run in parallel;
31.2-002 is the highest-risk story in the epic and the one that most changes
the baseline's conclusion, so it should not be deferred. 31.2-003 depends on
31.2-002's not-comparable machinery and is the cheapest to drop under time
pressure. 31.3-001 is the epic's acceptance test and needs 31.1 plus 31.2-001
and 31.2-002; 31.3-002 records the method once the numbers exist. Recommended
serial order: 31.1-001 → 31.1-002 → 31.2-002 → 31.2-001 → 31.2-003 → 31.3-001
→ 31.3-002.

The qwen arm of 31.3-001 can proceed on Epic-29's current state; the OpenCode
arm blocks on 29.2-001 (OpenCode is not in the registry today) and improves
materially with 29.2-003 (real usage telemetry, which would move OpenCode from
the not-comparable token path onto the measured one).
