# Epic 22: Centralized Story Tracking — Mirror Every Story to GitHub or GitLab

> **Status: COMPLETE (11/11)** — created 2026-06-28. All 11 stories (22.1-001 → 22.6-001) are
> implemented, tested, and merged: the story inventory + migration, the `gh`/`glab` code-host adapter,
> host-aware issue rendering, idempotent story↔issue mapping, `sdlc issues init`/`sync`/`assign`, the
> build-loop close-link + live status, developer-identity resolution, and the portfolio dashboard panel.
> From FX's request for a centralized, multi-developer view
> of every epic and story (status + ownership), since the framework is being shared from a solo
> personal project into company team work. Two code hosts are in play by design: **GitHub** (FX's
> personal repos, where the framework was built) and **GitLab** (the company corporate standard). The
> design was shaped by hard push-back: the SQLite **ledger is a local, per-laptop, gitignored file**
> (`.sdlc-state.db`) and therefore *cannot* be the shared source of truth; the **MD files stay the
> human-readable spec**; so the **code host (GitHub *or* GitLab) becomes the shared coordination
> master**, and each ledger becomes a local projection of it. Issues are a **field-directional
> projection, not a third independent master** — each concern has exactly one writer. Epic-21 is being
> authored in a parallel session (number reserved); this is 22.
>
> **Scope boundary (important):** this epic makes the **issue/story mirror** code-host-agnostic. It does
> **not** make the autonomous *build pipeline* run on GitLab (Merge Requests instead of PRs, GitLab CI
> instead of GitHub Actions, release on GitLab, `glab mr diff` review) — that is a separate, larger
> future epic ("Pipeline on GitLab"). Epic-22 delivers the shared board first, on either host.

## Epic Overview

**Epic ID**: Epic-22
**Description**: Today the only shared, versioned record of a backlog is the Markdown spec
(`docs/stories/epic-*.md`, `STORIES.md`); execution state lives in a **local, per-laptop** SQLite
ledger that no teammate can see. That's fine for a solo owner but blind for a team. This epic makes
the **configured code host** (GitHub or GitLab) the shared coordination master: every story across
every epic is mirrored to an **issue**, organized on a board (GitHub **Projects** / GitLab **Boards**),
with status and ownership visible to all contributors. The mirror is a **projection with one writer per
field** — the MD spec owns the *definition* (rendered into a managed, do-not-edit issue block); the
build/ledger owns *execution status* (reflected via labels/board + a `Closes #N` close-link on each
story's PR/MR); and the **host owns ownership and human signals** (assignee, epic DRI, "blocked"/
"won't-do", discussion), which sync back into each laptop's ledger as a cached read. A thin **code-host
adapter** lets the same `sdlc issues …` commands target **`gh` (GitHub)** or **`glab` (GitLab)** — you
type the same command; it knows which host to call. A new **`sdlc issues init`** bootstrap stands up
the whole board for any repo — a fresh one or one being taken over — giving the *full* picture (done
stories included). **`sdlc issues assign`** assigns a single story *or* an entire epic (cascading to all
its stories). The framework dashboard (Epic-11) is extended with a portfolio view fed from the local
inventory cache.

**Business Value**: Unblocks multi-developer collaboration across FX's GitHub repos *and* the company's
GitLab. Contributors see one shared board (assign yourself or get assigned, avoid double-grabbing,
discuss in comments, watch status move as builds run); external/teammates get a host-native entry point;
FX gets an at-a-glance portfolio across all epics without opening 20 Markdown files or someone else's
laptop. Identity is free — each developer's own `gh auth`/`glab auth` is the identity, no passwords, no
shared infrastructure to operate (the host *is* the hosted shared store).

**Success Metrics**:
- `sdlc issues init` mirrors **100% of stories** across all epics into issues + a board on the
  configured host in one idempotent, resumable pass — re-running creates **zero duplicates**.
