# Epic 11: Realtime Progress & Multi-Run Observability

> **Status: IN PROGRESS (8/13 shipped)** — created 2026-06-20. The original 8 stories merged
> (released through v1.34.0): 11.1-001 (#78), 11.1-002 (#80), 11.1-003 (#81), 11.2-001 (#79),
> 11.2-004 (#82), 11.2-005 (#83); 11.2-002 and 11.2-003 were stranded `NEEDS_ATTENTION` by a
> malformed result envelope, then recovered (committed work preserved — R10) and merged directly
> (commits `fa49d61`, `b88d934`) — the exact failure mode Epic-12 12.1-001 will automate. **Still
> planned:** the 5 follow-up dashboard stories added 2026-06-20 — 11.2-006 (GitHub repo-health),
> 11.2-007 (wave/dep persistence), 11.2-008 (wave-column DAG), 11.2-009 (live story status),
> 11.2-010 (transcript viewer). Builds on Epic-07 (external controller), Epic-04 (SQLite ledger),
> and Epic-10 (the `dashboard`/`status` verbs). Makes the controller's progress observable *as it
> happens*, and lets one dashboard watch several builds running in different repos at once.

## Epic Overview

**Epic ID**: Epic-11
**Description**: Today the controller dispatches each agent as a `claude -p --output-format json`
subprocess with **captured output**, so a stage is opaque until it finishes — the dashboard
and `sdlc status` only ever show a coarse "build in progress" pill, and the per-stage
transcript file is written only on stage completion. Separately, the dashboard reads a single
`.sdlc-state.db`, so it can show exactly one run from one repo. This epic closes both gaps:
(1) the controller streams agent activity and emits fine-grained sub-stage progress + running
token/cost into the ledger as work happens; (2) a central run registry lets one auto-refreshing
dashboard discover and display every active run across repos simultaneously.

**Business Value**: FX runs long, unattended autonomous batches (often overnight, and
increasingly two repos in parallel). Without live insight, a stuck or misbehaving agent is
invisible until a whole stage times out — and there's no single pane of glass when more than
one build is running. Realtime progress turns the controller from a black box into something
debuggable in flight; multi-run support makes parallel batches manageable from one screen.

**Success Metrics**:
- Time-to-first-signal on a running stage drops from "stage duration" (minutes) to **< 3 s**.
- The dashboard reflects a state change (sub-stage event, token tick, stage transition) within
  **2 s** of it being written to the ledger, with **no manual reload**.
- Two `sdlc build` runs in two different repos both appear in one dashboard automatically,
  with correct per-run isolation (no cross-run data bleed).
- Live token/cost per story is accurate within rounding of the final `--output-format` totals.

## Epic Scope

**Total Stories**: 16 | **Total Points**: 56 | **MVP Stories**: 0 (roadmap — Should Have)

## Out of Scope (Non-Goals)

- **Remote/multi-machine aggregation.** The registry and dashboard are single-host (localhost
  only, like today's `dashboard --host 127.0.0.1`). Watching runs on another machine is not in
  this epic.
- **Persisting full agent transcripts in the ledger.** Streamed events are summarized into
  structured ledger rows; the verbatim stream still lands in the per-stage `.log` transcript
  files (Epic-10), not the SQLite DB.
- **Changing the result contract.** The `<<<RESULT_JSON>>>` envelope and schema validation
  (Epic-07) stay exactly as-is; streaming must not alter how final results are parsed/validated.
- **Authentication / multi-user dashboard.** Still a local, single-user tool.
- **Historical analytics / charts beyond live token/cost.** Trend dashboards are a later epic.

## Features in This Epic

### Feature 11.1: Realtime Controller Progress

Make a running stage observable: stream the agent subprocess, tee it live to the transcript,
and emit structured sub-stage events + running token/cost to the ledger as they occur.

#### Stories

##### Story 11.1-001: Stream agent output with a live transcript tee
**User Story**: As FX watching a build, I want each agent's output streamed and written to its
transcript as it is produced so that I can see what a stage is doing without waiting for the
stage to finish.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the controller dispatches an agent **When** the subprocess is launched **Then** it
  uses `--output-format stream-json --verbose` (configurable via `SDLC_AGENT_CMD`) and the
  controller consumes the stream incrementally rather than buffering all of stdout.
- **Given** a stage is running **When** the agent emits stream events **Then** each event is
  appended to the per-stage transcript file (`.sdlc-state.db.logs/<run>/<story>-<stage>-<attempt>.log`)
  within ~1 s, so `tail -f` on that file shows live activity.
- **Given** the agent completes **When** the stream ends **Then** the controller still extracts
  the final `<<<RESULT_JSON>>>` block and the usage/cost/session_id totals exactly as before,
  and schema validation behaves identically to the captured-output path.
- **Given** `SDLC_AGENT_CMD` is overridden to a non-streaming command (or stream parsing fails)
  **When** the agent runs **Then** the controller falls back to the current captured-output
  behavior without failing the run (graceful degradation).

**Technical Notes**: Touches `controller/src/sdlc/dispatch.py` (the `subprocess.run(..., capture_output=True)`
call becomes a streamed read of stdout line-by-line; `_parse_envelope` consumes the terminal
`result` event). Keep `_write_transcript` semantics but make it incremental. The stream-json
format emits one JSON object per line (system/assistant/tool_use/tool_result/result); parse
defensively — unknown event types are teed but ignored for control flow.

**Definition of Done**:
- [ ] Streaming dispatch implemented with incremental transcript tee
- [ ] Final result envelope + usage extraction unchanged; schema validation parity proven by test
- [ ] Graceful fallback to captured mode covered by a test
- [ ] Unit tests with a synthetic stream-json fixture (no live `claude` dependency)
- [ ] Documentation updated (`docs/controller-architecture.md` / dispatch section)

**Dependencies**: None
**Risk Level**: High

##### Story 11.1-002: Emit fine-grained sub-stage progress events to the ledger
**User Story**: As FX, I want the controller to record what an agent is doing mid-stage
(tool calls, files edited, tests run) so that the dashboard and `sdlc status` can show
sub-stage detail instead of a single pill.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the agent stream is being consumed (11.1-001) **When** a meaningful milestone occurs
  (agent started, a tool is invoked, a file is written, a test command runs) **Then** the
  controller appends a structured progress event to the ledger `events` table tagged with
  `run_id`, `story_id`, `stage`, an event `kind`, and a short human-readable message.
- **Given** progress events exist for a run **When** `sdlc status` is invoked **Then** it shows
  the current sub-stage activity for each in-flight story (e.g. "build: editing cli.py"),
  not just the stage name.
- **Given** a high-volume stream **When** events are emitted **Then** they are rate-limited /
  coalesced so the ledger is not flooded (e.g. de-dupe consecutive identical kinds, cap per
  second), and writes never block the agent stream.

**Technical Notes**: Map stream-json event types to a small fixed `kind` enum
(`agent_started`, `tool_use`, `file_changed`, `test_run`, `message`). Reuse the Epic-04 events
schema; add columns or a typed `kind`/`stage` if needed (migration). Writes are append-only and
must tolerate concurrent readers (the dashboard).

**Definition of Done**:
- [ ] Stream events mapped to ledger progress rows with rate-limiting
- [ ] `sdlc status` renders current sub-stage activity
- [ ] Tests cover mapping, coalescing, and the status rendering
- [ ] Ledger migration (if any) is additive and back-compatible

**Dependencies**: 11.1-001
**Risk Level**: Medium

##### Story 11.1-003: Track running token usage and cost per story and stage
**User Story**: As FX, I want live token and cost accrual per story/stage so that I can see
spend building up during a run, not only the final total.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the agent stream reports incremental usage **When** events arrive **Then** the
  controller accumulates input/output tokens and cost into the ledger keyed by `run_id` +
  `story_id` + `stage`, updated as the stage progresses.
- **Given** a stage completes **When** the final `result` usage is known **Then** the
  accumulated figure is reconciled to the authoritative total (no double-counting, final value
  wins).
- **Given** a run with several completed and in-flight stages **When** queried **Then** the
  ledger exposes a per-run running total and per-story/stage breakdown.

**Technical Notes**: If the stream does not carry incremental usage, fall back to recording the
final per-stage usage on completion (still a strict improvement over today's run-level total).
Store as integers (tokens) + a decimal cost; surface via the same query path `status`/dashboard use.

**Definition of Done**:
- [ ] Running token/cost accrual persisted per story/stage
- [ ] Reconciliation to final totals on stage completion (tested)
- [ ] Query surface exposes per-run + per-story breakdown
- [ ] Tests with a fixture stream covering accrual + reconciliation

**Dependencies**: 11.1-001
**Risk Level**: Low

### Feature 11.2: Multi-Run Live Dashboard

Let one auto-refreshing dashboard discover and display every active run across repos, with live
sub-stage detail per run.

#### Stories

##### Story 11.2-001: Central run registry
**User Story**: As FX running builds in more than one repo, I want each `sdlc build` to register
itself in a shared location so that a single dashboard can find every run without me listing
ledger paths.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `sdlc build` starts **When** the run is created **Then** it writes an entry to a
  host-level registry (default `~/.sdlc/registry.json`, XDG-aware) containing at least:
  `run_id`, absolute repo path, ledger `db` path, `scope`, `pid`, `status`, and `started_at`.
- **Given** a run finishes (success, failure, or abort) **When** the controller exits **Then**
  the registry entry's `status` and `finished_at` are updated.
- **Given** a registry entry whose `pid` is no longer alive and has no `finished_at` **When** the
  registry is read **Then** it is reported as `stale`/`dead` (and prunable), so a crashed run
  does not linger as "in progress" forever.
- **Given** the registry **When** `sdlc runs` is invoked **Then** it lists all known runs with
  repo, scope, status, and progress.

**Technical Notes**: New `controller/src/sdlc/registry.py`. Concurrency-safe writes (atomic
replace / file lock) since two `sdlc build` processes may register at once. Registry is a cache,
not a source of truth — the per-repo ledger remains authoritative for a run's detail.

**Definition of Done**:
- [ ] Registry module with atomic, concurrency-safe writes
- [ ] `sdlc build` registers on start and updates on exit (incl. abnormal exit best-effort)
- [ ] Dead-pid detection + prune
- [ ] `sdlc runs` CLI lists entries
- [ ] Tests cover concurrent registration, stale detection, prune

**Dependencies**: None
**Risk Level**: Medium

##### Story 11.2-002: Multi-run dashboard overview
**User Story**: As FX, I want the dashboard to show all runs from the registry so that I can see
two parallel builds in two repos from one screen.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the registry lists multiple runs **When** I open the dashboard **Then** it shows an
  overview of every active/recent run (repo, scope, status, stories done/total) and lets me
  click into a single run's detail.
- **Given** a specific run is selected **When** its detail loads **Then** the dashboard reads
  that run's own ledger `db` path from the registry (correct per-run isolation, no cross-run
  bleed).
- **Given** the existing single-run usage **When** `sdlc dashboard --db <path>` is passed
  **Then** it still works (back-compatible); registry discovery is the default when no `--db`
  is given.

**Technical Notes**: Extend `controller/src/sdlc/dashboard.py`. New `/api/runs` returns the
registry view; per-run endpoints take a run id and resolve the ledger via the registry.

**Definition of Done**:
- [ ] Overview view lists all registry runs with status/progress
- [ ] Per-run detail resolves the correct ledger; isolation verified by test
- [ ] `--db` single-run path preserved
- [ ] Tests for multi-run discovery + isolation

**Dependencies**: 11.2-001
**Risk Level**: Low

##### Story 11.2-003: Live auto-refresh transport (no manual reload)
**User Story**: As FX, I want the dashboard to update by itself so that I can leave it open and
watch progress without hitting reload.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the dashboard is open **When** the ledger changes (new event, stage transition,
  token tick) **Then** the page updates within ~2 s with no manual reload, via server push
  (Server-Sent Events) over the existing local HTTP server.
- **Given** a transient disconnect **When** the connection drops **Then** the client
  auto-reconnects and resumes without a full page reload or duplicated rows.
- **Given** no changes are occurring **When** the dashboard is idle **Then** the transport is
  quiet (heartbeat only) and CPU use stays negligible.

**Technical Notes**: SSE keeps the dependency footprint minimal (stdlib HTTP server can stream
`text/event-stream`); avoids a websocket library. The server watches the ledger (poll the
`events` table max-rowid or a change token on a short interval) and pushes deltas. Must support
multiple connected browser tabs.

**Definition of Done**:
- [ ] SSE endpoint streaming ledger deltas; client auto-updates
- [ ] Reconnect handling without duplicate/again rendering
- [ ] Works with the single-stdlib-server constraint (no heavy deps)
- [ ] Tests for the change-detection/delta logic

**Dependencies**: None (operates off the ledger; pairs with 11.1-002)
**Risk Level**: Medium

##### Story 11.2-004: Live per-run detail view with sub-stage activity
**User Story**: As FX, I want a run's detail view to show live sub-stage activity, files touched,
and running token/cost so that I can tell exactly what each story's agent is doing right now.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a run with sub-stage progress events (11.1-002) **When** I view its detail **Then**
  each in-flight story shows its current stage, the latest sub-stage activity, and a live-updating
  token/cost figure (11.1-003), refreshing via the transport (11.2-003).
- **Given** a stage transitions or a new sub-stage event arrives **When** it is written to the
  ledger **Then** the detail view reflects it within ~2 s without reload.
- **Given** a completed run **When** I open its detail **Then** it shows the final per-stage
  timeline, totals, and PR links (graceful for finished as well as live runs).

**Technical Notes**: Builds on the PR #67 per-stage pipeline view; adds a live sub-stage row and
binds to the SSE stream. Render defensively when sub-stage data is absent (older runs / fallback
mode) — degrade to stage-level.

**Definition of Done**:
- [ ] Per-run detail renders live sub-stage activity + running token/cost
- [ ] Live updates via the transport within the latency target
- [ ] Graceful rendering for finished runs and fallback (no sub-stage data)
- [ ] Tests for the rendering/state logic

**Dependencies**: 11.1-002, 11.2-003
**Risk Level**: Medium

##### Story 11.2-005: Display run and per-story durations on the dashboard
**User Story**: As FX, I want the dashboard to show how long each run took and how long each
story took (with a total), so that I can spot slow stories/stages and compare runs at a glance.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a finished run **When** I view it (overview row and detail) **Then** the dashboard
  shows the run's **total duration** (`finished_at − started_at`) in a human-readable form
  (e.g. `4m 12s`, `1h 03m`).
- **Given** an in-progress run **When** I view it **Then** the run shows its **elapsed**
  duration ticking from `started_at` to now (updating via the 11.2-003 transport; a static
  computed elapsed is acceptable when the transport is absent).
- **Given** a run's stories **When** I view the per-run detail **Then** each story row shows its
  own duration, derived from that story's stage rows (earliest stage `started_at` → latest stage
  `finished_at`), and an in-flight story shows elapsed-so-far.
- **Given** a story or stage with missing/null timestamps **When** duration is computed **Then**
  the cell degrades gracefully (e.g. `—`) and never renders `NaN` or a negative value.
- **Given** the durations are shown **When** they are computed **Then** they come from the
  ledger timestamps already persisted (`runs.started_at/finished_at`, `stages.started_at/finished_at`) —
  no new schema is required.

**Technical Notes**: Surfaced in `controller/src/sdlc/dashboard.py` (run overview + per-run
detail) and the `/api/status` / `/api/runs` payloads. Story duration could be the wall-clock
span (first stage start → last stage finish) or the sum of stage durations; the span is
recommended (it reflects real elapsed time including gaps) — capture the choice in the PR.
Formatting helper should be shared so overview and detail render identically. Live ticking for
in-progress runs rides on the 11.2-003 transport; without it, show the elapsed value computed
at page load.

**Definition of Done**:
- [ ] Run total duration shown for finished and in-progress runs (elapsed for the latter)
- [ ] Per-story duration shown in the detail view, derived from stage timestamps
- [ ] Graceful handling of missing/null timestamps (no NaN/negative)
- [ ] Durations exposed in the dashboard API payload; shared human-readable formatter
- [ ] Tests for the duration computation + formatting (incl. in-progress and missing-timestamp cases)

**Dependencies**: None (reads existing ledger timestamps; the existing dashboard from #66/#67). Live ticking pairs with 11.2-003.
**Risk Level**: Low

##### Story 11.2-006: GitHub repo health on the multi-run dashboard (issues, PRs, CI status)
**User Story**: As FX watching builds across several repos, I want each repo's GitHub health —
open/closed issues, open/closed PRs, and CI status — shown on the dashboard so that I can see
every run's external state at a glance and drill into one without leaving the dashboard or opening
GitHub.
**Priority**: Should Have
**Story Points**: 3

> **Design note (revised 2026-06-21)**: written for the now-deployed **multi-run** dashboard
> (registry discovery shipped in 11.2-001/11.2-002, v1.34.0). Placement is **per-repo badge on
> each overview row + a full panel in the selected run's detail view** — not a single top bar.
> The earlier "no run selected → show the launched-from repo" notion is dropped: in registry mode
> there is no single launch repo.

**Acceptance Criteria**:
- **Given** the multi-run overview lists runs across repos **When** each run row renders **Then**
  it shows a **compact** GitHub badge for that run's repo — open-issue count, open-PR count, and
  the latest default-branch CI conclusion as a clear visual state (✓ success / ✗ failure / ◷
  in-progress) — keyed off the registry `repo` path.
- **Given** several runs share one repo **When** the overview renders **Then** their GitHub stats
  come from a **single per-repo fetch** (deduped by repo), not one fetch per run.
- **Given** a run is selected **When** its detail view opens **Then** it shows the **full** GitHub
  panel for that run's repo: issues open/closed, PRs open/closed (merged + closed), and the latest
  default-branch CI status with its branch and age.
- **Given** the CI status **When** it is fetched **Then** it reflects the conclusion of the most
  recent workflow run on the repo's **default branch** (`success` / `failure` / `in_progress` /
  `cancelled`).
- **Given** the dashboard is open **When** ~60 s elapses **Then** the GitHub data refreshes from a
  backend-cached fetch without a full page reload; the existing ~2.5 s client poll reads the cache
  and never itself drives `gh`.
- **Given** multiple runs across different repos **When** they render **Then** stats are per-repo
  with no cross-repo bleed, resolved via the registry, and cached **per repo** (TTL ~60 s) so
  GitHub's rate limit is respected regardless of how many runs/tabs are open.
- **Given** `gh` is unavailable, unauthenticated, rate-limited, or the repo has no GitHub remote
  **When** the badge/panel renders **Then** it degrades gracefully to a muted "GitHub unavailable"
  state, never blocks the ledger-driven dashboard, and never throws.

**Technical Notes**: New backend helper `controller/src/sdlc/github_stats.py` that derives the
`owner/repo` slug from a run's repo path git remote — reuse `git_project_url()` and the
`_SCP_REMOTE`/`_URL_REMOTE` regexes already in `dashboard.py` — and reads counts via the GitHub
API (`gh api`/search for issue + PR open/closed counts; `gh run list` or `actions/runs` for the
latest default-branch run). Results cached with a short TTL (~60 s) **keyed by repo slug** and
fetched **off the request path** (serve cached, refresh when stale) so a slow/failing call never
blocks the dashboard and N runs in one repo cost one fetch. Wiring: enrich `_registry_runs_view`
with a compact per-row `github` summary (deduped per repo) for the **overview rows**; add a
`/api/github?run=<id>` route in `do_GET` (resolve the repo via `_resolve_run` → `RunRecord.repo`;
single-`--db` mode resolves from the ledger's parent dir) for the **detail panel**; render in
`renderRuns()` (row badge) and `renderMain()` (full panel). Note: `gh` consumes **GitHub's** rate
limit, not the Claude Max window. **Deferred**: PR-branch-specific CI (the SDLC run's own PR's
checks) — default-branch latest run only; revisit if per-PR CI proves more useful.

**Definition of Done**:
- [x] `github_stats.py` fetch with per-repo-slug TTL cache (~60 s), off the request path; graceful "unavailable" sentinel
- [x] Repo slug derived from the run's git remote (reusing `git_project_url`/remote regexes)
- [x] Overview rows show a compact per-repo badge (issues / PRs / CI), deduped per repo
- [x] Selected run's detail view shows the full GitHub panel
- [x] ~60 s server-side refresh; client poll reads cache, never drives `gh`; no full reload
- [x] Per-repo isolation via the registry (no cross-repo bleed); single-`--db` fallback works
- [x] Graceful degradation when `gh` is absent/unauthenticated/rate-limited or no GitHub remote
- [x] Tests for slug resolution, count/CI parsing, caching/TTL, dedup, and the unavailable fallback (no live `gh` dependency)
- [x] Story doc + `docs/controller-architecture.md` dashboard section updated

**Dependencies**: 11.2-001 (registry `repo` path) and 11.2-002 (multi-run overview) — **both
shipped (v1.34.0), so this is unblocked**. The ~60 s poll is independent of the 11.2-003 SSE
transport.
**Risk Level**: Medium

##### Story 11.2-007: Persist story dependencies and wave (cohort) index to the ledger
**User Story**: As FX, I want each story's in-queue dependencies and its computed wave (cohort)
index recorded in the ledger when a build is scheduled, so that the dashboard and `sdlc status`
can show the parallelism structure of a run without re-reading the epic files.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `run_build` computes cohorts (`cohort.compute_cohorts`) **When** it schedules the
  run's stories **Then** each story row records its wave index (the cohort's position, stories
  in the same wave run in parallel) and its intra-queue dependency list, via an **additive**
  ledger migration; existing rows of older ledgers are untouched and read as NULL/empty.
- **Given** `resume` recomputes cohorts for the same queue **When** it records story rows
  **Then** it persists the **same** wave indices as `run_build` would for that queue (the two
  scheduling paths agree), proven by a test.
- **Given** a story depends on an already-merged (out-of-queue) story **When** the wave index is
  computed **Then** only intra-queue edges count — matching `compute_cohorts` semantics — so the
  persisted structure reflects the actual runtime parallelism.
- **Given** the ledger **When** queried via the status/dashboard query path **Then** it exposes
  per-story wave index and dependency list for a run.

**Technical Notes**: Additive migration in the `build.py` migrations list (same pattern as
Migration 1 "stage usage columns"): add `wave INTEGER` and `dependencies TEXT` (JSON array of
story ids) to the `stories` table — both nullable for back-compat. Populate when scheduling: in
`run_build` Phase 2 by enumerating the `compute_cohorts` result, and mirror the same assignment
in `resume.py` so both paths stay consistent. No change to the result contract or stage
execution. The `stories` table has no `dependencies`/`wave` columns today, so the DAG view
(11.2-008) cannot recompute waves without this story.

**Definition of Done**:
- [ ] Additive migration adds `wave` + `dependencies` to `stories` (idempotent, back-compatible)
- [ ] `run_build` records wave index + intra-queue deps per story at schedule time
- [ ] `resume` records identical wave indices for the same queue (parity test)
- [ ] Query surface exposes wave + deps per story for a run
- [ ] Tests: migration idempotency, wave/deps recording, build↔resume parity, NULL degradation for old ledgers

**Dependencies**: None
**Risk Level**: Medium

##### Story 11.2-008: Wave-column dependency DAG in the per-run detail view
**User Story**: As FX, I want the per-run detail to render a dependency DAG with waves as columns
so that I can see at a glance which stories run in parallel and what blocks what.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a run whose stories carry wave + dependency data (11.2-007) **When** I open its
  detail **Then** the dashboard renders a DAG where each **column is a wave** (left→right =
  execution order), each **node is a story** (id, title, live status), and **edges** connect a
  story to its in-queue dependencies.
- **Given** stories in the same wave **When** rendered **Then** they stack in the same column
  (visually "in parallel"), under a header like "Wave N — runs in parallel".
- **Given** the run progresses **When** a story's status changes (pending → running →
  done/failed) **Then** the node's visual state updates on the existing refresh cadence (live via
  11.2-003 when present; static at page load otherwise) without a full reload.
