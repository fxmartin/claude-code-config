# Epic 23: Pipeline on GitLab — run the autonomous build against GitLab projects

> **Status: COMPLETE (10/10)** — created 2026-06-28. All 10 stories (23.1-001 → 23.7-001) are
> implemented, tested, and merged; **23.7-001** (forge-agnostic dashboard repo-health) shipped in v2.10.0.
> Carved out of Epic-22 (which made the *issue/story mirror*
> code-host-agnostic). Epic-22 lets the team *track* work on GitHub or GitLab; this epic lets the
> autonomous **build pipeline itself run against a GitLab project** — opening **Merge Requests** instead
> of PRs, gating on **GitLab CI** instead of GitHub Actions, releasing on GitLab, and reviewing via
> `glab mr diff`. The company standard is **GitLab Free/Core** (no Premium features). The framework's own
> repo stays on GitHub; this epic makes the controller able to *target* GitLab company repos, not migrate
> the framework. It builds directly on **Epic-22's code-host adapter**, extending it from issue
> operations to MR operations.

## Epic Overview

**Epic ID**: Epic-23
**Description**: The autonomous controller's build loop is GitHub-coupled end to end: it opens a
**Pull Request** per story (`gh pr create`), gates merge on **GitHub Actions** checks (`gh pr checks`),
merges, runs adversarial review via `gh pr diff`, and releases via GitHub Actions + GitHub Releases
(Epic-05). For company work on **GitLab** this doesn't function. This epic ports the *pipeline* to
GitLab while reusing everything host-neutral: the controller stays the local tool (authenticated with
`glab`), the per-story branch produces a **Merge Request** (via the Epic-22 adapter extended to MR ops),
the MR triggers **GitLab CI** quality gates, the controller polls the **MR pipeline status** to gate the
merge (the `gh pr checks` equivalent), the merge auto-closes the story issue via `Closes #N`, adversarial
review runs `glab mr diff`, and release runs as a **GitLab CI** job producing **GitLab Releases** + tags.
A `.gitlab-ci.yml` quality-gate template brings the Epic-02 gates (lint, tests, schema/contract checks,
secret scan, commit-format) to a company target repo. Everything stays inside **Free/Core** — no merge
trains, no Premium-only constructs.

**Business Value**: Makes the autonomous SDLC framework usable for **company work**, not just FX's
personal GitHub repos. Without this, the team can *see* the board on GitLab (Epic-22) but the actual
build/MR/CI/release loop can't run there — so the framework's core value (autonomous story execution to a
merged change) is GitHub-only. This unlocks the corporate-standard host end to end.

**Success Metrics**:
- A full `sdlc build` run completes against a **GitLab project**: per-story branch → **MR** → GitLab CI
  gates pass → merge → story issue auto-closed, with **zero `gh`/GitHub** calls.
- The controller **gates the merge on the MR's GitLab CI pipeline** (a red pipeline blocks merge, exactly
  as GitHub Actions checks do today).
- Adversarial review produces the same verdict contract from `glab mr diff` as it does from `gh pr diff`.
- A release runs on GitLab CI: a conventional-commit-driven semver **tag** + a **GitLab Release** with
  generated notes — the GitLab equivalent of Epic-05.
- All of it works on **GitLab Free/Core** — no feature requires Premium/Ultimate.

## Epic Scope

**Total Stories**: 10 | **Total Points**: 42 | **MVP Stories**: 0 (roadmap — Should Have)

## Out of Scope (Non-Goals)

- **Migrating the framework's own repo to GitLab.** It stays on GitHub; this epic makes the controller
  *target* GitLab projects.
- **The issue/story mirror.** That is Epic-22 (which built the shared board + the code-host adapter for
  issues). Epic-23 reuses that adapter and adds the *build pipeline*.
- **GitLab Premium/Ultimate features** — merge trains, native Epics, security dashboards, multiple
  assignees, code-quality widgets. The target is Free/Core only.
- **Provisioning GitLab CI runners.** Assume the company GitLab provides runners; this epic supplies the
  pipeline config, not the infrastructure.
- **Other hosts** (Bitbucket, Gitea). The adapter pattern allows them later; not built here.

## Features in This Epic

