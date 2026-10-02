# Epic 35: Fleet Execution — One Queue, Many Workers

> **Status: NOT STARTED (0/12)** — authored 2026-10-02. Thesis: a build pins
> the XPS for 30–90 minutes, dies when the lid closes, and runs while two Macs
> sit idle a few metres away on the same tailnet. Epic 32 built the durable
> development queue but deliberately stopped at one host ("multi-host execution
> — option B leaves the door open; not built here"): the queue is a per-host
> SQLite under `~/.local/state/sdlc/queue.db`, drained by `sdlc queue run` on
> the same machine, with the run registry and the dashboard per host too. This
> epic walks through that door — not by swapping the file for Postgres, but by
> putting the existing `QueueStore` behind a small HTTP service on `home-lab`,
> reachable only over Tailscale, and teaching `sdlc queue run` to be a
> *worker*: register what it can do, claim what it can run, sync the repo, run
> the unchanged build/fix machinery, and report back.
>
> **Decisions locked 2026-10-02** (FX): a shared central queue, because a
> Hetzner dev box may join later; the queue lives on `home-lab` as HTTP over
> the tailnet wrapping today's SQLite store (no Postgres); workers are chosen
> by capability match then least load, with `--host` as an explicit pin;
> workers auto-sync their repo clone before a run; **rate-limit windows are
> per subscription pool, not per host** — the M3 Max has its own Claude Max,
> the XPS/home-lab/Hetzner share the second Max, and one Codex subscription is
> shared by all; the XPS dashboard shows the whole fleet, opens remote logs,
> and controls the queue; Telegram names worker and pool. MVP is XPS → M3 Max
> end to end. Hetzner is designed for (tailnet-only auth, capability
> registration) but not provisioned or tested here. Local mode keeps working
> unchanged when no queue service is configured. The queue service never
> holds credentials — workers use their own Claude/Codex/gh/glab logins and
> jobs carry no tokens.

## Epic Overview

**Epic ID**: Epic-35
**Description**: Turn the single-host development queue into a fleet. Four
features: (35.1) a queue service on `home-lab` — `sdlc queue serve` exposes
the existing store over an authenticated tailnet-only HTTP API, a
`QueueClient` speaks it from any host, and `SDLC_QUEUE_URL` selects remote
versus local; (35.2) workers — `sdlc queue run` registers its name,
subscription pools, harnesses, cloned repos and free slots, claims only jobs
it can run, syncs the repo before dispatch, and pauses per pool on a rate
limit; (35.3) launching from the XPS — `sdlc build … --enqueue` lands on the
fleet queue with an optional `--host` pin and `--pool`; (35.4) fleet
visibility — workers push run records to the service, the XPS dashboard shows
every run with its worker, opens remote transcripts, and controls the queue.
**Business Value**: The laptop stops being the build machine. A story
dispatched from the XPS finishes on a plugged-in Mac whether the lid is open
or not, two idle machines carry the load in parallel, and the second Max
subscription stops being wasted. Visibility and control stay in one place, so
FX never ssh-hops to find out why a run stalled. The same shape absorbs a
Hetzner box later with zero redesign.
**Success Metrics**:
- From the XPS, `sdlc build <story> --enqueue` with the lid then closed
  produces a merged PR, built on the M3 Max, visible on the XPS dashboard with
  the worker named — zero ssh.
- Two jobs for two repos run concurrently on two different workers from one
  queue, each ending in a merged PR, with both ledgers and the fleet registry
  consistent.
- A `RATE_LIMITED` park on a shared-pool worker pauses claims for that pool
  only; the M3 Max pool keeps dispatching. Measured by the pool pause
  recorded once and the other pool's claims continuing in the same minute.
- Killing a worker mid-job loses nothing: the lease expires, another eligible
  worker (or the same one restarted) reclaims and resumes.
- With `SDLC_QUEUE_URL` unset, every existing test and every `sdlc build` /
  `sdlc queue` invocation behaves byte-for-byte as today.

## Epic Scope
**Total Stories**: 12 | **Total Points**: 41 | **MVP Stories**: 7 (28 pts)

## Features in This Epic

### Feature 35.1: Queue Service on home-lab

Today's `QueueStore` (`controller/src/sdlc/queue.py`) is already the right
data model — jobs with state, lease, claim, pause, budget. This feature puts
it on the network without changing it.

#### Stories

##### Story 35.1-001: `sdlc queue serve` — the store behind a tailnet-only HTTP API
**User Story**: As FX, I want one queue reachable from every machine on my
tailnet so that a job enqueued anywhere can be claimed anywhere, without a new
database.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `sdlc queue serve --bind <tailnet-ip>:8790` on home-lab **When**
  a tailnet peer calls `POST /jobs`, `GET /jobs`, `POST /jobs/claim`,
  `POST /jobs/{id}/renew`, `/release`, `/finish`, `/cancel`, `/requeue`,
  `/prioritise`, `POST /pause`, `DELETE /pause` **Then** each maps one-to-one
  onto the `QueueStore` method of the same name and returns the same
  `JobRecord` JSON `sdlc queue list --json` emits today.
- **Given** a request from a non-tailnet address, or one whose Tailscale
  identity (`tailscale whois <peer-ip>`) is not in the service's allowlist
  **When** received **Then** it is refused with 403 and logged; the service
  never binds `0.0.0.0`.
- **Given** two workers claim concurrently **When** both hit `/jobs/claim`
  **Then** exactly one gets each job (the store's lease transaction holds;
  the service is single-writer by construction).
- **Given** `sdlc queue serve --help` and `docs/controller-architecture.md`
  **When** read **Then** the API, bind rules and identity model are stated.

**Technical Notes**: Stdlib `http.server` + `ThreadingHTTPServer`, the pattern
`dashboard.py` already uses — no new dependency. Identity via `tailscale
whois --json` on the peer address (login name), cached per IP for a minute;
a `SDLC_QUEUE_TOKEN` shared secret as the fallback for a host without the
Tailscale CLI (Hetzner later). Schema additions on `jobs`: `host` (pin,
nullable), `pool` (nullable), `requirements` (JSON: repo, harness, sandbox),
`worker` (who holds the claim) — one migration, additive. Reuse
`_apply_migrations` idioms from the ledger.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: every route against a real store in tmp; 403 on unknown
      identity; concurrent claim yields one winner; migration idempotent
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: None
**Risk Level**: High

##### Story 35.1-002: `QueueClient` and the `SDLC_QUEUE_URL` switch
**User Story**: As FX on any machine, I want `sdlc queue …`, `--enqueue` and
`sdlc queue run` to talk to the fleet queue when one is configured and to
behave exactly as today when none is, so that remote execution is additive
and never a hard dependency.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `SDLC_QUEUE_URL=http://home-lab.<tailnet>:8790` **When** any
  queue consumer opens its store **Then** it gets a `QueueClient` with the
  `QueueStore` interface over HTTP; with the variable unset it gets the local
  SQLite store, and the existing test suite passes unchanged.
- **Given** the URL is set but the service is unreachable **When**
  `sdlc build … --enqueue` runs **Then** it fails fast with the URL and the
  error, never silently enqueues locally; a plain `sdlc build` (no
  `--enqueue`) is unaffected.
- **Given** `sdlc doctor` **When** run with the URL set **Then** a finding
  reports reachability, identity accepted, and the service's controller
  version.
- **Given** `~/.sdlc-fleet.yaml` (or `.sdlc-queue.yaml`) with `queue_url:`
  **When** present **Then** it is the file form of the variable, precedence
  env > file > none, mirroring `.sdlc-forge.yaml`.

**Technical Notes**: Extract a `QueueBackend` Protocol from `QueueStore`'s
public methods; `QueueClient` implements it with `urllib` (no requests
dependency), 10 s timeout, one retry on connection error. `default_queue_path`
callers move to a `open_queue()` factory. Keep `queue_view()` for the
dashboard on the same factory so the dashboard shows the fleet queue when
configured.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: factory selection; client against a live `serve` in-process;
      unreachable-URL fail-fast; doctor finding; file precedence
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.1-001
**Risk Level**: Medium

##### Story 35.1-003: Resident service on home-lab
**User Story**: As FX, I want the queue service to start at boot on home-lab
and be health-checked so that the fleet queue is as always-on as the GitLab
appliance next to it.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `templates/launchd/com.fxmartin.sdlc-queue.plist` and the
  documented `nix-install` snippet **When** installed **Then**
  `sdlc queue serve` runs under launchd with KeepAlive, logs under
  `~/.local/state/sdlc/`, and survives a reboot.
- **Given** `sdlc doctor` on home-lab **When** run **Then** it reports the
  service as running, its bind address, and the store path.
- **Given** the appliance's post-reboot recovery doc
  (`~/.claude/reference-docs/source-control.md`) **When** read **Then** the
  queue service is listed with its check.

**Technical Notes**: Mirrors Story 30.3-001's LaunchAgent shape (which this
story supersedes for the queue; 30.3-001 remains about `sdlc listen`). The nix
wiring itself lands in `nix-install`; this repo ships the plist template, the
doctor check and the doc.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: plist template validates (`plutil -lint` in CI on macOS);
      doctor finding
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.1-001
**Risk Level**: Low