- The **same `sdlc issues …` commands work unchanged on GitHub and GitLab** (adapter swaps `gh`↔`glab`).
- A story's status moves **without manual status-setting**: a build transitions it and the merge
  auto-closes it via `Closes #N` in the PR/MR.
- `sdlc issues assign <epic-id> <user>` assigns **every story in that epic** in one command; a story-id
  assigns just that one.
- **No drift / no third master**: editing the managed spec block on the host is reverted on the next
  sync (MD wins); human fields (assignee, discussion, status labels) are never overwritten.
- The dashboard shows a **global portfolio** (every epic/story, status + owner) offline, from the local
  ledger cache the sync populates.

## Epic Scope

**Total Stories**: 11 | **Total Points**: 47 | **MVP Stories**: 0 (roadmap — Should Have)

## Out of Scope (Non-Goals)

- **Running the build *pipeline* on GitLab** — Merge Requests instead of PRs, GitLab CI instead of
  GitHub Actions, release on GitLab, `glab mr diff` adversarial review. This ripples through Epic-02
  (self-CI), Epic-05 (release), the build's PR creation, and the review path; it is a **separate future
  epic ("Pipeline on GitLab")**. Epic-22 only makes the *issue/story mirror* host-agnostic.
- **A central / hosted shared database.** No Postgres, no server. The host is the shared store; each
  ledger stays a local file.
- **Bidirectional sync of the *spec*.** The MD remains the definition master; edits to the managed
  issue block are overwritten on the next sync.
- **Reconciling pre-existing legacy issues** in a taken-over repo. The mirror only manages issues it
  created, identified by a hidden `<!-- sdlc-story: <id> -->` marker.
- **Syncing discussion back into the repo.** Comments / MR-review threads stay on the host.
- **A shared identity system / shared bot token.** Identity = each contributor's own `gh`/`glab` auth;
  a shared token is discouraged (it collapses attribution).
- **Changes to `fix-issue`/`resume-build-agents`** or the bug-oriented `create-issue` skill — those
  manage *bug* issues, not *story* mirrors.

## Features in This Epic

### Feature 22.1: Local story inventory — the projection foundation

Everything (host issues *and* the dashboard) renders from one local model: a `stories` table in the
ledger, projected from the MD specs. Coordinate with Epic-04 (owns the ledger) — extend, don't redefine.

#### Stories

##### Story 22.1-001: Story inventory schema + migration
**User Story**: As FX, I want a `stories` table in the ledger that can hold every story's id, epic,
status, owner, and host issue link so that the issue mirror and the dashboard share one local model.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** the ledger DDL/migrations **When** a new `stories` table is added **Then** it carries at
  least `story_id` (PK), `epic`, `feature`, `title`, `points`, `risk`, `status`, `owner`, `host`
  (`github`/`gitlab`), `issue_ref` (GitHub number / GitLab iid), `harness` (the harness summary for the
  story — see Technical Notes), and `updated_at`, applied through the existing auto-migration path.
- **Given** an existing ledger **When** the migration runs at launch **Then** it applies idempotently
  and older ledgers upgrade without data loss.

**Technical Notes**: Reuse Epic-04's ledger + the auto-apply-migrations mechanism (Story 12.2-003). The
table is a *cache*: `status`/`owner`/`issue_ref` are populated by sync and the build, not hand-edited.
`host`+`issue_ref` together identify the remote item (GitHub uses a repo-wide issue *number*; GitLab uses
a per-project *iid*). The `harness` summary is **derived, not a new source of truth**: for a *built*
story it is rolled up from the **per-stage `harness` already recorded in the ledger** (Epic-20 Story
20.2-002, e.g. `build:claude review:codex`); for an *unbuilt* story it is the **planned** harness from
the per-repo routing config (`.sdlc-harness.yaml` / `harnesses.yaml` default, Epic-20).