- **Given** the epic's no-heavy-dependency constraint **When** the DAG renders **Then** it uses
  inline SVG/HTML + vanilla JS (no external graph library): column x derived from wave index,
  node y from order-within-wave, edges as simple SVG connectors.
- **Given** a run with missing wave/dependency data (older ledger or a `--sequential` run)
  **When** the detail loads **Then** the view degrades gracefully — falls back to the existing
  flat story list or a single-column layout — and never errors.

**Technical Notes**: Extend `dashboard.py` per-run detail + API payload to include wave/deps from
11.2-007. Layout is constrained (wave = column), so no general graph-layout library is needed —
route edges as SVG paths between node anchors. Reuse existing status colors/chips. `--sequential`
runs collapse to one story per wave (still valid). Stay within the epic's single stdlib HTTP
server + no-heavy-frontend-deps constraint.

**Definition of Done**:
- [x] Per-run detail renders a wave-column DAG (columns=waves, nodes=stories, edges=deps) with inline SVG, no external graph lib
- [x] Node status updates live on the refresh transport; static fallback at page load
- [x] Graceful degradation for missing wave/deps and `--sequential` runs
- [x] API payload exposes wave + deps per story
- [x] Tests for layout assignment (wave→column, order→row), edge derivation, and the degradation path

**Dependencies**: 11.2-007 (wave + deps in the ledger); pairs with 11.2-002 (run selection) and 11.2-003 (live status)
**Risk Level**: Medium