### Feature 35.2: Workers

`sdlc queue run` becomes a worker: it says what it is, claims only what it can
run, prepares the repo, runs the unchanged pipeline, and pauses per pool.

#### Stories

##### Story 35.2-001: Worker registration and capability-matched claims
**User Story**: As FX, I want each worker to register what it can do and the
queue to hand a job only to a worker that can run it — least-loaded first,
`--host` pin respected — so that a job never lands on a machine without the
repo, the harness or the sandbox it needs.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `sdlc queue run --worker m3max --pool claude-m3 --pool codex-shared`
  **When** it starts **Then** it registers `{worker, host, pools, harnesses,
  sandbox, repos: [names under ~/Work], slots_free}` with the service and
  heartbeats every 30 s; a worker silent for 3 heartbeats is marked offline
  and its leases become reclaimable.
- **Given** a job with `requirements {repo: agentic-coding-monitor, harness:
  claude}` **When** two workers are eligible **Then** the claim goes to the
  one with more free slots; a job pinned `--host home-lab` is claimed only by
  that worker; an unsatisfiable job stays `queued` with `reason: no eligible
  worker (needs repo X, sandbox)` visible in `sdlc queue list`.
- **Given** a job whose harness map routes a stage to `codex` **When**
  matched **Then** the worker must declare the `codex-shared` pool.