**Definition of Done**:
- [x] Schema + migration implemented and peer reviewed
- [x] Tests: fresh-create, backward-compat upgrade
- [x] `docs/controller-architecture.md` updated

**Dependencies**: None
**Risk Level**: Medium

##### Story 22.1-002: Project the MD specs into the inventory
**User Story**: As FX, I want every story in the MD specs loaded into the inventory so that the local
model reflects the full backlog, not just stories that have build runs.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `docs/stories/epic-*.md` + `STORIES.md` **When** the projector runs **Then** every story
  (`NN.F-NNN`) is upserted into `stories` with its epic/feature/title/points/risk parsed from the file.
- **Given** a re-run after the MD changed **When** the projector runs **Then** rows are updated in place
  (idempotent), new stories added, removed stories flagged (not silently dropped).
- **Given** a story already linked to an issue **When** projected **Then** its `host`/`issue_ref`/`owner`
  cache is preserved.

**Technical Notes**: A parser over the established epic format. Reuse the dependency-line parsing
discipline from Story 12.5-001 (parse intended structure, not prose). Same inventory the host mirror and
dashboard read.

**Definition of Done**:
- [x] Projector implemented and peer reviewed
- [x] Tests: full-parse of a sample epic, idempotent re-run, added/removed-story handling
- [x] Docs updated

**Dependencies**: 22.1-001
**Risk Level**: Medium

### Feature 22.2: Code-host-agnostic issue mirror (GitHub + GitLab)

One thin adapter, two hosts; idempotent story↔issue mapping with a managed (MD-owned) body block.

#### Stories

##### Story 22.2-001: Code-host adapter — GitHub (`gh`) + GitLab (`glab`)
**User Story**: As FX running on GitHub personally and GitLab at the company, I want one adapter
interface with a GitHub and a GitLab implementation so that the same `sdlc issues …` commands work on
either host.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** an adapter interface (`issue_create`, `issue_update`, `issue_assign`, `issue_close`,
  `issue_find`, `whoami`) **When** the host is GitHub **Then** calls route through `gh`; **When** the
  host is GitLab **Then** they route through `glab` — same inputs/outputs, host differences hidden.
- **Given** host selection (auto-detected from the repo remote, overridable via config/flag) **When** a
  command runs **Then** the correct adapter is chosen; an unsupported/unauthenticated host fails fast
  with a clear message.
- **Given** the `Closes #N` close-keyword **When** rendered **Then** it is emitted in the host's correct
  form (GitHub PR / GitLab MR both accept `Closes #N` for issues).

**Technical Notes**: Mirror Epic-20's adapter philosophy (swap the CLI behind a stable interface).
Normalize GitHub *issue number* vs GitLab *iid* + project path behind `issue_ref`. `glab` is GitLab's
official CLI (issues, MRs, auth). No build-pipeline changes here — issue operations only.

**Definition of Done**:
- [x] Adapter interface + GitHub and GitLab implementations, peer reviewed
- [x] Tests: each verb on both adapters (mocked `gh`/`glab`), host auto-detect, unauth fail-fast
- [x] Docs: the adapter contract + "choosing the host"

**Dependencies**: None
**Risk Level**: High

##### Story 22.2-002: Issue rendering + board/label taxonomy (host-aware)
**User Story**: As a contributor, I want each story's issue to show its spec and carry consistent
labels/board fields so that the board is filterable by epic, feature, points, and risk on either host.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** an inventory row **When** its issue body is rendered **Then** the spec (user story, AC, DoD,
  points, risk) sits inside a `<!-- managed: do not edit -->` … `<!-- /managed -->` block plus a hidden
  `<!-- sdlc-story: <id> -->` marker.
- **Given** the taxonomy **When** an issue is created/updated **Then** it carries a **`story` label**
  (marking it a framework-managed story issue, distinct from `bug`/`create-issue` issues and humans' own
  issues, and the fast filter for `sdlc issues sync --label story`), plus `epic:NN`, `feature:NN.F`,
  `points:N`, `risk:*`, and a Status surface: a **GitHub Project (v2) Status field**, or a **GitLab Issue
  Board** with epics mapped to **labels + milestones** (the company GitLab is **Free/Core**, so no
  Premium-only constructs are used).
