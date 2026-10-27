# Epic 36: Buildable NFR Stories

> **Status: NOT STARTED (0/2)** — created 2026-10-27 from issue #901.
> generate-epics writes non-functional requirements as `### Story NFR-SEC-002:`
> stories in `docs/stories/non-functional-requirements.md`, but the controller
> discovers only `docs/stories/epic-*.md` and only numeric `N.N-NNN` ids. On
> local-ai-router-manager (2026-10-26) fleet jobs 32 and 33
> (`sdlc build NFR-SEC-002 NFR-INF-001 NFR-PERF-001 NFR-SEC-001 --enqueue
> --host m3max --auto`) failed at once with `scope 'nfr-sec-002,…' matched no
> stories`; the workaround was one GitLab issue per NFR story through
> `sdlc fix` (jobs 34–39). This epic makes NFR stories first-class for the
> controller and pins the generate-epics NFR template to the same story shape.

## Epic Overview

**Epic ID**: Epic-36
**Description**: Two halves of the framework disagree about what a story is.
The generate-epics NFR template (`generation-rules.md`, "NFR File Template")
emits `NFR-<AREA>-NNN` story headings and leaves their field lines to the
model; the controller's discovery, scope resolution, Done markers and issue
mirror all assume `epic-*.md` files and numeric ids. (36.1) teaches the
controller to discover, select, build and mark NFR stories, keeping the
`NFR-…` naming every existing project already uses. (36.2) makes the template
spell out the same field lines as an epic story and adds a test that runs
discovery over a file built from the template, so the two cannot drift again.
Changing NFR ids to numeric ones was rejected: it would break the naming in
every existing project and its STORIES.md overview.

**Business Value**: Every NFR story a project generates is unbuildable today,
so security, performance and infrastructure requirements — the ones most
often deferred — are the ones that fall out of the pipeline. Each needs a
hand-written issue and a `sdlc fix` run, without the build stage's story
context, review packet or Done marker.

**Success Metrics**:
- `sdlc build NFR-SEC-002 --enqueue` on a generate-epics project builds the
  story end to end and marks it Done.
- A discovery test over a template-generated NFR fixture passes in CI.

## Epic Scope
**Total Stories**: 2 | **Total Points**: 7 | **MVP Stories**: 2

## Features in This Epic

### Feature 36.1: Controller builds NFR stories

#### Stories

##### Story 36.1-001: Discover, select and mark NFR stories
**User Story**: As FX, I want `sdlc build` to find and build the `NFR-*`
stories generate-epics writes, so that non-functional requirements go
through the same pipeline as epic stories instead of hand-made issues.
**Priority**: Must Have
**Story Points**: 5

**Acceptance Criteria**:
- **Given** `docs/stories/non-functional-requirements.md` (or
  `stories/non-functional-requirements.md`) holds `### Story NFR-SEC-002:
  Title` headings **When** discovery runs **Then** each heading yields a story
  with id `NFR-SEC-002`, epic `nfr`, and its points, risk, dependencies and
  status parsed like an epic story's.
- **Given** an NFR file that is prose only, with no story headings (as in this
  repo) **When** discovery runs **Then** it yields no NFR stories and no error.
- **Given** a scope of `NFR-SEC-002`, `nfr-sec-002` or a mix with numeric ids
  **When** `sdlc build` resolves it, locally or through `--enqueue` **Then**
  each id matches its story regardless of case; a scope of `nfr` selects every
  NFR story, as `epic-03` selects an epic.
- **Given** an epic story that depends on `NFR-SEC-001`, or the reverse
  **When** the scheduler orders the run **Then** the dependency is honoured
  like any other story edge.
- **Given** an NFR story is merged **When** the controller writes its Done
  marker (build finalize or `sdlc reconcile`) **Then** the story's status line
  in the NFR file is updated, and its row in a `## NFR Progress` table when
  one exists; `find_epic_file` resolves an `NFR-` id to the NFR file.
- **Given** `sdlc issues init` / `sync` **When** it mirrors stories **Then**
  NFR stories get issues like epic stories, and `Closes #N` on their change
  requests works.
- **Given** a repo with no NFR file **When** any of the above runs **Then**
  behaviour is unchanged.

**Technical Notes**:
- Numeric-only id patterns: `_STORY_HEADER` in `discovery.py:14` and
  `story_markdown.py:12`; `_STORY_ID_SCOPE` in `discovery.py:40`.
- `epic-*.md` globs: `discovery.py:436`, `story_render.py:296`,
  `story_init.py:61`, `story_markdown.py:47`.
- `find_epic_file` (`story_markdown.py:30`) returns None for a non-numeric
  major; route an `NFR-` prefix to the NFR file instead.
- Scope is lowercased in `canonical_scope` (`discovery.py:373`) and
  `_resolve_scope_token` (`:402`). Compare ids case-insensitively but keep
  the canonical `NFR-SEC-002` spelling in the ledger, registry and branch
  names. A bare `nfr` does not substring-match `non-functional-requirements`
  (`:411`), so it needs its own branch.
- The portfolio view already sorts a non-numeric `nfr` epic
  (`test_portfolio.py:58`); keep that epic key.
- Prefer one id pattern shared by discovery and `story_markdown` over two
  copies that can drift again.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: None
**Risk Level**: Medium

### Feature 36.2: One story shape for epics and NFRs

#### Stories

##### Story 36.2-001: NFR template emits standard story fields
**User Story**: As FX, I want generate-epics to write NFR stories with the
same field lines as epic stories, and a test that feeds its template to the
controller's parser, so that a generated NFR file is always buildable.
**Priority**: Must Have
**Story Points**: 2

**Acceptance Criteria**:
- **Given** the "NFR File Template" in
  `plugins/autonomous-sdlc/skills/generate-epics/generation-rules.md`
  **When** it is read **Then** each NFR story carries the epic-story fields on
  their own lines: `**User Story**`, `**Priority**`, `**Story Points**`,
  acceptance criteria, `**Dependencies**`, `**Risk Level**`.
- **Given** a fixture NFR file built from that template **When** the
  controller's discovery and `story_inventory` parse it **Then** every story is
  found with its points and risk set; the test fails if the template and the
  parser drift apart.
- **Given** an existing NFR file in the one-line shape seen on
  local-ai-router-manager (`**Priority**: … · **Story Points**: N ·
  **Phase**: …`) **When** it is parsed **Then** the story is still found, and
  missing points or risk are reported as missing rather than guessed.
- **Given** the template change **When** the skill-parity gate runs **Then**
  every generated harness copy of the skill matches.

**Technical Notes**: `_POINTS` / `_RISK` (`story_inventory.py:16-19`) match
only a field at the start of its own line. The template today
(`generation-rules.md:98-120`) lists only headings, so the field layout is
whatever the model chose. Put the fixture where both the controller tests and
the skill can be checked against it.

**Definition of Done**:
- [ ] Code implemented and peer reviewed
- [ ] Tests written and passing
- [ ] User-facing docs updated in the same commit for behavior-changing diffs (README/docs/usage/help; CHANGELOG excluded — Epic-05 owns it)

**Dependencies**: 36.1-001
**Risk Level**: Low
