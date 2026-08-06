<!-- ABOUTME: ADR-004 recording how the controller adopts weaker/local models without losing quality. -->
<!-- ABOUTME: Decision: no wholesale swap — staged, measured routing behind two prerequisites. Status: Accepted. -->

# ADR-004: Model Strength Strategy — Reinforced Harnesses and Local Models

- **Status**: Accepted
- **Date**: 2026-08-06
- **Epic / Story**: Epic-29 (informs 29.1-001 and 29.2-001); consumes Epic-28 telemetry
- **Deciders**: FX

## Context

Two forces make weaker models attractive right now:

1. **The binding constraint is quota, not money.** Long unattended batches are
   limited by the Claude Max rate-limit window. A second Max plan (5× + 20×)
   widens that window but does not remove it. Work that could run on a local
   model would not consume the window at all.
2. **The registry already anticipates it.** Epic-20 made dispatch pluggable, and
   `harnesses.yaml` already carries a `qwen` entry; Epic-29 adds an `opencode`
   adapter whose `-m provider/model` flag can target a locally served model.

The open question is quality: **does routing pipeline work to a weaker, locally
served model degrade output, and if so, can the harness compensate?**

[tsforge](https://github.com/agjs/tsforge) is the strongest prior art for the
"compensate" side and was reviewed in detail before writing this ADR. It is an
explicitly *reinforced* harness for weak models — the project describes itself as
starting from the question *"could a small model running on local hardware produce
TypeScript you'd actually merge, if the harness enforced `tsc`, stack rules, and
stream-level corrections?"*, answering yes, and noting that *"guardrails matter
most when the model is constrained."* Its default local stack is a small model
served on-box. If that thesis transferred to this controller, weak-model adoption
would be mostly a routing change.

**It does not transfer directly.** Two structural differences decide this ADR.

### Difference 1 — we do not own the loop

tsforge's reinforcement is **in-loop**, acting between the model's tokens and the
filesystem:

- stream rules (TTSR) that abort a malformed tool argument *mid-generation*
- hashline edits that anchor a replacement to a file hash so line numbers cannot drift
- an in-process TypeScript language service that flags a bad write *before the next turn*
- the gate (`tsc` + rule packs + tests) re-run on every validation cycle

This controller's seam is deliberately coarser: **prompt in → agent subprocess →
`<<<RESULT_JSON>>>` out** (`dispatch.py`, `harnesses.yaml`). Every quality
mechanism we own is therefore **post-hoc**: CI checks on the finished PR,
adversarial review, the coverage gate, the bugfix loop, the re-ask.

Against a strong model those gates are well matched — the agent is mostly right
and the gates catch the occasional miss. Against a weak model the ratio inverts:
many small errors per turn, compounding within a single large diff, discovered
only after the turn completes, then handed to a bugfix loop that must untangle
them all at once.

### Difference 2 — our oracle is weaker, by language

tsforge is opinionated about TypeScript precisely because `tsc` is fast, complete,
machine-checkable truth, and it overlays a strict `tsconfig` so the gate cannot
inherit a loose upstream config. Reinforcement is only ever as good as the oracle
behind it.

This controller is Python, and **has no static type checker configured** — no
`mypy`, no `pyright` in `controller/pyproject.toml` or the CI gate (which runs
ruff-style static checks, the pytest suite, bats behavior tests, contract checks,
and smoke tests). The strongest oracle available to a dispatched agent is the test
suite: slower than a type check, and blind to anything not already covered.

So the technique that makes weak models viable for tsforge rests on a foundation
this repository does not yet have.

### Evidence already in this repository

- **Issue #527** — *"Bugfix loop can't converge on stories whose review findings
  span multiple files"* — was observed with a **frontier** model. Non-convergence
  of the repair loop is exactly the failure mode a weaker model amplifies, and it
  is our post-hoc-gate architecture failing, not the model alone.
- **Epic-28's cost finding** — spend is **tail-driven**: rework (review retries,
  bugfix loops, stalls) drives cost far more than story size. A model that raises
  rework probability can therefore cost *more* in total tokens and wall-clock than
  the expensive model it replaced. Per-token price is the wrong metric; **total
  cost-to-green** is the right one.

## Decision

**No wholesale swap to a weaker or local model. Adoption is staged by stage-risk
tier, gated on measurement, and blocked behind two reinforcement prerequisites.**

Concretely:

1. **Measure, do not reason.** Quality is decided empirically with apparatus this
   repository already has — Epic-11 evals plus the ledger's per-stage
   token/cost/rework record (Epic-28). A "quality holds" threshold is defined
   **before** any stage is re-routed, and the comparison metric is total
   cost-to-green and rework rate, not per-token price.
