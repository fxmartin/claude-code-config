# Cross-Harness Benchmarking Method

> Story 31.3-002 (Epic-31 — Harness Benchmarking & Local-Model Baseline).
> Status: shipped.

This is a **method note**, not a results archive. It says what a scoreboard
measures, what it deliberately cannot compare, and the exact commands to
re-run the baseline against a new harness or a newer model. The actual
results — claude vs local Qwen — live in
[`controller/eval/GO-NO-GO-31.3-001.md`](../controller/eval/GO-NO-GO-31.3-001.md)
and the scoreboards under `controller/eval/results/`; those carry their own
provenance and are not duplicated here. Re-read this doc before trusting, or
re-running, any of them.

See [`docs/evaluation.md`](evaluation.md) for the eval harness itself (how a
ticket is dispatched, scored, and turned into a scoreboard) and the
"Cross-harness benchmarking" section of
[`docs/controller-architecture.md`](controller-architecture.md) for how the
harness registry and degradation matrix feed it. This doc is the seam between
the two: what the numbers they produce mean when you put two of them side by
side.

## What is measured, and from where

Every scoreboard row (per-ticket mean, plus `OVERALL`) carries five metrics.
Each has exactly one source — never both:

| Metric | Source | Precision | Notes |
|---|---|---|---|
| `quality_pass_rate` | the ticket's `quality_cmd` exit code (`evaluate.py`) | boolean per run | `None` when a ticket sets no `quality_cmd` |
| `loc_net_mean` (+`loc_added`/`loc_removed`) | `git diff --numstat` on the throwaway workspace | exact (line counts) | new files included; framework repo and template are never touched |
| `tokens_mean` (+ 4-way component breakdown) | the harness's own usage envelope (`usage.py`), or `None` when the harness declares `usage_tracking: false` | as reported by the harness | never `0` for an untracked harness — see Non-comparabilities below |
| `cost_mean` | `total_cost_usd` from the envelope, else a notional `$/Mtok` derivation from tokens (`cost_estimate.py`) | derived from token precision | not a real spend figure for a not-metered harness |
| `wall_s` (stall-adjusted) | `time.monotonic()` around the eval's own dispatch call (`evaluate.py`, `run_eval`), minus any `stall_seconds` attributed to that ticket | **sub-second**, in-process | this is *eval* wall-clock, not ledger wall-clock |

### Two precisions, never mixed in one table

Two different clocks exist in this codebase and a scoreboard uses only one of
them:

- **The ledger** (`controller/src/sdlc/build.py`, the `runs`/`stages` tables)
  stamps `started_at`/`finished_at` with SQLite `CURRENT_TIMESTAMP` —
  **whole-second UTC**. It times a real pipeline run: build → coverage →
  review → merge, over minutes, with process restarts and human-in-the-loop
  gaps in between. Whole-second resolution is adequate at that scale and a
  schema migration to sub-second timestamps was explicitly ruled out of scope
  for this epic (nothing here changes that).
- **The eval harness** (`controller/src/sdlc/evaluate.py`) times a single
  in-process dispatch with `time.monotonic()` — **sub-second**, immune to
  wall-clock adjustments, and scoped to one ticket's one dispatch call.

They are never mixed in the same comparison table because they are not the
same measurement. A ledger duration includes queue time, human review gaps,
CI round-trips and process restarts that an eval dispatch never sees; an eval
duration is a tight, in-process span around one agent call, with none of
that. Putting a `wall_s` from a scoreboard next to a `duration_seconds` from
`sdlc status` in one row would silently compare two different kinds of clock
at two different scales. If you need both signals, report them in two tables
labelled by source, not one column that mixes them.

### Stall accounting is a scoreboard-only correction, in only one direction

