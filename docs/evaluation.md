# Agent Output Evaluation Harness

> Story 18.1-001 (Epic-18 — Agent Output Quality). Status: shipped.

We change agent prompts, swap models (Epic-14 routing), add skills, and tweak
schemas — but without a way to **measure** agent output we are guessing whether
any of it helped or hurt. The eval harness closes that gap: a single command
drives the build agent headlessly over a fixed ticket set on a sample repo, and
scores every result on **LOC delta, token usage, cost, wall-time, and a
quality check** (tests pass / no breakage), emitting a comparable scoreboard.

It is deliberately small and inspectable — a promptfoo-style eval over real
tickets, **not** a hosted experiment-tracking platform (see the Epic-18 non-goals).

## Quick start

```bash
cd controller

# List what the eval would run — spends no quota.
uv run sdlc eval --dry-run

# Run the full eval (drives the live build agent — spends real quota on Max).
uv run sdlc eval

# JSON scoreboard (for storage / comparison), one quick run per ticket.
uv run sdlc eval --json --n 1

# Run the same ticket set on another harness from the registry (Story 31.1-001).
uv run sdlc eval --harness qwen --json
```

The default config is `controller/eval/eval-config.yaml`. Point `--config` at any
other versioned bundle.

## How it works (isolation)

For each ticket × `n` runs, the harness:

1. **Copies** the sample target (`eval/sample-target/`, plain files — *not* a
   nested git repo) into a throwaway workspace and `git init`s it, committing a
   clean baseline.
2. **Dispatches** the agent headlessly into that copy (reusing the controller's
   `dispatch_agent`, so token/cost extraction matches ledger metrics — the same
   `usage` envelope keys and `total_cost_usd`).