2. **Route per stage, never globally.** `--harness role=…` and the per-stage
   `models:` map already exist. Weak models are introduced from the low-risk end:

   | Tier | Stages | Rationale |
   | --- | --- | --- |
   | **Low risk** | `merge`, `docs` | Mechanical, heavily gated, cheap to redo |
   | **Medium risk** | `coverage` | Well-specified and mechanical, but shapes the QA gate |
   | **High risk** | `investigation`, `build`, `review` | Judgment-bound. A bad root cause poisons every downstream stage, and a weak **reviewer rubber-stamps** — worse than no reviewer at all |

3. **Prerequisite A — in-loop feedback via hooks (#585).** The one tsforge
   technique available at our layer: agent-session hooks (`PostToolUse` on
   Edit/Write) run *inside* the dispatched agent's own loop, so a
   linter/typecheck/test-subset result reaches the model before its next turn.
   This is our TTSR analogue. It improves output for **any** model, frontier
   included, so it is worth doing on its own merits.
4. **Prerequisite B — strengthen the Python oracle (#586).** Add a static type
   checker to the gate. This is the highest-leverage single change for weak-model
   viability, because every other reinforcement mechanism is bounded by oracle
   quality.
5. **Harness verification comes first.** Epic-29's 29.1-001 (verify qwen) and
   29.2-001 (opencode adapter) are prerequisites for any experiment: today there
   is **zero** operational data on either harness.

## Rationale

- The controller's value is the SDLC envelope (epic → story → issue → branch →
  PR → CI → merge → release), not the inner coding loop. Rebuilding tsforge-style
  in-loop control would mean owning the model loop, which contradicts the Epic-20
  harness abstraction and re-couples us to one runtime.
- Staged routing exploits an asymmetry: stage risk is **not** uniform. Mechanical
  stages are cheap to get wrong and heavily gated; judgment stages are expensive
  to get wrong and weakly gated. Spending the strong model where judgment lives is
  the same principle Epic-28 applied to model escalation.
- Both prerequisites are unconditionally useful. Neither is wasted effort if the
  local-model experiment is later abandoned — which makes them safe to fund now.
- Measuring first is affordable *because the instrument already exists*. Building
  the eval and ledger apparatus was the expensive part, and it is done.

## Consequences

- Local models will not relieve the rate-limit window in the short term; the
  second Max plan carries that load until the prerequisites land.
- Two new work items enter the backlog (hook-based in-loop feedback; type checker
  in the gate), both independent of Epic-29's sequencing.
- Adding a type checker to an existing ~73k-line Python codebase will surface a
  backlog of annotations and likely needs a phased/ratchet rollout rather than a
  single red-to-green push.
- The per-stage `models:` map and `--harness role=…` become load-bearing for cost
  strategy, so their tests and documentation matter more than they did when they
  were a convenience.
- Any future claim that "a local model works here" must cite eval numbers, not
  impressions. This is deliberate.

## Alternatives Considered

1. **Swap the default harness to a local model and rely on existing gates.**
   Rejected: our gates are post-hoc, the repair loop already has a documented
   non-convergence failure (#527), and Epic-28 shows rework — not token price —
   dominates cost. Highest chance of quietly degrading output while appearing
   cheaper.
2. **Port tsforge-style in-loop reinforcement into the controller.** Rejected as
   out of scope: it requires owning the model loop (stream interception, edit
   mechanism, in-process language service), which is precisely what the harness
   abstraction delegates. Revisit only if harnesses stop being pluggable.
3. **Adopt tsforge itself as a harness for TypeScript repositories.** *Not
   rejected — deferred.* It fits the registry contract (task in, drive-to-green,
   result out), runs in an arbitrary directory (so it may earn
   `worktree_isolation`), and ships its own metrics. But it is **TypeScript-only**,
   so it can never build this Python controller; it would be a per-repo harness
   choice. Worth revisiting after Epic-29 proves the multi-harness parallel path.
4. **Do nothing and stay frontier-only.** Rejected as the long-run answer because
   it leaves the offline ambition of Epic-30 (fully-local SDLC loop) unreachable —
   a local forge with a cloud-only model is still cloud-tethered.

## References

- `docs/stories/epic-29-harness-parallelism.md` (29.1-001, 29.2-001)
- `docs/stories/epic-30-local-forge-event-driven-dispatch.md` (offline end-state)
- `docs/stories/epic-28-empirical-estimation.md` (tail-driven cost; the measurement apparatus)
- `docs/harness-adapters.md`, `controller/src/sdlc/config/harnesses.yaml`
- `docs/evaluation.md` (Epic-11 eval harness)
- Issue #527 (bugfix-loop non-convergence with a frontier model)
- Issue #585 (Prerequisite A — in-loop feedback via `PostToolUse` hooks)
- Issue #586 (Prerequisite B — static type checker in the gate)
- [tsforge](https://github.com/agjs/tsforge) — reviewed 2026-08-06 as prior art for
  weak-model reinforcement (in-loop gate, TTSR, hashline edits, LSP feedback)