- **Given** the `story` label vs the hidden `<!-- sdlc-story: <id> -->` marker **When** sync identifies
  managed issues **Then** the label is the coarse human/list filter and the marker is the exact-id match
  (the label never replaces the marker as the source of identity).
- **Given** the **GitHub** host **When** an issue is added to the Project **Then** its story points are
  also written to a Projects v2 custom **number field `Points`** (enabling native velocity/roll-up
  views); on **GitLab Free** the `points:N` label remains the only points surface (no native numeric
  field outside Premium "weight").
- **Given** the managed block was hand-edited on the host **When** the next sync runs **Then** the block
  is regenerated from the MD (MD wins); comments and assignee are untouched.

**Technical Notes**: Body generation is pure (inventory row → markdown). **The company GitLab tier is
Free/Core (confirmed)** — so the GitLab path must avoid all Premium/Ultimate features: **no native
Epics** (epics → an `epic:NN` label + a milestone, shown on an Issue Board), **no issue "weight"**
(points stay a `points:N` label), and treat ownership as a **single assignee** (multiple assignees are
Premium). GitHub uses Projects v2 + its Status field **and a custom number field `Points`** (native
velocity/roll-up; GitLab Free has no numeric field, so its `points:N` label is the only points surface).
The `points:N` **label is the portable cross-host baseline**; the GitHub number field is an additive
GitHub-only nicety. Body markdown is host-neutral; only the status/board surface differs per host.

**Definition of Done**:
- [x] Renderer + taxonomy implemented and peer reviewed
- [x] Tests: managed-block round-trip, marker present, managed-edit reversion, GitHub vs GitLab status surface
- [x] Docs: the label/board schema per host

**Dependencies**: 22.1-002
**Risk Level**: Medium

##### Story 22.2-003: Idempotent story ↔ issue mapping
**User Story**: As FX, I want each story mapped to exactly one host issue so that re-running the mirror
updates issues instead of creating duplicates.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a story with no issue **When** the mirror runs **Then** it creates one issue via the adapter,
  records `host`+`issue_ref` in the inventory, and writes the `<!-- sdlc-story: <id> -->` marker.
- **Given** a story already mapped **When** the mirror runs again **Then** it **updates** that issue (no
  duplicate), matching by inventory `issue_ref` first and the body marker as a fallback.
- **Given** a mapped issue deleted on the host **When** the mirror runs **Then** the orphan is detected
  and re-created (or flagged), not silently lost.

**Technical Notes**: Mapping lives in the inventory (`host`+`issue_ref`) and is recoverable from the body
marker via the adapter's `issue_find`. All host calls via the adapter, batched and rate-limit aware.

**Definition of Done**:
- [x] Mapping implemented and peer reviewed
- [x] Tests: create-once, update-not-duplicate, marker-fallback match, orphan re-create — both hosts
- [x] Docs updated

**Dependencies**: 22.2-001, 22.2-002
**Risk Level**: High

### Feature 22.3: Bootstrap any repo's board

One command to stand up the full board for a fresh or taken-over repo — the *full* view, done included.

#### Stories

##### Story 22.3-001: `sdlc issues init` — full backfill
**User Story**: As FX adopting a repo, I want one command to create the board, labels, and an issue for
every epic/story on the configured host so that I get the complete picture immediately, not just open work.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a repo with framework-format stories **When** `sdlc issues init` runs **Then** it provisions
  the board + taxonomy and creates an issue (via the adapter) for **every** story across **every** epic,
  recording the inventory mapping.
- **Given** a story whose status is **Done** **When** init creates its issue **Then** the issue is
  created **and immediately closed** (board Status = Done), so the board shows full history while the
  open-issues list stays = real remaining work.