3. **Scores** the result against the baseline:
   - **LOC delta** — `git diff --numstat` (new files included), added/removed/net.
   - **Tokens** — the four usage components (`input`, `output`, `cache_read`,
     `cache_creation`) carried *individually*, plus their total. `None` — never
     `0` — for a plain-text agent or a harness that declares no `usage_tracking`
     (31.2-002).
   - **Cost** — the envelope `total_cost_usd`, else a notional figure from tokens
     (the controller's `$15/Mtok` convention — never real subscription spend); a
     harness with no per-token price at all renders "not metered" instead
     (31.2-003, below).
     A run with no token figure has no derived cost either.
   - **Wall-time** — monotonic seconds for the dispatch.
   - **Quality** — the ticket's `quality_cmd` (exit 0 = pass); `None` if none set.

The framework repo and the sample-target template are **never mutated**, and the
eval **never opens PRs or touches `main`** — it scores diffs in throwaway clones.
A dispatch failure is captured as a per-run `error` (with a zero diff) so one bad
run never aborts the eval.

## Config format

`eval/eval-config.yaml` is the versioned definition — config + tickets + sample
target + run count all live in-repo so a re-run is comparable within model
variance:

```yaml
name: strutils-baseline      # scoreboard label
target: sample-target        # dir of plain files, relative to this config
n: 3                         # runs per ticket (averages out model variance)
seed: 1801                   # reproducibility provenance for the harness inputs
agent_type: build            # which agent role to dispatch
# harness: qwen               # optional harness pin (Story 31.1-001); default: claude
tickets:
  - id: add-capitalize
    prompt: >-
      In strutils.py add a function ...
    quality_cmd: ["python", "-m", "pytest", "-q"]   # exit 0 = pass
```

> **On reproducibility:** the seed pins the *harness inputs* (config, tickets,
> target), not the model. Live model sampling is not bit-for-bit deterministic,
> so `n>1` averages out variance and results match only *within* that variance —
> exactly the comparability the success metric calls for.

### Harness selection (31.1-001)

`harness:` in the config (or `--harness` on the CLI, which wins over the
config — the same CLI > config > default precedence as `model:`) resolves
through the harness registry
([`controller/src/sdlc/config/harnesses.yaml`](../controller/src/sdlc/config/harnesses.yaml))
to that harness's command + parser, so every ticket dispatch in the run goes
through it and the resulting scoreboard records the harness name — the same
fixed ticket set is then comparable across claude, qwen, opencode, etc. *by
construction*. Omitting it entirely runs on the built-in `claude` default,
byte-identical to every eval before this field existed.

The harness is resolved and preflighted **once, before any ticket dispatches**
— never mid-run:

- an unknown or `enabled: false` harness aborts, naming the registry file;
- a harness whose `probe` command fails (its CLI isn't installed on this
  machine) aborts with the probe's own diagnostic;
- a harness that cannot honour the eval's `model` pin — a registry command
  with no `{model}` placeholder — aborts rather than silently ignoring it.

See [`docs/harness-adapters.md`](harness-adapters.md) for the full registry
format and the qwen/opencode/codex adapters.

## Scoreboard

Text table (default) or `--json`. Each row is a per-ticket mean over its `n`
runs, with a final `OVERALL` aggregate:

```
eval: strutils-baseline (harness: claude)
ticket           runs err    +LOC    -LOC  netLOC    tokens    cost$  wall_s stalled  qual
------------------------------------------------------------------------------------------
add-capitalize      3   0     7.0     0.0     7.0      4120   0.0618    22.4       —  100%
...
OVERALL             9   0     8.1     0.3     7.8      4310   0.0646    23.1       —  100%
```

`wall_s` is agent time: any in-process wait on a Max rate limit mid-ticket is
excluded and shown separately in `stalled` (a bare `—` when the ticket never
stalled) — a throttled ticket's wait time never inflates `wall_mean` (31.2-001).

### Honest token accounting (31.2-002)

A token *total* is a lossy summary. Two arms on the same prompt measured **15,160
vs 14,152 tokens** — within 7% of each other — and one was 99.9% cache-*write*
while the other was 99.9% cache-*read*: completely different work at completely
different prices. So the scoreboard carries the breakdown, not just the total:

| JSON key | meaning |
| --- | --- |
| `tokens_mean` | mean total (`None` when unavailable — never `0`) |
| `input_tokens_mean` / `output_tokens_mean` | mean per component |
| `cache_read_tokens_mean` / `cache_creation_tokens_mean` | mean per component |
| `tokens_source` | `measured`, `estimated`, `external`, `mixed`, or `unavailable` |
| `cost_source` | `measured`, `estimated`, `local_rate`, `not_metered`, `mixed`, or `unavailable` (31.2-003, below) |

Two rules follow, and both are **capability-driven** — they read the harness's
`usage_tracking` flag from the registry, never its name:

- A harness that does not declare `usage_tracking` records its tokens and cost as
  **unavailable** everywhere they surface (ledger, scoreboard, dashboard) — it
  renders `—`, never `0`, so the arm that cannot report never looks free. A
  harness that grows usage telemetry later only has to flip the flag.
- An approximate figure (a pre-dispatch estimate, or an external count) renders
  with a leading `~` and is labelled as an estimate, so it can never be read as a
  measurement.

**External token counts.** A local model whose serving layer can report an
approximate token count plugs in through
`sdlc.usage.register_token_counter(harness, counter)`: the counter receives the
dispatch's usage envelope and returns a `TokenBreakdown` (or `None` to decline).
Whatever comes back is labelled `external` — approximate, never `measured`. The
controller ships no counter and commits to no particular server; the seam is there
for one.

The `--json` form (`scoreboard_to_dict`) is the shape later stories store as a
**baseline** to flag regressions (18.1-002) and run in **CI** on agent-affecting
changes (18.1-003).

### Cost for local inference (31.2-003)

`$/Mtok` is a hosted-API assumption — it does not hold for a model running on
your own hardware. The field case: the same prompt on two arms, one hosted, one
local — the local arm's own telemetry reported `cost: 0`. That zero is
*correct* (there is no per-token meter) and also **not** a saving of the hosted
arm's dollar figure in any sense a comparator should assert. So cost carries its
own provenance, distinct from `tokens_source`:

- A **metered** harness (`metered: true`, the default — every harness that
  predates this story) is unchanged: the envelope's own cost wins when present
  (`measured`), else a notional `$/Mtok` figure is derived from tokens
  (`estimated`), exactly as before.
- A **non-metered** harness (`metered: false` in the harness registry — see
  [`docs/harness-adapters.md`](harness-adapters.md)) never has its own `cost`
  telemetry trusted, even a literal `0` — the scoreboard renders **"not
  metered"** instead of a dollar figure (`cost_source: not_metered`), so it can
  never be misread as a real or notional spend.
- If the registry configures an explicit `local_rate_usd_per_million_tokens`
  for that harness (e.g. an assumed energy cost), cost is derived from tokens
  under *that* rate instead (`cost_source: local_rate`) — a deliberate,
  recorded assumption rather than an invented one. The rate travels with the
  number: it is recorded in the scoreboard's `provenance` block (below), so the
  assumption behind any given dollar figure is never lost.

`sdlc eval-compare` reuses the existing not-comparable machinery (31.2-002):
when one arm's cost is `not_metered` (so its `cost_mean` is `None`, never `0`),
the cost row renders as a labelled non-comparison — "a missing figure is not
zero" — and is excluded from the verdict, same as any other unavailable
metric. The **token** axis is unaffected by any of this: a non-metered harness
that reports real tokens keeps `tokens_mean`/`tokens_source` exactly as
`usage_tracking` decides — tokens are the currency on the local arm, dollars on
the hosted one.

This is purely a Epic-31 (`sdlc eval`/`eval-compare`) concern. Epic-14/28's
build-time cost governance (budget gates, calibration) does not read a
harness's `metered` field at all — a build stage is always priced via the
hosted `$/Mtok` convention regardless of which harness ran it, so encountering
a not-metered harness there changes nothing.

## Scoreboard provenance (31.1-002)

Every `--json` scoreboard carries a `provenance` block — enough to trace a
number back to the conditions that produced it, weeks later:

| Field             | Meaning                                                                 |
|-------------------|--------------------------------------------------------------------------|
| `harness`         | The resolved harness name (e.g. `claude`, `qwen`).                       |
| `model`           | The resolved, pinned model id the run actually dispatched on.            |
| `harness_version` | The harness's declared `probe` command output (Story 31.1-001) — the same source of truth the preflight uses; `null` when the harness declares no `probe` (e.g. the built-in `claude` harness), not a failure. |
| `host`            | A coarse host id, `hostname/machine-arch` — never a hardware fingerprint. |
| `config_name`     | The eval config's `name:`.                                               |
| `seed`            | The config's `seed:`, or `null` when unset.                              |
| `ticket_ids`      | Every ticket id the config ran.                                          |
| `n`               | Runs per ticket.                                                         |
| `timestamp`       | UTC, `%Y-%m-%dT%H:%M:%SZ`.                                                |
| `cost_metered`    | Whether the harness's cost is a real/notional `$/Mtok` figure (31.2-003). `true` (default) unless the registry declares `metered: false`. |
| `local_rate_usd_per_million_tokens` | The harness's configured local rate, or `null` if none is set (31.2-003). |

`config_name` + `seed` + `ticket_ids` together are a scoreboard's **ticket-set
identity** — what `eval-compare` checks before treating two scoreboards as a
valid A/B (below).

## Variant comparison & regression baselines (18.1-002)

A scoreboard on its own says *how good*, not *better or worse than what*. Story
18.1-002 adds a thin layer on top: diff two scoreboards (variant A vs B) and check
a fresh scoreboard against a committed **baseline** to catch regressions. Both work
on the `--json` scoreboards above — no live model is involved, so the logic is
fully unit-tested.

### A/B compare two variants

Run the eval once per variant (e.g. prompt A vs B, or Haiku vs Sonnet on a stage),
saving each scoreboard, then compare:

```bash
cd controller

uv run sdlc eval --config eval/variant-a.yaml --json > /tmp/a.json
uv run sdlc eval --config eval/variant-b.yaml --json > /tmp/b.json

# Side-by-side per-metric delta + a better/worse/neutral verdict per ticket + overall.
uv run sdlc eval-compare --baseline /tmp/a.json --candidate /tmp/b.json

# Record the decision (so a prompt/model choice is backed by data, not vibes).
uv run sdlc eval-compare --baseline /tmp/a.json --candidate /tmp/b.json \
  --json --out docs/decisions/haiku-vs-sonnet-coverage.json
```

Each ticket (and the `OVERALL` row) gets a verdict:

- **Quality is decisive** — a `quality_pass_rate` drop is always `WORSE`, a rise is
  always `BETTER`, however much cheaper the run got. We never trade quality for cost.
- With quality unchanged, the efficiency metrics (netLOC, tokens, cost, wall) are
  tallied: more improvements than regressions → `BETTER`, the reverse → `WORSE`, a
  tie → `NEUTRAL`.
- **Only comparable axes count** (31.2-002). An axis the two arms cannot be judged
  on contributes nothing in either direction, and the verdict line names it:

  ```
  t1: BETTER  (excluded from verdict: tokens, cost$)
    tokens   15160.0000 -> 14152.0000  not comparable — token mix differs
      materially: baseline 99.9% cache_creation vs candidate 99.9% cache_read
      (largest component gap 100% > 25%)
  ```

  The token and cost axes are excluded together — cost is derived from the same
  components — for any of three reasons, checked in order:

  1. **Unavailable** — a figure missing on either side. A missing number is not zero.
  2. **Provenance** — an estimate against a measurement. Different quantities.
  3. **Mix** — the largest per-component share gap exceeds 25%, or a scoreboard
     carries no component breakdown to judge at all.

  A scoreboard written before 31.2-002 has no component breakdown, so its token
  and cost axes report *not comparable* until it is regenerated with
  `uv run sdlc eval --json`. Every other axis compares as before.

A metric only counts as moved when it changes by more than `--tolerance` (default
**10%**, relative to the baseline value) — below that, model-run variance swamps
the signal, so it stays neutral. This is the knob that keeps the false-positive
rate down. This directly answers the Epic-14 question: *does cheaper-model routing
hold quality?* — compare the two scoreboards and read the verdict.

### Refusing to compare unlike runs (31.1-002)

`eval-compare` checks the two scoreboards' **ticket-set identity**
(`config_name` + `seed` + `ticket_ids`, from their `provenance` blocks and
`tickets`) before comparing. Comparing runs built from different work is not
an A/B:

```bash
uv run sdlc eval-compare --baseline /tmp/a.json --candidate /tmp/b.json
# error: refusing to compare different ticket sets (not an A/B):
#   - config name differs: 'strutils-baseline' vs 'other-eval'
# pass --force to compare anyway
```

`--force` compares anyway, with the mismatch printed as a warning instead of a
refusal — for the deliberate case (e.g. checking whether an old baseline is
even in the same ballpark as a newer, differently-scoped eval):

```bash
uv run sdlc eval-compare --baseline /tmp/a.json --candidate /tmp/b.json --force
```

A **legacy scoreboard** — one predating provenance tracking, with no
`provenance` block at all — is still accepted, never rejected; it prints a
`provenance unknown` warning instead, so an old committed baseline stays
usable. And when the two scoreboards ran on **different harnesses**, the
comparison proceeds and states both harnesses in its header
(`compare: ... (baseline, harness=claude) vs ... (candidate, harness=codex)`),
so a delta is never misread as model-only. `--json` carries the same warnings
under a `warnings` key.

### Regression baselines

`eval/baseline.json` is a committed scoreboard (regenerate it from a real run with
`uv run sdlc eval --json > eval/baseline.json` — the shipped file is an illustrative
placeholder). Check a fresh scoreboard against it:

```bash
uv run sdlc eval --json > /tmp/new.json

# Flags any metric that regressed beyond tolerance; exits 1 if so, 0 if clean.
uv run sdlc eval-baseline --baseline eval/baseline.json --candidate /tmp/new.json

# Advisory mode — report regressions but never fail (exit 0).
uv run sdlc eval-baseline --candidate /tmp/new.json --warn-only

# Promote a new known-good scoreboard to the baseline.
uv run sdlc eval-baseline --candidate /tmp/new.json --update
```

A "regression" is a `quality_pass_rate` drop or a netLOC/tokens/cost/wall **rise**
beyond `--tolerance`; cost and wall that hold steady are not flagged. The non-zero
exit on regression is what later wires a bounded eval into CI (18.1-003, warn or
fail configurable). The comparison itself never mutates `main` or opens PRs — it is
pure scoreboard arithmetic.

An axis excluded as **not comparable** (31.2-002) was never checked, so the gate
names it on stderr and qualifies its verdict rather than letting the exclusion
read as a pass:

```
not compared — tokens: no component breakdown recorded on the baseline, so the
  token mix cannot be judged — regenerate it with `sdlc eval --json`
not compared — cost$: …
baseline OK: no regressions beyond 10% on the comparable metrics (new vs base)
```

With nothing excluded the line reads "on all metrics". `controller/eval/baseline.json`
predates 31.2-002 and carries no component breakdown, so **its token and cost axes
are excluded on every check until it is regenerated** with
`uv run sdlc eval --json > eval/baseline.json` — the netLOC/wall/quality axes are
unaffected.

## CI integration (18.1-003)

The full eval stays a manual/local command (it spends real quota on Max). What
runs **in CI** is a deliberately tiny slice — one ticket at `n=1` — wired to flag
a quality regression on the PR rather than in an overnight batch. The job lives in
[`.github/workflows/eval-ci.yml`](../.github/workflows/eval-ci.yml) and the bounded
subset is `controller/eval/ci-config.yaml` (a true slice of the full eval: same
sample target, prompt, and quality check as the `add-capitalize` ticket, so its
scoreboard is directly comparable to `eval/baseline.json`).

**Path-filtered.** The job only triggers on PRs that touch agent-affecting files,
so unrelated PRs skip it and CI stays fast and cheap:

- `controller/src/sdlc/build.py` — the build-agent prompt the eval drives
- `controller/src/sdlc/schemas/**` — the agent response schemas
- `skills/**` — skills the agents may invoke
- `controller/eval/**` — the eval bundle itself

**Quota-bounded.** The eval drives the live agent, so the job only runs when the
`ANTHROPIC_API_KEY` secret is configured — forks and credential-less PRs skip it
cleanly (a notice, not a failure). One ticket × `n=1` plus a 15-minute timeout cap
the spend.

**Warn or fail — configurable.** After the run, the job checks the fresh
scoreboard against the committed baseline with `sdlc eval-baseline`. The
warn-vs-fail behaviour is driven by the repo variable **`EVAL_CI_WARN_ONLY`**:

- **unset / anything but `false`** (default) → **advisory**: regressions are
  reported but the job stays green, so a borderline eval never blocks a PR.
- **`false`** → **blocking**: a baseline regression beyond tolerance fails the job.

Flipping the policy needs no code change — set the variable under
*Settings → Secrets and variables → Actions → Variables*. The full local commands
(`sdlc eval`, `eval-compare`, `eval-baseline`) are unchanged; CI just reuses them.

## Tested vs. live

The scoring and aggregation logic is fully unit-tested (`tests/test_evaluate.py`)
with an injected fake dispatcher and real git — diff parsing, usage/cost
extraction, quality checks, aggregation, config validation, and the isolation
guarantee (no template mutation). The CLI wiring is covered end-to-end
(`tests/test_cli_eval.py`) with a stub agent via `$SDLC_AGENT_CMD`. The CI wiring
itself is asserted without a model: `tests/test_eval_ci_config.py` checks the
bounded subset is a valid, baseline-comparable slice, and
`tests/test_eval_ci_workflow.py` checks the workflow is path-filtered,
quota-gated, and baseline-checked. The **live model** is never invoked from the
test suite — only from `sdlc eval` itself.

## Related: skill pressure-tests (process compliance)

This harness scores **output quality** on real tickets. A complementary,
narrower suite scores **process compliance** — does a discipline prompt actually
change agent behaviour under pressure? The Epic-26 RED/GREEN skill pressure-tests
live at [`plugins/autonomous-sdlc/evals/`](../plugins/autonomous-sdlc/evals/README.md)
and run on-demand via `claude plugin eval autonomous-sdlc --ablation with-without`
(the with/without-plugin ablation is the RED/GREEN split). Like this harness they
dispatch live agents and are **not** wired into the PR gate; CI only asserts the
suite's *shape* (`controller/tests/test_skill_pressure_tests.py`). If that suite's
live runs are ever automated, fold them into this eval harness (Epic-18) rather
than standing up a second runner.