`wall_s` on the scoreboard already excludes rate-limit stall time
(`stall_seconds`, Story 31.2-001) and shows the excluded amount beside it,
so a quota-throttled Claude run is measured on agent time, not on how full
FX's quota was that day. The ledger's own duration helpers
(`_duration_seconds`/`_story_duration_seconds`, `build.py:3912`) apply the
same subtraction to *raw ledger timestamps* for the same reason — but that is
a ledger-side correction over whole-second data, not a bridge between the two
clocks. Still never mix the two tables.

## Known non-comparabilities

A scoreboard can be provenance-complete and *still* not be comparable to
another one. These are the causes this rig knows about; a reader who ignores
them will misread a delta as a finding.

### 1. Telemetry-less harnesses (token/cost axis)

A harness that declares `usage_tracking: false` in the registry
(`controller/src/sdlc/config/harnesses.yaml` — `codex` and `qwen` today)
reports its tokens as **unavailable**, never `0`, everywhere (ledger,
scoreboard, dashboard). `eval-compare` marks the token and cost rows
**not comparable** and excludes them from the verdict rather than treating a
blank as free (Story 31.2-002). This holds even when *both* arms report a
number: two totals within 7% of each other measured on the same prompt were
99.9% cache-write on one side and 99.9% cache-read on the other — completely
different work at completely different prices. A near-equal total is not
proof of a comparable mix; the comparator checks the four-way component
breakdown (`input`/`output`/`cache_read`/`cache_creation`) and calls the axis
not-comparable when the largest per-component share gap exceeds 25%, or when
either side carries no breakdown to judge at all.

### 2. Not-metered cost

A hosted harness's cost is either a real subscription-metered figure or a
notional `$/Mtok` derivation — a genuine dollar quantity either way. A local
harness's `cost: 0` is **not metered**, not "free": there is no per-token
meter on hardware FX already owns, and reporting it as a `$0` saving would
assert a specific dollar figure that was never spent one way or the other.
`eval-compare` reuses the token axis's not-comparable machinery for this —
never a delta implying the local arm saved a specific amount (Story
31.2-003). A configured local rate (e.g. an energy-per-hour figure), if one
is ever set, travels as provenance rather than silently changing the
comparison's meaning.

### 3. Stall asymmetry

Only the hosted `claude` harness declares `rate_limit_aware: true`; it is
the only arm that can stall on a quota backoff mid-ticket, and the only one
whose `wall_s` needs the stall subtraction to mean "agent time". `qwen` and
`codex` declare `rate_limit_aware: false` — they have no stall concept at
all, so their stalled column is correctly **absent**, not `0`. A reader must
not conclude the local arm is "faster because it never waits" — it was never
being asked to wait on anything in the first place.

### 4. Serial-vs-parallel

`claude` declares `parallel: true` in the registry (it can run parallel
worktree cohorts inside the normal build pipeline); `qwen` and `codex`
declare `parallel: false` / `worktree_isolation: false`. The eval harness
never exercises parallelism on either arm — `sdlc eval`'s per-ticket loop
dispatches one ticket-run at a time regardless of what a harness *can* do —
so every scoreboard in this rig is a serial-vs-serial comparison by
construction. But a reader pulling a *ledger* wall-clock figure for `claude`
from a real pipeline run (where parallel cohorts are common) and comparing it
against an eval scoreboard's serial `wall_s` for a local harness would be
comparing a parallel-capable run against a serial one — restate explicitly,
in any such comparison, that both figures are serial, or don't make the
comparison.

## Re-run command sequence

To reproduce a baseline for a new harness or a newer local model, from
`controller/`:

```bash
cd controller

# 1. Confirm the harness resolves and its CLI is reachable before spending
#    any ticket budget on it. Fails fast with an actionable message if the
#    harness is unknown/disabled, its probe fails, or it can't take a model pin.
uv run sdlc eval --harness <name> --dry-run

# 2. Run the identical ticket set, n>=3, and save a provenance-complete
#    scoreboard. Repeat once per arm you are comparing.
uv run sdlc eval --harness <name> --json > eval/results/<name>-scoreboard.json

# 3. Compare two arms' scoreboards — deltas, verdict, and any axes excluded
#    as not-comparable (never silently dropped).
uv run sdlc eval-compare \
  --baseline eval/results/<arm-a>-scoreboard.json \
  --candidate eval/results/<arm-b>-scoreboard.json

# 4. Record the recorded run and comparison output, plus a written go/no-go,
#    under controller/eval/ (see GO-NO-GO-31.3-001.md for the shape).
```

