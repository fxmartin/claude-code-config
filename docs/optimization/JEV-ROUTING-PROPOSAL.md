<!-- ABOUTME: Analysis and proposal for using TypeSafe's Jev as a decision judge in the sdlc controller -->
<!-- ABOUTME: Analysis only. Untracked draft, 2026-10-15. Nothing in this document has been implemented. -->

# Jev (TypeSafe) in the autonomous SDLC: model selection and cost optimisation

**Status**: proposal, analysis only. Not committed, not implemented.
**Date**: 2026-10-15
**Scope**: the `sdlc` controller (`sdlc build`, `sdlc fix`, fleet queue, eval harness) and the interactive Claude Code path this repo configures.

---

## 1. Executive summary

**Verdict: do not adopt Jev as the router today. Build a small "judge" seam in the controller, run Jev and Claude Haiku 5.5 side by side in shadow mode for 60 days, then let the ledger decide.**

Three facts drive that verdict:

1. **The big savings are already harvested.** Balanced routing moved build, coverage and review from Opus to Sonnet, and the effort map took another 31 to 37 percent off at equal quality (`controller/eval/results/eval-compare-34.4-003*.txt`). What is left for a smarter router is the precision of the escalation decision and the effort level per story. That is worth roughly 5 to 15 percent of controller spend, not 50.
2. **Jev is a judge, not a worker.** It returns typed decisions with calibrated probabilities (Choice, Score, Noul) in under 250 ms for about $0.04 per million input tokens. It cannot write code, review a diff in prose, or repair a JSON envelope. The only stages it can touch are the ones that already produce a decision: tier and effort selection, fix complexity, review-finding triage.
3. **Independent benchmarks put Jev level with Haiku on classification, not ahead.** jev-bench (72 decisions, 2026-10-14) scored Jev 94.4 percent against Haiku 5.5 at 95.8 percent and Opus 5.5 at 97.2 percent. A 111-message routing test scored Jev 98 percent against Haiku 99 percent. Nobody has published a result on coding-task difficulty classification, which is the decision this SDLC needs. Jev's edges, latency and calibration, matter little in a pipeline whose stages run for minutes.

**Realistic savings**: about $0.40 per story notional (base case) on a modelled post-Balanced run-rate of roughly $4 per story, so 10 percent, with a range of 5 to 15 percent. At 150 stories a month that is $60 a month in API-equivalent terms. On the current Claude Max subscription this is not cash. It is quota headroom: fewer Opus tokens, fewer rate-limit stalls, shorter runs.

**Jev's own cost is negligible**: under $0.01 per story. **The real cost is engineering** (8 to 12 stories), a new third-party data flow for story text and diff excerpts, and an early-access vendor with a waitlist.

**Two cheaper moves come first**:

- **Bump the `haiku` tier from Haiku 4.5 to Haiku 5.5.** Haiku 5.5 lists at $0.10 input and $0.50 output per million tokens, ten times cheaper than Haiku 4.5 at $1 and $5. Merge, reask and summary stages ran 13 percent of the baseline bill on Haiku 4.5 prices. One line in `model_routing.py` and the price table, plus an eval run on the merge stage. Verify entitlement on the Max plan first.
- **Make the judge a seam, not a vendor choice.** A `Judge` protocol with `jev`, `haiku` and `fake` backends costs the same to build as a Jev-only adapter and keeps the decision reversible.

---

## 2. What Jev is, as far as it can be verified

Direct access to `docs.typesafe.ai` is blocked from this environment, so the facts below come from search summaries, the awesome-typesafe-jev field guide, and two community benchmark repositories. Treat version, context and pricing figures as "verify on the official docs before building".

