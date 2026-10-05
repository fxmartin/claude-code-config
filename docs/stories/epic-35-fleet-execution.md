# Epic 35: Fleet Execution — One Queue, Many Workers

> **Status: IN PROGRESS (13/19)** — authored 2026-10-02; 35.2-005 added 2026-10-03; 35.2-006, 35.2-007, 35.4-005 and 35.4-006 added 2026-10-04 from the first fleet jobs; 35.2-008 (Hetzner Linux worker) added 2026-10-04 after the M3 Max was gated by Little Snitch; 35.5-001 (push committed recovery work before failing) added 2026-10-04 from job 4. Thesis: a build pins
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
**Total Stories**: 19 | **Total Points**: 72 | **MVP Stories**: 12 (49 pts)

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

##### Story 35.2-005: Worker drains the fleet queue over HTTP
**User Story**: As FX, I want `sdlc queue run --worker` on the M3 Max to claim
jobs from the fleet queue on home-lab so that a job enqueued from the XPS
actually runs on the Mac — the epic's first success metric, which 35.1–35.4 as
shipped do not meet.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `SDLC_QUEUE_URL` (or `~/.sdlc-fleet.yaml`) configured on a worker
  **When** `sdlc queue run --worker m3max --pool … --follow` starts **Then**
  it registers, heartbeats, claims, renews, releases, finishes, parks,
  reclaims, records sync/files/fix-rounds and pushes fleet runs **through the
  `QueueClient`** against the service — it no longer refuses a fleet URL.
- **Given** the scheduler's store surface (`peek_claimable`, `claim_job`,
  `claimable_for_worker`, `reclaim_job`, `expired_running_jobs`,
  `running_repos`, `overlap_holds`, `park_job`, `take_parked_job`,
  `due_parked_jobs`, `schedule_poll`, `set_reason`, `attach_run`,
  `record_sync`, `record_files`, `record_fix_rounds_baseline`,
  `restart_fresh`, `requeue_job`, `dispatch_pause(s)`, `mark_pause_probed`,
  `get_worker`) **When** a fleet URL is configured **Then** every one of them
  has a route on `sdlc queue serve` and a `QueueClient` method, and the
  scheduler is typed against `QueueBackend`, not `QueueStore`.
- **Given** a worker whose network drops mid-job **When** its lease lapses
  **Then** the service surfaces the job as expired and another eligible
  worker reclaims it only once the run's pid is confirmed gone on the
  original worker's next heartbeat (the 35.2-001 rule, now over HTTP).
- **Given** no fleet URL **When** `sdlc queue run` starts **Then** behaviour is
  byte-identical to today (local store).
- **Given** the MVP acceptance test **When** `sdlc build <story> --enqueue`
  runs on the XPS with the M3 Max worker online **Then** the job is claimed by
  `m3max`, the PR merges, and the run shows on the XPS dashboard with
  `worker=m3max`.

**Technical Notes**: The refusal lives in `scheduler.py` (~`:985`) and the
`queue run` CLI; `QueueBackend` (queue.py) currently declares only six
methods — widen it to the scheduler's surface. Routes follow the 35.1-001
shape (one per store method, `JobRecord.to_dict()` JSON, 409 on a lost
claim). Worker-side writes that today go "straight into the store"
(`put_fleet_run`, registration) already have routes. Keep `QueueStore` as the
local implementation; `QueueClient` becomes the second full implementation.
Update the worker plist header and `docs/controller-architecture.md`
("stays local-only" paragraphs) accordingly.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: each new route against a live in-process `serve`; scheduler
      against a `QueueClient` end to end (claim → run → finish) with a fake
      job process; lease lapse + reclaim over HTTP; local mode unchanged
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.1-001, 35.1-002, 35.2-001, 35.4-001
**Risk Level**: High