##### Story 11.2-009: Live story status in the dashboard (TODO / STARTED / DONE / FAILED)
**User Story**: As FX watching a build, I want the dashboard status column to show STARTED while a
story is being worked on (not TODO) and the right terminal label when it ends, so that the column
reflects what is actually happening instead of sitting on TODO through the whole build.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a story has begun (its first stage has started / `current_stage` is set) and is not
  yet terminal **When** the dashboard status column renders **Then** it shows **STARTED** — not
  TODO.
- **Given** a story that has not begun (no stage started) **When** rendered **Then** it shows
  **TODO**; a story that finished cleanly shows **DONE**; a story that failed shows **FAILED**.
- **Given** the underlying cause — the controller sets the story's ledger status only on
  completion, leaving it `TODO` for the whole build — **When** a story starts its first stage
  **Then** the controller marks the story `IN_PROGRESS` (mirrored in `build` and `resume`), so the
  dashboard, `sdlc status`, and the ledger all reflect "started" consistently.
- **Given** the dashboard maps statuses to labels **When** it renders **Then** `IN_PROGRESS` →
  `STARTED`, while `BLOCKED`, `NEEDS_ATTENTION`, and `SKIPPED` remain their own distinct labels
  (not collapsed into the four) — no real state is hidden.
- **Given** `--sequential` or parallel execution **When** stories run **Then** the displayed
  status tracks actual progress identically.

