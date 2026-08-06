<!-- ABOUTME: ADR-005 mapping a local shared-model multi-agent coding target (e.g. Qwen on Apple Silicon) onto the sdlc controller. -->
<!-- ABOUTME: Decision: reuse the controller + qwen harness as the orchestration/guardrail layer; keep serving out of scope. Status: Proposed. -->

# ADR-005: Local Multi-Agent Inference as a Controller Target

- **Status**: Proposed
- **Date**: 2026-08-06
- **Epic / Story**: Epic-29 (Harness Parallelism) / Epic-20 (Cross-Harness Portability)
- **Deciders**: FX

## Context

A recurring design question: can this repo drive a *local* multi-agent coding
setup — several open-weight models (e.g. Qwen 27B) coordinated on a single
workstation (e.g. an M5 Max, 128 GB unified memory, ~614 GB/s bandwidth), with
an orchestrator, isolated workers, and a review gate?

The proposed reference architecture is the familiar one:

- **Orchestrator** — decompose the request, select context per worker, track
  dependencies, review, control the merge.
- **Workers A/B** — implement one bounded component each, in their own git
  worktree, running targeted tests.
- **Reviewer** — diff review, integration tests, regressions, request
  corrections before merge.
- **Optional 5/6** — security review, static-analysis triage, docs.
- Concurrency target: **6 logical agents, 3–4 decoding simultaneously**, on
  **one shared model instance** (do NOT load one model per agent), Q5/Q6, with
  per-agent context budgets and a required validation step before results
  return.

The critical constraint is bandwidth, not RAM: a dense 27B model is
memory-bound per token, and N independent model processes contend for the same
memory bandwidth. The architecture's own rule is therefore *one shared inference
server, several thin clients*.

This ADR records how that target maps onto what the controller already provides,
where the single seam is, and — deliberately — what stays out of scope.

## Decision

**The local multi-agent target is served by the existing `sdlc` controller as
the orchestration and guardrail layer, driving the existing `qwen` harness
(`controller/src/sdlc/config/harnesses.yaml`) configured to point at one shared
local OpenAI-compatible endpoint. The model-serving layer (llama.cpp / MLX,
quantization, parallel decode slots, continuous batching) is out of scope for
this repo — it is the endpoint the harness talks to, not something the
controller owns.**

Two consequences follow from this decision and are load-bearing:

1. **The orchestrator is deterministic Python, not an LLM agent.** Per ADR-001,
   task decomposition, dependency ordering (`cohort.py`), scheduling
   (`build.py`), and merge control are code. The reference architecture's
   "Agent 1: Orchestrator" with a 64K–128K resident context is *removed*, not
   ported.
2. **"One shared model, load once" is satisfied by construction, not by new
   code.** The controller spawns N Qwen Code CLI processes, but each is a thin
   client to the *same* endpoint. Weights load once in the server; the CLI
   processes hold conversation state and tool loops, not model weights.

## Mapping

| Reference-architecture component | Controller mechanism (exists today) |
|---|---|
| Orchestrator (decompose, select context, track deps, review, merge) | The `sdlc` controller — deterministic Python state machine (`build.py`), dependency cohorts (`cohort.py`), work-package extraction (`discovery.py`). **Not an LLM; costs zero model memory and zero decode slots.** |
| Workers A/B (isolated component, own worktree, targeted tests) | Per-story build agents dispatched into **per-story git worktrees** (branch cut from a fresh `origin/main`; `dispatch.py`, worktree hooks in `hooks/`), agent auto-selected by story type (`role_routing.py`) |
| Reviewer / tester (diffs, integration tests, regressions, corrections before merge) | `senior-code-reviewer` agent + coverage gate + Playwright e2e gate + bugfix loop (max 2 retries); optional Codex adversarial cross-harness review |
| Optional 5/6 (security, static analysis, docs) | `security-review` skill, `qa-engineer`, `risk_gate.py` / `adversarial.py` |
| "6 logical agents, 3–4 decoding simultaneously" | Continuous ready-queue dispatch over a bounded `ThreadPoolExecutor` — `--concurrency=N` (default 5). Readiness recomputed on every completion, so an agent blocked on tests/builds frees its slot — the architecture's own insight, already realized |
| Per-agent: own state, worktree, scratch, bounded task, tool allowlist, max context/turns, required validation | Worktree isolation ✓; per-agent tool allowlists (agent frontmatter) ✓; bounded task = one story ✓; **required validation = every stage returns a `<<<RESULT_JSON>>>` block validated against a per-stage JSON schema (`contracts.py`) before the next stage runs** |
| Merge authority: orchestrator only | Per-story `build → coverage → review → merge`; merge gated on green CI; high-risk PRs held `AWAITING_APPROVAL`; dependents `BLOCKED` if a dep fails |
| **One shared model, don't load per agent** | The `qwen` harness invokes `qwen -p` (thin CLI client). Point every process at one local OpenAI-compatible endpoint → weights load once in the shared server, by construction |

## The seam

The only integration point is the harness → endpoint wiring, and it is
config, not code:

- **Harness**: the `qwen` entry already exists in `harnesses.yaml` (adapter
  `scripts/qwen-build-adapter.sh`, parser `codex-exec`). Route roles to it with
  `default: qwen`, a per-repo `.sdlc-harness.yaml`, or `--harness role=qwen`.
