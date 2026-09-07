# Epic 32: Development Queue

> **Status: NOT STARTED (0/7)** — authored 2026-09-07 from `docs/development-queue-investigation.md`.
> Thesis: `sdlc build` and `sdlc fix` are imperative, foreground, one-run-per-invocation commands —
> whoever types them is the scheduler. Twelve stories shipped in the last 48 hours by a human typing
> `build`, waiting, labelling, typing `resume`, and overnight by a shell `for` loop that was a queue
> with none of a queue's guarantees. The controller already contains four partial schedulers — the
> Epic-24 ready set within a run, the batch-`fix` file-overlap graph within a batch, the host registry
> across repos, and Epic-30's designed-but-unbuilt FIFO trigger queue — and what is missing between them
> is a **durable, host-level list of work-to-do with an owner that drains it**. This epic adds exactly
> that and nothing else: per-repo ledgers stay the truth for runs, worktrees stay the unit of
> parallelism, the human gate stays human. It only stops the human from also being the resume button.
>
> **Decisions locked 2026-09-07** (FX): the queue store is a **host-level SQLite database**
> (`$XDG_STATE_HOME/sdlc/queue.db`, WAL) with claim-and-lease semantics — option B of the
> investigation; the queue **owns approval re-polling and auto-resume**; and the queue is built
> **first**, serial-per-repo, with the shared-checkout status markers moved off the checkout as a
> story *inside* this epic (32.1-003) rather than as an external blocker, so same-repo overlap unlocks
> as a sequenced step instead of gating the whole epic. The scheduler loop is designed as the same
> process Epic-30's `sdlc listen` supervisor becomes: webhooks enqueue, one drainer dispatches.

## Epic Overview

**Epic ID**: Epic-32
**Description**: A durable queue of development jobs and a scheduler that drains it across every
repository on the host, under the host's real limits. `sdlc build --enqueue` / `sdlc fix --enqueue`
write a job (repo, kind, scope, priority); `sdlc queue run` claims jobs with a short lease and starts the
existing `build`/`fix` machinery unchanged. The scheduler enforces the rules the controller already
knows but never coordinated: fix jobs are exclusive per repo (they run in the repo root,
`fix_issue.py:1767`); build jobs are one run per repo until 32.1-003 moves the status markers off the
shared checkout, then N stories in parallel inside one run as today; agent slots are capped **per host**
(this machine OOM-killed a live build with a 27B model loaded); the Max rate-limit window is **one shared
resource** — today each run discovers it alone and waits up to 5h in isolation, persisted only in its own
repo's ledger (`build.py:1189`); and `AWAITING_APPROVAL`, terminal within a run (`build.py:322`), becomes
a queue state the scheduler re-polls and resumes. Different repositories are independent by
construction — separate checkouts, ledgers, worktrees, PR streams — and the registry already lists ten of
them on this host; the queue lets them actually run side by side while sharing the window, the memory and
the human.
**Business Value**: Removes FX from the loop as scheduler and resume button, which is where the human
hours went this week, not into code. Turns the overnight `for` loop into a durable, prioritised,
restart-safe queue that pauses on quota once for everyone instead of once per repo, and that never
runs a job on a stale controller (per-job version check). Gives Epic-30's webhook intake a proper
consumer, and gives the pipeline's per-job budgets (REVIEW.md item 8) a home. Hyqs runs the multi-host
version of this model — Postgres job table, leased claims, 23.5% of jobs ending in human review — and
ships 2,673 changes in 68 days on it; this is that model at single-host scale on SQLite.

**Success Metrics**:
- Two jobs in two different repos run concurrently from one `sdlc queue run`, each producing a merged PR,
  with the registry and both ledgers consistent.
- A job that parks on `RATE_LIMITED` pauses **all** dispatch; the queue resumes at the reset with no human
  action and no second job discovering the same wall.
- A job parked `AWAITING_APPROVAL` resumes and merges within one poll interval of the `risk-approved`
  label appearing — zero `sdlc resume` invocations across a full night.
- Killing the scheduler mid-job loses nothing: the job's lease expires and it is reclaimed and resumed by
  the next `sdlc queue run`.