**Technical Notes**: Root cause is in `controller/src/sdlc/build.py`: `story_upsert` seeds `TODO`
(~line 1211) and `set_story_status(outcome)` runs only *after* `_run_story` returns (~line 1241) —
the story row is never set `IN_PROGRESS` at start, even though stage rows do go `IN_PROGRESS` via
`stage_start`. Preferred fix: set the story status to `IN_PROGRESS` when its first stage starts
(mirror in `resume.py`), which fixes every consumer. The dashboard label map lives in
`controller/src/sdlc/dashboard.py` — badge rendering at `dashboard.py:321` (`badge(s.status)`), the
`ORDER` list (~line 216), and the badge CSS (~lines 179–185): add an `IN_PROGRESS`→`STARTED` label
plus a `.STARTED`/`.IN_PROGRESS` badge style. A dashboard-only fallback (derive STARTED from
`current_stage`/active stage rows without the controller write) is acceptable if the controller
change must be deferred, but the controller fix is preferred. Foundational for Epic-17 story
17.3-001 (which extends "started" to *multiple* concurrently-active stories).

**Definition of Done**:
- [ ] Controller marks a story `IN_PROGRESS` when its first stage starts (`build` + `resume`)
- [ ] Dashboard status column shows TODO / STARTED / DONE / FAILED (STARTED = `IN_PROGRESS`)
- [ ] `BLOCKED` / `NEEDS_ATTENTION` / `SKIPPED` retained as distinct labels
- [ ] No mid-build TODO: a story in any active stage renders STARTED
- [ ] Tests: story-status transition (start → `IN_PROGRESS`; terminal labels) + dashboard label map
- [ ] Status lifecycle documented in `docs/controller-architecture.md`