- **Given** init is interrupted (or hits a rate limit) **When** re-run **Then** it resumes idempotently —
  already-mapped stories skipped/updated, none duplicated.
- **Given** a repo with no framework-format stories **When** init runs **Then** it exits with a clear
  message pointing to `generate-epics` first.

**Technical Notes**: Under a new `sdlc issues` command group. **Do not** reuse the bare `init` verb
(Epic-10 deliberately removed it). Batch creation, honour secondary rate limits (GitHub *and* GitLab),
persist progress to the inventory so a resume is cheap.

**Definition of Done**:
- [x] `sdlc issues init` implemented and peer reviewed
- [x] Tests: full backfill, done→closed, resume-after-interrupt, no-stories guidance — both hosts
- [x] Docs: an "adopt a repo" walkthrough (GitHub + GitLab)

**Dependencies**: 22.1-002, 22.2-003
**Risk Level**: High

### Feature 22.4: Field-directional status sync

Push spec+status to the host, pull ownership+human-signals back — one writer per field.

#### Stories

##### Story 22.4-001: `sdlc issues sync` — reconcile (push + pull)
**User Story**: As a team, I want a reconcile command that keeps issues and the ledger consistent so that
the board reflects reality and the local cache reflects the board.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** an MD/ledger change **When** sync runs **Then** it **pushes** the managed spec block and the
  ledger-derived status (labels/board) to each story's issue via the adapter.
- **Given** a human set the assignee or a `blocked`/`wontfix` label on the host **When** sync runs **Then**
  it **pulls** those into the inventory (`owner`, human-status), and the build respects them (e.g. skips
  `wontfix`).
- **Given** repeated syncs with no changes **When** they run **Then** they are no-ops — push writes only
  managed fields, pull reads only human fields, so there is **no echo loop**.

**Technical Notes**: Strictly field-directional — this is what prevents drift. Pull is the *only*
write-back into the ledger from the host. Idempotent, batched, resumable; the same engine `init` builds on.

**Definition of Done**:
- [x] Sync implemented and peer reviewed
- [x] Tests: push managed block, pull assignee/labels, no-op idempotency, wontfix-skip — both hosts
- [x] Docs: the field-ownership / direction table

**Dependencies**: 22.2-003
**Risk Level**: High

##### Story 22.4-002: Build-loop integration — close-link + live status
**User Story**: As a contributor, I want a story's issue to move on its own as the build runs so that
status is truthful without anyone setting it by hand.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** a story has a mapped issue **When** the build opens its PR/MR **Then** the description includes
  `Closes #<issue>`, so a merge auto-closes the story issue.
- **Given** a stage transition (building / in-review / NEEDS_ATTENTION) **When** it occurs **Then** the
  controller updates the issue's status label/field and posts a short comment, attributed to the running
  developer's host identity.
- **Given** the build runs with no mapped issue **When** it dispatches **Then** behaviour is unchanged —
  issue updates are best-effort and never block a build.

**Technical Notes**: Hook the existing per-story PR creation (add `Closes #N`) and the ledger
stage-transition points (`notify.py`/`sdlc-state-emit.sh`) — touches `build.py`, so land it when no long
run is active. Best-effort: a host failure logs and continues. NB: on GitLab the build opens an **MR**,
which is part of the *separate "Pipeline on GitLab"* epic — until that ships, the close-link lands on the
GitHub PR path; the issue-status comments work on both hosts via the adapter.

**Definition of Done**:
- [x] Build integration implemented and peer reviewed
- [x] Tests: PR carries Closes #N, status comment on transition, no-issue no-op, host-failure tolerated
- [x] Docs updated (incl. the GitLab-MR dependency note)

**Dependencies**: 22.2-003
**Risk Level**: High

### Feature 22.5: Identity, ownership & assignment

Who's doing what — from each developer's own host auth, with a real assign command.