- After 32.1-003, two build jobs in the **same** repo overlap without tripping the #590 guard.

**Out of Scope**:
- Multi-host execution — option B leaves the door open (swap the file for Postgres); not built here.
- Webhook/label intake — Epic-30 owns triggers and *enqueues* into this; the listener is not built here.
- Review quality, retry loops, cost per story — the pipeline's; 32.3-001 only gives budgets a per-job home.
- Replacing per-repo ledgers, the registry's discovery role, or worktree isolation — all kept as is.
- Approving anything. The gate stays human; the queue only notices the label.

## Features in This Epic

### Feature 32.1: Queue Store & Scheduler

The durable list, the verbs to manage it, the loop that drains it, and the one structural change that
lets same-repo jobs overlap.

#### Stories

##### Story 32.1-001: Queue store and `sdlc queue` verbs
**User Story**: As FX, I want `sdlc build --enqueue <scope>` and `sdlc fix --enqueue <issue>` to record a
job in a host-level queue instead of running it, and `sdlc queue list|add|cancel|prioritise` to manage
that list, so that work can be declared now and executed later, in an order I control, by something
other than me.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `sdlc build --enqueue epic-33` in a repo **When** it returns **Then** a `queued` job exists in
  `$XDG_STATE_HOME/sdlc/queue.db` with `repo` (absolute, resolved as the registry resolves it), `kind=build`,
  `scope`, `priority`, `created_at`, and the run options frozen as JSON — and no run has started.
- **Given** `--enqueue` is omitted **When** `build`/`fix` run **Then** behaviour is byte-identical to today
  (foreground stays the default until 32.1-002 is trusted).
- **Given** `sdlc queue list` **Then** it shows every job on the host across repos with state, priority,
  repo, scope, age, and the linked `run_id` once one exists; `cancel <id>` marks a `queued` job `cancelled`
  and refuses on `running`; `prioritise <id> <class>` reorders.
- **Given** the store **Then** it is SQLite in WAL mode with one `jobs` table (`id, repo, kind, scope,
  priority, state, claimed_by, lease_until, run_id, options, created_at, updated_at, reason`), schema
  applied through the same migration discipline as the ledger, and `sdlc doctor` reports its presence,
  schema currency, and job counts by state.
- **Given** the per-repo ledger **Then** it remains the sole truth for a run; the queue stores only the
  `run_id` link and the job's own lifecycle — never stage or story state.