If the local model is served with a raised context window or a different
quantisation than last time, also raise the eval's per-ticket timeout
(`evaluate.py`'s `DEFAULT_TICKET_TIMEOUT_S`) if the model is materially
slower — and record that you did, since a timeout must be reported as a
timeout, not as a quality failure.

### Serving-setup provenance to capture

The scoreboard's own `provenance` block (Story 31.1-002) captures harness,
model, harness version, host, ticket-set identity, `n`, and timestamp — but
none of that describes *how a local model was served*, and that dominates the
numbers. Record the following alongside the scoreboard (in the same commit or
go/no-go note, since the rig has no field for it):

- **Server** — the inference backend (e.g. Ollama, LM Studio, llama.cpp,
  vLLM) and its version.
- **Quantisation** — the exact quant (e.g. `Q4_K_M`, `Q8_0`, fp16) — this
  alone can dominate both quality and speed.
- **Context length** — the context window the server was configured with,
  since a truncated context silently degrades quality in a way this rig
  cannot detect.
- **Hardware** — the coarse host id the scoreboard already records
  (`hostname/arch`) is not enough on its own for a local arm; note GPU/unified
  memory if relevant, since it is the local arm's real cost.

None of this is enforced by the tooling — it is FX's judgment call what to
serve and how, per the epic's explicit non-goal. This is the checklist for
writing it down so the numbers it produced are traceable later.

## Admission criteria for a harness joining a comparison

Before a harness (new or existing) can be one side of a comparison in this
rig, all of the following must be true — capability-driven, never a
harness-name special case:

1. **The harness's registry `probe` passes.** `sdlc eval --harness <name>
   --dry-run` (or a direct preflight) must resolve the harness, confirm it is
   `enabled: true`, and run its `probe` command successfully. A harness with
   no `probe` declared preflights as "unknown" rather than failing — that is
   acceptable, but note it in the write-up rather than silently treating
   "unknown" as "passed".
2. **Its capability flags are declared, not assumed.** `worktree_isolation`,
   `parallel`, `json_contract`, `usage_tracking`, `rate_limit_aware` must all
   be present in its `harnesses.yaml` entry. An undeclared flag is treated as
   absent by `capability.py`'s conservative default — never assume a flag
   silently defaults to capable.
3. **Its telemetry status is known before the run, not discovered from a
   blank column after it.** If `usage_tracking: true`, confirm at least one
   real dispatch actually returns a non-empty usage envelope before trusting
   the token axis; if `usage_tracking: false`, the token/cost axes are
   expected to render "unavailable" — that is correct, not a bug in the new
   harness.
4. **It can honour the eval's model pin, or the config omits one.** A
   registry command with no `{model}` placeholder aborts the eval at
   preflight rather than silently running whatever model the CLI defaults to
   (Story 31.1-001 AC6) — add the placeholder and a `models:` map before
   pinning a model for that harness in an eval config.

A harness that fails (1) is not in the comparison at all — record the
failure by category (exactly as the qwen arm of 31.3-001 did when it had no
auth configured), rather than omitting it as if it were never attempted.

## Applying to a new local model

The registry entry (harness) usually does not change when only the model
being served behind it changes — re-point the same `qwen`/`opencode` harness
at the new model (via its `models:` map, if configured) and re-run the
sequence above. Treat it as a new baseline, not a patch to the old one: save
a new scoreboard file, do not overwrite the previous one, and note the model
change (name, size, quantisation) in the serving-setup provenance so a reader
three months from now can tell which run used which model without cross
referencing git history.
