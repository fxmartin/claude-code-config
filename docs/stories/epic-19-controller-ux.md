# Epic 19: Controller UX & Ergonomics

> **Status: IN PROGRESS (3/4)** — 19.2-003 added 2026-09-07 (sidebar status filter,
> not yet built). The original three merged on `main` (2026-06-25, parallel
> run 1ac839c4): multi-epic build scope 19.1-001 (#193), dashboard sidebar
> active-vs-finished 19.2-001 (#192), live per-story parallel progress 19.2-002
> (#194). Created 2026-06-25. Quality-of-life improvements to the
> `sdlc` controller CLI + dashboard surface (Epic-07/Epic-11) discovered through
> daily use. Seeded by the multi-epic build-scope request and two dashboard
> observability gaps spotted in a live parallel run.

## Epic Overview

**Epic ID**: Epic-19
**Description**: The controller's verbs and dashboard work, but several rough
edges surface in real use. (1) `sdlc build`/`resume` accept a single scope token
(`all`, `epic-NN`, name, `X.Y-NNN`) — you cannot target an explicit subset of
epics in one run. (2) The dashboard's left RUNS sidebar makes active (building)
runs hard to distinguish from finished ones at a glance. (3) In a parallel run,
the top progress bar + done/started/todo counter don't credit a story until its
whole cohort/wave finishes, so a story that's already merged still reads "0 done".
This epic collects small, additive CLI + dashboard ergonomics fixes; it
deliberately excludes new orchestration behavior (that belongs in Epic-12/17).

**Business Value**: Lower friction for the maintainer and the five colleagues —
fewer `all` over-builds, fewer sequential one-epic runs, and faster at-a-glance
read of what's actually running. Small, low-risk changes with immediate daily
payoff.

**Success Metrics**:
- `sdlc build epic-A epic-B` builds the union of those epics' incomplete stories
  in one cohort-scheduled run, with single-scope and `all` behavior unchanged.
- The dashboard sidebar makes in-progress runs recognizable at a glance, distinct
  from finished runs and from the currently-selected run.
- In a parallel run, the top progress bar + counter credit each story the moment
  it finishes, not at the cohort barrier.

## Stories

##### Story 19.1-001: `sdlc build`/`resume` accept multiple explicit epic scopes

**User Story**: As FX, I want `sdlc build epic-15 epic-18` (and the matching
`resume`) to accept several explicit epic/story scopes in one run, so I can
develop a chosen subset of epics together without falling back to `all` or
running them one at a time.

**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `sdlc build epic-15 epic-18` **When** it runs **Then** the queue is
  the union of both epics' incomplete stories, deduped by story id, and
  cohort-scheduled across epics (cross-epic dependencies honored) — as `all` does.
- **Given** space- or comma-separated scopes (`epic-15,epic-18`) **When** parsed
  **Then** both forms work; a single scope and `all` behave exactly as today.
- **Given** `all` mixed with explicit epics **When** parsed **Then** it resolves
  to all epics (documented in `--help`).
- **Given** a composite run **When** `sdlc status` / registry / dashboard show it
  **Then** the scope renders as a canonical label (e.g. `epic-15,epic-18`).
- **Given** an interrupted composite run **When** `sdlc resume epic-18 epic-15`
  (any order) or `sdlc resume --run <id>` **Then** it resumes the same run.

**Technical Notes**: The single-scope limit lives in two functions —
`discover_queue` (`controller/src/sdlc/discovery.py:183`: accept multiple tokens,
run the existing per-token resolution, union + dedup by `Story.id` preserving
order) and `parse_build_args` (`controller/src/sdlc/build.py` ~950-953: collect
all positionals instead of raising on the 2nd; store a canonical
lowercased/deduped/sorted/comma-joined label). Scope is free-form TEXT in the
ledger (`build.py:113`) and registry (`registry.py:55`) — no schema change. The
`resume` arg (`cli.py:260-264`) + `latest_resumable_run` exact-match
(`build.py:1401-1405`) need the same canonicalizer so order-independent resume
matches. **No changes** to `cohort.py` (`compute_cohorts`) or the executor — they
already handle multi-epic queues. Update the scope docs in `--help`
(`cli.py:74-77`).

**Definition of Done**:
- [ ] `build`/`resume` accept N space/comma-separated epic/story scopes; single
      scope and `all` unchanged (backward compatible)
- [ ] `discover_queue` unions + dedups by story id; canonical label persisted and
      shown in `status`/registry/dashboard
- [ ] `resume` round-trips a composite run by scope (order-independent) and `--run`
- [ ] Tests: `test_discovery.py` (union / dedup / `all`-mix), `test_cli_build.py`
      (multi-positional build + a `parse_build_args` unit test), `test_resume.py`
      (composite resume)
- [ ] `--help` documents the multi-scope form

**Dependencies**: Builds on Epic-07 (controller) and Epic-17 (parallel executor),
both COMPLETE. None blocking.
**Risk Level**: Low — additive; single-token and `all` paths unchanged; no schema
or executor changes; touches `controller/src/sdlc/*.py` only (no `.sh`/`.github`),
so it will not trip the high-risk approval gate.

##### Story 19.2-001: Dashboard sidebar — distinguish active runs from finished

**User Story**: As someone watching the dashboard, I want active (building) runs
to stand out from finished (DONE/FAILED) ones in the left RUNS sidebar, so I can
tell at a glance which runs are still in progress — especially with several runs
(across repos) listed.

**Priority**: Should Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** the RUNS sidebar with a mix of states **When** rendered **Then** runs
  whose status is IN_PROGRESS/STARTED are visually distinct from terminal runs
  (DONE/FAILED/etc.) — recognizable without reading the badge text (e.g. a live
  "pulsing" dot and/or accent border).
- **Given** a run transitions IN_PROGRESS→terminal **When** the sidebar refreshes
  on the live tick **Then** its active styling clears.
- **Given** multiple concurrent active runs (e.g. cross-repo) **When** listed
  **Then** every active run carries the active styling, not just the selected one.
- **Given** the existing "● Live (latest)" entry and the selected-run highlight
  (the `.active` CSS class) **When** styled **Then** the new active-run styling is
  visually separable from the selection highlight — they mean different things
  (building vs currently-viewed).
- (Optional) Active runs sort/group above terminal runs in the list.

**Technical Notes**: Sidebar render is `renderRuns()`
(`controller/src/sdlc/dashboard.py:621-642`): each run is
`<div class='run [active]' data-run=…>` with `badge(r.status)` (`:497`) using the
status CSS classes `.DONE` / `.IN_PROGRESS` / `.STARTED` / `.FAILED`
(`:339-344`). Add a status-derived modifier class to the run card (e.g.
`run--live` when `r.status` ∈ {IN_PROGRESS, STARTED}) inside `renderRuns`, plus
CSS near `:339` for a pulsing live dot and/or a blue left-accent border.
**Important:** the existing `.active` class means *selected-for-viewing*, NOT
building — keep the two visually distinct. Reuse `LABELS`/`ORDER` (`:481, :495`)
for any active-first sort. The SSE live tick (`:803`) already re-renders, so the
styling tracks state automatically.

**Definition of Done**:
- [ ] Active (IN_PROGRESS/STARTED) sidebar runs are visually distinct from
      terminal runs at a glance, and distinct from the selection highlight
- [ ] Styling clears on transition to terminal; all concurrent active runs marked
- [ ] No regression to the "● Live (latest)" entry or run selection
- [ ] Covered by a dashboard render assertion (the `run--live` class is emitted
      for an IN_PROGRESS run) or a documented manual check

**Dependencies**: Epic-11 dashboard (COMPLETE). None blocking.
**Risk Level**: Low — render-only CSS/JS change in `dashboard.py`; no
controller/ledger logic; no `.sh`/`.github` (no high-risk gate).

##### Story 19.2-002: Live progress credits each parallel story as it finishes

**User Story**: As someone watching a parallel run, I want the top progress bar
and the done/started/todo counter to update the moment each parallel story
finishes — not only when the whole wave/cohort completes — so the dashboard
reflects real progress.

**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a parallel run where one story reaches merge=DONE before its siblings
  **When** the dashboard ticks **Then** that story's status shows DONE and the
  "done" counter + top progress bar increment immediately (not at the cohort
  barrier).
- **Given** the run snapshot **When** `sdlc status` / `--json` is read mid-cohort
  **Then** `counts.done` reflects every already-terminal story, and each finished
  story's row is DONE.
- **Given** a story that ends FAILED/BLOCKED/AWAITING_APPROVAL mid-cohort **When**
  it finishes **Then** its terminal status is reflected live too.
- **Given** the wave eventually closes **When** the barrier finalizes **Then**
  final counts equal the per-story sum (idempotent — no double counting, no
  regression to serial mode).

**Technical Notes**: Evidence (live run c82ed1f3): 18.2-001 and 18.3-001 had all
four stages DONE with PRs merged, yet story-status read STARTED and the counter
said "0 done, 3 started". Root cause: the parallel executor applies per-story
terminal status (`set_story_status`, `controller/src/sdlc/build.py:1123`, called
~`:3197`) and the run-count update (`run_update_counts`) at the **cohort
barrier**, not as each worker's story reaches a terminal outcome.
`status_snapshot` counts (~`build.py:1737`) derive "done" from persisted story
rows, and the dashboard's top bar reads `done/total` from the snapshot and
re-renders on the SSE tick — so once the controller writes each story's status
promptly, the bar/counter update automatically. **Fix (controller-side):** in the
parallel cohort worker-completion path, call `set_story_status(outcome)` + bump
`run_update_counts` + emit a progress event the instant a story finishes, keeping
the barrier finalize idempotent (no double count). No dashboard change needed
beyond confirming the bar width derives from live `done/total`.

**Definition of Done**:
- [ ] Each story's terminal status + the run done/total counts are persisted on
      that story's completion, not at the cohort barrier
- [ ] Dashboard top progress bar + done/started/todo counter update live as
      parallel stories finish (verified on a multi-story parallel run)
- [ ] Barrier finalize stays idempotent (final counts = per-story sum); serial
      mode unchanged
- [ ] Test: a parallel-run test asserting `status_snapshot.counts.done` increments
      after the first story finishes while siblings are still IN_PROGRESS

**Dependencies**: Epic-17 parallel executor + Epic-11 dashboard/`status_snapshot`
(both COMPLETE). None blocking.
**Risk Level**: Medium — touches the parallel executor's status/count writes
(`controller/src/sdlc/build.py`); must stay idempotent with the existing barrier
finalize and not regress serial mode. No `.sh`/`.github` (no high-risk gate).

##### Story 19.2-003: Dashboard sidebar — filter runs by status

**User Story**: As someone watching the dashboard with runs from several repos
listed, I want to filter the left RUNS sidebar by status — any combination of
STARTED, DONE, FAILED, ABORTED, RATE_LIMITED, NEEDS_ATTENTION, AWAITING_APPROVAL,
DEAD — with a count per status, so that "what needs me right now" is one gesture
instead of a scroll through a week of finished runs.

**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the RUNS sidebar **When** rendered **Then** a row of status chips sits
  above the list, one per status *present in the registry* (absent statuses are
  not shown), each carrying its live count, e.g. `STARTED 2 · DONE 14 · FAILED 3`;
  chips use the existing badge colours so the vocabulary matches the cards.
- **Given** chips **When** one is tapped **Then** it toggles; any combination may
  be selected; the list shows only runs whose status is selected; an `all` chip
  (or tapping the last selected chip off) clears the filter. Filtering never
  changes the sort order 19.2-001 established.
- **Given** a filter that excludes `STARTED` **When** a run is live **Then** live
  runs are **always listed regardless of the filter** — a persisted "DONE only"
  can never hide something in flight; the chip row makes this visible (e.g. the
  STARTED chip renders as pinned/always-on).
- **Given** the selected run (`.active`) is filtered out **When** the filter
  applies **Then** the detail pane keeps showing it and the sidebar shows a
  one-line "selected run hidden by filter" hint with a clear action — the
  selection is never silently orphaned.
- **Given** the live tick (SSE) re-renders the sidebar **When** counts or statuses
  change **Then** the chips update in place and the filter persists across the
  re-render; the stable-height rule from 11.2-011 holds (no screen jump).
- **Given** the filter **When** the page is reloaded **Then** it is restored from
  `localStorage` under a namespaced key, with every storage access guarded —
  browsers can throw on `localStorage` — and the page renders unfiltered with no
  stored value.
- **Given** the status vocabulary **Then** the chips are driven by the statuses
  the server actually reports (`derive_state`, incl. `DEAD`) and the
  `IN_PROGRESS → STARTED` display mapping from 11.2-009, not a hardcoded list —
  a new status shows up as a chip with no dashboard change.

**Technical Notes**: Sidebar render is `renderRuns()`
(`controller/src/sdlc/dashboard.py:715-740`); statuses arrive as
`derive_state(rec)` (`:150`). Reuse `ORDER` (`:562`) for chip order and
`statusLabel`/`LABELS` (`:576-577`) plus `badge()` (`:586`) for chip text and
colour, so chips and cards can never disagree. The filter is a `Set` of statuses
held beside `sel` (`:563`) and applied inside `renderRuns` before the HTML is
built; the click handler at `:933` gets a sibling for the chip row. Persistence
follows the Runs-toggle pattern exactly (`SIDE_KEY`, `:1127-1133`: namespaced
key, `try/catch` on every access, restore on load). Keep the always-visible rule
for live runs in the filter function, not in CSS, so it is testable. No server
change: everything needed is already in the `/api/status` payload.

**Definition of Done**:
- [ ] Chip row with per-status counts, derived from reported statuses; multi-select
      toggle; `all` reset; sort order unchanged
- [ ] Live runs always listed; hidden-selection hint; stable height on re-render
- [ ] Persisted per browser under a namespaced key with guarded storage access
- [ ] Render tests: chips emitted with counts, filter markup present, storage key
      and guard present (the 19.2-001 / sidebar-toggle assertion style); a
      Playwright pass mirroring the sidebar-toggle verification (toggle a chip,
      reload, filter restored, live run still visible)
- [ ] No regression to the "● Live (latest)" entry, selection, or 19.2-001 styling

**Dependencies**: 19.2-001 (COMPLETE); Epic-11 11.2-002/11.2-009 (COMPLETE)
**Risk Level**: Low — render-only JS/CSS in `dashboard.py`; no controller or
ledger logic; no `.sh`/`.github` (no high-risk gate)

## Epic Complete When
- `sdlc build`/`resume` accept multiple explicit epic/story scopes (union + dedup,
  cohort-scheduled), with single-scope and `all` behavior unchanged and covered by
  tests.
- The dashboard sidebar visually distinguishes in-progress runs from finished ones
  (and from the selected run), tracking live state.
- In a parallel run, the top progress bar + done/started/todo counter credit each
  story the moment it finishes, not at the cohort barrier.