| Topic | What the sources say | Confidence |
|---|---|---|
| What it is | A "System One" decision model. Takes unstructured state as text, returns typed answers in one parallel pass. Does not generate text. | High (consistent across all sources) |
| Primitives | `Choice` (up to 255 options, returns per-option probabilities and a selected option), `Score` (ordinal rubric, per-level probabilities), `Noul` (a yes/no probability from 0 to 1). Several questions share one request and one state. | High |
| Response shape | Model id, `answers` keyed by question id, per-answer `probabilities` and `confidence`, usage fields. Confidence is derived from the shape of the distribution. | Medium (third-party guides) |
| Endpoint and SDKs | `POST https://api.typesafe.ai/v1/systemone`, model route `jev-latest`, pinned build `jev-1.13.0`. Python `typesafe-sdk` (3.10+, sync and async), TypeScript `@typesafe-ai/sdk` (Node 20+). Key in `TYPESAFE_API_KEY`. Also reachable through OpenRouter's decisions API and Vercel AI Gateway. | High |
| Pricing | $0.042 per million input tokens, output unmetered. No published tiers. | High |
| Latency | Vendor claims 70 to 500 ms. Community measurements: 116 ms median routing, 171 ms p50 on jev-bench, under 250 ms p95. | High |
| Context | Conflicting: 32K, 64K total with a 32K state budget, or 262K on OpenRouter's listing. Assume 32K of state until confirmed. | Low |
| Rate limits | 250,000 tokens per second, 1,200 requests per minute, "adjusting dynamically". | Medium |
| Availability | Early access behind a waitlist. OpenRouter and Vercel routes bypass the waitlist. General availability date not established. | High |
| Language | Text only, English preferred. | Medium |
| Claude Code integration | A vendor skill (`claude plugin marketplace add typesafe-ai/skills`) and a community plugin (`jev-use`) that batches typed questions and offers a PreToolUse gate. Neither changes which Claude model a session runs on. | Medium |
| Model-routing precedents | Jevonian (local proxy whose `auto` route asks Jev for model and thinking level), Distill (coding harness using Jev to pick model and effort), LangChain middleware pattern. All experimental, none with published accuracy on coding tasks. | Medium |

### Benchmark evidence

| Study | Task | Jev | Haiku | Opus / best | Note |
|---|---|---|---|---|---|
| jev-bench, 2026-10-14, n=72 | support routing, phishing, incident severity | 94.4 % | 95.8 % (Haiku 5.5) | 97.2 % (Opus 5.5), 100 % (GPT-6.1 Sol) | Jev $0.017 per 1k decisions, Haiku $0.12, Opus $4.33. Jev ECE 0.025 on routing, Claude models 0.08 to 0.13. |
| jev-agent-router, n=111 x 3 runs | 4-way support routing | 98 % | 99 % (Haiku 4.5) | n/a | 116 ms vs 607 ms median. Two models "indistinguishable on accuracy". |
| TypeSafe's own 4-workflow benchmark | mixed | 67.8 % | n/a | 73.1 % (Opus 5), 74.1 % (GPT-5.6 Sol) | Vendor-run. |
| Independent phishing test | yes/no | 62.6 % | 81.3 % (Haiku 4.5) | n/a | Jev's weakest published result. |
| Janus cascade, n=500 x 2 | Banking77, Web of Science | cascade helped on Banking77, matched Jev alone on WoS at 47 % higher cost | | | Cascades are not free wins. |

Read: on short, well-bounded classification Jev is a Haiku-class judge at a fraction of the latency and price. On nuanced rubrics (adjacent severity levels) it trails Opus. There is no evidence either way on "how hard is this user story", which is the question that matters here.

---

## 3. Where models are chosen in this SDLC today

The controller already contains a per-stage router, an effort router, a predictor, a ledger with per-stage tokens and cost, and an eval harness. Jev would not replace any of this. It would feed one signal into it.