- **Endpoint**: Qwen Code is a Gemini-CLI fork that speaks to OpenAI-compatible
  providers. Point it at the local server (llama.cpp / MLX) via its
  OpenAI-compatible endpoint environment (base-URL / API-key / model vars —
  *exact variable names to be confirmed against the installed Qwen Code
  version*) and, if needed, `QWEN_FLAGS='--model <served-model>'` /
  `QWEN_BIN`. The adapter passes these through untouched.
- **Serving layer**: llama.cpp `server` (parallel slots, continuous batching) or
  MLX. The controller never sees it — it only sees the endpoint. Slot count,
  quantization (Q5/Q6 recommended for tool-call/structured-output fidelity), and
  KV-cache budgets are tuned there, independent of this repo.

### Reconciling the two concurrency numbers

The architecture states "6 logical, 3–4 decoding." In the controller model there
is no always-resident agent; agents are ephemeral per-stage dispatches. So:

- **`--concurrency=N`** caps in-flight dispatches (Qwen Code processes) — set it
  to **4** to match the decode target.
- **Server slots** cap *simultaneous decode*; set the shared server to ~4 slots
  with continuous batching. `--concurrency` should be **≤ server slots**, else
  dispatches queue on the server instead of in the scheduler.
- "Agents waiting on tools" (tests, builds, file ops) is handled natively:
  a dispatch mid-`pytest` holds a scheduler slot but issues no tokens, so the
  server's decode capacity flows to peers — exactly the "3–4 of 6 actually
  generating" behavior, with no extra machinery.

## The gap

The `qwen` harness declares `worktree_isolation: false` and `parallel: false`
(`harnesses.yaml`). Those flags mean *unverified*, not *impossible* (the
conservative-by-default rule in `capability.py`). Until they are flipped, a
`--parallel` run routed to qwen **degrades to serial**
(`degradation.py` PARALLEL_TO_SERIAL) — correct and safe, but not the target.

**Epic-29 Story 29.1-001 is exactly this work**: prove `qwen -p` completes an
edit task unattended inside a freshly cut git worktree (approval-mode / `-y`
semantics — a Gemini-CLI fork detail), record the evidence, then flip both
flags. No controller change is expected beyond the YAML flags and a preflight
unit test. Once flipped, parallel qwen cohorts run concurrently with per-story
worktree isolation — the full target.

## Consequences

### Positive

- **~85% of the target ships today.** Orchestration, worktree isolation,
  schema-validated hand-offs, risk-tiered gates, and merge control are the
  controller's existing behavior, harness-agnostic since Epic-20.
- **The weakest part of the reference architecture is removed, not ported.**
  A deterministic orchestrator reclaims the most context-hungry, always-resident
  agent — freeing a whole decode slot and ~64K–128K of KV cache — and makes
  decomposition/merge reproducible instead of a variable token-spend.
- **"Load once" is structural.** Thin CLI clients against one endpoint make the
  bandwidth-contention failure mode (N independent model processes) impossible
  by construction.
- **No lock-in.** The same run can route different roles to claude/codex/qwen;
  the local endpoint is one more routing target, not a rewrite.

### Negative / Trade-offs

- **Not yet parallel on qwen.** Blocked on Epic-29 29.1-001's evidence-then-flip;
  until then the local path is serial (or a manual, verified flip).
- **Unattended-write verification is real risk.** Whether `qwen -p` writes files
  in a worktree without a TTY prompt is the specific thing 29.1-001 must prove;
  a Gemini-CLI-fork approval quirk could require an adapter flag.
- **No usage/rate-limit telemetry through this harness** (`usage_tracking`,
  `rate_limit_aware` are false) — local runs record cost as unavailable, which
  is acceptable for a self-hosted endpoint with no per-token billing.
- **Serving-layer tuning is the user's problem.** Slots, quantization, and KV
  budgets live in llama.cpp/MLX config; this repo neither sets nor validates
  them.

## Alternatives Considered

- **One model process per agent (the architecture's explicit anti-pattern).**
  Rejected for the reason the architecture itself gives: N dense-27B processes
  contend for the same memory bandwidth, collapsing per-agent throughput. The
  shared-endpoint mapping avoids it structurally.
- **An LLM orchestrator agent (port "Agent 1" as-is).** Rejected per ADR-001: a
  resident 64K–128K orchestrator is the single largest, always-on consumer of
  both a decode slot and KV cache on a bandwidth-bound box, for work the
  controller already does deterministically.
- **A new bespoke local harness.** Unnecessary — the `qwen` registry entry and
  adapter already exist; the work is verification + config (Epic-29), not a new
  adapter. (OpenCode, added serial-first in Epic-29 Feature 29.2, is the pattern
  if a second local/OpenAI-compatible client is wanted.)

## References

- `docs/adr/001-controller-runtime.md` (deterministic Python controller)
- `docs/adr/004-model-strength-strategy.md` (per-stage model routing)
- `docs/stories/epic-29-harness-parallelism.md` (qwen verify-and-flip; opencode)
- `docs/stories/epic-20-cross-harness-portability.md` (pluggable harness registry)
- `controller/src/sdlc/config/harnesses.yaml` (`qwen` entry)
- `scripts/qwen-build-adapter.sh` (the adapter and its `QWEN_BIN` / `QWEN_FLAGS` env)
- `controller/src/sdlc/dispatch.py`, `cohort.py`, `contracts.py`, `degradation.py`, `capability.py`