- **Given** `sdlc queue workers` **When** run **Then** it lists workers,
  pools, slots, last heartbeat and online/offline.

**Technical Notes**: New `workers` table (service-side). Capability detection
reuses `doctor.check_dependencies`/harness probes and `~/Work` listing;
`slots_free` = configured cap minus running jobs (Story 32.2-003's per-host
cap becomes per-worker). Claim ordering: eligible → least loaded → priority →
age. Pools are free-form strings declared per worker; the recommended names
are documented (`claude-m3`, `claude-shared`, `codex-shared`).

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: registration + heartbeat expiry; eligibility by repo/harness/
      sandbox/pin; least-loaded tie-break; unsatisfiable job reason
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.1-001, 35.1-002
**Risk Level**: High

##### Story 35.2-002: Repo auto-sync before dispatch
**User Story**: As FX, I want a worker to bring its clone to the forge's
`main` before running a job — cloning it if absent — so that a build never
starts from a stale or missing tree on a machine I have not touched in days.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a claimed job for a repo cloned under `~/Work` **When** the
  worker prepares it **Then** it runs `git fetch origin && git checkout -q
  main && git merge --ff-only origin/main` and records the resulting sha in
  the job; a tracked dirty file refuses the job back to `queued` with the
  #590 reason (never a stash).
- **Given** the repo is absent **When** the job requires it **Then** the
  worker clones it from the forge declared in the job (`origin` URL recorded
  at enqueue) using its own `gh`/`glab` credentials, then proceeds.
- **Given** the clone has a different `origin` than the job records **When**
  prepared **Then** the job is refused with the mismatch named.