**Dependencies**: None (foundational for Epic-17 17.3-001)
**Risk Level**: Low

##### Story 11.2-010: In-dashboard transcript viewer (expand / modal per story)
**User Story**: As FX debugging a run, I want to read a story's agent transcripts from inside the
dashboard so that I can see what each `claude -p` session did without hunting for `.log` files or
leaving the page.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a story row with stage transcripts on disk **When** I click a labelled "view session"
  control **Then** a modal (or inline expand) lists that story's stage transcripts — build,
  coverage, review, merge, and any bugfix attempts — and lets me read each one **without leaving
  the dashboard**.
- **Given** a selected stage transcript **When** it renders **Then** its content is fetched via
  the existing path-restricted, localhost-only `/log` serving (or a small `/api/logs?run=&story=`
  that returns the stage→path list + content) — never exposing files outside the logs root.
- **Given** plain-text transcripts (today) **When** displayed **Then** they render readably; once
  Epic-11 11.1-001 streaming lands and logs become stream-json, the viewer degrades gracefully
  (pretty-prints events, or shows raw lines) rather than breaking.
- **Given** a stage with no transcript yet (not started, or transcript missing) **When** the
  viewer opens **Then** it shows a clear empty/placeholder state, not an error.
- **Given** the current behaviour **When** this ships **Then** the existing new-tab `/log` link is
  preserved as a fallback (no regression).