##### Story 35.2-006: Worker git access is non-interactive, and a stall is a refusal
**User Story**: As FX watching the first fleet job on the M3 Max, I want the
worker's repo sync to authenticate to the forge without any UI and to give the
job back with a reason when it cannot, so that a job never sits `running` for
minutes behind a Keychain dialog nobody can see.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a worker on macOS whose clone uses an `http(s)://` origin **When**
  the sync fetches **Then** git runs with the forge CLI's credential helper
  (`glab auth git-credential` / `gh auth git-credential`, chosen by the
  origin's host) and `GIT_TERMINAL_PROMPT=0`, never the user's interactive
  `osxkeychain` helper — observed 2026-10-04: job 1 (`12.4-008`, `m3max`) hung
  for minutes in `git credential-osxkeychain get` waiting for a Keychain
  prompt on the Mac's screen; the lease kept renewing, so the queue showed a
  healthy `running` job.
- **Given** the forge CLI is not authenticated for that host on the worker
  **When** the sync runs **Then** the fetch fails within seconds and the job
  is refused back to `queued` with `reason: worker m3max cannot authenticate
  to gitlab.test (glab auth login …)`, visible in `sdlc queue list` and on the
  dashboard queue panel, and the worker heartbeat flags the host as lacking
  that forge credential so the matcher stops offering it such jobs.
- **Given** a sync whose git process produces no output for 60 s **When**
  the worker notices **Then** it kills the process group, refuses the job with
  `reason: repo sync stalled`, and never renews a lease on a stalled sync.
- **Given** `sdlc doctor` on a worker **When** run **Then** a finding reports,
  per forge host it may be offered jobs for, whether the CLI credential is
  present and usable non-interactively.

**Technical Notes**: `queue_worker.py` already sets `GIT_TERMINAL_PROMPT=0`;
add `-c credential.helper=` to clear inherited helpers and `-c
credential.helper=!glab auth git-credential` (or `gh`) per host, with
`GLAB_CONFIG_DIR`/`GITLAB_HOST` set the way `issue_host.py` does for a
plaintext instance. The stall watchdog mirrors `dispatch._dispatch_streaming`'s
heartbeat dead-man (Story 13.4-001) at a smaller scale. The capability flag
(`forges: {gitlab.test: true}`) rides the existing registration payload.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: helper selection by origin host; unauthenticated → refusal with
      reason within the timeout; stalled git killed and refused; doctor finding
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.2-002, 35.2-005
**Risk Level**: Medium

##### Story 35.2-007: A worker proves it can run an agent before it registers
**User Story**: As FX, I want a worker to run one real agent turn from its own
launchd context at startup and to refuse to register — naming the exact
cause — when that turn does not complete, so that a Mac whose agents would
stall is never offered a job, instead of failing two stories 300 s at a time.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `sdlc queue run --worker …` starting under launchd **When** it
  initialises **Then** before its first registration it runs the harness's
  own probe (`claude -p ok --model <haiku id> --output-format json`, the
  Story 34.1-002 entitlement probe) **from the worker's process context**,
  with a 90 s cap; a completed turn registers the worker as today.
- **Given** the probe produces no output within the cap **When** the cap
  lapses **Then** the worker kills the probe's process group, does not
  register, logs `worker self-check failed: agent produced no output in 90s
  — a dialog is probably waiting on this Mac (Keychain / Privacy & Security)
  or ~/.claude resolves into a protected folder`, repeats the check every
  60 s, and registers the moment a probe completes (the dialog was answered).
  KeepAlive never restart-loops it: the worker stays up and silent.
- **Given** `~/.claude`, or any of `settings.json`, `CLAUDE.md`, `hooks`,
  `commands`, `agents`, `skills` under it, resolves into a macOS
  TCC-protected folder (`~/Documents`, `~/Desktop`, `~/Downloads`, any
  cloud-drive root) **When** the self-check runs **Then** it fails fast with
  `~/.claude/settings.json → ~/Documents/…: launchd agents cannot read TCC
  protected folders; move the checkout (nix-install: ~/.config/nix-install)`
  — the 2026-10-04 M3 Max cause, found only after two failed fleet jobs.
- **Given** `sdlc doctor` on a worker host **When** run **Then** a
  `worker-self-check` finding reports the last probe result, its timestamp
  and the TCC path verdict; CLEAN only when the probe completed from the
  LaunchAgent, not from the shell doctor runs in.
- **Given** the worker's heartbeat **When** registered **Then** it carries
  `self_check: {ok, at, reason}` so `sdlc queue workers` and the fleet
  dashboard show a worker that is online but unable to run agents as such,
  and the matcher treats it as having zero free slots.

**Technical Notes**: The probe reuses `_probe_model` / the 34.1-002 seam
rather than a new subprocess path; the stall cap mirrors
`dispatch._dispatch_streaming`'s dead-man. The TCC check is a path test on
`os.path.realpath` of each `~/.claude` entry against the protected roots —
cheap, deterministic, macOS-only (`platform.system() == "Darwin"`). Over ssh
these checks pass (sshd carries disk access and sees no dialogs), which is
exactly why the worker must run them itself: the plist header and
`docs/controller-architecture.md` should say so. The 2026-10-04 incident:
TCC denial (`Operation not permitted` from a launchd `/bin/sh`), then two
GUI-session dialogs the user had to accept, each costing a 300 s stall per
agent before any signal reached the queue.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: probe completes → registers; probe stalls → no registration,
      retry, registers on later success; TCC path verdict for each root;
      heartbeat carries `self_check`; doctor finding; non-macOS skips the TCC
      test
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.2-001, 35.2-005
**Risk Level**: Medium

##### Story 35.2-008: Resident Linux worker on the Hetzner dev box
**User Story**: As FX, I want the Hetzner `dev-server` to drain the
`claude-shared` pool as a third worker, so that a shared-pool job runs even
when both Macs are busy or gated by a GUI dialog I cannot answer from 3000 km
away — and so the fleet has one worker with no Keychain, no TCC and no
outbound filter in front of its agents.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the dev box as found on 2026-10-04 — Ubuntu 24.04 x86_64, 4 CPU /
  7 GB, Determinate Nix, `nix develop ~/.config/nix-dev-env` providing
  `claude` 2.1.289 (logged in to the shared Max), `gh` 2.102.0 (logged in),
  `uv`, `node`; no `sdlc`, no `autonomous-sdlc` plugin, no clone, no fleet
  config **When** the documented bootstrap runs inside that dev shell
  (`install.sh --core` + `scripts/deploy.sh`, or a `scripts/fleet-bootstrap.sh`
  if those assume macOS paths) **Then** `sdlc` is a `uv tool`, the
  `fx-claude-config` marketplace and `autonomous-sdlc` plugin are installed,
  `~/.sdlc-fleet.yaml` names the queue, and `sdlc doctor` is CLEAN.
- **Given** `templates/systemd/sdlc-worker.service` (a `systemd --user` unit;
  `loginctl enable-linger` already on) installed **When** the box boots
  **Then** `nix develop ~/.config/nix-dev-env -c sdlc queue run --worker dev
  --pool claude-shared --follow` runs with `Restart=always`, reads
  `SDLC_QUEUE_TOKEN` from an `EnvironmentFile` (0600) only as the fallback —
  on Linux the `tailscale` CLI works unattended, so `tailscale whois`
  (`tag:trusted`) is the primary identity and the #833 misreport does not
  apply — and logs to the journal. `caffeinate`/`systemd-inhibit` are not
  needed on a server and the 35.2-004 note says so.
- **Given** a job enqueued from the XPS with repo
  `/home/fxmartin/Work/claude-code-config` **When** `dev` claims it **Then**
  the worker resolves the clone under *its* home (`/home/fx/Work/…`) exactly
  as the Macs map it to `/Users/fxmartin/…` — the first worker whose user
  name differs from the XPS's, so the home-relative mapping of 35.2-002 is
  proven, not assumed.
- **Given** the worker registers **When** `sdlc queue workers` runs on the XPS
  **Then** it lists `dev` (host `dev-server`, pool `claude-shared`, harness
  `claude`; `codex-shared` only once Codex is installed there — capability
  registration decides, not the unit file), and a `claude-shared` job goes to
  the least-loaded of `home-lab`/`dev`.
- **Given** the 35.2-006 and 35.2-007 self-checks **When** the worker starts
  on Linux **Then** both pass without a GUI (`GIT_TERMINAL_PROMPT=0`, `gh auth
  setup-git`; the TCC check is skipped off-macOS) and the probe completes
  from the unit's own context.
- **Given** a story job for this repo **When** it runs on `dev` **Then** the
  controller's own suites pass on x86_64 Linux as they do on arm64 CI, and
  the run ends in a merged PR shown on the XPS dashboard with worker `dev`.
- **Given** `README.md` / `docs/controller-architecture.md` **When** updated
  **Then** the fleet section documents the Linux worker, the nix-shell
  wrapper, and how to reach the box (Tailscale SSH policy denies the XPS and
  public port 22 is home-IP-only — operate via `ssh -J home-lab`, or widen the
  ACL in `nix-install`).

**Technical Notes**: The unit's `ExecStart` must go through
`/nix/var/nix/profiles/default/bin/nix develop <flake> -c …` because every
tool lives in the dev shell, not on the login PATH; `~/.local/bin` (uv tools)
must be on the unit's PATH. Pools are a capability of the login present on
the box: the shared Max (`mail@fxmartin.me`, Max 20x) → `claude-shared`.
Found 2026-10-04 while the M3 Max was gated by Little Snitch (`gh` blocked
outbound; alerts refuse remote-desktop input by design; FileVault forbids a
remote reboot) — the day's fifth GUI-only gate, none of which exists on a
headless Linux box. Relaxes the Non-Goal "provisioning the Hetzner box".

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: unit template renders/validates; home-relative repo mapping for
      a differing user name; pool list derived from capabilities; doctor
      finding on Linux; bootstrap script idempotent (bats)
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.2-004, 35.2-005, 35.2-006
**Risk Level**: Medium

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

##### Story 35.4-005: Preflight is a visible run phase everywhere a run is visible
**User Story**: As FX watching a run from the XPS dashboard, the acm overlay or
a worker's own dashboard, I want the run to exist from the moment the
controller starts it — preflight included — so that a two-minute test suite
or a preflight failure on a remote Mac is something I can see, not something
I infer from a `running` job with no run.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `sdlc build` / `sdlc fix` / a worker-launched job **When** the
  guards that need no run (dry-run, undenied host-auth, forge declaration,
  dirty tree, parked conflicts) have passed **Then** the ledger is created and
  the run row registered — locally and, on a fleet, pushed to the fleet
  registry — **before** preflight starts; the run reads `IN_PROGRESS` with a
  `preflight` phase and the dashboard header shows `preflight: running
  (<command>, <elapsed>)` live.
- **Given** preflight passes **When** the pipeline continues **Then** the
  header shows `preflight: passed (<duration>)` as today and nothing else
  about the run changes (same run id, same registry record, same counts).
- **Given** preflight fails or times out **When** the controller stops
  **Then** the run is stamped `FAILED` with the preflight reason
  (`PRE_FLIGHT_RED` / `PRE_FLIGHT_TIMEOUT` and the command) as an `error`
  event, the registry and fleet records are finished `FAILED`, the fleet job
  finishes `failed` with that reason, Telegram carries it, and `sdlc status`
  shows it — today it leaves no run at all.
- **Given** the acm overlay and the fleet view **When** a remote worker is in
  preflight **Then** both show the run with its worker and the `preflight`
  phase, because the fleet record carries a `phase` field updated on each
  heartbeat.
- **Given** a resume **When** the interrupted run was in preflight **Then** it
  resumes by re-running preflight (nothing was dispatched), not by skipping it.

**Technical Notes**: In `run_build` the preflight block sits before "Ledger
bootstrap" (`build.py` ~7593 → ~7613); swap the order and emit
`preflight started/passed/failed` events; `fix_issue.run_fix` and
`run_fix_batch` mirror it (~2268, ~3166). `status_snapshot` gains `phase`
(`preflight` | `stories` | `closing`), derived from the events, which the
dashboard header and `/api/runs` surface; `RunRecord` gains `phase` and the
worker's heartbeat push carries it (35.4-001). `default_preflight` already
prints `PRE_FLIGHT_TIMEOUT` / `PRE_FLIGHT_RED` lines — route them into the
ledger as events instead of stderr only. Tests that assert "preflight failure
leaves no run row" invert to "leaves a FAILED run with the reason".

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: run row exists before preflight on build/fix/batch; failure
      stamps FAILED with reason + finishes registry/fleet/job; dashboard
      header string contract; phase in `/api/runs`; resume re-runs preflight
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.4-001
**Risk Level**: Medium

##### Story 35.4-006: A remote run shows the same detail as a local one
**User Story**: As FX selecting a run that is executing on the M3 Max from the
XPS dashboard, I want to see what I see for a local run — stories and their
stages, the dependency DAG, the live event stream, tokens and cost, the model
routing banner, the preflight phase and the forge panel — so that watching a
remote run is not "STARTED · remote · elapsed 4m" and `no stories yet…`.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a fleet run whose record carries its worker's `dashboard_url`
  (Story 35.4-002) **When** it is selected on the XPS **Then** `/api/status`
  (and the SSE change token) is served by relaying the worker dashboard's own
  `/api/status?run=<id>` over the tailnet — the 35.4-002 `/api/logs` relay,
  generalised — so the header, counts, stories table, stage attempts, DAG,
  events, usage/cost and routing banner render exactly as for a local run,
  with the worker named in the header.
- **Given** the worker's dashboard does not answer **When** the run is
  selected **Then** the page falls back to today's header-only snapshot and
  says so (`detail unavailable — m3max dashboard not reachable`), never a
  blank "no stories yet…" that reads like an empty run.
- **Given** the run's repo lives at a path that does not exist on the XPS
  (`/Users/fxmartin/Work/…`) **When** the forge panel renders **Then** it
  resolves the forge from the fleet record's `origin`/slug, not from a local
  path, so it shows the repo's issues/PRs/CI instead of `GitHub unavailable`.
- **Given** a worker **When** it starts **Then** its own dashboard is running
  and bound to its tailnet address (`sdlc dashboard --host <tailnet-ip>`), as
  a LaunchAgent alongside the worker (template +
  `--dashboard-url` advertised by the worker), so the relay has something to
  relay; `sdlc doctor` on the worker warns when the advertised URL does not
  answer.
- **Given** `"view session"` on a remote story **When** clicked **Then** it
  keeps working as 35.4-002 built it (same relay, same confinement).

**Technical Notes**: `dashboard.py` already proxies `/api/logs` server-side
with a 5 s timeout, no redirects and no env proxy (35.4-002); add the same
for `/api/status` and the stream token, keyed on the selected run's
`dashboard_url`. Relay responses are the worker's JSON passed through, plus
`worker` and `origin`; nothing is read from the XPS's disk for a remote run.
Tailnet ACL: XPS → M3 Max is `trusted → trusted` (any port); XPS → home-lab
needs `tcp:8787`, already granted on 2026-10-04. The worker plist gains a
sibling `com.fxmartin.sdlc-dashboard.plist` (or the worker spawns the
dashboard) — one decision for the story.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: relay of `/api/status` and the change token for a remote run;
      fallback message when unreachable; forge slug from the fleet record;
      worker advertises and doctor checks the dashboard URL; local runs
      byte-identical
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.4-001, 35.4-002
**Risk Level**: Medium

### Feature 35.5: Unattended Recovery

> A fleet worker has nobody beside it. Whatever the controller leaves "on the
> branch" without pushing is invisible from the XPS, and whatever it decides
> from a local test verdict is decided without the one judge every PR already
> has — hosted CI.

##### Story 35.5-001: Committed recovery work is pushed before a story is declared failed
**User Story**: As FX, I want a bugfix round that *committed* a fix to push
that commit and let CI adjudicate before the story is declared failed, so
that a fix the agent already wrote is never stranded on a worker's clone and
a test that was already red on `main` in that environment cannot sink the
round.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a bugfix agent returns `fix_status=FIXED` with `tests_passing=
  false` **And** `feature/<id>` is ahead of `origin/feature/<id>` **When** the
  controller evaluates the round **Then** it pushes the branch first
  (`_push_story_branch`, force-with-lease on the pre-round sha), logs
  `bugfix commit <sha> pushed to feature/<id>; CI adjudicates`, and — when
  the repo has CI (`_repo_has_ci_config`) and a PR exists — treats the round
  as *pending CI*: it polls the PR's pipeline through the 35/#793 gate seam
  (same grace, same cap) and a green pipeline counts as `tests_passing`. The
  worker's local verdict is advisory wherever hosted CI exists; it stays
  authoritative when the repo has no CI or no PR yet.
- **Given** recovery is exhausted at any stage and the branch is ahead of its
  remote **When** `_exhausted_status` runs **Then** the commits are pushed
  before the status is decided, the event reads `recovery exhausted; N
  unpushed commit(s) pushed to feature/<id> (<sha>)`, and the story parks
  `NEEDS_ATTENTION` — the R10 "work committed" rule applies to every `kind`,
  not only `contract`. `FAILED` is reserved for a round that produced no
  commit.
- **Given** the isolated worktree is torn down with "branch/PR preserved"
  **When** the branch has commits not on `origin` **Then** teardown pushes
  them, or on a rejected push keeps the worktree and logs the sha — the #614
  sandbox rule, applied to every worker clone. *Preserved* means on the
  remote.
- **Given** the bugfix agent's own suite run fails on a test that also fails
  on the base branch in the same environment (it already says so in prose:
  "fails on `main` too") **When** it fills the envelope **Then** the schema
  carries `baseline_failures: [test names]`, the controller excludes those
  from the `tests_passing` verdict (`tests_passing` = no *new* failures) and
  logs them, so a macOS-only bats failure can no longer veto a fix that is
  green on Linux CI.
- **Given** `sdlc status`, the worker dashboard and the XPS fleet view
  **When** a story is parked this way **Then** the row reads `fix pushed ·
  awaiting CI` (or `· CI red`) with the sha and PR, carried through the
  fleet registry record like any other status.
- **Given** a local run with no fleet configured **When** the same path runs
  **Then** behaviour is identical (the push is the only addition and it is
  what the first-pass stages already do); the full controller suite stays
  green on both platforms.

**Technical Notes**: Today `_run_bugfix` returns `False` whenever
`tests_passing` is false; `_exhausted_status` parks only `kind == "contract"`
with a stage artifact; `_push_story_branch` is called only from the first-pass
push path, and `_sync_branch_to_remote` fast-forwards *from* the remote. The
incident: run `11dda412` on home-lab (2026-10-04, job 4) — both bugfix rounds
committed working fixes (`1780523` for 35.4-006, `f33a98b` for 35.2-007),
both reported one pre-existing local bats failure, both stories went `FAILED`
with the fixes stranded on the Mac's clone; recovered by hand with `git fetch
ssh://home-lab/~/Work/claude-code-config feature/<id>` and a push from the
XPS, after which CI re-ran on the fixed heads. Reuse the #793/#794 CI-gate
seam for the pending-CI wait rather than a new poller; the `baseline_failures`
field is additive to the bugfix envelope schema (older agents omit it → no
change in verdict).

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests: FIXED + not-green + ahead-of-remote → push then pending-CI (green
      → resolved, red → one more round, no CI → today's verdict); exhausted
      with commits ahead → pushed + NEEDS_ATTENTION for every kind; teardown
      pushes or keeps the worktree; `baseline_failures` excluded from the
      verdict; status/dashboard/registry wording; local-mode parity
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 35.2-005, 35.4-001
**Risk Level**: Medium

## Epic Sequencing

1. **MVP (XPS → M3 Max end to end)**: 35.1-001 → 35.1-002 → 35.2-001 →
   35.2-002 → 35.3-001 → 35.2-004 → 35.4-001 → **35.2-005**. Exit: the first
   success metric. (Added 2026-10-03: the first twelve stories shipped with
   `queue run` refusing a fleet URL, so a worker drained only its own host's
   store; the service's host was the only possible worker. 35.2-005 closes
   that gap.)
2. **Pools and the second worker**: 35.2-003, then home-lab joins as a worker
   in pool `claude-shared` (no new story — it is 35.2-004's plist on a second
   Mac).
3. **Operability**: 35.1-003, 35.4-002, 35.4-003, 35.4-004.
4. **From the first fleet job (2026-10-04)**: 35.4-005 (preflight is a visible
   run phase — the job's two-minute Go preflight on the M3 Max was invisible
   on every dashboard) and 35.2-006 (non-interactive git auth on the worker —
   the sync hung on a Keychain prompt). Both Must Have before the fleet is
   trusted unattended.
5. **From the second fleet job (2026-10-04)**: 35.4-006 — a remote run on the
   XPS dashboard showed only its header (`STARTED · remote · elapsed`, "no
   stories yet…", "GitHub unavailable"); relay the worker dashboard's status
   like 35.4-002 relays its logs.
6. **From the M3 Max stall (2026-10-04)**: 35.2-007 — the worker proves it
   can run an agent from its own launchd context before registering, and
   refuses with the cause (TCC-protected `~/.claude`, pending GUI dialog)
   instead of failing stories 300 s at a time.
7. **From the Little Snitch gate (2026-10-04)**: 35.2-008 — the Hetzner dev
   box joins `claude-shared` as a headless Linux worker, so a shared-pool job
   never waits on a dialog only a physical keyboard can answer.
8. **From fleet job 4 (2026-10-04)**: 35.5-001 — two bugfix rounds on
   home-lab committed working fixes, reported one pre-existing local test
   failure, and the stories went `FAILED` with the commits stranded on the
   Mac; committed recovery work is pushed and CI adjudicates before a story
   is declared failed. Must Have before unattended fleet bugfix rounds are
   trusted.

## Non-Goals

- Fleet hosts beyond the two Macs and the Hetzner dev box (35.2-008); Codex
  on the dev box until it is installed there.
- Postgres or any new storage technology; the service wraps the SQLite store.
- Webhook/label intake — Epic 30 enqueues *into* this queue; the listener is
  not built here.
- Credential distribution — workers use their own logins; the service and the
  jobs never carry tokens.
- Cross-worker worktrees or shared checkouts — each worker owns its clones.
- Approving anything; the human gate stays human.