| # | Decision point | Mechanism today | Signal quality | Jev fit |
|---|---|---|---|---|
| 1 | Interactive session default (planning skills, commands, personal agents) | CLI default, historically Opus. 94 % of interactive output tokens ran on Opus in the June to July window (`docs/optimization/BASELINE.md`). | None | **None.** Claude Code has no supported per-prompt model switch a hook could drive. A routing proxy via `ANTHROPIC_BASE_URL` is a BLOCK rule in the supply-chain scanner (`controller/src/sdlc/supply_chain_scan.py:79-83`) and would route subscription OAuth traffic through a third party. Not an option. |
| 2 | Agent-tool dispatch | `model: sonnet` pinned on 7 code agents | Static | None |
| 3 | `sdlc build` tier per stage | Balanced profile: build/coverage/review/bugfix on Sonnet, merge/reask on Haiku. Escalation of build/review/adversarial to Opus when `high_risk`, or a confident prediction crosses 2M tokens or 0.5 rework probability, else `points >= 8` (`model_routing.py:302-358`). | The code's own comment: story points "carry almost no cost signal (172 of 193 builds on one of two values)". The predictor is "priors, not fitted coefficients" on n=76 stories. | **Yes.** A `Choice` over {haiku, sonnet, opus} with the story text and discovery features as state, used as an `EscalationSignal` input. |
| 4 | `sdlc build` effort per stage | Static map: build high, review xhigh, coverage medium (`BALANCED_STAGE_EFFORT`). Same effort for a one-line story and a schema migration. | Static | **Yes.** A `Score` over {low, medium, high, xhigh} per story for build and coverage. The 34.4-003 eval shows medium instead of high on easy work saves a further 6 points of cost at equal quality. |
| 5 | `sdlc fix` escalation | Sonnet investigation agent emits `complexity` LOW/MEDIUM/HIGH inside a larger JSON plan; HIGH escalates build/review/bugfix to Opus (`fix_issue.py:118-141, 990-1000`). No effort routing on this path. | A Sonnet agent in the loop; reasonable | **Partial.** Jev can pre-classify the issue before investigation (cheap second opinion, and the investigation agent itself could be tiered down to Haiku on LOW). It cannot replace the investigation, which must write a root cause and plan. |
| 6 | Risk gate | Deterministic path-pattern match (`risk_gate.py`), label `risk:high` | Deterministic, auditable | Low. A `Noul` "does this diff touch auth, money or data paths" is possible but trades a deterministic gate for a probabilistic one. Not recommended for a gate. |
| 7 | Review verdict and findings | Review agent returns `APPROVED` or `CHANGES_NEEDED` plus findings; any changes request starts a bugfix loop (up to 2, or 3 when spread) | Agent-written | **Yes, small.** A `Noul` per finding, "is this blocking", to skip bugfix rounds on nits. Bugfix plus reask were 4 % of the baseline bill, so the ceiling is low. |
| 8 | Docs-only skip | Deterministic `git diff --name-only` classifier | Deterministic | None, already solved cheaply. |
| 9 | Retry escalation | Ladder haiku to sonnet to opus, plus one effort step per retry | Deterministic | None |
| 10 | Harness choice (Claude, Codex, Qwen, OpenCode) | `--harness` flag, `.sdlc-harness.yaml` | Manual | Possible later: a `Choice` over harnesses by task type. Out of scope for a first phase. |
| 11 | Eval judge | `claude plugin eval --judge-model haiku` | Haiku | Possible: Jev `Score` as a cheap grader. Out of scope. |

Integration seams already exist and are clean:

- `model_routing.select_model` and `select_effort` are pure functions fed an `EscalationSignal`. Adding `judge_tier`, `judge_effort` and `judge_confidence` to that dataclass is a surgical change.
- `.sdlc-model-routing.yaml` is the per-repo override file. A `judge:` block fits there.
- The ledger `stages` table already records model, effort, tokens and cost per stage attempt, and `stories` records predictions and actuals. A judge decision recorded next to them gives a free accuracy report.
- `sdlc eval-compare` with its 10 percent tolerance is the go/no-go instrument.

---

## 4. Proposed design: a judge seam, Jev as one backend

### 4.1 Principle

Jev proposes, code decides. Thresholds live in `.sdlc-model-routing.yaml`, not in the vendor. Every judge call is logged to the ledger. Every backend is replaceable, and CI runs offline on a fake.

### 4.2 Components

**`controller/src/sdlc/judge.py`** (new, about 200 lines)