**Technical Notes**: Transcripts already exist as per-stage files
(`.sdlc-state.db.logs/<run>/<story>-<stage>-<attempt>.log`), pointed to by `stages.output_path`;
the dashboard already wraps each stage badge in `<a href='/log?path=…' target='_blank'>`
(`dashboard.py:232`) and serves them via the path-traversal-safe `/log` route
(`dashboard.py:391`). This story adds an in-page viewer over that existing surface — front-end
modal/expand plus, if cleaner, an `/api/logs` endpoint that enumerates a story's stage logs from
the ledger. Keep transcripts on disk (Epic-11 non-goal: not in SQLite). Pairs with 11.2-004 (live
detail) and 11.1-001 (stream-json format). Minor: transcripts may contain sensitive agent output —
acceptable for a localhost tool; dovetails with Epic-13 sanitization.

**Definition of Done**:
- [ ] Per-story "view session" control opens an in-dashboard modal/expand listing stage transcripts
- [ ] Content served via the existing path-restricted `/log` (or new `/api/logs`); no path-traversal
- [ ] Plain-text renders today; stream-json (post 11.1-001) degrades gracefully
- [ ] Empty/missing-transcript placeholder; new-tab link preserved as fallback
- [ ] Tests for the endpoint/enumeration + the path-restriction guard
- [ ] Documented in the dashboard section of `docs/controller-architecture.md`

