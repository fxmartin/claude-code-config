# Epic 30: Local Forge & Event-Driven Dispatch

> **Status: NOT STARTED (0/9)** — authored 2026-08-04, expanding Issue #561 into a
> full epic and absorbing the event-driven dispatch design discussed the same
> day. Thesis: with local-ci-cd's self-hosted GitLab CE proven live (green
> pipelines, 50-job leak-free soak) the sdlc controller is the **last cloud
> tether** in the development loop — every `sdlc build`/`fix` still round-trips
> github.com for issues, PRs, merges, and CI status. Epic-22 made the
> issue/story mirror host-agnostic and Epic-23 ported the pipeline to GitLab
> (MRs, CI gates, `glab`), but both assume a remote whose hostname *says*
> "gitlab": `host_from_remote` (`issue_host.py:240`) detects by hostname
> substring, so the local instance at `http://127.0.0.1:8080` resolves to
> **None** and the whole adapter stack is unreachable for exactly the repos the
> offline vision needs. This epic closes that gap with an explicit per-repo
> **forge declaration** (the `.sdlc-harness.yaml` precedent), wires credentials
> from local-ci-cd's secrets machinery, and then makes the local forge the
> **trigger**, not just the record: a `sdlc listen` webhook daemon so that
> creating a labeled issue on local GitLab starts the fix loop, and mirroring
> an epic starts its build — no manual `sdlc fix`/`sdlc build` invocation.
>
> **Decisions locked 2026-08-04** (FX): webhooks (not polling) as the primary
> trigger transport; triggers cover **both** issue-opened→fix and
> epic-mirrored→build; the injection/spend gate is an explicit **`auto-fix`
> label opt-in** (issue bodies feed agent prompts — nothing untrusted may
> start a run); the listener ships as **`sdlc listen` + a nix-managed
> LaunchAgent**; Issue #561 is **closed by this epic's PR** and the mirrored
> story issues become the tracking surface.

## Epic Overview