### Feature 23.1: VCS adapter — Merge Request operations

Extend Epic-22's code-host adapter from *issue* ops to *MR/PR* ops, so the build's VCS calls are host-neutral.

#### Stories

##### Story 23.1-001: MR/PR operation interface + GitLab and GitHub implementations
**User Story**: As FX, I want one interface for change-request operations with `gh pr` and `glab mr`
implementations so that the build loop opens/diffs/checks/merges a change without knowing the host.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the adapter interface (`cr_create`, `cr_diff`, `cr_status`, `cr_merge`, `cr_url`) **When**
  the host is GitHub **Then** calls route through `gh pr`; **When** GitLab **Then** through `glab mr` —
  same inputs/outputs, host differences (PR↔MR, number↔iid, `Closes #N` phrasing) hidden.
- **Given** a build dispatch on a GitLab project **When** it opens a change request **Then** an **MR**
  is created with the story branch, title, and `Closes #<issue>` in the description.
- **Given** the existing GitHub path **When** unchanged **Then** behaviour is byte-identical (the adapter
  defaults to `gh pr`).

**Technical Notes**: Extends the Epic-22 adapter (`issue_*` → add `cr_*`). `glab` is GitLab's official
CLI (`glab mr create/diff/merge`, `glab ci status`). Normalize PR/MR identity behind a `cr_ref`. This is
the seam every later story in this epic routes through.

**Definition of Done**:
- [x] Adapter CR interface + GitHub and GitLab implementations, peer reviewed
- [x] Tests: each verb on both adapters (mocked `gh`/`glab`), GitHub-unchanged regression
- [x] `docs/controller-architecture.md` + the adapter docs updated

**Dependencies**: Epic-22 Story 22.2-001 (the code-host adapter foundation)
**Risk Level**: High

### Feature 23.2: Build pipeline against GitLab

The controller's per-story loop opens MRs, gates on GitLab CI, and merges — the core port.

#### Stories

##### Story 23.2-001: Build opens Merge Requests on a GitLab target
**User Story**: As FX running a build on a company GitLab repo, I want each story's change to open an MR
so that work lands through the same review/merge flow the team already uses.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a GitLab target project **When** the build finishes a story's implementation **Then** it
  pushes the story branch and opens an MR via the adapter, recording the MR `cr_ref` in the ledger.
- **Given** the build's PR-creation points in `build.py` **When** routed through the adapter **Then** the
  GitHub path is unchanged and the GitLab path opens an MR instead.
- **Given** branch isolation (Epic-12 12.4-001) **When** a GitLab build runs **Then** branches are cut
  from the GitLab default branch and MRs target it.

**Technical Notes**: Touches `build.py`'s change-request creation — land when no long run is active.
Reuse the existing per-story branch/PR lifecycle; only the create call changes (via 23.1-001).

**Definition of Done**:
- [x] MR creation in the build loop implemented and peer reviewed
- [x] Tests: MR opened on GitLab target, GitHub PR path unchanged, branch targets GitLab default
- [x] Docs updated

**Dependencies**: 23.1-001
**Risk Level**: High

##### Story 23.2-002: Gate the merge on GitLab CI pipeline status
**User Story**: As FX, I want the controller to wait for the MR's GitLab CI pipeline and only merge on
green so that a failing pipeline blocks the merge exactly as GitHub Actions checks do today.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** an open MR with a running pipeline **When** the controller checks status (`glab ci status` /
  MR pipeline) **Then** it polls until the pipeline finishes, with a bounded timeout.
- **Given** a **failed** pipeline **When** the gate evaluates **Then** the merge is blocked and the
  story is routed to the bugfix loop / parked, not merged.
- **Given** a **passed** pipeline **When** the gate evaluates **Then** the merge proceeds.
- **Given** a project with **no CI** configured **When** the gate runs **Then** it degrades to a clear
  warning (configurable allow/deny), not a hang.

**Technical Notes**: The GitLab analogue of `gh pr checks`. Reuse the controller's existing
status-gate/poll structure; only the status source changes (via the adapter). Coordinate with the
GitLab CI template (23.3-001) for what "green" means.