**Technical Notes**: Lives in the worker, before the existing `run_build` /
`run_fix` call; `dirty_tree_paths` and `_sync_branch_to_remote` (#794) are
the building blocks. Enqueue records `origin` from `git remote get-url
origin` so the job is self-describing.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests against real bare-origin fixtures: fast-forward, clone-if-absent,
      dirty refusal, origin mismatch
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.2-001
**Risk Level**: Medium

##### Story 35.2-003: Rate-limit pauses per subscription pool
**User Story**: As FX with two Claude Max subscriptions and one Codex, I want
a rate-limit park to pause only the pool that hit it so that the M3 Max keeps
building while the shared pool waits for its window.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a worker in pool `claude-shared` parks `RATE_LIMITED` **When**
  the pause is recorded **Then** `pause_dispatch` carries `pool=claude-shared`,
  claims for that pool return nothing until `paused_until`, and claims for
  `claude-m3` continue in the same minute.
- **Given** the early-reset probe (#727) succeeds on any worker of a paused
  pool **When** it reports **Then** the pool's pause clears for every worker.
- **Given** `sdlc queue unpause --pool claude-shared` **When** run from the
  XPS **Then** only that pool resumes; `sdlc queue list` and the dashboard
  banner show one line per paused pool.

**Technical Notes**: `pause` table gains `pool`; a job's pool is the pool of
the harness its routed stage uses (claude → the worker's declared Claude
pool, codex → `codex-shared`). Story 32.2-001's single-pause semantics become
the degenerate case of one pool when `SDLC_QUEUE_URL` is unset.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: pause isolates by pool; probe clears pool-wide; unpause by pool;
      local mode unchanged
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.2-001
**Risk Level**: Medium

##### Story 35.2-004: Resident worker on the M3 Max
**User Story**: As FX, I want the M3 Max to drain the fleet queue whenever it
is on, without me starting anything, so that a job enqueued from the XPS runs
even when I am not at the Mac.
**Priority**: Must Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** `templates/launchd/com.fxmartin.sdlc-worker.plist` installed
  **When** the Mac boots **Then** `sdlc queue run --worker m3max --pool
  claude-m3 --pool codex-shared` runs under launchd with KeepAlive, holds a
  `caffeinate -i` assertion while a job runs, and logs under
  `~/.local/state/sdlc/`.
- **Given** the controller is upgraded on the Mac **When** the worker claims
  its next job **Then** the existing per-job version guard (32.1-004) applies
  — a stale worker refuses rather than running on old code — and the
  LaunchAgent restarts it on exit.
- **Given** `sdlc doctor` on the Mac **When** run **Then** it reports the
  worker as registered and online.

**Technical Notes**: Same plist shape as 35.1-003. `caffeinate` is macOS-only
and guarded by `uname -s`; the Linux equivalent (`systemd-inhibit`) is a
follow-up for a Linux worker.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: plist validates; worker argv built from the template; doctor
      finding
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.2-001
**Risk Level**: Low

### Feature 35.3: Launch from the XPS

#### Stories

##### Story 35.3-001: `--enqueue` targets the fleet, with `--host` and `--pool`
**User Story**: As FX on the XPS, I want `sdlc build <scope> --enqueue` (and
`sdlc fix`) to land on the fleet queue, optionally pinned to a host or a pool,
so that launching remotely is the same command I already use.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `SDLC_QUEUE_URL` set **When** `sdlc build 12.4-005 --enqueue`
  runs in a repo **Then** the job is created on the service with `repo`
  (name + origin URL), the frozen CLI args, `requirements` derived from the
  harness map and `.sdlc-harness.yaml`, and the output names the queue URL
  and job id.
- **Given** `--host home-lab` or `--pool claude-shared` **When** enqueued
  **Then** the pin/pool is recorded and honoured by 35.2-001's matching;
  `--host` for an unknown worker is a parse error listing known workers.
- **Given** `sdlc queue list` on the XPS **When** run **Then** it shows the
  fleet queue with `worker`, `host`, `pool` columns added to today's output.

**Technical Notes**: `_enqueue_job` (cli.py:158) already freezes argv; add
origin + requirements. `--host`/`--pool` are enqueue-only flags (rejected
without `--enqueue`).

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: job payload shape; pin/pool recorded; flag validation; list columns
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.1-002
**Risk Level**: Low

### Feature 35.4: Fleet Visibility and Control

#### Stories

##### Story 35.4-001: Fleet registry — every run on the XPS dashboard
**User Story**: As FX looking at the XPS dashboard, I want to see every run on
every worker with the worker named so that one page tells me what the fleet
is doing.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a worker starts, updates or finishes a run **When** it writes its
  local registry record (`_registry_register` / `_registry_finish`) **Then**
  it also `PUT`s the record to the service's `/runs` with `worker`; the
  local file stays authoritative for the worker, the service holds the fleet
  view.
- **Given** the XPS dashboard with `SDLC_QUEUE_URL` set **When** `/api/runs`
  is served **Then** it merges the local registry with the fleet registry,
  dedupes by run id, and each row carries `worker`; the sidebar shows the
  worker beside the repo; `derive_state` uses the worker's heartbeat, not a
  local pid, for remote runs.
- **Given** the service is unreachable **When** the dashboard ticks **Then**
  local runs still render and a muted "fleet unavailable" line appears (the
  GitHub-panel precedent).

**Technical Notes**: `RunRecord` gains `worker: str | None`. The fleet view is
a second table on the service's SQLite. Status counts for a remote run come
from the pushed record (done/total), refreshed on each push, since the
worker's ledger is not reachable from the XPS.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: push on register/finish; merge + dedupe; remote state from
      heartbeat; degrade when unreachable; sidebar string contract
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.1-002, 35.2-001
**Risk Level**: Medium

##### Story 35.4-002: Remote transcripts and console log from the XPS
**User Story**: As FX, I want to open a remote run's stage transcripts from the
XPS dashboard so that diagnosing a stalled Mac build needs no ssh.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a run that lives on worker `m3max` **When** I click "view
  session" on the XPS **Then** the modal loads `/api/logs` from that worker's
  dashboard over the tailnet (`http://m3max.<tailnet>:8787`), path-confined
  exactly as the local `/log` is (Story 11.2-010), and shows which worker
  served it.
- **Given** the worker's dashboard is down **When** clicked **Then** the modal
  says so and offers the worker's log root path.

**Technical Notes**: Each worker runs `sdlc dashboard --host <tailnet-ip>`
(today it binds 127.0.0.1 only); the fleet registry record carries the
worker's dashboard URL. No proxying through the queue service — a direct
tailnet fetch keeps the service stateless about logs.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: dashboard URL in the record; modal fetches the remote origin
      (string contract); confinement unchanged
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.4-001
**Risk Level**: Medium

##### Story 35.4-003: Queue control from the XPS
**User Story**: As FX, I want cancel / requeue / prioritise / unpause-pool to
work from the XPS against the fleet queue so that I can steer the fleet
without touching a worker.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `SDLC_QUEUE_URL` set **When** `sdlc queue cancel|requeue|
  prioritise|unpause [--pool]` runs on the XPS **Then** the verb applies on
  the service and the next `list` reflects it; cancelling a `running` job
  signals its worker, which terminates the run's process group and releases
  the lease.
- **Given** the dashboard queue panel **When** rendered **Then** each row
  shows its worker and pool; paused pools render one banner each.

**Technical Notes**: The verbs already exist on the local store (Epic 32);
this is the client wiring plus a `cancel` signal path (`/jobs/{id}/cancel`
sets `cancel_requested`, the worker checks it on heartbeat and uses the
Story 13.4-001 process-group kill).

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: each verb through the client; cancel of a running job reaches
      the worker's kill path; dashboard string contract
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.1-002, 35.2-001
**Risk Level**: Low

##### Story 35.4-004: Telegram names the worker and the pool
**User Story**: As FX reading a Telegram ping away from my desk, I want the
message to name which worker ran it and which pool paused so that a
rate-limit notice is attributable to a subscription.
**Priority**: Should Have
**Story Points**: 1

**Acceptance Criteria**:
- **Given** a run started/finished on a worker **When** `notify` fires
  **Then** the message carries `worker=<name>`; a rate-limit pause carries
  `pool=<name>` and `resumes <time>`.

**Technical Notes**: `notify.py` gets two optional fields; absent in local mode.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: message templates with and without worker/pool
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.2-003, 35.4-001
**Risk Level**: Low

## Epic Sequencing

1. **MVP (XPS → M3 Max end to end)**: 35.1-001 → 35.1-002 → 35.2-001 →
   35.2-002 → 35.3-001 → 35.2-004 → 35.4-001. Exit: the first success metric.
2. **Pools and the second worker**: 35.2-003, then home-lab joins as a worker
   in pool `claude-shared` (no new story — it is 35.2-004's plist on a second
   Mac).
3. **Operability**: 35.1-003, 35.4-002, 35.4-003, 35.4-004.

## Non-Goals

- Provisioning or testing the Hetzner box — the design admits it (shared
  token fallback, capability registration); a later story adds it.
- Postgres or any new storage technology; the service wraps the SQLite store.
- Webhook/label intake — Epic 30 enqueues *into* this queue; the listener is
  not built here.
- Credential distribution — workers use their own logins; the service and the
  jobs never carry tokens.
- Cross-worker worktrees or shared checkouts — each worker owns its clones.
- Approving anything; the human gate stays human.
