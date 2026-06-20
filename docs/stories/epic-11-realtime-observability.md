# Epic 11: Realtime Progress & Multi-Run Observability

> **Status: PLANNED** — created 2026-06-20. Builds on Epic-07 (external controller),
> Epic-04 (SQLite ledger), and Epic-10 (the `dashboard`/`status` verbs). Makes the
> controller's progress observable *as it happens*, and lets one dashboard watch
> several builds running in different repos at once.

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

**Total Stories**: 8 | **Total Points**: 30 | **MVP Stories**: 0 (roadmap — Should Have)

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

## Story Dependencies (within Epic-11)

```
11.1-001 (stream) ──┬─> 11.1-002 (sub-stage events) ──┐
                    └─> 11.1-003 (token/cost)          ├─> 11.2-004 (live detail view)
11.2-001 (registry) ──> 11.2-002 (multi-run overview)  │
11.2-003 (SSE transport) ──────────────────────────────┘
```

- **Cohort 1** (no deps): 11.1-001, 11.2-001, 11.2-003, 11.2-005
- **Cohort 2**: 11.1-002, 11.1-003 (need 11.1-001); 11.2-002 (needs 11.2-001)
- **Cohort 3**: 11.2-004 (needs 11.1-002 + 11.2-003)

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