**Definition of Done**:
- [x] Pipeline-status merge gate implemented and peer reviewed
- [x] Tests: poll-to-completion, fail-blocks-merge, pass-merges, no-CI degradation
- [x] Docs updated

**Dependencies**: 23.2-001
**Risk Level**: High

##### Story 23.2-003: Merge the MR + close the story issue + branch cleanup
**User Story**: As FX, I want a green MR merged and its story issue closed automatically so that the
GitLab loop ends the same way the GitHub loop does.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a green MR **When** the controller merges it via the adapter **Then** the merge completes and
  the linked story issue auto-closes via the `Closes #<issue>` in the MR description (Epic-22 mapping).
- **Given** a merged MR **When** cleanup runs **Then** the story branch is deleted per the existing
  worktree/branch GC, on GitLab.
- **Given** the merge **When** recorded **Then** the ledger marks the story DONE with the GitLab merge sha.

**Technical Notes**: Reuse Epic-22's story↔issue mapping for the close-link. GitLab MR merge via the
adapter (`cr_merge`); branch cleanup via the existing GC hooks.

**Definition of Done**:
- [x] MR merge + issue close + cleanup implemented and peer reviewed
- [x] Tests: merge closes issue, branch removed, ledger DONE with sha
- [x] Docs updated

**Dependencies**: 23.2-002
**Risk Level**: Medium

### Feature 23.3: Quality gates as GitLab CI

Bring the Epic-02 self-CI gates to a GitLab target repo so MRs actually run them.

#### Stories

##### Story 23.3-001: `.gitlab-ci.yml` quality-gate template
**User Story**: As FX adopting a GitLab repo, I want a CI template that runs the same quality gates as
the GitHub Actions ones so that MRs are gated to the same standard.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a company GitLab project **When** the template is installed **Then** MRs run the gate set
  mirroring Epic-02/09: lint (shellcheck/ruff), tests (pytest/bats), JSON-schema + contract checks,
  secret scan, and **commit-format** (commitlint) — as GitLab CI jobs.
- **Given** a job fails **When** the pipeline runs **Then** the MR pipeline is red (so 23.2-002 blocks merge).
- **Given** Free/Core **When** the template runs **Then** it uses only Free-tier CI (no merge trains, no
  Premium-only keywords).

**Technical Notes**: Translate `.github/workflows/*` gate intent into `.gitlab-ci.yml` stages. Keep the
gate *definitions* the source of truth where possible (the controller already has the gate scripts);
the CI config invokes them. This is shipped as an installable template for target repos, not the
framework's own CI.

**Definition of Done**:
- [x] `.gitlab-ci.yml` template + gate jobs implemented and peer reviewed
- [x] Tests/CI dry-run validating the pipeline lints/passes on a sample repo
- [x] Docs: gate-parity table (GitHub Actions ↔ GitLab CI)

**Dependencies**: None (informs 23.2-002)
**Risk Level**: Medium

### Feature 23.4: Release on GitLab

Port Epic-05's release management to GitLab.

#### Stories

##### Story 23.4-001: GitLab release flow (semver tag + GitLab Release)
**User Story**: As FX, I want releases on GitLab to work like Epic-05 on GitHub so that company repos get
clean semver tags and release notes.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** conventional commits merged to the default branch **When** the release job runs in GitLab CI
  **Then** it computes the semver bump, creates a `vX.Y.Z` **tag**, and publishes a **GitLab Release**
  with generated notes — the GitLab equivalent of the Epic-05 GitHub-Actions flow.
- **Given** no release-worthy commits **When** the job runs **Then** it no-ops (same semantics as Epic-05).
- **Given** Free/Core **When** releasing **Then** it uses GitLab Releases + `release-cli` (Free-tier).

**Technical Notes**: Reuse Epic-05's `compute-release.sh`/conventional-commit logic; swap the publish
surface (GitHub Release → GitLab Release via `release-cli`/`glab release`). Coordinate with Epic-05
(owns the release semver logic) — port, don't fork.

**Definition of Done**:
- [x] GitLab release job implemented and peer reviewed
- [x] Tests: bump computed, tag + release created, no-op on non-release commits
- [x] Docs: the GitLab release flow

**Dependencies**: 23.3-001
**Risk Level**: Medium

