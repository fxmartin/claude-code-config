<!-- ABOUTME: Measured performance/token baseline for Epic-27 with reproduction queries -->

# Performance & Token Baseline (Epic-27)

Measured baseline for Epic-27 (Performance & Token Optimization). Every optimization
story in the epic — and any future cost/latency work — is verified against these
numbers, regenerated with the reproduction code below.

- **Snapshot date**: 2026-07-15 (numbers match the 2026-07-11 epic analysis; the
  interactive totals drift slightly upward as sessions keep accruing)
- **Measurement window**: 2026-06-10 → 2026-07-11 (controller stage-log aggregation
  and ledger durations); interactive model mix is open-ended from 2026-06-10
  (first usage record: 2026-06-15)
- **Data sources**:
  - `.sdlc-state.db` — controller ledger (`stages` table: timestamps, status; token
    columns are only populated for recent runs, so tokens/cost come from the logs)
  - `.sdlc-state.db.logs/<run_id>/<story>-<stage>-<attempt>.log` — stream-json stage
    logs; the final `"type":"result"` line carries `usage`, `total_cost_usd`,
    `duration_ms`, and `modelUsage`
  - `~/.claude/projects/<project-dir>/*.jsonl` — session transcripts for the
    interactive/Agent-tool path (project dirs matching `claude-code-config`,
    excluding `worktrees` and `sdlc-eval` dirs, which belong to the controller and
    eval harness paths)

## 1. Controller path: per-stage cost / tokens / duration

Aggregated over the **50 stories** whose stage logs carry stream-json usage in the
window (455.19 USD total ⇒ **≈ $9.10 per story**). Durations here are the agent
session's own wall clock (`duration_ms`), not the ledger's start→finish span.

| Stage | Dispatches | Total cost | Avg cost | Avg output tok | Avg cache-read tok | Avg duration (min) | Cost share |
|-------|-----------:|-----------:|---------:|---------------:|-------------------:|-------------------:|-----------:|
| build | 50 | $263.45 | $5.27 | 38,494 | 5,687,404 | 12.4 | 58% |
| coverage | 47 | $82.23 | $1.75 | 11,762 | 1,542,457 | 6.3 | 18% |
| review | 48 | $59.26 | $1.23 | 7,598 | 817,687 | 2.9 | 13% |
| merge | 44 | $29.65 | $0.67 | 3,558 | 340,425 | 1.6 | 7% |
| bugfix | 9 | $13.34 | $1.48 | 9,242 | 1,054,715 | 4.0 | 3% |
| reask | 12 | $6.21 | $0.52 | 2,399 | 207,192 | 1.0 | 1% |
| commitlint | 2 | $1.05 | $0.53 | 2,064 | 218,966 | 0.6 | 0% |

Read: build dominates at **58% of controller cost**; coverage + review together add
another 31%. Any per-dispatch prompt shrink (27.1-003/004) or gate tiering (27.2-x)
is levered through these dispatch counts.

## 2. Interactive/Agent-tool path: model mix

Per-model output tokens across all interactive session transcripts since 2026-06-10
(310 sessions with usage; scanned 2026-07-15). This is the path where `fix-issue`
hardcodes Opus and repo agents inherit the interactive session default.

| Model | Messages | Output tok | Cache-read tok | Output share |
|-------|---------:|-----------:|---------------:|-------------:|
| claude-opus-4-8 | 23,967 | 20,276,987 | 3,946,072,008 | **93.3%** |
| claude-fable-5 | 1,716 | 1,450,800 | 396,221,840 | 6.7% |

Read: **≈ 94% of interactive token traffic runs on Opus** (~21.7M output /
~4,342M cache-read tokens total in the window; the epic quotes 21.6M/4,268M from
the 2026-07-11 snapshot). No Sonnet or Haiku traffic appears on this path at all —
the silent default is absolute. Feature 27.1 targets exactly this table.

## 3. Healthy vs outlier stage durations (ledger)

Ledger start→finish spans (`stages` table, `status='DONE'`, window
2026-06-10 → 2026-07-11). Unlike §1's session wall clock, these include queueing,
retries, and quota-backoff loops — which is where the outliers live.

| Stage | n | p50 (min) | p90 (min) | max (min) | stages > 1h |
|-------|--:|----------:|----------:|----------:|------------:|
| build | 118 | 10.3 | 22.5 | 24,742.1 | 1 |
| coverage | 112 | 4.4 | 10.2 | 1,231.2 | 4 |
| bugfix | 4 | 3.9 | 4.9 | 11.2 | 0 |
| review | 109 | 2.2 | 4.0 | 10.9 | 0 |
| commitlint | 20 | 1.3 | 2.3 | 4.7 | 0 |
| merge | 112 | 1.0 | 11.3 | 24,717.5 | 8 |
| reask | 18 | 0.9 | 1.6 | 4.2 | 0 |

