# Epic 33: GitLab as Master Forge for claude-code-config

> **Status: NOT STARTED (0/11)** — authored 2026-09-26. Thesis: on 2026-09-26 FX
> re-pointed this repo's `origin` at the home-lab GitLab (`root/claude-code-config`)
> and asked for GitLab to be the master. The remote flip alone breaks the
> pipeline in three verified ways: (1) host resolution fails — the tailnet
> hostname carries no `gitlab` substring, so auto-detection returns nothing and
> a fix run dies at its change-request open unless a `.sdlc-forge.yaml` is
> checked in (Story 30.1-001 built the seam; this repo never declared itself);
> (2) there is no `.gitlab-ci.yml`, so the merge gate that Issue #696 just made
> resolve GitLab pipelines finds no pipeline and follows the no-CI policy;
> (3) releases exist only as a GitHub Actions workflow on push to `main` —
> version bumps, tags, `chore(release)` commits, the CHANGELOG, the queue's
> version guard and `scripts/deploy.sh` all hang off it, and GitLab has no
> equivalent. `docs/gitlab-adoption.md` records "the framework itself stays
> on GitHub" as an Epic-23 non-goal; this epic reverses that non-goal
> deliberately, graduating this repo to local-ci-cd's **mode A**
> (local-authoritative, GitHub as backup mirror) — the end state Epic-30's
> thesis names for every repo.
>
> **Decisions locked 2026-09-26** (FX): full move — GitLab holds `main`, runs
> the CI gates, cuts releases, and tracks issues; GitHub becomes a push mirror
> with its Actions disabled. The three in-flight fixes (#693, #694, #701)
> finish on GitHub first, so the cutover starts from a clean queue; until
> then `origin` stays on GitHub and the GitLab remote is named `gitlab`.
> Dead `gitlab.test` links are tracked separately (local-ci-cd#271,
> nix-install#684) and do not block this epic.

## Epic Overview

**Epic ID**: Epic-33
**Description**: Move the framework's own repository from GitHub-authoritative
to GitLab-authoritative on the home-lab instance, without losing any gate the
GitHub path enforces today. Five features: (33.1) the repo declares its forge
and installs the GitLab CI gate template with parity to the ten-job GitHub
pipeline; (33.2) the release workflow is ported to a GitLab CI job on `main`
and every consumer of a release (queue version guard, deploy script, plugin
marketplace) keeps working; (33.3) GitHub becomes a push mirror of `main` and
tags with its workflows disabled so a mirrored push can never re-release and
diverge; (33.4) issues, labels and the high-risk approval gate live on GitLab
and are enforced there; (33.5) a rehearsed cutover with a rollback.
**Business Value**: Removes the last cloud dependency from the framework's own
development loop — the repo that runs the factory is built by the factory on
the appliance. Dissolves the dual-forge drift observed today (local `main`
ahead of one remote and behind another) by naming one master. Makes the
GitLab path first-class by dogfooding it: every gap Epic-23 left "best
effort" on GitLab becomes a defect in the maintainer's daily loop rather than
a caveat in the README.

**Success Metrics**:
- `sdlc fix <iid>` on this repo completes investigate → MR → GitLab CI green →
  merge → GitLab release with **zero `gh` calls** (verified by the ledger's
  command log).
- The GitLab pipeline enforces every gate `.github/workflows/ci.yml` enforces
  today, or the gap is listed in `docs/gitlab-ci-template.md` with an owner.
- A high-risk MR parks `AWAITING_APPROVAL` on GitLab and resumes on the
  `risk-approved` label, end-to-end, without a human touching the queue.
- GitHub `main` and tags equal GitLab's within one mirror interval; the
  GitHub release workflow has produced no commit since cutover.
- `nix-install`'s submodule and a fresh `./install.sh --core` still work,
  from either forge.

**Non-goals**: Moving `model-shelf` (an upstream fork, stays on GitHub).
Making `gitlab.test` resolvable (local-ci-cd#271 / nix-install#684). The
`sdlc listen` webhook daemon (Epic-30 Feature 30.2) — this epic makes the repo
a valid target for it, nothing more. Migrating closed GitHub issues or PR
history.

## Features in This Epic

### Feature 33.1: Forge Declaration and CI Gates on GitLab

The repo tells the controller what it is, and the GitLab project holds an MR to
the same standard a PR is held to today.

#### Stories

##### Story 33.1-001: Declare this repo's forge and remote layout
**User Story**: As FX running `sdlc fix` on this repo with `origin` on the
home-lab GitLab, I want the repo to declare `forge: gitlab` and the instance
URL so that host resolution never guesses from a tailnet hostname, and the
remote layout (`origin` = GitLab, `github` = mirror) is documented and
checked by `sdlc doctor`.
**Priority**: Must Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** `.sdlc-forge.yaml` at the repo root with `forge: gitlab` and
  `gitlab_url: http://home-lab.tailac3c7a.ts.net:8080` **When**
  `sdlc doctor --gitlab` runs **Then** every GitLab check is `CLEAN` and the
  effective forge is logged at preflight.
- **Given** the declaration **When** `sdlc fix <iid>` reaches its
  change-request open **Then** the MR is created on `root/claude-code-config`
  via `glab`, never `gh`.
- **Given** `docs/controller-architecture.md` and the README install section
  **When** read **Then** the two-remote layout is described, including the
  temporary inverse layout used while the last GitHub fixes finished.

**Technical Notes**: The loader and precedence exist (Story 30.1-001). The
plaintext-`http://` instance path sets `GITLAB_HOST` + `GLAB_CONFIG_DIR` per
invocation (`docs/issue-host-adapters.md`); confirm the `glab` keyring entry
for `home-lab.tailac3c7a.ts.net:8080` is what the adapter reaches. Once
local-ci-cd#271 lands, the URL can become `http://gitlab.test`; keep it a
one-line change.

**Definition of Done**:
- [ ] `.sdlc-forge.yaml` committed; `sdlc doctor --gitlab --exit-code` is 0
- [ ] A dry-run `sdlc fix --dry-run` (or the preflight log) shows the GitLab
      adapter selected
- [ ] Remote layout documented; `tests/` asserts the declaration parses

**Dependencies**: none
**Risk Level**: Low

##### Story 33.1-002: Install the GitLab CI gate template for this repo
**User Story**: As FX merging an MR on GitLab, I want `.gitlab-ci.yml` at the
repo root, derived from `templates/gitlab-ci.yml`, so that the merge CI gate
(Story 23.2-002, fixed for unmirrored repos by #696) has a real pipeline to
poll and a red pipeline blocks the merge.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `.gitlab-ci.yml` committed **When** an MR is opened **Then** a
  pipeline runs on the home-lab runner and reports per-job status the merge
  gate reads as `CR_SUCCESS`/`CR_FAILED`/`CR_PENDING`.
- **Given** a deliberately failing commit-format or secrets finding **When**
  the pipeline runs **Then** the MR's pipeline is red and `sdlc fix` refuses
  to merge, routing to the bugfix loop.
- **Given** the single-slot, 3 GB job runner declared in
  `home-manager/modules/local-ci-cd.nix` **When** the full pipeline runs
  **Then** it completes inside the runner's memory floor and under 15 minutes
  wall clock; jobs exceeding it are split or cached, not dropped.

**Technical Notes**: The template already ports `secrets-scan`, `shellcheck`,
`ruff`, `json-schema`, `commit-format` and `risk-gate`. The controller's
pytest suite (4,800+ tests, 2.5 min on the laptop) and the 45-file bats suite
must be added as jobs; the `uv`/`bats` toolchain images live in the zot proxy
(`local-ci-cd` `dependencyProjects.ts`). The CI-compatibility contract in
`CLAUDE.md` (root, offline, arm64, case-sensitive fs) applies verbatim.

**Definition of Done**:
- [ ] `.gitlab-ci.yml` committed with every job this repo needs
- [ ] One green MR pipeline and one red one recorded in the story's PR/MR
- [ ] Runner resource envelope measured and written into the file's header

**Dependencies**: 33.1-001
**Risk Level**: Medium

##### Story 33.1-003: Gate parity audit against the GitHub pipeline
**User Story**: As FX trusting the GitLab pipeline as the only gate, I want a
job-by-job parity table against `.github/workflows/ci.yml` (ten jobs:
secrets-scan, static-checks, type-check, supply-chain-scan, contract-checks,
commit-format, behavior-tests, controller-smoke, smoke-test, smoke-test-arch)
so that every check that gates a PR today also gates an MR, or its absence is
a named, owned exception.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the parity table in `docs/gitlab-ci-template.md` **When** compared
  with `ci.yml` **Then** every GitHub job maps to a GitLab job or to an
  exception row with a reason and an owner.
- **Given** `sdlc typecheck` and `sdlc supplychain` **When** run in the GitLab
  pipeline **Then** they exit non-zero on `BLOCK` exactly as in CI today.
- **Given** the macOS-only smoke jobs **When** the GitLab runner cannot host
  them **Then** they are listed as exceptions with the manual verification
  step, not silently dropped.

**Technical Notes**: `scripts/validate-gitlab-ci-template.sh` (Story 23.3-001)
enforces the full gate set on the shipped template; extend it, or add a
second validator, to check this repo's live `.gitlab-ci.yml` against the
parity table so drift fails CI.

**Definition of Done**:
- [ ] Parity table committed and referenced from the README gate stack
- [ ] Validator fails when a mapped job disappears
- [ ] Exceptions have owners and a follow-up issue each

**Dependencies**: 33.1-002
**Risk Level**: Low

### Feature 33.2: Releases Cut on GitLab

The `chore(release)` commit, tag, CHANGELOG section and release object come
from a GitLab CI job on `main`, and every downstream consumer of a release
keeps working.

#### Stories

##### Story 33.2-001: Port the release workflow to a GitLab CI job
**User Story**: As FX merging a `fix:` MR on GitLab, I want a `release` job on
`main` that runs `scripts/compute-release.sh` and `scripts/release-guard.sh`,
aligns the manifest versions, prepends the CHANGELOG section, commits the
bump, tags `vX.Y.Z` and creates a GitLab Release, so that semver keeps
flowing from Conventional Commits exactly as `release.yml` does today.
**Priority**: Must Have
**Story Points**: 8

**Acceptance Criteria**:
- **Given** a merged `fix:` commit on `main` **When** the pipeline runs
  **Then** one `chore(release): vX.Y.Z` commit and one `vX.Y.Z` tag land on
  GitLab `main`, with `controller/pyproject.toml`, `marketplace.json` and the
  plugin manifest aligned (the Release Version Alignment rule in `CLAUDE.md`).
- **Given** the bump commit itself **When** the pipeline runs again **Then**
  the two independent guards from `release.yml` (guard job + `BUMP=none`
  classification) stop a second release; the loop is proven by a recorded
  pipeline pair.
- **Given** two `main` pushes in quick succession **When** both pipelines run
  **Then** the second serialises behind the first (`resource_group`) instead
  of racing to the same tag.
- **Given** the job's push credential **When** inspected **Then** it is a
  project access token with `write_repository` only, stored as a masked CI
  variable, never in the repo.

**Technical Notes**: `compute-release.sh` and `release-guard.sh` are already
forge-neutral shell. The GitHub job publishes via `gh release create`; use
`glab release create` (or the Releases API) instead. Job-token pushes to a
protected `main` need the token allow-listed in the branch protection rules.
Keep the GitHub `release.yml` file but gate it on a repository variable set
to off (33.3-001) so a mirrored push cannot release.

**Definition of Done**:
- [ ] `release` job in `.gitlab-ci.yml` with the two guards and
      `resource_group: release`
- [ ] Two consecutive real releases on GitLab with correct CHANGELOG sections
- [ ] `docs/` release section rewritten for GitLab; GitHub steps marked legacy

**Dependencies**: 33.1-002
**Risk Level**: High

##### Story 33.2-002: Release consumers keep working from GitLab tags
**User Story**: As FX running the queue, `scripts/deploy.sh` and
`claude plugin update`, I want every consumer of a release to read GitLab's
tags and manifest so that the version guard (Story 15.1-004), the deploy
script and the plugin marketplace behave as they do with GitHub releases.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a release cut on GitLab **When** `sdlc queue run` claims the next
  job **Then** the version guard parks it with the reinstall remedy, and
  `./scripts/deploy.sh` converges the controller and plugin from the GitLab
  checkout.
- **Given** the marketplace manifest **When** `claude plugin update` runs
  **Then** it resolves the plugin from the local checkout path (as today) and
  the `version` field matches the GitLab tag.
- **Given** `git describe --tags` on a fresh GitLab clone **When** run
  **Then** it reports the latest release, so `compute-release.sh` has its
  baseline.

**Technical Notes**: The marketplace `source` is a relative path, so nothing
network-bound changes; verify `scripts/deploy.sh` has no `gh` call
(`rg -n '\bgh\b' scripts/deploy.sh`). Tags must be pushed by the release job,
not mirrored back from GitHub.

**Definition of Done**:
- [ ] Deploy and version-guard flow verified after a GitLab release
- [ ] No `gh` invocation left on the deploy path
- [ ] README install section updated

**Dependencies**: 33.2-001
**Risk Level**: Low

### Feature 33.3: GitHub as Push Mirror

GitHub keeps serving clones, submodules and the public README, but writes
flow one way.

#### Stories

##### Story 33.3-001: GitLab push mirror to GitHub with Actions disabled
**User Story**: As FX keeping GitHub as backup and public face, I want GitLab
to push-mirror `main` and tags to `github.com/fxmartin/claude-code-config`
with GitHub's `release.yml`, `ci.yml`, `eval-ci.yml` and `risk-gate.yml`
disabled, so that GitHub always equals GitLab and a mirrored push can never
create a `chore(release)` commit that GitLab does not have.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a merge on GitLab `main` **When** the mirror runs **Then** GitHub
  `main` and tags match within one mirror interval (verified by
  `git ls-remote` on both).
- **Given** the mirrored push **When** GitHub receives it **Then** no
  workflow runs (Actions disabled at the repo level, or each workflow gated on
  a repository variable), and the GitHub `main` reflog shows only mirror
  pushes.
- **Given** the mirror credential **When** inspected **Then** it is a
  fine-grained GitHub token limited to this repo's contents, stored only in
  the GitLab mirror settings.

**Technical Notes**: Push mirroring is a GitLab CE feature. Disable Actions
via repository settings (recorded in the runbook) rather than deleting the
workflow files, so a rollback (33.5-001) is a settings flip. Branch
protection on GitHub `main` is currently absent (verified 2026-09-26); the
mirror needs it to stay absent, or the mirror token must be allowed to push.

**Definition of Done**:
- [ ] Mirror configured and one merge observed landing on GitHub
- [ ] Zero workflow runs on GitHub after the observed push
- [ ] Runbook records the settings and the token scopes

**Dependencies**: 33.2-001
**Risk Level**: High

##### Story 33.3-002: Downstream consumers of the GitHub URL
**User Story**: As FX maintaining `nix-install`, the Codex mirror and the
pilot docs, I want every consumer of the GitHub clone URL either left on the
mirror knowingly or re-pointed at GitLab, so that nothing breaks silently
when the mirror lags or is retired.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `nix-install/.gitmodules` (`config/claude-code-config`) and its
  `shared-skills` consumption **When** reviewed **Then** the decision (stay on
  the mirror, or move to the GitLab URL over the tailnet) is recorded and
  implemented, and a rebuild of `home-lab` succeeds.
- **Given** `README.md`, `docs/onboarding.md`, `docs/pilot-kit/*` and
  `plugins/autonomous-sdlc/README.md` **When** they name a clone URL **Then**
  they name the master forge and note the mirror.
- **Given** `controller/tests/test_dashboard.py` and any test fixture using
  the GitHub URL **When** the suite runs **Then** it passes with the repo
  declared as GitLab.

**Technical Notes**: `rg -n "github.com/fxmartin/claude-code-config"` lists
six files today. `model-shelf` stays on GitHub (non-goal). LTM pilot
colleagues cannot reach the tailnet, so the pilot docs must keep the GitHub
URL and say it is a mirror.

**Definition of Done**:
- [ ] Consumer table committed with a decision per row
- [ ] `nix-install` rebuilt once from the chosen URL
- [ ] Tests green with the GitLab declaration in place

**Dependencies**: 33.3-001
**Risk Level**: Medium

### Feature 33.4: Work Tracking and the Human Gate on GitLab

The board the queue reads, and the one gate that must stay human, both move.

#### Stories

##### Story 33.4-001: Issue board migration to the GitLab project
**User Story**: As FX filing and fixing defects, I want the open GitHub issues
and the story mirror to exist on `root/claude-code-config` with the label
taxonomy the pipeline aligns to, so that `sdlc fix <iid>` and `sdlc fix all`
read the GitLab board and GitHub issues stop being the source of truth.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `sdlc issues init` against the declared GitLab forge **When** it
  runs **Then** every story in `docs/stories/` has a GitLab issue with its
  labels, and the inventory maps story → iid.
- **Given** the open GitHub issues at cutover (bugs and enhancements) **When**
  migrated **Then** each has a GitLab issue carrying the body, labels and a
  back-link, and the GitHub issue is closed with a forward-link.
- **Given** `sdlc fix all` **When** run **Then** its "bugs first, then
  enhancements" ordering reads GitLab labels.

**Technical Notes**: Epic-22 built the mirror host-agnostically. The migration
of ad-hoc issues (not stories) needs a small one-off script (`glab issue
create` from `gh issue list --json`); keep it in `scripts/` with a dry-run.
Issue numbers change; the `(#N)` tag convention in commit subjects refers to
the forge the commit was merged on — document that.

**Definition of Done**:
- [ ] Stories and open issues present on GitLab with labels
- [ ] Migration script committed with dry-run and a bats test
- [ ] GitHub issues closed with forward-links; README issue links updated

**Dependencies**: 33.1-001
**Risk Level**: Medium

##### Story 33.4-002: High-risk approval gate enforced on GitLab end-to-end
**User Story**: As FX relying on the human gate for CI, install-script and
security-sensitive changes, I want the template's `risk-gate` job, the merge
CI gate and the queue's approval park to work together on GitLab so that a
high-risk MR parks `AWAITING_APPROVAL` and merges only after the
`risk-approved` label, exactly as on GitHub.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** an MR touching `install/` **When** the pipeline runs **Then** the
  `risk-gate` job fails, the MR carries `risk:high`, and `sdlc fix` parks the
  story `AWAITING_APPROVAL` (deterministic recognition, Story 25.1-001, via
  the MR's labels + head-pipeline jobs).
- **Given** the `risk-approved` label applied **When** the queue polls
  **Then** the job is retried or re-evaluated, the pipeline goes green, and
  the run resumes to merge without a manual `sdlc resume`.
- **Given** the README gate stack **When** read **Then** the "enforced on
  GitHub only" caveat is gone and replaced by the GitLab mechanism.

**Technical Notes**: The template already fails the job until the label is
present, but the label does not re-run the job by itself (the template's
own comment: retry via `glab ci retry`). The approval probe
(`sdlc/approval.py`) must trigger that retry or the gate must be
re-evaluated by a label-event pipeline rule. Confirm the detector and
`high-risk-patterns.yaml` are read from the target branch, not the MR source
(the #640 trust-loop fix must hold on GitLab too).

**Definition of Done**:
- [ ] One real high-risk MR observed parking and resuming on label
- [ ] Detector/policy evaluated from the base ref on GitLab
- [ ] `docs/high-risk-gate.md` and README updated

**Dependencies**: 33.1-002
**Risk Level**: High

##### Story 33.4-003: Documentation of the reversed non-goal
**User Story**: As a reader of `docs/gitlab-adoption.md`, the README and
`docs/controller-architecture.md`, I want the framework's own forge described
accurately so that the "stays on GitHub" statements, the gate-stack caveats
and the install instructions match reality after cutover.
**Priority**: Should Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** `docs/gitlab-adoption.md` **When** read **Then** the Epic-23
  non-goal is replaced by a pointer to this epic and the cutover runbook.
- **Given** the README **When** read **Then** badges, clone URLs, the gate
  stack table and "Where it stands" describe GitLab as master and GitHub as
  mirror.
- **Given** `CLAUDE.md`'s Commit Format section **When** read **Then** the CI
  job that enforces commitlint is named for GitLab.

**Definition of Done**:
- [ ] Docs updated; `markdown-link-check` green
- [ ] Documentation-currency reviewer finds no stale statement on a sample MR

**Dependencies**: 33.3-002, 33.4-001
**Risk Level**: Low

### Feature 33.5: Cutover and Rollback

One rehearsed switch, one documented way back.

#### Stories

##### Story 33.5-001: Cutover runbook, rehearsal and rollback
**User Story**: As FX flipping the master forge, I want an ordered runbook
(sync `main` and tags, enable the GitLab pipeline, disable GitHub Actions,
swap remote names, run one `sdlc fix` end-to-end, enable the mirror) with a
verification checklist and a rollback that restores GitHub as master in
under ten minutes, so that the switch is an operation, not an incident.
**Priority**: Must Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the runbook **When** rehearsed on a throwaway GitLab project
  **Then** every step has a verification command and the rehearsal log is
  attached to the story.
- **Given** the live cutover **When** complete **Then** the success metrics
  above are all measured and recorded in the epic status line.
- **Given** a failure after cutover **When** the rollback runs **Then**
  `origin` is GitHub again, Actions are re-enabled, and no commit is lost
  (both forges' `main` compared before and after).

**Technical Notes**: The 2026-09-26 near-miss is the motivating case: local
`main` was ahead of GitLab and behind GitHub for an hour with a fix job
building against the stale side. The runbook's first step is therefore a
`git merge-base --is-ancestor` check in both directions.

**Definition of Done**:
- [ ] Runbook in `docs/` with rehearsal log
- [ ] Rollback rehearsed once
- [ ] Epic status line updated with measured metrics

**Dependencies**: 33.1-003, 33.2-002, 33.3-002, 33.4-002, 33.4-003
**Risk Level**: Medium