```
class Judge(Protocol):
    def choice(self, state: str, question: str, options: list[str]) -> ChoiceAnswer
    def score(self, state: str, question: str, levels: list[str]) -> ScoreAnswer
    def noul(self, state: str, question: str) -> NoulAnswer

Backends:
  jev     -> typesafe-sdk, model pinned (jev-1.13.0), TYPESAFE_API_KEY, 5 s timeout, no retry storms
  haiku   -> claude -p --model claude-haiku-5-5 --output-format json with a strict schema, same contract
  fake    -> deterministic answers from a fixture; the only backend the test suite and CI ever touch
  off     -> returns None; routing behaves exactly as today
```

Every answer carries `backend`, `model_version`, `probabilities`, `confidence`, `latency_ms`, `input_tokens`.

**Ledger** (`stories` table): `judge_backend`, `judge_tier`, `judge_tier_conf`, `judge_effort`, `judge_effort_conf`, `judge_applied` (0 in shadow mode). Migration follows the existing additive-column pattern.

**Routing** (`model_routing.py`):

- `EscalationSignal` gains `judge_tier`, `judge_effort`, `judge_confidence`.
- `select_model`: when `judge_confidence >= min_confidence` and the judge is enabled (not shadow), the judged tier wins over the predictor and points fallback. The `high_risk` path is untouched and still forces Opus. A judge can only move a stage within the tier ladder, never below the profile's floor on a high-risk story.
- `select_effort`: same rule, one level down at most from the profile's level, never above `max`.

**Config** (`.sdlc-model-routing.yaml`):

```yaml
model_routing:
  profile: balanced
  judge:
    backend: jev        # jev | haiku | off
    shadow: true        # log only, never applied
    min_confidence: 0.75
    stages: [build, coverage, review]
    max_state_chars: 24000
```

**Question set, phase 1** (one request, one state: story markdown, acceptance criteria, discovery features, file list, repo language):

- `tier`: Choice over {haiku, sonnet, opus}. "Which model tier can implement this story correctly on the first attempt?"
- `effort`: Score over {low, medium, high, xhigh}. "How much reasoning does the build stage need?"
- `rework_risk`: Noul. "Will a mid-tier model's first attempt need a review-driven fix?"

**`sdlc fix`**: a `complexity` Choice over {LOW, MEDIUM, HIGH} on the issue title, body and labels, recorded before the investigation. In shadow it is compared to the investigation agent's own assessment. If agreement is above 90 percent after 50 issues, the investigation stage itself tiers down to Haiku on judged LOW.

**Review triage** (phase 3): a `Noul` per finding, "does this finding block merge". Findings below the threshold are carried as a comment, not a bugfix round.

**Reporting**: `sdlc judge-quality` joins judged tier against realised tokens and `actual_rework`, by backend, and prints agreement with the predictor, the points fallback, and the stage outcome. Same shape as `sdlc predict-quality`.

### 4.3 Security and compliance

- New third-party egress: story text, acceptance criteria, file paths, and in phase 3 review findings. No source files, no diffs beyond file names in phase 1. `max_state_chars` caps the payload.
- `TYPESAFE_API_KEY` joins the dispatch environment allowlist. The supply-chain scanner gets an allowlisted pattern for the SDK import, nothing else.
- The controller's deny baseline is unaffected: the judge call happens in the controller process, not inside the agent sandbox.
- TypeSafe's data retention and residency terms are not published in the sources found. This must be read before phase 1 on any repository that is not personal. For client-adjacent work (bank code, customer names) the `haiku` backend keeps data inside the existing Anthropic trust boundary.
- Offline CI contract from `CLAUDE.md` holds: only the `fake` backend runs under test.

---

## 5. Savings forecast

### 5.1 Baseline and modelled run-rate

Measured baseline (`BASELINE.md`, pre-Balanced, Opus default, 50 stories): **$9.10 per story notional**. Build 58 %, coverage 18 %, review 13 %, merge 7 %, bugfix 3 %, reask 1 %.

Post-Balanced run-rate is not yet measured on a real cohort. Modelled with: Sonnet stages at about 55 % of Opus cost, Haiku stages at about 30 %, the effort map at minus 31 %, and about 25 % of stories escalating to Opus by points.