#### Stories

##### Story 22.5-001: Resolve developer identity; cache owner & actor
**Status**: Done
**User Story**: As a team, I want each story's owner and each run's actor recorded from host identity so
that we know who is doing what without passwords or shared accounts.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** a developer with host auth configured **When** the controller needs an identity **Then** it
  resolves their login via the adapter's `whoami` (`gh api user` / `glab` equivalent) and stamps each
  story-run's `actor` in the ledger.
- **Given** an issue with an assignee (or an epic with a DRI) **When** sync pulls **Then** the inventory
  `owner` reflects it, so a local build can show/skip by owner without an API call.
- **Given** no host auth **When** identity is needed **Then** it degrades gracefully (actor `unknown`),
  not a crash.

**Technical Notes**: The host is the identity provider — no custom auth. Flag the shared-token
anti-pattern in docs. `owner` is a cached read of the host assignee; the assignee on the host stays
authoritative.

**Definition of Done**:
- [x] Identity resolution + owner/actor cache implemented and peer reviewed
- [x] Tests: login resolution (both hosts), assignee→owner cache, no-auth degradation
- [x] Docs: the identity/attribution model

**Dependencies**: 22.2-001, 22.2-003
**Risk Level**: Medium

##### Story 22.5-002: `sdlc issues assign` — assign a story or a whole epic
**Status**: Done
**User Story**: As FX, I want to assign a single story *or* an entire epic to someone so that I can hand
off a whole area of work in one command and everyone knows who owns it.
**Priority**: Should Have
**Story Points**: 3

**Acceptance Criteria**:
- **Given** `sdlc issues assign <story-id> <user>` **When** run **Then** the adapter sets that story
  issue's assignee on the host, and the inventory `owner` updates.