**Technical Notes**: New module `controller/src/sdlc/queue.py`; store beside the registry
(`default_registry_path()` sibling, honouring `SDLC_REGISTRY_PATH`'s precedent with `SDLC_QUEUE_PATH`).
Reuse the ledger's connection/migration helpers rather than a second pattern (this is the moment to
extract them — REVIEW.md item 5's first slice — if it is cheap; if not, mirror and note the debt).
Job `options` freeze the CLI flags exactly as `runs.harness_routing`/`model_routing` freeze routing,
so a job replays with the options it was enqueued with.

**Definition of Done**:
- [ ] `queue.db` store with the `jobs` schema, WAL, migration bookkeeping
- [ ] `--enqueue` on `build` and `fix`; foreground default unchanged (byte-identical test)
- [ ] `sdlc queue list|add|cancel|prioritise`; `doctor` reports queue health
- [ ] Tests: enqueue shape, list across repos, cancel semantics, options round-trip
- [ ] Documented in the controller reference

**Dependencies**: none
**Risk Level**: Low

##### Story 32.1-002: Scheduler loop with leased claims, per-repo exclusivity, and host agent slots
**User Story**: As FX, I want `sdlc queue run` to claim queued jobs and start them with the existing
`build`/`fix` machinery, holding a short renewable lease per job, never running two fix jobs — or, until
32.1-003, two runs of any kind — in one repo, and never exceeding a host-wide agent-slot cap, so that a
night's work drains itself in the right order and a killed scheduler loses nothing.
**Priority**: Must Have
**Story Points**: 8

**Acceptance Criteria**:
- **Given** queued jobs in three repos and a slot cap of 2 **When** `sdlc queue run` starts **Then** two
  jobs run concurrently in two repos, the third starts when a slot frees, and the registry shows each as a
  normal run (the dashboard needs no change to see them).
- **Given** two jobs for the same repo **When** one is `running` **Then** the other stays `queued` with
  `reason=repo busy` — no `--allow-dirty`, no stash, no bypass of the #590 guard; after 32.1-003 lands,
  two **build** jobs in one repo may overlap while **fix** jobs stay exclusive (repo root).
- **Given** a claimed job **Then** the scheduler renews `lease_until` on an interval; **Given** the
  scheduler process dies **When** the next `sdlc queue run` starts **Then** jobs whose lease expired are
  reclaimed and resumed through `sdlc resume` semantics (`resume.py` is the re-entry path — reuse it, do
  not reinvent), and a job whose pid is still alive is left alone (registry liveness, `registry.py:270`).
- **Given** a job whose repo's installed controller differs from its `controller/pyproject.toml`
  **When** claimed **Then** the 15.1-004 check runs per job and the job is parked `blocked` with the
  reinstall remedy rather than executed on stale code.
- **Given** the loop **Then** it runs foreground first (`sdlc queue run`, Ctrl-C safe: releases its leases);
  daemonisation is documented as the LaunchAgent pattern from Epic-30 30.3-001 and not built here.
- **Given** a job finishes **Then** its state mirrors the run's terminal status (`done`/`failed`/`parked`)
  and the existing Telegram notify path announces it.

**Technical Notes**: The scheduler is a thin loop over `queue.py` + `registry.py` + the existing
`run_build`/`fix` entry points invoked as subprocesses (so a job's crash cannot take the scheduler
down, and process-group kill applies). Claim = single `UPDATE … WHERE state='queued' AND (lease_until IS
NULL OR lease_until < now)` — SQLite's write lock makes it atomic. Lease numbers: 90s renewed every 30s is
Hyqs's tested figure; start there. Slot accounting counts live agent subprocesses across all jobs, not
runs. This is the process Epic-30's `sdlc listen` supervisor will become — design the loop so an intake
can enqueue while it drains.

**Definition of Done**:
- [ ] Claim/lease/renew/reclaim with a killed-scheduler test
- [ ] Per-repo exclusivity (fix always; build until 32.1-003) and host slot cap, both tested
- [ ] Reclaimed jobs resume via `resume.py`, never restart from scratch
- [ ] Per-job controller-version check parks stale jobs
- [ ] Foreground loop, Ctrl-C releases leases; daemon path documented, not built
- [ ] Notify on finish/park; dashboard shows queued runs unchanged

**Dependencies**: 32.1-001; 15.1-004 (shipped) for the version check
**Risk Level**: High

##### Story 32.1-003: Status markers off the shared checkout
**User Story**: As FX, I want the controller to stop writing `**Status**: Done` markers into
`docs/stories/*.md` in the shared checkout during a run — deriving the markdown view from the ledger on
demand instead — so that two build jobs in one repo can overlap without the #590 dirty-tree guard
refusing, and so that no run ever needs `--allow-dirty` or a stash of regenerable files.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a run completes a story **Then** nothing under the shared checkout changes; the story's
  status lives in the ledger only.
- **Given** `sdlc status --markdown` or a docs-update step **When** it needs the markers **Then** they are
  rendered from the ledger (`story_markdown.py` becomes a pure renderer) into the target file on request —
  the existing `sdlc reconcile` write path is the model.
- **Given** two build runs in one repo **When** both are live **Then** the dirty-tree guard is satisfied
  throughout and neither run's worktree sees the other's edits.
- **Given** the epic-status headers and `STORIES.md` counts **Then** they are updated by the same
  on-demand renderer, never mid-run.

**Technical Notes**: `story_markdown.py:47-90` is the writer; the ledger already carries every fact it
writes. Keep the output byte-identical so existing docs PRs (#632, #652) remain the precedent for how the
rendered result lands. This is REVIEW.md item 9, sequenced here because it is what turns "one run per
repo" into "N stories in N runs per repo".

**Definition of Done**:
- [ ] No writes to the shared checkout during a run (test with a fingerprint before/after)
- [ ] On-demand renderer produces byte-identical markers/headers
- [ ] Two overlapping build runs in one repo pass the guard (test)
- [ ] 32.1-002's per-repo rule relaxed for build jobs, with a test

**Dependencies**: 32.1-002 (the rule it relaxes)
**Risk Level**: Medium

### Feature 32.2: Shared Limits & the Human Gate

The two coordination points that today cost the most wall-clock and human attention.

#### Stories

##### Story 32.2-001: One rate-limit window for the whole queue
**User Story**: As FX on one Max subscription shared by every repo, I want a job that parks on
`RATE_LIMITED` to pause **all** dispatch until the reset, and the queue to resume everything itself at
that moment, so that the window is discovered once and waited out once, not once per repo.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** any running job's ledger records `RATE_LIMITED` with a reset time **When** the scheduler sees
  it **Then** it records a host-level `paused_until` in the queue and claims no further jobs until then.
- **Given** the reset passes **Then** the scheduler resumes the parked job (via `resume.py`) and continues
  claiming, with one notify for the pause and one for the resume — not one per job.
- **Given** a job without a reset time (usage-limit without retry-after) **Then** the queue pauses for the
  existing `rate_limit_max_wait` and probes, mirroring `_probe_parked_reset` (`build.py:1307`).
- **Given** the pause **Then** `sdlc queue list` and the dashboard show it as the queue's state, not as N
  independent parked runs.

**Technical Notes**: `rate_limit.py` is pure; the detection and the ledger write already exist per run
(`build.py:1189,1259`). The change is a host-level read of "is any live job rate-limited" plus a queue
column, not a new detector. Do not move rate-limit truth out of the ledger; the queue caches the reset.

**Definition of Done**:
- [ ] Host-level pause on any `RATE_LIMITED`; single resume at reset
- [ ] Probe path for reset-less limits reused, not duplicated
- [ ] Tests with a fake clock: pause, resume, no double-discovery
- [ ] Dashboard/CLI surface the queue-level pause

**Dependencies**: 32.1-002
**Risk Level**: Medium

##### Story 32.2-002: Approval-aware queue — re-poll and auto-resume
**User Story**: As FX, I want a job that parks `AWAITING_APPROVAL` to be re-polled by the queue, resumed
the moment its PR carries `risk-approved` (or an approving review), and its story driven to DONE with
its PR recorded — so that a night's high-risk fixes are waiting for my label in the morning, not for my
label *and* three `sdlc resume` commands.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a job whose run parks `AWAITING_APPROVAL` **Then** the queue records it `parked` with the PR
  number and polls the PR on a bounded interval (default 5 min) for the label or an approving review.
- **Given** the label appears **Then** the queue resumes the run (`resume.py`) so the controller performs
  the merge and records DONE in the ledger — never a hand-merge plus `reconcile`.
- **Given** the PR is closed without merging **Then** the job is marked `failed` with `reason=pr closed`
  and notify fires; **Given** it is merged by hand **Then** the queue reconciles rather than resumes.
- **Given** polling **Then** it is `gh`/`glab` read-only, respects API rate limits, and stops when the job
  reaches a terminal state; parked jobs never hold a slot.

**Technical Notes**: `AWAITING_APPROVAL` is terminal for a *run* (`build.py:322`) and must stay so — the
queue is the layer that outlives the run. The reconcile-on-hand-merge path exists (`sdlc reconcile`,
used four times this week); wire it, do not re-implement it. This is the story that removes the most
human steps per night; it is the one to demo.

**Definition of Done**:
- [ ] Parked jobs re-polled; label/review → resume → merge → DONE recorded by the controller
- [ ] Closed-PR and hand-merged-PR branches handled and tested with fixture PR states
- [ ] Parked jobs hold no slot; poll interval configurable and bounded
- [ ] Notify on park and on resume

**Dependencies**: 32.1-002
**Risk Level**: Medium

### Feature 32.3: Policy & Visibility

What gets built first, how much a job may cost, and where to look.

#### Stories

##### Story 32.3-001: Priority classes, per-job budgets, and cross-job overlap within a repo
**User Story**: As FX, I want jobs ordered by priority class (P0 and bugs before features, as batch
`fix all` already orders), each job to carry per-class budgets (fix rounds, wall-clock cap), and jobs in
the same repo that touch overlapping files to serialise automatically, so that the queue makes the same
judgement calls I make by hand, and a runaway job is capped by policy rather than by my noticing it.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** queued jobs **Then** claim order is priority class, then age; `prioritise` moves a job between
  classes; `fix` jobs default to a higher class than `build` jobs, and issues labelled `bug` above
  `enhancement`, mirroring `fix all`'s order.
- **Given** two jobs in one repo whose investigated `files_to_modify` overlap **Then** the second waits for
  the first, using `build_overlap_dependencies` (`fix_issue.py:2334`) extended from "one batch" to "one
  repo's queue"; non-overlapping jobs may run concurrently once 32.1-003 allows it.
- **Given** a job **Then** it carries a per-class budget — max fix rounds and a wall-clock cap per job —
  and exceeding either parks it `needs_attention` with notify, giving REVIEW.md item 8's cumulative-spend
  breaker a per-job home (Hyqs: 5 fix rounds, 30-min AI-stage cap, 2 stage timeouts).
- **Given** budgets **Then** they are config with sane defaults, recorded on the job, and visible in
  `queue list`.

**Technical Notes**: Ordering and overlap are pure functions over the queue rows plus each job's
investigation output; keep them in `queue.py` and test them without a scheduler. Budgets are enforced by
the scheduler reading the run's ledger (`stage_breakdown`), not by touching the pipeline's own
`MAX_BUGFIX_ATTEMPTS` — two layers, two knobs, documented.

**Definition of Done**:
- [ ] Priority classes with tested claim order; `prioritise` verb honoured
- [ ] Repo-scoped overlap serialisation reusing `build_overlap_dependencies`
- [ ] Per-job budgets recorded, enforced, parked with notify
- [ ] Documented beside the pipeline's own retry budgets, with the distinction stated

**Dependencies**: 32.1-002; 32.1-003 for concurrent non-overlapping jobs in one repo
**Risk Level**: Medium

##### Story 32.3-002: Queue panel on the dashboard
**User Story**: As FX glancing at the dashboard, I want a queue panel — jobs by state and repo, the
current pause reason if any, parked jobs with their PR links, slot usage, and each job's controller
version check — so that "what will run next and why isn't it running" is answerable in one look.
**Priority**: Could Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** the dashboard with a queue present **Then** a panel on the Builds view lists jobs grouped by
  state, with repo, scope, priority, age, and PR links for parked jobs; a queue-level pause
  (`RATE_LIMITED`) is shown as one banner with its reset time.
- **Given** no queue store **Then** the panel degrades to a muted "no queue" line (the GitHub-panel
  precedent), never an error.
- **Given** the panel **Then** it reads the same JSON `sdlc queue list --json` emits — one source, two
  consumers — and follows the DAG panel's lesson (#655): wide content scrolls inside the panel.

**Technical Notes**: Read-only over `queue.py`; reuse the 30s poll cadence of the GitHub badge. Keep it a
panel on the existing view, not a new view.

**Definition of Done**:
- [ ] Panel with states, pause banner, parked PR links, slot usage; graceful absence
- [ ] Backed by `queue list --json`
- [ ] Render test with fixture queue states; overflow contained

**Dependencies**: 32.1-001; richer with 32.2-001/32.2-002
**Risk Level**: Low

## Epic Sequencing

32.1-001 → 32.1-002 is the spine and must be first; the queue is useful — serial per repo — the moment
32.1-002 lands. 32.2-002 (approval auto-resume) is the highest-value story per point and should follow
immediately: it removes the step that cost the most human attention this week. 32.2-001 (shared
rate-limit) can run in parallel with it. 32.1-003 (markers off the checkout) comes next and unlocks
same-repo overlap for both 32.1-002's rule and 32.3-001's overlap logic. 32.3-001 then 32.3-002 close.
Recommended order: 32.1-001 → 32.1-002 → 32.2-002 → 32.2-001 → 32.1-003 → 32.3-001 → 32.3-002.
Epic-30's `sdlc listen` should be re-planned to enqueue into this rather than own a queue; that
reconciliation is a one-line note on 30.2-002, not a story here.
