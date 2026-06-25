# Agent Output Evaluation Harness

> Story 18.1-001 (Epic-18 — Agent Output Quality). Status: shipped.

We change agent prompts, swap models (Epic-14 routing), add skills, and tweak
schemas — but without a way to **measure** agent output we are guessing whether
any of it helped or hurt. The eval harness closes that gap: a single command
drives the build agent headlessly over a fixed ticket set on a sample repo, and
scores every result on **LOC delta, token usage, notional cost, wall-time, and a
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
   - **Tokens** — sum of the four usage components, or `None` for a plain-text agent.
   - **Cost** — the envelope `total_cost_usd`, else a notional figure from tokens
     (the controller's `$15/Mtok` convention — never real subscription spend).
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

## Scoreboard

Text table (default) or `--json`. Each row is a per-ticket mean over its `n`
runs, with a final `OVERALL` aggregate:

```
eval: strutils-baseline
ticket             runs err    +LOC    -LOC  netLOC    tokens    cost$  wall_s  qual
-------------------------------------------------------------------------------------
add-capitalize        3   0     7.0     0.0     7.0      4120   0.0618    22.4  100%
...
OVERALL               9   0     8.1     0.3     7.8      4310   0.0646    23.1  100%
```

The `--json` form (`scoreboard_to_dict`) is the shape later stories store as a
**baseline** to flag regressions (18.1-002) and run in **CI** on agent-affecting
changes (18.1-003).

## Tested vs. live

The scoring and aggregation logic is fully unit-tested (`tests/test_evaluate.py`)
with an injected fake dispatcher and real git — diff parsing, usage/cost
extraction, quality checks, aggregation, config validation, and the isolation
guarantee (no template mutation). The CLI wiring is covered end-to-end
(`tests/test_cli_eval.py`) with a stub agent via `$SDLC_AGENT_CMD`. The **live
model** is never invoked from the test suite — only from `sdlc eval` itself.