- **Given** `sdlc issues assign <epic-id> <user>` (e.g. `epic-22`) **When** run **Then** it **cascades**:
  every story in that epic is assigned to `<user>` (and the epic's DRI is set), in one batched, resumable
  call.
- **Given** an unknown user or a story with no mapped issue **When** run **Then** it fails fast / reports
  the unmapped stories rather than partially silently succeeding.
- **Given** assignment **When** it completes **Then** GitHub/GitLab remains authoritative (the command
  writes *to* the host; the ledger `owner` is the cached read).

**Technical Notes**: A thin command over the adapter's `issue_assign`. Epic-cascade enumerates the epic's
stories from the inventory (22.1-002). Keep it idempotent (re-assigning the same user is a no-op). This is
the human-write-back lane of the projection — the one place a CLI writes ownership to the host.

**Definition of Done**:
- [x] `sdlc issues assign` (story + epic-cascade) implemented and peer reviewed
- [x] Tests: single-story assign, epic cascade, unknown-user/unmapped fail-fast, idempotent re-assign — both hosts
- [x] Docs: assignment usage

**Dependencies**: 22.2-001, 22.2-003, 22.1-002 (epic→stories enumeration)
**Risk Level**: Medium

### Feature 22.6: Portfolio dashboard view

Surface the whole backlog (status + owner) in the framework's own dashboard — extend Epic-11, don't rebuild.

#### Stories

##### Story 22.6-001: All-epics/all-stories portfolio panel
**User Story**: As FX, I want the dashboard to show every epic and story with its status and owner so that
I have one at-a-glance portfolio across the whole project.
**Priority**: Should Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** the inventory (populated by sync) **When** the dashboard renders the portfolio panel **Then**
  it lists every epic and its stories with status and owner, grouped by epic.
- **Given** a story's `harness` summary **When** the panel renders **Then** each story shows a **harness
  badge** (Claude / Codex / qwen) — the *actual* per-stage harness for a built story (from the ledger,
  Epic-20 20.2-002), or the *planned* harness for an unbuilt one — and each epic shows a per-harness
  **roll-up** (e.g. "Epic-13: 4 on Codex, 1 on Claude").
- **Given** the panel **When** viewed offline **Then** it renders from the **local ledger cache** (no live
  host call required); an optional refresh pulls the latest.
- **Given** Epic-11's existing dashboard **When** the panel is added **Then** it is a new view/panel that
  reuses the existing dashboard server/registry, not a parallel dashboard.

**Technical Notes**: Coordinate with Epic-11 (dashboard owner). The panel reads the `stories` inventory;
ownership comes from the `owner` cache (22.5-001). Host-agnostic — it shows whatever the inventory holds,
regardless of GitHub vs GitLab origin.

**Definition of Done**:
- [x] Portfolio panel implemented and peer reviewed
- [x] Tests: render from cache, offline behaviour, integration with the existing dashboard
- [x] Docs updated

**Dependencies**: 22.1-002, 22.5-001
**Risk Level**: Medium

## Story Dependencies (within Epic-22)

```
22.1-001 (inventory schema) ─> 22.1-002 (project MD) ─┬─> 22.2-002 (render/taxonomy) ─┐
                                                       │                               ▼
22.2-001 (code-host adapter gh+glab) ──────────────────┴────────────────────────> 22.2-003 (idempotent map) ─┬─> 22.3-001 (init)
                                                                                                              ├─> 22.4-001 (sync)
                                                                                                              ├─> 22.4-002 (build close-link + status)
                                                                                                              ├─> 22.5-001 (identity & owner)
                                                                                                              └─> 22.5-002 (assign story|epic)
22.1-002 + 22.5-001 ─────────────────────────────────────────────────────────────────────────────────────> 22.6-001 (dashboard)
```

- **Cohort 1** (no deps): 22.1-001, 22.2-001 (adapter)
- **Cohort 2**: 22.1-002 (needs 22.1-001)
- **Cohort 3**: 22.2-002 (needs 22.1-002)
- **Cohort 4**: 22.2-003 (needs 22.2-001, 22.2-002)
- **Cohort 5** (all build on the idempotent map): 22.3-001, 22.4-001, 22.4-002, 22.5-001, 22.5-002
- **Cohort 6**: 22.6-001 (needs 22.1-002 + 22.5-001 for owner)

> Cross-epic: **Epic-04** owns the SQLite ledger — 22.1 *extends* it with a `stories` inventory table.
> **Epic-20** records the per-stage `harness` (Story 20.2-002) — 22.1/22.6 *surface* it as a per-story/
> per-epic harness badge (Claude/Codex/qwen), they don't re-record it.
> **Epic-11** owns the dashboard — 22.6 *adds a panel*, reusing its server/registry. **Epic-05** owns
> PR/release conventions — 22.4-002's `Closes #N` rides the existing per-story PR. The bug-oriented
> `create-issue`/`fix-issue` skills are separate. **A future "Pipeline on GitLab" epic** owns running the
> *build* on GitLab (MRs, GitLab CI, release, `glab mr diff`); Epic-22 only mirrors *issues*. **Epic-21**
> is authored in a parallel session; STORIES.md counters may need reconciliation at merge — the very
> drift this epic exists to remove.

## Epic Complete When

- `sdlc issues init` mirrors every story across every epic into issues + a board, on **GitHub or
  GitLab**, in one idempotent, resumable pass — zero duplicates; Done stories show as closed.
- The same `sdlc issues …` commands run unchanged on both hosts (adapter swaps `gh`↔`glab`).
- A build moves a story's status on its own and the merge auto-closes it via `Closes #<issue>`.
- `sdlc issues assign` assigns a single story, and an epic-id cascades to all its stories; assignee/owner
  is visible on the board and the dashboard, attributed to each developer's own host identity.
- Editing the managed spec block on the host is reverted on the next sync (MD wins); human fields are
  never overwritten — no drift, no third master.
- The dashboard renders a global portfolio (every epic/story, status + owner) from the local ledger
  cache, offline.