### Feature 23.5: Adversarial review on GitLab

Port Epic-08's reviewer slot to `glab mr diff`.

#### Stories

##### Story 23.5-001: `glab mr diff` adversarial review + high-risk gate
**User Story**: As FX, I want the adversarial review and high-risk approval gate to work on GitLab MRs so
that company changes get the same scrutiny as GitHub PRs.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a GitLab MR **When** adversarial review runs **Then** `codex-adversarial-review.sh` (and any
  reviewer) sources the diff via the adapter (`glab mr diff`) and returns the same verdict contract it
  does from `gh pr diff`.
- **Given** a high-risk change **When** the gate evaluates on GitLab **Then** it requires the maintainer
  approval signal as a **label** (`risk-approved`) — the Free/Core equivalent of the GitHub gate (which
  also uses the label for solo/non-org repos).
- **Given** the GitHub path **When** unchanged **Then** review still uses `gh pr diff`.

**Technical Notes**: Make `scripts/codex-adversarial-review.sh` host-aware via the adapter. Reuse Epic-08's
`adversarial-reviewers.yaml` consensus + the `risk-approved` label mechanism (already label-based).

**Definition of Done**:
- [x] Host-aware adversarial review + GitLab high-risk gate implemented and peer reviewed
- [x] Tests: glab-mr-diff verdict parity, GitLab risk-approved gate, GitHub-unchanged
- [x] Docs updated

**Dependencies**: 23.1-001 (Epic-08 owns the reviewer registry)
**Risk Level**: Medium

### Feature 23.6: GitLab auth, tokens & adoption

The runtime credentials and the "adopt a GitLab project" path.

#### Stories

##### Story 23.6-001: GitLab auth & CI tokens
**User Story**: As a developer, I want clear auth for local runs and CI so that the controller can act on
a GitLab project without leaking or hardcoding credentials.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a local run **When** the controller acts on GitLab **Then** it uses the developer's `glab
  auth` identity (and `whoami` for attribution, per Epic-22 identity).
- **Given** CI-side actions (release, status) **When** they run in GitLab CI **Then** they use CI/CD
  variables / a project access token / `CI_JOB_TOKEN`, never a committed secret.
- **Given** the secret-path deny baseline (Epic-13) **When** a GitLab token is present **Then** it is
  handled under the same protections as GitHub tokens.

**Technical Notes**: Mirror the GitHub auth handling; document the minimal token scopes (api, write_repo).
Coordinate with Epic-13 (agent runtime security) for token handling.

**Definition of Done**:
- [x] Auth/token handling implemented and peer reviewed
- [x] Tests: local glab-auth path, CI-token path, no-secret-committed check
- [x] Docs: token scopes + setup

**Dependencies**: None
**Risk Level**: Medium

##### Story 23.6-002: "Adopt a GitLab project" guide + preflight
**User Story**: As FX, I want a guided check that a GitLab project is ready for the autonomous pipeline so
that a first run doesn't fail halfway on a missing prerequisite.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a target GitLab project **When** a preflight (`sdlc doctor`-style) runs **Then** it verifies
  `glab` is installed/authenticated, the project + default branch exist, CI is enabled, and the gate
  template is present — reporting any gap.
- **Given** the guide **When** followed **Then** a worked example takes a company repo from zero to a
  first green `sdlc build` MR on GitLab.

**Technical Notes**: Extend the Epic-15 `sdlc doctor` checks with GitLab-target checks. Reference the
Epic-22 board setup so issues and the pipeline align.

**Definition of Done**:
- [x] GitLab preflight + adoption guide written and reviewed
- [x] Tests: preflight detects each missing prerequisite
- [x] Linked from the harness/host docs

**Dependencies**: 23.2-003, 23.3-001
**Risk Level**: Low

### Feature 23.7: Forge-agnostic dashboard

Make the dashboard's repo-health surface host-aware so a GitLab project shows
GitLab health instead of "GitHub unavailable".

#### Stories

##### Story 23.7-001: Forge-agnostic dashboard repo-health surface
**User Story**: As FX running the dashboard against a GitLab project, I want the repo-health badge/panel to
show GitLab issue/MR/pipeline health instead of "GitHub unavailable" so that the dashboard is
forge-agnostic like the rest of the pipeline.
**Priority**: Could Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a GitLab project **When** the dashboard renders the repo-health surface (Story 11.2-006)
  **Then** it fetches health via `glab` (open issues, open MRs, default-branch pipeline status) through the
  host adapter and shows a populated badge/panel — not the "GitHub unavailable" sentinel.
- **Given** a GitHub project **When** unchanged **Then** the badge still fetches via `gh` exactly as today.
- **Given** neither `gh` nor `glab` is available / no forge remote **When** the fetch fails **Then** it
  degrades to the existing muted "unavailable" sentinel (never throws), with host-appropriate wording.

**Technical Notes**: `controller/src/sdlc/github_stats.py` currently shells out to `["gh", ...]`
unconditionally. Make the fetch host-aware via `resolve_host()` / the issue-host adapter (Epic-22 / 23.1),
adding a `glab`-based fetcher (`glab issue list`, `glab mr list`, `glab ci status`) behind the same stats
shape and TTL cache. Rename the surface neutrally (repo-health, not GitHub-specific) where it doesn't churn
the API. Keep the off-request-path cache behaviour from Story 11.2-006.

**Definition of Done**:
- [ ] Host-aware repo-health fetch (`gh` + `glab`) implemented and peer reviewed
- [ ] Tests: `glab` fetch parity, GitHub-unchanged, graceful unavailable on both hosts
- [ ] Dashboard shows GitLab repo health against a GitLab project (no "GitHub unavailable")

**Dependencies**: 23.1-001 (adapter); Epic-11 (owns the dashboard/observability surface)
**Risk Level**: Low

## Story Dependencies (within Epic-23)

```
Epic-22 adapter ─> 23.1-001 (MR adapter) ─┬─> 23.2-001 (open MR) ─> 23.2-002 (CI merge gate) ─> 23.2-003 (merge+close)
                                          └─> 23.5-001 (glab mr diff review)