| | Per story, notional |
|---|---|
| Measured baseline, Opus everywhere | $9.10 |
| Modelled today (Balanced + effort map) | about $4.00 |

**Action before any go/no-go: re-run the `BASELINE.md` aggregation on a post-Balanced window and replace the modelled figure with a measured one.** Everything below scales with it.

### 5.2 Levers Jev can move

| Lever | Mechanism | Share of story cost touched | Expected saving (base) | Range |
|---|---|---|---|---|
| L1 Escalation precision | Fewer stories on Opus that Sonnet handles; fewer Sonnet stories that bounce to bugfix | build + review on the roughly 25 % of escalated stories | 5 % | 2 to 8 % |
| L2 Effort per story | medium instead of high on easy build and coverage work | build + coverage, about half of stories | 4 % | 2 to 6 % |
| L3 Fix complexity | Haiku investigation on LOW issues; fewer wrong-tier fix builds | `sdlc fix` runs only | 1 % | 0 to 3 % |
| L4 Review finding triage | Skip bugfix rounds on non-blocking findings | bugfix + reask, 4 % of bill | 1 % | 0 to 2 % |
| **Total** | | | **about 10 %** | **5 to 15 %** |

### 5.3 Monthly view

| Scenario | Stories / month | Saving / story | Notional saving / month | Jev cost / month |
|---|---|---|---|---|
| Low | 100 | $0.20 | $20 | under $0.50 |
| Base | 150 | $0.40 | $60 | under $1 |
| High | 250 | $0.60 | $150 | under $2 |

Jev cost assumption: 3 questions per story on about 6K tokens of state, plus 1 per fix issue and 1 per review finding, at $0.042 per million input tokens. It never matters.

### 5.4 What the saving really is on a Max subscription

The controller's dollar figures are API-list equivalents, not spend (`cost_estimate.py` says so in its own comments). The benefit is quota: a 10 percent cut in Opus-weighted tokens means fewer 5-minute rate-limit backoff loops, which the baseline shows were the cause of the 13 stages that ran over an hour. If the fleet ever moves to API keys, the dollar column becomes cash.

### 5.5 Cost to build

| Item | Estimate |
|---|---|
| Judge seam, fake backend, ledger columns, config, tests | 3 stories |
| Jev backend and Haiku backend | 2 stories |
| Shadow logging in build and fix, `judge-quality` report | 2 stories |
| Apply mode, eval runs, go/no-go record | 2 stories |
| Review triage (phase 3) | 2 stories |
| **Total** | **9 to 11 stories**, about $40 to $60 notional at the modelled run-rate, plus review time |

Payback in notional terms: about one to two months after apply mode. In quota terms: immediate but small.

---

## 6. Pros and cons

### Pros

- **Cheap and fast enough to ask on every story and every finding.** Latency and price are both two orders of magnitude below a Haiku call. This makes shadow mode free to run for months.
- **Calibrated probabilities.** A threshold in code on a calibrated confidence is a better control than a prompt that says "answer HIGH only when sure". jev-bench measured ECE of 0.025 on routing against 0.08 to 0.13 for Claude models.
- **Schema-constrained by construction.** No envelope repair, no reask stage for the judge call.
- **Zero-shot.** The predictor needs reconciled history and has n=76. Jev needs none to start, so a new repository gets a routing signal on day one.
- **Keeps the SDLC's existing control plane.** Everything stays in `select_model`, `select_effort`, the ledger and `eval-compare`.
- **A judge seam has uses beyond routing.** Skill selection, context pruning (the mintocode and Oko patterns), prompt-injection screening of fetched content, eval grading.

### Cons

