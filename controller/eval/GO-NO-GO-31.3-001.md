# Go / No-Go — recorded claude-vs-local baseline (Story 31.3-001)

**Ticket set**: `strutils-baseline` (`controller/eval/eval-config.yaml`), 3 tickets,
n=3, seed 1801 — identical across every arm below.

**Serial discipline (AC2)**: every arm below ran serially, one ticket-run at a
time, via `sdlc eval`'s per-ticket loop (`run_eval` in `sdlc/evaluate.py`),
regardless of a harness's declared `parallel` capability. The `claude` harness
declares `parallel: true` in the registry (it *can* run parallel worktrees
inside the normal build pipeline), but this eval never exercises that — it is
a single-process, one-dispatch-at-a-time sweep for both arms. `qwen` declares
`parallel: false` / `worktree_isolation: false` and would have run serially
regardless. **No wall-clock number in this report compares a parallel claude
cohort against a serial local run** — both arms here are serial by
construction.

## Arm 1 — claude (recorded, real run)

Scoreboard: [`results/claude-scoreboard-31.3-001.json`](results/claude-scoreboard-31.3-001.json)

- Harness: `claude`, model `sonnet`, host `fxmartins-MacBook-Pro/arm64`,
  timestamp `2026-09-05T09:37:17Z`.
- 9/9 runs completed, 0 errors, **100% quality pass rate** on all three
  tickets (each ticket's `quality_cmd` — `pytest -q` on the sample target —
  passed every run).
- OVERALL: netLOC 9.9, tokens 199,430 (measured), cost $0.106 notional,
  wall 27.5s (stall-adjusted; no run stalled on a rate limit).

Compared against the pre-existing committed baseline
(`eval/baseline.json`, a live claude run from 2026-07-15, legacy/no
provenance) via a real `sdlc eval-compare` run — output recorded verbatim at
[`results/eval-compare-31.3-001.txt`](results/eval-compare-31.3-001.txt):
tokens/cost are flagged **not comparable** (the legacy baseline predates the
Story 31.2-002 component breakdown, so its token mix can't be judged), netLOC
moved up (agent wrote more code this time — a prompt/model-version drift
signal, not a regression this story investigates further), wall time dropped
across the board, quality held at 100% both times. This is provenance-honest
drift tracking over time, not the claude-vs-local comparison this story is
actually about — see Arm 2 for why that comparison could not run.

## Arm 2 — qwen (recorded, real attempt — outright failure)

Failure record: [`results/qwen-preflight-failure.json`](results/qwen-preflight-failure.json)

Real command run on this machine: `uv run sdlc eval --harness qwen --json
--config eval/eval-config.yaml`. **0 of 9 runs executed** — the sweep aborted
at preflight, before ticket 1, for two independently real, categorized
reasons (AC4 — reported by category, not dropped):

1. **`harness_preflight_model_pin_unsupported`** — the qwen registry entry
   (`controller/src/sdlc/config/harnesses.yaml`) has no `{model}` placeholder
   in its command template. `sdlc.evaluate.resolve_eval_harness` (Story
   31.1-001 AC6) refuses to run a registry harness that would silently ignore
   the eval's pinned model, so the whole sweep aborts up front rather than
   mis-report a model that never ran. Exit code 2, stderr recorded verbatim
   in the failure record.
2. **No qwen auth configured on this host** — even bypassing (1), a direct
   `qwen -p "hello, reply with just OK"` on this machine fails immediately:
   `No auth type is selected. Please configure an auth type (e.g. via
   settings or --auth-type) before running in non-interactive mode.` There is
   no local model server (checked Ollama :11434 and LM Studio :1234 — neither
   is running) and no DashScope/API credential wired up.

This is exactly the outcome Story 31.3-001's technical notes anticipated:
*"Expect the local arm to expose adapter-level failures before it exposes
quality differences; that is a legitimate result."* It is one.

**No OpenCode arm**: `harnesses.yaml` has no `opencode` entry yet (Epic-29
29.2-001 had not landed an adapter as of this run), and per this story's own
dependency note the qwen arm was authorized to proceed without it.

## Comparison (AC3)

`sdlc eval-compare` requires two scoreboards in the same JSON shape. The qwen
arm never produced one — there is nothing to feed the comparator, and forcing
zeros into `tickets[].wall_mean`/`loc_*` etc. would fabricate a "the local arm
was instant and touched nothing" reading, which is false; it never ran. The
honest, recorded comparison is therefore qualitative, stated here rather than
mechanically computed:

| | claude | qwen (local) |
|---|---|---|
| Runs completed | 9 / 9 | 0 / 9 |
| Quality pass rate | 100% | n/a — never dispatched |
| Tokens / cost | measured (199,430 / $0.106 overall) | n/a |
| Wall time | 27.5s mean (stall-adjusted) | n/a |
| Failure category | none | `harness_preflight_model_pin_unsupported` + unauthenticated CLI |

**Excluded from any verdict**: every metric, for every ticket, on the qwen
side — none are comparable when one side never ran (this is the same
"missing is not zero" discipline `usage_comparability`/`classify_metric`
already enforce for token/cost; it applies with full force to a whole-arm
failure too).

## Go / No-Go

**No-go, for now** — do not route any real stage to local (qwen) inference
on this machine as it stands. Two concrete, independent blockers, both fixed
by configuration rather than by model quality:

1. Add a `{model}` placeholder + `models` map to the qwen registry entry
   (mirroring the already-commented-out codex example in `harnesses.yaml`),
   and confirm the `qwen-build-adapter.sh` wrapper accepts a `--model` flag
   end to end (it currently rejects any positional argument other than
   `--self-test`).
2. Wire up a real inference backend for the qwen CLI on the target
   machine — a local model server (Ollama/LM Studio, neither running here)
   or a DashScope credential — and re-run `qwen -p "hello"` non-interactively
   to confirm auth resolves before spending any ticket budget on it.

Until both are done, there is no local-arm quality/cost/speed signal to
weigh against claude at all — the honest state of this decision is "blocked
on adapter wiring," not "local models are worse." claude itself is in good
shape to keep routing production stages to: 100% quality across all three
tickets, real measured token/cost provenance, no stalls, no errors.

**Re-run checklist**, once qwen is wired up: `uv run sdlc eval --harness qwen
--json --config eval/eval-config.yaml > eval/results/qwen-scoreboard-31.3-001.json`,
then `sdlc eval-compare --baseline eval/results/claude-scoreboard-31.3-001.json
--candidate eval/results/qwen-scoreboard-31.3-001.json` for the real
claude-vs-local verdict this story set out to produce.