Worst run: `cdfb8cc8-f2be-422f-92d3-23331c5180cc` averaged **504 min per coverage
stage** across 4 coverage stages (healthy median: 4.4 min — a ~115× blowup). The
next-worst (`4fed56b0…`) averaged 249 min over 5. The 13 stages that ran > 1h are
concentrated in merge and coverage, consistent with 5-minute rate-limit backoff
loops after quota exhaustion — tokens and wall clock are coupled, so Feature 27.1's
token cuts are also the primary stall fix. Story 27.3-005 adds the telemetry to
attribute these outliers directly.

## 4. Reproduction

All commands run from the deployment checkout root
(`~/Documents/nix-install/config/claude-code-config`), where `.sdlc-state.db` and
its `.logs` directory live.

### 4.1 Table 1 — stage-log usage aggregation

Save as `stage_usage.py` and run
`python3 stage_usage.py .sdlc-state.db.logs 2026-06-10 2026-07-11`
(args: log root, optional since/until — logs are windowed by file mtime).

```python
#!/usr/bin/env python3
# ABOUTME: Aggregates per-stage cost/token/duration from sdlc stage logs (stream-json result lines)
"""Per-stage usage aggregation over .sdlc-state.db.logs/<run>/<story>-<stage>-<n>.log."""
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

LOG_ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".sdlc-state.db.logs")
SINCE = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=timezone.utc) if len(sys.argv) > 2 else None
UNTIL = datetime.fromisoformat(sys.argv[3]).replace(tzinfo=timezone.utc) if len(sys.argv) > 3 else None

NAME_RE = re.compile(r"^(\d+\.\d+-\d+)-(.+)-(\d+)\.log$")

stages = defaultdict(lambda: {"n": 0, "cost": 0.0, "out": 0, "cache_read": 0, "cache_create": 0, "dur_ms": 0})
stories = set()
per_story_cost = defaultdict(float)
skipped = 0

for log in sorted(LOG_ROOT.glob("*/*.log")):
    m = NAME_RE.match(log.name)
    if not m:
        skipped += 1
        continue
    mtime = datetime.fromtimestamp(log.stat().st_mtime, tz=timezone.utc)
    if (SINCE and mtime < SINCE) or (UNTIL and mtime >= UNTIL):
        continue
    story, stage_full = m.group(1), m.group(2)
    stage = stage_full.split("-")[0]  # bugfix-merge -> bugfix
    result = None
    with open(log, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"type":"result"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "result":
                result = obj
    if result is None or "usage" not in result:
        skipped += 1
        continue
    u = result["usage"]
    s = stages[stage]
    s["n"] += 1
    s["cost"] += result.get("total_cost_usd") or 0.0
    s["out"] += u.get("output_tokens", 0)
    s["cache_read"] += u.get("cache_read_input_tokens", 0)
    s["cache_create"] += u.get("cache_creation_input_tokens", 0)
    s["dur_ms"] += result.get("duration_ms") or 0
    stories.add((log.parent.name, story))
    per_story_cost[(log.parent.name, story)] += result.get("total_cost_usd") or 0.0

total_cost = sum(s["cost"] for s in stages.values())
print(f"stories with usage logs: {len(stories)}   stage logs skipped (no result line / unparsable name): {skipped}")
print(f"total cost: ${total_cost:.2f}   avg per story: ${total_cost / max(len(stories), 1):.2f}")
print()
print("| Stage | Dispatches | Total cost | Avg cost | Avg output tok | Avg cache-read tok | Avg duration (min) | Cost share |")
print("|-------|-----------:|-----------:|---------:|---------------:|-------------------:|-------------------:|-----------:|")
for name, s in sorted(stages.items(), key=lambda kv: -kv[1]["cost"]):
    n = s["n"]
    print(
        f"| {name} | {n} | ${s['cost']:.2f} | ${s['cost']/n:.2f} | {s['out']//n:,} "
        f"| {s['cache_read']//n:,} | {s['dur_ms']/n/60000:.1f} | {100*s['cost']/total_cost:.0f}% |"
    )
```

### 4.2 Table 2 — transcript model-mix scan