**Epic ID**: Epic-30
**Description**: Two features of prior art stop just short of the goal.
(1) *Targeting*: Epic-22/23's adapter reaches GitLab through `glab`, but host
resolution is substring-based hostname detection with no explicit override
surface — a `127.0.0.1`/`localhost` remote, or any remote whose name lacks
"gitlab", returns None and the caller has no seam to say "this repo's forge is
the local GitLab at this URL". Credentials are likewise assumed ambient
(`glab auth`), while local-ci-cd mints PATs via `gitlab-rails runner`
(local-ci-cd#107) and stores them under `.stack/secrets/` — never argv, never
logs. (2) *Invocation*: every run starts with FX typing a command. GitLab CE
(Free tier) fires project webhooks on issue events; a small authenticated
listener can turn "issue labeled `auto-fix` appears on the local board" into
`sdlc fix <iid>` and "epic mirror labeled `auto-build`" into `sdlc build`,
with the host registry providing the one-live-run concurrency guard the
dashboard already reads. Combined with a local-model harness (Epic-29's qwen/
opencode work), this completes the fully-offline autonomous SDLC loop that is
local-ci-cd's end-state vision: issue filed locally → webhook → agent fix →
MR → local CI gate → merge → mirror fans out to GitHub as backup.
**Business Value**: Removes the last github.com dependency from the
development loop, so company/private work never leaves the machine (the
local-ci-cd premise), and removes FX-as-scheduler from the loop, so the
factory works the backlog the moment work is filed instead of when FX
remembers to launch it. Graduates local-ci-cd Epic-08 mode-B repos
(dual-push, GitHub-authoritative — a mode that exists *only because* this
controller is GitHub-only) to mode A (local-authoritative), dissolving the
dual-push complexity. The opt-in label gate keeps spend and prompt-injection
exposure under explicit human control while the loop is young.

**Success Metrics**:
- A full fix loop completes against the local instance with **zero cloud
  calls**: issue labeled `auto-fix` on local GitLab → webhook → `sdlc fix` →
  MR → local CI gate → merge → issue auto-closed, verified with network
  egress observed only to `127.0.0.1`.
- A repo whose `origin` is `http://127.0.0.1:8080/...` (undetectable by
  hostname) runs `sdlc build`/`fix`/`issues init` end-to-end via its
  `.sdlc-forge.yaml` declaration.
- An unlabeled issue created on the local board triggers **nothing** — the
  listener logs the event and stays idle (the opt-in gate holds).
- Two `auto-fix` issues filed in quick succession produce sequential runs, not
  concurrent ones — the registry-backed guard holds, and the second is queued,
  not lost.
- Webhook redelivery (GitLab retries on timeout) never produces a duplicate
  run for the same issue.

**Out of Scope**:
- Polling mode (`sdlc fix all` on a LaunchAgent timer) — viable degraded
  fallback, documented in 30.2-002's notes, but webhooks are the decided
  transport; build the poller only if webhooks prove unreliable in practice.
- GitHub webhooks — github.com repos keep manual invocation; the event loop
  is a local-forge feature (a cloud webhook needs an inbound tunnel, which
  contradicts the offline premise).
- Local-model harness enablement — that is Epic-29; this epic must work with
  the claude harness and merely not obstruct qwen/opencode routing.
- GitLab Premium constructs (merge trains, scoped labels) — Free/Core only,
  per Epic-23's constraint.
- Migrating the framework's own repo off GitHub — per Epic-23's constraint,
  the controller *targets* local repos; claude-code-config stays on GitHub.

## Features in This Epic

### Feature 30.1: Forge Declaration Seam

Make "which forge, at which URL, with which credentials" an explicit per-repo
fact instead of a hostname guess. Mirrors the `.sdlc-harness.yaml` precedent:
a small checked-in YAML, a resolution precedence, and a preflight that fails
loud and early.

#### Stories

##### Story 30.1-001: `.sdlc-forge.yaml` declaration and resolution precedence
**User Story**: As FX pointing the controller at a repo whose origin is the
local GitLab (`http://127.0.0.1:8080/...`), I want a checked-in
`.sdlc-forge.yaml` declaring the forge kind and instance URL, resolved ahead
of hostname auto-detection, so that host resolution never returns None for a
declared repo and never silently guesses wrong for an ambiguous remote.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a repo containing `.sdlc-forge.yaml` with `forge: gitlab` and
  `gitlab_url: http://127.0.0.1:8080` **When** any host-touching path resolves
  the adapter (`detect_host` call sites: build, fix, issues init, dashboard)
  **Then** the GitLab adapter is selected with that instance URL, regardless
  of what the origin hostname looks like.
- **Given** no `.sdlc-forge.yaml` **When** resolution runs **Then** behavior
  is byte-identical to today's hostname detection — the file is purely
  additive (the Epic-20 registry pattern: no declaration, no change).
- **Given** a declaration naming an unsupported forge or a malformed URL
  **When** resolution runs **Then** the run aborts at preflight with a
  one-line actionable error, never mid-pipeline.
- **Given** both a declaration and a `--forge`/env override **When**
  resolution runs **Then** precedence is CLI/env > repo file > auto-detection,
  mirroring the harness precedence, and the effective forge is logged as a
  preflight line (the `harness routing:` precedent).
- **Given** the GitLab adapter with a custom instance URL **When** `glab`
  subprocesses run **Then** the instance is passed per-invocation (e.g.
  `GITLAB_HOST` in the subprocess env), never by mutating user-global `glab`
  config.

**Technical Notes**: Extend `issue_host.py` resolution (`detect_host` /
`host_from_remote` at `issue_host.py:240-268`) with a declaration-first step;
the file loader belongs beside the harness pin loader for symmetry. Every
adapter constructor call site must thread the instance URL — audit
`build.py`, `fix_issue.py`, `story_init.py`/`story_sync.py`, `dashboard.py`
(repo web base at `dashboard.py:39` must use the declared URL for deep links
so MR links on the dashboard open the local instance). Keep `SUPPORTED_HOSTS`
authoritative for the `forge:` value.

**Definition of Done**:
- [ ] Loader + precedence (CLI/env > file > auto-detect) with preflight log line
- [ ] All adapter call sites thread the declared instance URL; dashboard deep
      links honor it
- [ ] Malformed/unsupported declarations abort at preflight with actionable text
- [ ] Tests: declared-local resolution, no-file byte-identical fallback,
      precedence order, bad-declaration abort
- [ ] Documented in `docs/controller-architecture.md` beside the harness pin

**Dependencies**: none
**Risk Level**: Medium

##### Story 30.1-002: Local-instance credentials from the secrets store
**User Story**: As FX authenticating the controller against the local GitLab,
I want the adapter to source the PAT from local-ci-cd's secrets machinery
(env var pointing at `.stack/secrets/`, or the file directly) with an auth
preflight against the declared instance, so that tokens never appear in argv
or logs and a bad/expired PAT fails the run before any stage spends tokens.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a PAT available via the documented env/file convention **When**
  the GitLab adapter runs against the declared instance **Then** `glab`
  authenticates using it (subprocess env, e.g. `GITLAB_TOKEN`), and the token
  never appears in argv, ledger events, transcripts, or the dashboard.
- **Given** no PAT or a rejected PAT **When** preflight runs **Then** the run
  aborts with a one-line hint referencing the local-ci-cd PAT minting pattern
  (`gitlab-rails runner`, local-ci-cd#107) — no mid-run auth surprises.
- **Given** the framework's own GitHub repos **When** any run executes
  **Then** GitHub auth paths are untouched (gh keeps its own credential flow).

**Technical Notes**: GitLab 19 removed the OAuth password grant — PATs are the
only sanctioned path (local-ci-cd#107 has the working mint pattern). Redaction:
the existing "never argv/logs" discipline from the notify/env handling applies;
add a test asserting the token string is absent from a captured preflight log.
Auth preflight belongs with the existing probe machinery (`capability.py`
probe precedent) — cheap, short-timeout, before any ledger row opens.

**Definition of Done**:
- [ ] PAT sourcing via env/file convention, threaded to `glab` subprocess env
- [ ] Auth preflight against the declared instance; actionable abort on failure
- [ ] Redaction test: token absent from logs/ledger/dashboard payloads
- [ ] Documented: minting (reference local-ci-cd#107), storage, rotation

**Dependencies**: 30.1-001 (the declared instance to authenticate against)
**Risk Level**: Medium

##### Story 30.1-003: End-to-end pipeline run against the local instance
**User Story**: As FX validating the seam, I want one full `sdlc fix` run
against a scratch repo on the local GitLab — issue → investigation → build →
MR → local CI gate → merge → issue auto-closed — so that the forge
declaration is proven end-to-end before the event loop builds on it.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a scratch repo on the local instance with `.sdlc-forge.yaml`, a
  seeded bug issue, and a minimal `.gitlab-ci.yml` gate **When** `sdlc fix
  <iid>` runs **Then** the loop completes to a merged MR with the issue
  auto-closed, and the merge is gated on the local pipeline status (red
  pipeline blocks, per Epic-23 semantics).
- **Given** the run **When** network egress is observed **Then** forge traffic
  goes only to `127.0.0.1` (agent/model traffic is out of scope of this
  assertion until an Epic-29 local harness lands).
- **Given** the run completes **Then** the dashboard shows it with working
  deep links into the local instance (MR link, issue link).

**Technical Notes**: This is a verification story, not new machinery — it
exercises 30.1-001/002 plus Epic-23's MR/CI-gate code against the local
instance for the first time. Record the working scratch-repo setup (CI
template, webhook-less) in `docs/` as the reference configuration; 30.3-002
reuses it with webhooks added. Egress observation: `nettop`/lsof sampling is
sufficient evidence; no need for a packet capture harness.

**Definition of Done**:
- [ ] Recorded green run: issue → MR → gated merge → auto-close on the local
      instance
- [ ] Red-pipeline block verified (a failing CI job holds the merge)
- [ ] Dashboard deep links open the local instance
- [ ] Reference scratch-repo setup documented

**Dependencies**: 30.1-001, 30.1-002
**Risk Level**: Medium

### Feature 30.2: Webhook Listener & Trigger Policy

The forge becomes the trigger. A small authenticated daemon (`sdlc listen`)
receives GitLab project webhooks and turns labeled issue events into runs —
under an explicit opt-in gate, an idempotency check, and the registry-backed
concurrency guard.

#### Stories

##### Story 30.2-001: `sdlc listen` webhook daemon
**User Story**: As FX wiring the local GitLab to the controller, I want a
`sdlc listen` daemon that binds `127.0.0.1`, verifies GitLab's
`X-Gitlab-Token` secret on every request, parses issue/label events, and logs
every accepted/ignored event, so that there is exactly one authenticated
doorway between the forge and the agent factory.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the daemon running with a configured secret **When** a request
  arrives without the correct `X-Gitlab-Token` **Then** it is rejected (401)
  and logged; nothing is parsed from its body.
- **Given** a valid issue event (opened / label added) **When** received
  **Then** the daemon extracts only structural fields (project, issue iid,
  labels, action) — issue title/body are never interpolated into any command
  or shell — and hands them to the trigger policy (30.2-002).
- **Given** any other event kind (push, MR, pipeline, note) **When** received
  **Then** it is acknowledged and ignored, logged at debug level.
- **Given** the daemon **When** it starts/stops **Then** lifecycle mirrors the
  dashboard's (`start/stop/status` verbs, pidfile, port conflict → actionable
  error), and `sdlc listen status` reports the last events seen.
- **Given** a malformed payload **When** received **Then** the daemon answers
  400 and stays up — a poison event can never crash the loop.

**Technical Notes**: Follow the dashboard server's shape (`dashboard.py`
start/stop/pidfile precedent, `127.0.0.1` bind at `dashboard.py:280`) —
stdlib `http.server` is fine; no framework. The command boundary is the
injection defense: the only thing that crosses from payload to subprocess is
a validated integer iid and a validated project path, mapped onto fixed argv
(`sdlc fix <iid>`), never a shell string. Webhook secret from the same
env/file convention as 30.1-002. GitLab webhook configuration itself (URL +
secret on the project) is documented, not automated, in this story.

**Definition of Done**:
- [ ] Daemon with token verification, structural-fields-only parsing, event
      logging, start/stop/status lifecycle
- [ ] 401/400/ignored paths tested with recorded GitLab payload fixtures
- [ ] Injection boundary test: hostile title/body strings never reach argv
- [ ] Webhook setup documented (project settings, secret, local URL)

**Dependencies**: 30.1-001 (forge declaration identifies the target repo)
**Risk Level**: High

##### Story 30.2-002: `auto-fix` trigger policy with idempotency and concurrency guard
**User Story**: As FX filing bugs on the local board, I want an issue to
trigger `sdlc fix <iid>` **only** when it carries the `auto-fix` label, with
webhook redeliveries deduplicated and runs serialized through the host
registry, so that labeling an issue is the single deliberate act that spends
agent tokens — and it can never double-spend or stampede.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** an issue opened **without** `auto-fix` **When** the event arrives
  **Then** nothing is dispatched; the decision is logged (`ignored:
  no auto-fix label`). Adding the label later triggers exactly one run
  (label-add event), so triage-then-arm works.
- **Given** an `auto-fix` issue **When** the event arrives **Then** the
  policy checks the registry + ledger for an existing live or completed run
  for that issue and dispatches `sdlc fix <iid>` only if none exists —
  webhook redelivery and label re-add are no-ops with a logged reason.
- **Given** a trigger while another run is live for the same repo **When**
  evaluated **Then** the new trigger is queued (FIFO) and dispatched when the
  registry shows the repo idle — never concurrent, never dropped; queue state
  survives a daemon restart.
- **Given** a dispatched run **When** it starts **Then** the existing
  notification path (`notify`) announces "auto-fix triggered: #<iid>" so an
  unattended trigger is never silent, and the dashboard shows the run via the
  normal registry path (#545 lineage).
- **Given** the story-mirror exclusion (#558) **When** a `story`-labeled
  issue somehow carries `auto-fix` **Then** the fix path's existing story
  filter still refuses it; the policy logs the refusal.

**Technical Notes**: The registry (`registry.py`) already records live runs
per repo with pid liveness — the guard reads it rather than inventing state.
Queue persistence: a small JSON beside the listener pidfile is sufficient;
crash-safety matters more than throughput (expected event rate is human-
scale). Spend guardrails (quiet hours, max runs/day) are deliberately a
**config surface of this policy**, default off, documented — not a separate
story; keep them simple counters, not a budget model (Epic-28 owns cost).
Degraded fallback if webhooks misbehave in practice: a documented
`sdlc fix all` LaunchAgent timer is the poller alternative — post-#559 it is
safe (story mirrors excluded); note it, do not build it.

**Definition of Done**:
- [ ] Label gate, dedup (registry+ledger check), FIFO queue with restart
      survival, one-live-run guard
- [ ] Trigger/refusal/queue decisions all logged; notify on dispatch
- [ ] Quiet-hours / max-per-day config (default off) with tests
- [ ] Tests: unlabeled ignore, label-add arm, redelivery no-op, queue under a
      live run, story-mirror refusal

**Dependencies**: 30.2-001; consumes #558/#559 (story-mirror exclusion) and
#545 (registry visibility)
**Risk Level**: High

##### Story 30.2-003: `auto-build` epic trigger
**User Story**: As FX mirroring a new epic to the local board with
`sdlc issues init`, I want applying an `auto-build` label to one of the
epic's story issues to trigger `sdlc build` scoped to that epic, so that
"mirror, review the board, arm the build" is a deliberate three-step that
needs no terminal — mirroring alone never starts a build.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a story issue carrying `epic:NN` **When** `auto-build` is added
  **Then** the policy resolves the epic from the label and dispatches the
  build for that epic's stories iff no live run exists for the repo (same
  queue/guard as 30.2-002), announcing via notify.
- **Given** `sdlc issues init` creating story issues (even if a template ever
  included `auto-build` at creation) **When** mirror events arrive **Then**
  creation events do not trigger — only a post-creation label **add** on an
  existing issue arms the build, so a mirror can never self-trigger.
- **Given** `auto-build` on an issue without an `epic:NN` label **When**
  evaluated **Then** refused and logged (no guessing).
- **Given** a second `auto-build` on the same epic while its build lives
  **Then** deduplicated exactly as 30.2-002 dedups fixes.

**Technical Notes**: Deliberate-label-add was chosen over "debounce after the
last mirror event" — deterministic, human-intentioned, and consistent with
the `auto-fix` gate; record the debounce alternative in the story doc for
posterity. Epic resolution reuses the `epic:NN` taxonomy from
`story_labels` (`story_render.py:168`). The dispatch maps to the existing
build entrypoint scoped to the epic's stories; no new build semantics.

**Definition of Done**:
- [ ] Label-add-only trigger with epic resolution from `epic:NN`
- [ ] Mirror-creation events provably inert (test with init-shaped fixtures)
- [ ] Shared queue/guard/dedup with 30.2-002; notify on dispatch
- [ ] Refusal path for unlabeled/ambiguous epics

**Dependencies**: 30.2-002 (shared policy machinery); Epic-22 mirror labels
**Risk Level**: Medium

### Feature 30.3: Always-On Deployment & Offline Loop Proof

Ship the listener as a managed service and prove the end-state: the full
labeled-issue → merged-MR loop with zero cloud calls.

#### Stories

##### Story 30.3-001: nix-managed LaunchAgent for `sdlc listen`
**User Story**: As FX rebuilding a Mac from the nix-install config, I want a
Home Manager LaunchAgent that keeps `sdlc listen` running (KeepAlive, logs
under the standard location, secret sourced from the env/file convention), so
that the event loop is part of the declarative machine state, not a terminal
session FX has to remember.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a rebuild on a machine with the listener enabled **When**
  activation completes **Then** the LaunchAgent is loaded, `sdlc listen
  status` reports healthy, and the agent survives logout/reboot (KeepAlive).
- **Given** the sdlc controller reinstall during rebuild (the existing
  `sdlcController` activation) **When** the binary is replaced **Then** the
  agent recovers (KeepAlive restart) without manual intervention.
- **Given** the nix-install steady-state RSS audit (`audit-launchagents.sh`,
  Epic-08 discipline) **When** run **Then** the listener's footprint is
  recorded and within the norms of the existing agents.
- **Given** a machine/profile where the local forge is not deployed **Then**
  the agent is not installed — profile-gated like other Power-only modules.

**Technical Notes**: **Cross-repo story** — the LaunchAgent lands in
nix-install (`darwin/maintenance.nix` / a home-manager module, following the
Beszel/health-api precedent), while the daemon itself ships in this repo;
sequence the controller release first, then the nix module pointing at the
installed `sdlc`. Respect the parent repo's per-commit release-metadata gate
(every nix-install commit carries a version bump). Secret delivery via the
same env/file convention as 30.1-002 — never in the plist.

**Definition of Done**:
- [ ] Profile-gated LaunchAgent module in nix-install, KeepAlive, standard logs
- [ ] Survives rebuild's controller reinstall; RSS audited
- [ ] Runbook: enable/disable, secret placement, `sdlc listen status`
- [ ] nix-install release shipped per its release process

**Dependencies**: 30.2-001 (the daemon it manages)
**Risk Level**: Medium

##### Story 30.3-002: Zero-cloud loop validation and mode-A graduation note
**User Story**: As FX closing the local-ci-cd vision, I want a recorded
end-to-end validation — file an issue on the local GitLab, label it
`auto-fix`, and watch the loop deliver a gated, merged MR with the issue
closed, with forge egress only to `127.0.0.1` — plus a written go/no-go note
for graduating mode-B repos to mode A, so the offline claim is evidence, not
aspiration.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the 30.1-003 scratch repo with webhooks configured **When** an
  issue is filed and labeled `auto-fix` with no further human action **Then**
  the loop runs to a merged MR + auto-closed issue, the dashboard shows the
  run, notify announced it, and observed forge egress is `127.0.0.1`-only.
- **Given** the same setup **When** an unlabeled issue is filed **Then**
  nothing runs (the negative control, recorded alongside the positive).
- **Given** the validation results **Then** a short doc records the mode-A
  graduation decision for local-ci-cd Epic-08: which repos flip to
  local-authoritative, what the push mirror now guarantees, and any caveats
  (e.g. agent/model traffic still cloud until Epic-29 local harnesses land).

**Technical Notes**: This is the epic's acceptance test and the artifact FX
shows for "the loop is autonomous and local". Reuse 30.1-003's setup +
egress observation method. The mode-A note belongs in local-ci-cd's docs;
this story only owes the decision inputs and a cross-reference here.

**Definition of Done**:
- [ ] Recorded positive run (auto-fix → merged MR, zero human steps after the
      label) and negative control (unlabeled → nothing)
- [ ] Egress evidence captured
- [ ] Mode-A graduation note delivered to local-ci-cd with caveats
- [ ] Epic status updated to reflect the proven loop

**Dependencies**: 30.1-003, 30.2-001, 30.2-002; 30.3-001 (or a manually
started listener, acceptable for the validation itself)
**Risk Level**: Medium

##### Story 30.3-003: Listener observability on the dashboard
**User Story**: As FX glancing at the dashboard, I want a small listener
panel — daemon health, last N webhook events with their accept/ignore/queue
decisions, and current queue depth — so that "did my label actually arm a
run?" is answerable in one look instead of tailing daemon logs.
**Priority**: Could Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** the dashboard open with the listener running **Then** a panel
  shows daemon status (up/down, uptime), the last events with decisions, and
  queue depth — degrading to a muted "listener not running" when absent (the
  GitHub-panel degradation precedent).
- **Given** a queued trigger **Then** the panel shows it as queued with its
  position, and the entry clears when the run dispatches (visible via the
  normal run list).

**Technical Notes**: Read-only surface over the listener's own status/log
state (30.2-001's `status` verb is the data source — expose it as JSON once,
consume in both CLI and dashboard). Keep it a panel on the existing Builds
view, not a new view; SSE not required — the GitHub-badge 30s poll cadence
precedent is fine.

**Definition of Done**:
- [ ] Panel with daemon health, recent decisions, queue depth; graceful absence
- [ ] Backed by the same status JSON the CLI verb uses (no second source)
- [ ] Render test with fixture status payloads

**Dependencies**: 30.2-001, 30.2-002
**Risk Level**: Low

## Epic Sequencing

Feature 30.1 is strictly first — the event loop is meaningless until the
controller can talk to the local forge at all (30.1-001 → 30.1-002 →
30.1-003). Feature 30.2 then builds the doorway and policy (30.2-001 →
30.2-002 → 30.2-003). Feature 30.3 closes: 30.3-001 (cross-repo, can start
once 30.2-001 stabilizes) and 30.3-002 as the epic's acceptance test;
30.3-003 rides whenever 30.2-002's status surface exists. Recommended serial
order: 30.1-001 → 30.1-002 → 30.1-003 → 30.2-001 → 30.2-002 → 30.2-003 →
30.3-001 → 30.3-002 → 30.3-003. Epic-29 (harness parallelism / local models)
is complementary and independent; the two converge in the fully-offline
vision but share no code path.