- **No evidence on the decision that matters.** Published results cover support routing, phishing and severity. "How hard is this coding story" is a different distribution, and on the one nuanced-rubric task (severity) Jev trailed Opus by 9 points.
- **Haiku 5.5 does the same job inside the trust boundary.** Same accuracy within noise, no new vendor, no new egress, no waitlist. Its cost on the Max plan is quota, and a single short structured-output call per story is noise against a 12-minute build.
- **Early access.** Waitlist, one published model build, dynamic rate limits, no SLA found. Pinning a version is recommended by the vendor and should be mandatory here.
- **Third-party egress of story content.** Fine for a personal repository. Must be reviewed for anything touching client code or data. Retention terms unknown from the sources available.
- **Thin validation data.** The ledger has 76 reconciled stories. Community guidance is 1,000 to 2,000 labelled decisions before trusting a Jev threshold in production. Shadow mode over 60 days yields perhaps 150 to 300 stories. Enough to see a trend, not enough to be certain.
- **Context limit.** 32K to 64K of state. Story files fit. Full diffs do not, so review triage has to work on findings, not on the diff.
- **Bounded upside.** The analysis above caps the gain near 15 percent of a bill that is already notional. The engineering cost is similar to the first year of savings.
- **Vendor claims need discounting.** "Cannot hallucinate" and "0 percent structured-output error" are properties of the output schema, not measured accuracy.

---

## 7. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Jev under-escalates a hard story and Sonnet burns bugfix rounds | Medium | Medium | `high_risk` path untouched; judge can only move within the ladder; retry escalation still climbs; shadow mode first |
| Jev over-escalates and Opus share rises | Low | Low | `judge-quality` report shows Opus share by backend; apply mode gated on eval-compare at 10 % tolerance |
| API down or slow | Medium | Low | 5 s timeout, no retries, fall through to today's routing, logged once per run |
| Waitlist blocks access | Medium | Low | OpenRouter decisions route, or start with the Haiku backend |
| Model version drift changes decisions silently | Medium | Medium | Pin `jev-1.13.0`; `judge-quality` segments by `model_version` like `predictor_version` |
| Data sent to a third party | Certain | Depends on repo | Payload cap, no source, no diffs; Haiku backend for client-adjacent repos; read retention terms before phase 1 |
| CI gains a network dependency | Low | High | Only the `fake` backend under test, enforced by a test like `test_prompt_audit` |
| Engineering effort exceeds payback | Medium | Low | Phase gates; stop after shadow if agreement with realised outcomes is below the predictor's |

---

## 8. Phased plan with verifiable gates

```
Phase 0  Measure (1 week)
  - Re-run BASELINE.md aggregation on the post-Balanced window
  -> verify: measured $/story and Opus token share by stage, written to BASELINE.md §5

Phase 1  Seam + shadow (2 to 3 weeks of factory time)
  - judge.py with fake, haiku, jev backends; ledger columns; config; judge-quality
  - shadow: true on this repository, both jev and haiku logged per story
  -> verify: 100 % of build/fix stories carry a judge row; CI green offline; no change in routing argv

Phase 2  Decide the backend (after 60 days or 150 stories, whichever first)
  - judge-quality: agreement of judged tier with realised tokens and rework, per backend,
    versus the predictor and the points fallback
  -> gate: a backend wins only if its tier agreement beats the points fallback by 10 points
     and its Opus share is not higher. Otherwise stop here and keep the seam dormant.

Phase 3  Apply effort first, then tier (2 weeks)
  - effort: apply on build and coverage; sdlc eval-compare, tolerance 10 %, quality must hold
  - tier: apply on build and review
  -> gate: eval-compare BETTER or NEUTRAL on cost with qual unchanged; GO-NO-GO record in eval/results

Phase 4  Fix path and review triage (2 weeks)
  - fix complexity pre-classification; finding-level Noul triage
  -> verify: bugfix rounds per story down, no merged story reopened within 14 days
```

Stop rules: any phase whose gate fails ends the initiative with the seam left in `off`. No sunk-cost continuation.

---

## 9. Alternatives considered

