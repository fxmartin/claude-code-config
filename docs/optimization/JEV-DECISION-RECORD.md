<!-- ABOUTME: Decision record: TypeSafe's Jev evaluated as a routing judge for the sdlc controller, rejected -->
<!-- ABOUTME: One page. Revisit only if the fleet moves to API-key billing or a coding-difficulty benchmark appears. -->

# Decision record: Jev (TypeSafe) as a model-routing judge

**Date**: 2026-10-16
**Decision**: rejected. No judge seam, no Jev backend, no shadow run.
**Revisit when**: the fleet bills on API keys instead of the Max subscription, or an independent benchmark scores Jev on coding-task difficulty classification.

## What Jev is

A hosted decision model. It takes text state and returns typed answers (`Choice`, `Score`, `Noul`) with calibrated probabilities in under 250 ms at $0.042 per million input tokens, output unmetered. It does not generate text, so it can only replace a decision, never a build, review, bugfix or envelope-repair stage. Early access behind a waitlist (OpenRouter and Vercel AI Gateway bypass it). Python `typesafe-sdk`, TypeScript `@typesafe-ai/sdk`, pinned build `jev-1.13.0`. Context budget reported as 32K to 64K of state; official docs were unreachable from the analysis session.

## What it could have touched here

Only the controller's existing decision points: tier per stage (`model_routing.select_model`), effort per stage (`select_effort`), `sdlc fix` complexity, and review-finding triage. The interactive Claude Code path, where 94 percent of tokens ran on Opus in the June to July baseline, is out of reach: there is no per-prompt model switch a hook can drive, and a routing proxy through `ANTHROPIC_BASE_URL` is a BLOCK rule in `supply_chain_scan.py`.

## Why rejected

| Reason | Evidence |
|---|---|
| The saving is small and not cash | Balanced routing plus the effort map already took the large cuts (`eval-compare-34.4-003`: minus 31 to 37 percent at equal quality). A better escalation and effort judge is worth 5 to 15 percent of a controller bill that is an API-equivalent figure on a Max subscription. Base case about $0.40 per story, $60 a month notional. |
| Haiku does the same job inside the trust boundary | jev-bench (n=72, 2026-10-14): Jev 94.4 percent, Haiku 5.5 95.8 percent, Opus 5.5 97.2 percent. jev-agent-router (n=111): Jev 98 percent, Haiku 99 percent. Jev's edges are latency and calibration, neither of which matters when the stage it gates runs for minutes. |
| No evidence on the decision that matters | Published results cover support routing, phishing and severity. Nothing on "how hard is this user story". On the one nuanced rubric (severity) Jev trailed Opus by 9 points. |
| Cannot be validated with the data we have | The ledger holds 76 reconciled stories. Community guidance is 1,000 to 2,000 labelled decisions before trusting a threshold. |
| New vendor, new egress | Story text and findings leave the Anthropic boundary; retention terms not found. Engineering estimate 9 to 11 stories, comparable to the first year of notional savings. |

## What we do instead

Story 34.5-001 in `docs/stories/epic-34-current-generation-models.md`:

1. Bump the `haiku` tier from `claude-haiku-4-5` ($1 / $5 per MTok) to `claude-haiku-5-5` ($0.10 / $0.50) after a live entitlement probe. Merge, reask, summary and the self-check probes ran 13 percent of the baseline bill on Haiku 4.5 prices.
2. Calibrate `predicted_tokens_threshold` and `rework_threshold` in `.sdlc-model-routing.yaml` from this repo's own reconciled story totals, and decide whether queue jobs run with `--predict`. Without it the thresholds are dormant and the `points >= 8` fallback, which the code itself calls noise, governs escalation.

## Sources

Community field guide https://github.com/AbdelStark/awesome-typesafe-jev · jev-bench https://github.com/stern9/jev-bench · jev-agent-router https://github.com/PlainJane20/jev-agent-router · pricing round-ups https://www.eesel.ai/blog/typesafe-jev-pricing and https://befailproof.ai/jev/pricing/ · repository evidence `docs/optimization/BASELINE.md`, `controller/src/sdlc/model_routing.py`, `controller/src/sdlc/cost_estimate.py`, `controller/eval/results/eval-compare-34.4-003*.txt`.