23.3-001 (GitLab CI gates) ───────────────────────────────────────> 23.2-002
23.3-001 ─> 23.4-001 (GitLab release)
23.6-001 (auth/tokens) ── foundational
23.2-003 + 23.3-001 ─> 23.6-002 (adopt guide + preflight)
23.1-001 ─> 23.7-001 (forge-agnostic dashboard repo-health)
```

- **Cohort 1**: 23.1-001 (needs Epic-22 adapter), 23.3-001 (CI gate template), 23.6-001 (auth)
- **Cohort 2**: 23.2-001 (needs 23.1-001), 23.5-001 (needs 23.1-001), 23.7-001 (needs 23.1-001)
- **Cohort 3**: 23.2-002 (needs 23.2-001; uses 23.3-001), 23.4-001 (needs 23.3-001)
- **Cohort 4**: 23.2-003 (needs 23.2-002), 23.6-002 (needs 23.2-003 + 23.3-001)

> Cross-epic: **Epic-22** owns the code-host adapter — 23.1 *extends* it from issues to MRs (and reuses
> its story↔issue mapping for the close-link). **Epic-02** owns self-CI — 23.3 *ports* its gate intent to
> GitLab CI. **Epic-05** owns release semver — 23.4 *ports* the publish surface to GitLab Releases.
> **Epic-08** owns the adversarial reviewer/risk gate — 23.5 makes it host-aware. **Epic-13** owns
> token/secret handling — 23.6 follows it. **Epic-07/12** own the controller build loop — 23.2 changes
> the change-request creation/merge there.

## Epic Complete When

- A full `sdlc build` completes against a GitLab project: branch → MR → GitLab CI green → merge → story
  issue auto-closed, with zero GitHub calls.
- The merge is gated on the MR's GitLab CI pipeline (red blocks merge).
- Adversarial review returns the same verdict contract from `glab mr diff`; the high-risk gate works via
  the `risk-approved` label on GitLab.
- A release runs on GitLab CI — semver tag + GitLab Release with notes — the Epic-05 equivalent.
- Everything runs on GitLab **Free/Core**; a preflight + guide take a company repo from zero to a first
  green MR.
- The dashboard's repo-health surface is forge-agnostic — a GitLab project shows GitLab health, not
  "GitHub unavailable".