**Dependencies**: None (builds on the existing `/log` endpoint + `stages.output_path`; pairs with 11.2-004 and 11.1-001)
**Risk Level**: Low

##### Story 11.2-011: Stable-height live regions (no screen jump on update)
**User Story**: As FX watching a live build, I want the auto-updating regions to keep a stable
height so that the page doesn't jump and reflow every time their content changes from one line to
two or three.
**Priority**: Should Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** a live-updating region whose content can render on **1, 2, or 3 lines** (the run
  summary `#head`, the per-story sub-stage `activityRow`, and the `#updated` line) **When** its
  content changes on a refresh / SSE tick **Then** the region's height stays stable (it reserves
  space for its maximum line count) so elements below it do **not** move and the page does not
  reflow.
- **Given** the viewer has scrolled while watching a run **When** updates arrive **Then** the
  scroll position is preserved — content does not shift under the cursor and the view does not
  jump to the top.
- **Given** content that would exceed the reserved lines **When** it renders **Then** it is
  clamped/ellipsized (or scrolls within the fixed-height box) rather than growing the layout.
- **Given** a region currently shows 1 line **When** it later shows 3 (and vice-versa) **Then**
  there is no visible reflow of surrounding elements.

**Technical Notes**: Pure front-end/CSS in the embedded dashboard HTML — **no backend change**.
The variable-height live regions are in `controller/src/sdlc/dashboard.py`: `#head` (run summary,
~line 432), the per-story `activityRow(s)` sub-stage line (~lines 347–352), and `#updated`
(~lines 296/369), all re-rendered each tick by `renderMain()` / the 11.2-003 SSE handler. Reserve
a stable height (e.g. `min-height` sized to the 3-line max, or a fixed-height container with
`overflow`/line-clamp) on those containers so a full `innerHTML` swap never changes their box
height. Pairs with 11.2-003 (auto-refresh transport) and 11.2-004 (live detail) which drive the
updates; this story only stabilises their layout.

**Definition of Done**:
- [ ] Live regions reserve stable height; content toggling 1↔2↔3 lines causes no reflow of elements below
- [ ] Scroll position preserved across updates (no jump-to-top, no shift under the cursor)
- [ ] Overflow beyond the reserved lines is clamped/scrolled, not grown
- [ ] Verified on a live (or simulated) run; test toggles 1/2/3-line content and asserts stable container height
- [ ] No backend change; dashboard section of `docs/controller-architecture.md` noted if relevant

**Dependencies**: None (stabilises the regions produced by 11.2-003 and 11.2-004)
**Risk Level**: Low

##### Story 11.2-012: Show story titles on the dashboard
**User Story**: As FX watching a build, I want each story's **title** shown next to its ID on the
dashboard so that I can tell what is being developed without opening the epic files.
**Priority**: Should Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** a run's stories **When** the per-story detail table renders **Then** each row shows
  the story **title** alongside its ID (e.g. `11.2-006 · GitHub repo health on the multi-run
  dashboard`), so the view is self-reading.
- **Given** the title already lives in the ledger `stories.title` **When** the status snapshot is
  built **Then** it carries the title through to the client (extend `status_snapshot` to include
  `title`; no schema change — the column exists, written on `story_upsert`).
- **Given** a long title **When** rendered **Then** it is truncated/ellipsized to keep the row
  layout stable (full title on hover), consistent with 11.2-011's no-reflow goal.
- **Given** an older ledger row with a null/empty title **When** rendered **Then** it degrades to
  just the ID (no blank / `undefined`).
- **Given** the multi-run overview **When** rendered **Then** it is unchanged (titles are
  per-story detail, not per-run).

**Technical Notes**: Pure read-path + front-end. The gap is (a) `status_snapshot`
(`controller/src/sdlc/status.py`) not selecting/returning `title`, and (b) `renderMain()`'s
story table in `dashboard.py` (~lines 460–475) not displaying it. No backend write change, no
schema change. Pairs with 11.2-004 (detail view) and 11.2-011 (stable-height rows).