Save as `model_mix.py` and run `python3 model_mix.py 2026-06-10`
(args: optional since/until, applied to each assistant message's timestamp).

```python
#!/usr/bin/env python3
# ABOUTME: Scans Claude Code session transcripts for per-model token usage (interactive/Agent-tool path)
"""Model-mix scan over ~/.claude/projects/<project-dir>/*.jsonl assistant messages."""
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
MATCH = "claude-code-config"
EXCLUDE = ("worktrees", "sdlc-eval")  # controller stage agents + eval harness are not the interactive path
SINCE = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=timezone.utc) if len(sys.argv) > 1 else None
UNTIL = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=timezone.utc) if len(sys.argv) > 2 else None

models = defaultdict(lambda: {"msgs": 0, "out": 0, "cache_read": 0, "cache_create": 0})
sessions = 0

for proj in sorted(PROJECTS.iterdir()):
    if MATCH not in proj.name or any(x in proj.name for x in EXCLUDE):
        continue
    for tr in proj.glob("*.jsonl"):
        counted = False
        with open(tr, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                ts = obj.get("timestamp")
                if ts:
                    when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if (SINCE and when < SINCE) or (UNTIL and when >= UNTIL):
                        continue
                msg = obj.get("message") or {}
                model = msg.get("model") or "unknown"
                u = msg.get("usage") or {}
                m = models[model]
                m["msgs"] += 1
                m["out"] += u.get("output_tokens", 0)
                m["cache_read"] += u.get("cache_read_input_tokens", 0)
                m["cache_create"] += u.get("cache_creation_input_tokens", 0)
                counted = True
        if counted:
            sessions += 1

models.pop("<synthetic>", None)
total_out = sum(m["out"] for m in models.values())
print(f"sessions with usage in window: {sessions}   total output tokens: {total_out:,}")
print()
print("| Model | Messages | Output tok | Cache-read tok | Output share |")
print("|-------|---------:|-----------:|---------------:|-------------:|")
for name, m in sorted(models.items(), key=lambda kv: -kv[1]["out"]):
    print(f"| {name} | {m['msgs']:,} | {m['out']:,} | {m['cache_read']:,} | {100*m['out']/max(total_out,1):.1f}% |")
```

### 4.3 Table 3 — ledger duration SQL

```sh
sqlite3 -header -column .sdlc-state.db "
WITH d AS (
  SELECT stage_name,
         (julianday(finished_at) - julianday(started_at)) * 1440.0 AS mins
  FROM stages
  WHERE status = 'DONE' AND started_at IS NOT NULL AND finished_at IS NOT NULL
        AND started_at >= '2026-06-10' AND started_at < '2026-07-11'
), r AS (
  SELECT stage_name, mins,
         PERCENT_RANK() OVER (PARTITION BY stage_name ORDER BY mins) AS pr
  FROM d
)
SELECT stage_name,
       COUNT(*) AS n,
       ROUND(MAX(CASE WHEN pr <= 0.50 THEN mins END), 1) AS p50_min,
       ROUND(MAX(CASE WHEN pr <= 0.90 THEN mins END), 1) AS p90_min,
       ROUND(MAX(mins), 1) AS max_min,
       SUM(mins > 60) AS over_1h
FROM r GROUP BY stage_name ORDER BY p50_min DESC;"
```

Outlier-run drill-down (which runs blew up on coverage):

```sh
sqlite3 -header -column .sdlc-state.db "
SELECT run_id, COUNT(*) AS n,
       ROUND(AVG((julianday(finished_at)-julianday(started_at))*1440.0), 0) AS avg_cov_min
FROM stages
WHERE stage_name = 'coverage' AND status = 'DONE' AND finished_at IS NOT NULL
GROUP BY run_id ORDER BY avg_cov_min DESC LIMIT 5;"
```

## 5. Epic-27 success criteria (exit measurements)

Re-run the reproduction code over a comparable post-epic window and compare:

| Metric | Baseline (this doc) | Exit target | Measured by |
|--------|---------------------|-------------|-------------|
| Opus share of interactive output tokens | 93.3% (§2) | Materially down (Sonnet-by-default with risk escalation ⇒ expect < 50%) | §4.2 scan |
| Controller cost per story | ≈ $9.10 (§1) | Down vs baseline on a comparable story mix | §4.1 aggregation |
| Build stage cost share | 58% (§1) | Down (prompt dedup/shrink, story-section injection) | §4.1 aggregation |
| Merge-gate pass rate / bugfix-loop rate | 9 bugfix + 12 reask dispatches per 50 stories (§1) | Not regressed | §4.1 dispatch counts |
| Multi-hour stage-duration outliers | 13 stages > 1h; worst run 504 min avg/coverage (§3) | None attributable to quota backoff on comparable batch sizes | §4.3 SQL + 27.3-005 telemetry |

Quality gates are the non-negotiable guardrail: escalation to Opus stays for
flagged risk, the adversarial review slot keeps its Opus floor on high-risk, and
the coverage criterion is never lowered — only enforced deterministically.