| Option | Saving | Effort | Verdict |
|---|---|---|---|
| **A. Haiku 5.5 as judge through the same seam** | Same levers as Jev | Same seam, no vendor | Run in parallel. Likely winner on trust boundary alone. |
| **B. Bump `haiku` tier to Haiku 5.5** | Merge, reask, summary, probes at one tenth of Haiku 4.5 prices: up to 10 % of the baseline bill | One line in `TIER_MODEL_IDS`, one in the price table, one eval run on merge | **Do first**, after checking entitlement on the Max plan and merge-stage quality. |
| **C. Calibrate predictor thresholds on reconciled history** | Part of L1 | Zero code, config only | Do now. `predicted_tokens_threshold` and `rework_threshold` are per-repo overrides. |
| **D. Opus at lower effort instead of Sonnet at high** | Unknown, the Anthropic guidance says lower effort on the newest model often matches a lower tier at high effort, and keeps one cache namespace | One eval run | Worth one `eval-compare` before any router work. |
| **E. Jevonian-style routing proxy for the interactive path** | Could touch the 94 % Opus interactive share | Blocked | Rejected: `ANTHROPIC_BASE_URL` is a BLOCK rule, and it would route subscription traffic through a third party. |
| **F. Do nothing** | 0 | 0 | Acceptable. The remaining upside is bounded. |

---

## 10. Recommendation

1. **Now**: option B (Haiku 5.5 tier bump, after entitlement check) and option C (threshold calibration). Both are config-level and measurable with `eval-compare`.
2. **Next**: build the judge seam with `fake`, `haiku` and `jev` backends and run both real backends in shadow on this repository for 60 days. Apply for Jev early access in parallel, or use the OpenRouter route for the shadow run.
3. **Then**: let `judge-quality` pick the backend. Apply effort routing first, tier second, each behind an `eval-compare` gate.
4. **Never**: route interactive Claude Code traffic through a third-party proxy, or let a probabilistic judge replace the deterministic risk gate.

If the shadow data shows Haiku 5.5 and Jev tied, prefer Haiku: one vendor, one trust boundary, one bill.

---

## 11. Open questions to close before phase 1

- Official context budget and whether `jev-latest` or a pinned build is the production recommendation (docs blocked from this environment).
- TypeSafe data retention, residency and training-use terms.
- Whether Claude Haiku 5.5 is entitled on the Max plan through `claude -p --model claude-haiku-5-5`.
- Current measured post-Balanced $/story and Opus share (phase 0).
- Whether the fleet will ever bill on API keys. If yes, the dollar column in this document becomes real money and the case strengthens.

---

## Sources

Official pages were unreachable from this session (network policy). Figures come from:

- Awesome Jev field guide (community, source-linked): https://github.com/AbdelStark/awesome-typesafe-jev
- jev-bench results 2026-10-14: https://github.com/stern9/jev-bench
- jev-agent-router benchmark: https://github.com/PlainJane20/jev-agent-router
- TypeSafe quickstart (not fetched, referenced by the guide): https://docs.typesafe.ai/introduction/quickstart
- Pricing round-ups: https://www.eesel.ai/blog/typesafe-jev-pricing , https://befailproof.ai/jev/pricing/ , https://openrouter.ai/typesafe/jev-1.14
- Reviews and explainers: https://www.datacamp.com/blog/system-one-models-jev , https://www.inovex.de/en/blog/typesafe-ai-jev-review-how-good-is-the-new-model-for-ai-classification/ , https://www.langchain.com/blog/building-a-harness-with-jev , https://dev.classmethod.jp/en/articles/jev-for-llm-model-routing/ , https://greennode.ai/blog/what-is-jev , https://dev.to/arifulislamat/typesafes-jev-model-is-it-really-193x-faster-and-444x-cheaper-56oa , https://www.firecrawl.dev/blog/what-is-jev , https://www.marktechpost.com/2026/09/19/typesafe-ai-releases-jev/
- Repository evidence: `docs/optimization/BASELINE.md`, `controller/src/sdlc/model_routing.py`, `controller/src/sdlc/cost_estimate.py`, `controller/src/sdlc/predictor.py`, `controller/src/sdlc/fix_issue.py`, `controller/eval/results/eval-compare-34.4-003*.txt`, `controller/src/sdlc/supply_chain_scan.py`
- Claude list prices: Anthropic pricing table as cached in the claude-api skill on 2026-10-06
