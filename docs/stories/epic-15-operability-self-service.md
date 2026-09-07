# Epic 15: Operability & Self-Service

> **Status: COMPLETE (6/6)** — 15.1-004 merged 2026-09-06 (#648; installed-controller staleness
> check, live since v2.59.0) after a day of runs silently executed on a twelve-release-old install.
> The original five merged on `main` (2026-06-25): 15.3-001 `sdlc clean`
> (#174), 15.1-001 `sdlc doctor` (#175), 15.1-003 `sdlc repair` (#176), 15.2-001 hook profiles
> (#177), 15.1-002 `status --markdown` (#178). Created 2026-06-20, inspired by the external
> [affaan-m/ECC](https://github.com/affaan-m/ECC) operator tooling (`ecc doctor`/`repair`,
> `status --markdown` portable handoff). Adds new **read-side** CLI verbs so the five LTM
> colleagues can self-diagnose instead of pinging FX. (These are new verbs, so they belong here
> rather than in Epic-12, whose non-goals exclude new CLI surface.)

## Epic Overview

**Epic ID**: Epic-15
**Description**: Today the only introspection is `sdlc status`/`state` (run/stage snapshots).
There is no single command that answers "is my install healthy and is anything stuck?", no
self-repair for a broken install, no portable export a colleague can paste into a message or a
ticket, and hook strictness is all-or-nothing. When something looks wrong, a colleague's only
recourse is to ask FX. This epic adds `sdlc doctor` (health-check across install, ledger, config,
and dependencies), a `status --markdown` handoff export, optional `sdlc repair`, and hook profiles
for tuning strictness.

**Business Value**: The framework ships to five colleagues on a mixed macOS/WSL2 fleet with low
tolerance for debugging. Self-service diagnostics turn "it's stuck, ask FX" into "run `sdlc
doctor`, follow the remedy" — cutting support load and making the framework safe to hand off. The
markdown export makes asking for help (when still needed) a paste, not a screen-share.

**Success Metrics**:
- `sdlc doctor` detects the common failure classes (broken install/symlinks, stale ledger schema,
  stuck/stale runs, missing `gh`/`semgrep`/`osv-scanner`) and prints an actionable remedy for
  each — verified against seeded-broken fixtures.
- A colleague can produce a portable status export (`status --markdown`) and share it without a
  live session.
- `sdlc repair` restores managed files/symlinks without a full reinstall (when in scope).
- Hook strictness is tunable via a documented profile without editing hook scripts.

## Epic Scope

**Total Stories**: 6 | **Total Points**: 15 | **MVP Stories**: 0 (roadmap — Should Have)

## Out of Scope (Non-Goals)

- **Changing orchestration / build behavior.** These are read-side/operational verbs; they do not
  alter how a build runs.
- **Auto-fixing application code.** `doctor`/`repair` operate on the *framework install and
  controller state*, not the target project's code.
- **A GUI.** Operability is CLI + markdown export; the web dashboard (Epic-11) stays the visual
  surface.
- **Replacing `install.sh`.** `repair` restores managed artifacts; full installation/uninstall
  remains `install.sh`'s job.

## Features in This Epic

### Feature 15.1: Diagnostics & Handoff

Let an operator answer "is this healthy?" and share the answer.

#### Stories

##### Story 15.1-001: `sdlc doctor` health-check
**User Story**: As an LTM colleague, I want a single command that checks my install and run state
and tells me what's wrong and how to fix it, so that I can resolve common problems without asking
FX.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `sdlc doctor` **When** it runs **Then** it checks: install integrity (expected
  symlinks/files present), ledger schema currency + integrity (migrations applied, DB readable),
  stuck/stale-run detection (a run IN_PROGRESS with a dead pid or no recent activity), config
  validity (settings/schemas parse), and dependency availability (`gh`, `semgrep`, `osv-scanner`,
  `claude`).
- **Given** any check fails **When** results are printed **Then** each problem reports a clear
  status (CLEAN/WARN/FAIL) and a concrete remedy (the command or doc to fix it).
- **Given** all checks pass **When** `doctor` runs **Then** it exits 0 with a concise green
  summary; `--exit-code` makes WARN/FAIL non-zero for automation.

**Technical Notes**: New read-side verb in `controller/src/sdlc/cli.py`; reuse `status.py`/
`ledger_view.py` for run/ledger checks and the registry/pid logic (Epic-11 11.2-001) for
stale-run detection. Dependency checks shell out to `--version`. Pairs naturally with Epic-12
12.2-003 (auto-migrate at launch) — `doctor` reports a behind-on-migrations DB that 12.2-003 then
fixes.

**Definition of Done**:
- [ ] `sdlc doctor` checks install/ledger/stuck-run/config/dependencies
- [ ] Each finding has a CLEAN/WARN/FAIL status + remedy; `--exit-code` for automation
- [ ] Tests against seeded-broken fixtures (missing dep, stale ledger, stuck run)
- [ ] Documented in the controller reference + onboarding

**Dependencies**: None (richer with Epic-11 11.2-001 registry, Epic-12 12.2-003)
**Risk Level**: Low

##### Story 15.1-002: `sdlc status --markdown` portable handoff
**User Story**: As a colleague needing help, I want to export my current state as markdown so that
I can share readiness, active runs, and pending approvals without a live session.
**Priority**: Should Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** `sdlc status --markdown` (optionally `--write <file>`) **When** it runs **Then** it
  emits a portable markdown report covering readiness (doctor summary), active/recent runs and
  their stages, install health, and pending governance events (risk-gate approvals).
- **Given** the export **When** pasted into an issue/chat **Then** it renders as readable markdown
  (tables/sections), self-contained, no secrets included.
- **Given** the existing `status`/`status --json` **When** used **Then** they are unchanged
  (markdown is an added format).

**Technical Notes**: Extend the `status` verb in `cli.py`/`status.py` with a markdown renderer;
reuse the `doctor` summary (15.1-001) and the ledger snapshot. Scrub paths/tokens that could leak.

**Definition of Done**:
- [ ] `status --markdown [--write]` emits a self-contained, secret-free report
- [ ] Existing `status`/`--json` unchanged
- [ ] Tests for the renderer (active run, idle, pending approval)
- [ ] Documented in onboarding (how to share state when asking for help)

**Dependencies**: 15.1-001 (reuses the doctor summary)
**Risk Level**: Low

##### Story 15.1-003: `sdlc repair` — restore managed files (optional)
**User Story**: As a colleague whose install drifted, I want a command that restores the
framework's managed symlinks/config so that I can recover without a full reinstall.
**Priority**: Could Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** `sdlc repair` (with `--dry-run`) **When** managed symlinks/config are missing or
  drifted **Then** it reports what it would restore and, without `--dry-run`, restores them
  idempotently.
- **Given** a healthy install **When** `repair` runs **Then** it is a no-op.
- **Given** anything outside the framework's managed set **When** `repair` runs **Then** it is
  never touched (no destructive action on user files).

**Technical Notes**: Thin wrapper over the managed-artifact set `install.sh` already knows; may
delegate to install internals rather than reimplement. Could be deferred to `install.sh --repair`
if a separate verb proves redundant.

**Definition of Done**:
- [ ] `sdlc repair [--dry-run]` restores managed artifacts idempotently
- [ ] No-op on healthy install; never touches unmanaged files
- [ ] Tests for drift → restore and the no-op path
- [ ] Documented

**Dependencies**: None
**Risk Level**: Low

##### Story 15.1-004: `sdlc doctor` warns when the installed controller is behind the checkout
**Field finding (2026-09-06, FX)**: the PATH-installed `sdlc` is a `uv tool install`
snapshot, not the checkout. It sat at v2.45.12 while `main` moved to v2.57.0 — twelve
releases — and every build run that day silently executed on the old code: none of the
adapter, model-pin or parser fixes merged that morning were in the controller actually
dispatching agents. The only tell was the version badge in the dashboard brand bar.
Merging to `main` does not update the running tool, and nothing said so.
**User Story**: As FX running `sdlc` from a PATH-installed wheel, I want `sdlc doctor` — and the
run-start preflight of `sdlc build` / `sdlc fix` — to warn when the installed controller's version
differs from the `controller/pyproject.toml` of the repo I am running against, naming both
versions and the exact reinstall command, so that a merge to `main` can never again leave runs
executing on stale code without a visible signal before any tokens are spent.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the installed `sdlc.__version__` is behind the version declared in the target
  repo's `controller/pyproject.toml` **When** `sdlc doctor` runs **Then** it reports a `WARN`
  finding under the `install` check naming both versions (e.g. `installed 2.45.12, checkout
  2.57.0`) with the remedy: reinstall from the machine's master checkout
  (`bash scripts/install-controller.sh`) then `sdlc dashboard --restart`; `--exit-code` makes
  it non-zero, matching every other doctor finding.
- **Given** the two versions are equal **When** `doctor` runs **Then** the finding is `CLEAN`
  and adds no noise; **Given** the installed tool is *newer* than the checkout **Then** it is
  still `WARN` — the two disagree either way — but the remedy names the other side
  (`git pull` the checkout), never a reinstall that would go backwards.
- **Given** the target repo has no `controller/pyproject.toml` (any project `sdlc` is pointed
  at that is not this framework) **When** `doctor` runs **Then** the check is reported as not
  applicable, never `WARN` or `FAIL` — the framework repo is the only one that declares a
  controller version.
- **Given** `sdlc build` or `sdlc fix` starts against a checkout that disagrees with the
  installed version **When** preflight runs **Then** the same one-line warning is printed
  beside the existing `harness routing:` line and recorded as a `warning`-level ledger event
  on the run, and **the run proceeds** — this is a signal before spend, not a gate (the
  `--allow-dirty` refusal pattern is deliberately *not* used here).
- **Given** the check runs anywhere (doctor, preflight, CI tests) **Then** it reads only the
  local `pyproject.toml` — no network, no `gh` call, no git fetch — so it is deterministic and
  satisfies the offline CI contract; tests use fixture pyprojects for equal, behind, ahead, and
  absent.
- **Given** the controller is run from a checkout via `uv run` (development mode, where
  `_resolve_version()` reads the same tree) **When** the check runs **Then** it is `CLEAN` —
  no false positive for the way the test suite and local development invoke it.

**Technical Notes**: The installed version is `sdlc.__version__` (`controller/src/sdlc/__init__.py:28`,
via `_resolve_version()`); the expected version is `[project].version` in the target repo's
`controller/pyproject.toml`, parsed with `tomllib` (stdlib, no new dependency). Add a check in
`doctor.py` following the `check_config(repo_root: Path) -> Finding` shape (`doctor.py:383`)
under the existing `install` category, emitting the same
`Finding(check, name, status, detail, remedy)` contract 15.1-001 defined (`doctor.py:108`) —
`detail` carries the two version strings, `remedy` the side-specific command. The preflight hook is the point where `build.py:4460` /
`fix_issue.py:916` emit the `harness routing:` line — surface the warning there so it lands in
the same ledger event stream and the dashboard's run header. Version comparison: prefer
`packaging.version` if already a transitive dependency, else a tuple parse of `MAJOR.MINOR.PATCH`;
record which was chosen. Open question for the implementer: whether `sdlc repair`
should also run this check, since #630 showed repair can relink to an empty install.

**Definition of Done**:
- [ ] `doctor` finding: WARN on any mismatch with both versions + side-specific remedy; CLEAN on
      equal; not-applicable when the repo declares no controller version
- [ ] `build`/`fix` preflight prints the warning beside `harness routing:` and records a
      `warning` ledger event; the run is never blocked
- [ ] Offline by construction; tests cover equal / behind / ahead / absent / dev-mode
- [ ] `--exit-code` semantics unchanged (WARN → non-zero)
- [ ] Documented in the controller reference beside the other doctor checks, and in
      `docs/onboarding.md` as the "I merged but nothing changed" answer

**Dependencies**: 15.1-001 (`sdlc doctor`); reads the `harness routing:` preflight seam from Epic-20
**Risk Level**: Low

### Feature 15.2: Hook Ergonomics

Make hook strictness tunable without editing scripts.

#### Stories

##### Story 15.2-001: Hook profiles and context controls
**User Story**: As an operator, I want to tune hook strictness and SessionStart context size via
environment without editing hook scripts so that I can run lean on a low-context setup or strict
in a hardened one.
**Priority**: Could Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** a `*_HOOK_PROFILE=minimal|standard|strict` env var **When** hooks run **Then** they
  honor the profile (e.g. minimal skips non-essential sidebar/notification work; strict enables
  all guardrails).
- **Given** a disable list (e.g. `*_DISABLED_HOOKS="…"`) **When** set **Then** the named hooks
  are skipped, while the rest run.
- **Given** a SessionStart context cap **When** set **Then** injected context is truncated to the
  cap; unset preserves today's behavior.
- **Given** none of these are set **When** hooks run **Then** behavior is unchanged from today
  (defaults preserved).

**Technical Notes**: Implement in the shared `hooks/cmux-bridge.sh` and the session hooks; the
bridge already degrades gracefully when cmux is absent, so add profile checks there. Keep variable
names consistent with the existing `cmux-`/hook conventions.

**Definition of Done**:
- [ ] Hook profile + disable-list + context-cap env controls honored
- [ ] Defaults unchanged when unset
- [ ] Tests/bats for profile gating
- [ ] Documented in `docs/cmux-integration.md`

**Dependencies**: None
**Risk Level**: Low

### Feature 15.3: Workspace Hygiene

Make build-leftover cleanup a safe, repeatable controller verb instead of a manual git ritual.

#### Stories

##### Story 15.3-001: `sdlc clean` — safe workspace garbage collection
**User Story**: As an operator whose repo has accumulated build leftovers, I want a controller
command that safely removes orphaned worktrees, merged story branches, and stale transcript logs
so that I can keep a clone build-ready without hand-running git worktree/branch incantations.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `sdlc clean` with no flags **When** it runs **Then** it is **dry-run by default** —
  it reports what it *would* remove (orphaned `agent-*` worktrees, squash-merged
  `feature/{story}` branches, stale `.sdlc-state.db.logs/` transcripts) and removes nothing until
  `--force`/`--yes` is passed.
- **Given** a build is **in progress** (here or in another session/clone) **When** `clean` runs
  **Then** it never touches a worktree or branch tied to an `IN_PROGRESS` run — it consults the
  run registry (Epic-11 **11.2-001**) + live-pid check and skips anything a live run owns, so it
  is safe to run while another build is active.
- **Given** a story branch **When** `clean` decides whether to delete it **Then** "merged" is
  determined by the ledger (`status=DONE`) and the PR's merge state (`gh pr view --json state` →
  `MERGED`), **not** `git branch --merged` — which misreports squash-merged branches as unmerged
  (observed: 0-of-18 shipped branches reported merged).
- **Given** an `agent-*` worktree **When** it is a candidate **Then** it is removed only if it is
  not dirty (no uncommitted changes) and its owning run is terminal or its pid is dead; locked
  worktrees owned by a live run are left alone.
- **Given** `clean` removes anything **When** it runs **Then** each removal is logged, deletions
  are recoverable where git allows (branch tips reachable via reflog), and the command never
  pushes to or mutates the remote.

**Technical Notes**: Promote the existing `hooks/sweep-orphan-worktrees.sh` (6-hour `agent-*`
sweep that already checks `git worktree list`) and `cmux-stop.sh`'s merged-worktree removal into a
first-class, cross-platform, testable controller verb in `controller/src/sdlc/`. Pairs with
`sdlc doctor` (15.1-001 *detects* the cruft → `clean` *fixes* it) and with Epic-17 **17.2-002**
(per-story worktree teardown under concurrency). Registry-awareness (11.2-001) is what makes it
safe to run concurrently with another build — the differentiator over the blunt hook sweeper.

**Definition of Done**:
- [ ] `sdlc clean` dry-run default; `--force`/`--yes` to act
- [ ] Registry/pid-aware: never touches an `IN_PROGRESS` run's worktrees/branches (test with a live-run fixture)
- [ ] Branch "merged" decided via ledger + `gh` PR state, not `git branch --merged` (squash-merge correct)
- [ ] Orphan `agent-*` worktrees removed only when clean + owning-run terminal; locked-by-live left alone
- [ ] Stale transcript logs swept; every removal logged; no remote mutation
- [ ] Tests for dry-run, squash-merge detection, live-run safety; documented in the controller reference

**Dependencies**: None (richer/safer with Epic-11 11.2-001 registry; complements 15.1-001 `doctor`)
**Risk Level**: Medium

## Story Dependencies (within Epic-15)

```
15.1-001 (doctor) ──> 15.1-002 (markdown handoff, reuses doctor summary)
15.1-003 (repair)     independent (optional)
15.2-001 (hook profiles) independent
15.3-001 (sdlc clean)    independent (safer with 11.2-001 registry; pairs with 15.1-001)
```

- **Cohort 1** (no deps): 15.1-001, 15.1-003, 15.2-001, 15.3-001
- **Cohort 2**: 15.1-002 (needs 15.1-001)

## Epic Complete When

- `sdlc doctor` detects the common failure classes with actionable remedies and an automation
  exit code.
- `status --markdown` produces a portable, secret-free handoff export.
- `sdlc repair` restores managed artifacts idempotently (or the capability lands in `install.sh`).
- Hook strictness and SessionStart context are tunable via documented env controls without editing
  scripts.
- `sdlc clean` safely garbage-collects build leftovers (orphan worktrees, squash-merged branches,
  stale logs) — dry-run by default and safe to run while another build is live.