**Definition of Done**:
- [ ] `status_snapshot` returns each story's `title`
- [ ] Per-story detail rows render `id · title`, truncated-with-hover; null title → ID only
- [ ] Overview unchanged; no reflow regression
- [ ] Tests for the snapshot field + the render (incl. null-title fallback)
- [ ] No schema/backend-write change

**Dependencies**: None (title already in the ledger; complements 11.2-004)
**Risk Level**: Low

##### Story 11.2-013: Surface `fix-issue` runs in the dashboard
**User Story**: As FX, I want a `fix-issue` session to show up in the dashboard like an
`sdlc build` run so that I can watch issue-fix pipelines on the same single pane of glass instead
of only via cmux/Telegram.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the `fix-issue` skill runs **When** it starts **Then** it **registers a run** in the
  host registry (`~/.sdlc/registry.json`) so the multi-run dashboard discovers it alongside
  `sdlc build` runs (repo, scope = the issue, pid, status, started_at).
- **Given** a `fix-issue` phase executes (investigate / build / coverage / review / E2E / merge)
  **When** it starts and finishes **Then** a **ledger stage row** is written (status, timestamps,
  and usage where available), so the run's detail view shows the pipeline progressing.
- **Given** the run completes (or fails/aborts) **When** it ends **Then** the run is **finalized**
  (DONE/FAILED) and a crashed/aborted `fix-issue` is detectable by dead pid, like a controller run.
- **Given** the run-logging path is unavailable (CLI missing, ledger unwritable) **When**
  `fix-issue` runs **Then** logging is **best-effort and never blocks the fix** — the pipeline
  still completes; the dashboard simply doesn't show it.
- **Given** the existing `sdlc build` runs **When** the dashboard renders **Then** they are
  unaffected (no change to controller-run behavior).

**Technical Notes**: `fix-issue` is a markdown skill (bash + Task sub-agents), not the controller,
so it cannot call Python directly. It needs a **minimal run-logging CLI** to shell out to per
phase — e.g. `sdlc run-open` / `sdlc run-stage <start|finish>` / `sdlc run-close` (or one
`sdlc run-event` verb) that reuses the existing `Registry.register` (`registry.py`),
`Ledger.run_create` / `stage_start` / `stage_finish` (`build.py`), and is read back by
`status_snapshot`. The skill's `## Phase N` blocks already shell `cmux-bridge.sh`, so the
run-logging calls slot in beside those. **Scope: fix-issue-only** — `build-stories` and future
skills can adopt the same CLI later (deliberately not generalised now). The new CLI surface is the
only addition; storage/render reuse the multi-run dashboard as-is.

**Definition of Done**:
- [ ] Minimal `sdlc` run-logging CLI (open/stage/close) reusing `Registry` + `Ledger`
- [ ] `fix-issue` registers a run on start, logs a stage per phase, finalizes on completion
- [ ] `fix-issue` session appears in the multi-run dashboard with per-phase detail
- [ ] Best-effort logging: a logging failure never blocks the fix
- [ ] `sdlc build` runs unaffected
- [ ] Tests for the run-logging CLI (open/stage/close → ledger + registry) without a live skill run
- [ ] Documented in `docs/controller-architecture.md` and the `fix-issue` skill

**Dependencies**: 11.2-001 (registry) and the ledger run/stage API — both shipped. Reuses
`status_snapshot` (11.2-002/11.2-004 rendering).
**Risk Level**: Medium

## Story Dependencies (within Epic-11)

```
11.1-001 (stream) ──┬─> 11.1-002 (sub-stage events) ──┐
                    └─> 11.1-003 (token/cost)          ├─> 11.2-004 (live detail view)
11.2-001 (registry) ──> 11.2-002 (multi-run overview)  │
11.2-003 (SSE transport) ──────────────────────────────┘
```

- **Cohort 1** (no deps): 11.1-001, 11.2-001, 11.2-003, 11.2-005, 11.2-007, 11.2-009, 11.2-010, 11.2-011, 11.2-012, 11.2-013
- **Cohort 2**: 11.1-002, 11.1-003 (need 11.1-001); 11.2-002 (needs 11.2-001)
- **Cohort 3**: 11.2-004 (needs 11.1-002 + 11.2-003); 11.2-006 (needs 11.2-001 + 11.2-002); 11.2-008 (needs 11.2-007)

(11.2-005 reads existing ledger timestamps and can land independently; its live-ticking
refinement pairs with 11.2-003 but is not blocked by it.)

## Epic Complete When

- A running stage is observable live: `tail -f` on the transcript and the dashboard both show
  sub-stage activity within seconds, and `sdlc status` shows current sub-stage detail.
- Running token/cost per story/stage is visible during the run and reconciles to final totals.
- Two `sdlc build` runs in two different repos both appear in one auto-refreshing dashboard,
  each resolving its own ledger with no cross-run bleed.
- The streaming path leaves the result contract and schema validation byte-for-byte unchanged,
  and falls back cleanly to captured mode when streaming is unavailable.
