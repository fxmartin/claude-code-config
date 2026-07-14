<!-- ABOUTME: The code-host adapter contract (gh / glab) + how the host is chosen (Story 22.2-001). -->
<!-- ABOUTME: One interface, two backends; the same `sdlc issues …` ops route to GitHub or GitLab. -->

# Code-host adapters (GitHub + GitLab)

The story mirror (Epic-22) treats the **configured code host** — GitHub *or*
GitLab — as the shared coordination master. So that the same `sdlc issues …`
commands work on either, every host call goes through **one adapter interface**
([`controller/src/sdlc/issue_host.py`](../controller/src/sdlc/issue_host.py))
with a GitHub backend over [`gh`](https://cli.github.com/) and a GitLab backend
over [`glab`](https://gitlab.com/gitlab-org/cli). This mirrors Epic-20's harness
philosophy: **swap the CLI behind a stable interface**; callers never branch on
the host.

> **Scope:** the same adapter covers **two** verb families — *issue* ops
> (Epic-22) and *change-request* (PR/MR) ops (Epic-23, Story 23.1-001, the
> [`cr_*` contract](#change-request-prmr-operations-story-231-001) below). It does
> **not** yet wire the build *pipeline* to those CR verbs (opening MRs in the
> loop, the GitLab-CI merge gate, `glab mr diff` review) — those are the later
> Epic-23 stories that route through this seam.

## The contract

`IssueHostAdapter` is the interface; `GitHubAdapter` and `GitLabAdapter`
implement it. Every verb takes and returns **host-neutral** values, so a caller
written against the interface is identical on both hosts:

| Verb | What it does | `gh` | `glab` |
|------|--------------|------|--------|
| `whoami()` | the authed login/username | `gh api user --jq .login` | `glab api user --jq .username` |
| `ensure_ready()` | verify CLI is installed + authed, return login | `gh auth status` → `whoami` | `glab auth status` → `whoami` |
| `issue_create(title, body, labels, assignee)` | create an issue, return it with `ref` | `gh issue create` | `glab issue create --yes` |
| `issue_update(ref, title, body, labels)` | edit title/body, add labels | `gh issue edit` | `glab issue update` |
| `issue_assign(ref, assignee)` | set a single assignee | `gh issue edit --add-assignee` | `glab issue update --assignee` |
| `issue_close(ref)` | close the issue | `gh issue close` | `glab issue close` |
| `issue_find(marker)` | find a managed issue by its hidden marker | `gh issue list --search` | `glab issue list --search` |
| `issue_view(ref)` | fetch one issue *with* body + labels + assignees | `gh issue view --json …` | `glab issue view --output json` |
| `user_exists(user)` | is this a real host account? (assign fail-fast) | `gh api users/<u>` (404 ⇒ no) | `glab api users?username=<u>` (empty ⇒ no) |
| `close_keyword(ref)` | the PR/MR close-link line | `Closes #N` | `Closes #N` |

### Normalised types

- **`issue_ref`** — a string that hides the GitHub *issue number* vs the GitLab
  per-project *iid*. The inventory stores `host` + `issue_ref` together (Story
  22.1-001) to identify the remote item. The adapter parses the ref out of the
  created-issue URL (`…/issues/123`, GitLab `…/-/issues/5`).
- **`Issue`** — a host-neutral record: `host`, `ref`, `url`, `title`, `state`
  (normalised to `open`/`closed` — GitHub `OPEN`/`CLOSED`, GitLab
  `opened`/`closed`), and `assignees` (a tuple of login/username strings).
  `body` and `labels` are populated only by `issue_view` (the reconcile reads
  them); the other verbs leave them empty.
- **`close_keyword`** — both GitHub PRs and GitLab MRs auto-close an issue in
  the same project on merge with `Closes #N`, so the form is shared. (On GitLab
  the build opens an MR, which belongs to the separate Pipeline-on-GitLab epic;
  until then the close-link rides the GitHub PR path.)

### GitLab tier note

The company GitLab is **Free/Core**, so the GitLab path avoids all
Premium/Ultimate constructs: a **single assignee** (multiple assignees are
Premium), and **labels** for taxonomy rather than native epics or issue weight.

## Change-request (PR/MR) operations (Story 23.1-001)

The same adapter hides **change-request** ops, so the build loop opens / diffs /
status-checks / merges a change without knowing whether the host calls it a
GitHub **Pull Request** or a GitLab **Merge Request**. This is the seam every
later Epic-23 story routes through.

| Verb | What it does | `gh pr` | `glab mr` |
|------|--------------|---------|-----------|
| `cr_create(source_branch, title, body, target_branch=None, draft=False)` | open a PR/MR for the story branch; `body` carries the `Closes #N` link | `gh pr create --head … [--base …] [--draft]` | `glab mr create --source-branch … --yes [--target-branch …] [--draft]` |
| `cr_diff(ref)` | the unified diff (the adversarial-review feed) | `gh pr diff` | `glab mr diff` |
| `cr_status(ref)` | the normalised CI status (the merge gate polls it) | `gh pr view --json statusCheckRollup` | `glab mr view --output json` → `.pipeline.status` |
| `cr_merge(ref)` | merge the change (the `Closes #N` auto-closes the story issue) | `gh pr merge --merge` | `glab mr merge --yes` |
| `cr_url(ref)` | the change's web URL | `gh pr view --json url` | `glab mr view --output json` → `.web_url` |

### Normalised CR types

- **`cr_ref`** — a string hiding the GitHub PR *number* vs the GitLab MR *iid*.
  The adapter parses it out of the created-CR URL (`…/pull/123`, GitLab
  `…/-/merge_requests/5`).
- **`ChangeRequest`** — a host-neutral record: `host`, `ref`, `url`, `title`,
  `state` (`open`/`closed`/`merged`), `source_branch`, `target_branch`, and
  `status` (the normalised CI signal, populated only by `cr_status`).
- **`cr_status`** — five normalised values shared by both hosts, so the merge
  gate (Story 23.2-002) is host-agnostic:

  | Value (`CR_*`) | Meaning | GitHub rollup | GitLab pipeline |
  |----------------|---------|---------------|-----------------|
  | `success` | green — the gate may merge | every check `SUCCESS` | `success` |
  | `failed` | a check/the pipeline failed (or was canceled) — block merge | any `FAILURE`/`TIMED_OUT`/… | `failed`, `canceled` |
  | `pending` | still in flight — keep polling | any `QUEUED`/`IN_PROGRESS` | `running`/`pending`/`created`/`manual`/… |
  | `none` | no gating signal — no checks, no pipeline, or only skipped/neutral | empty or only `SKIPPED`/`NEUTRAL` | `null` pipeline or `skipped` |
  | `unknown` | a status this adapter does not recognise | unmapped value | unmapped value |

- **`Closes #N`** — `cr_create`'s `body` carries the close-link (from
  `close_keyword`); merging the PR/MR auto-closes the story issue in the same
  project on **both** hosts.

The **GitHub path is unchanged** — a GitHub remote routes every CR verb through
`gh pr`, byte-for-byte as before. On GitLab everything stays inside **Free/Core**:
no merge trains, no Premium-only keywords.

## Choosing the host

`resolve_host(root, override=None)` decides which backend to use:

1. **Explicit override wins** — a `--host github|gitlab` flag or config value.
2. **Otherwise auto-detect** from `git remote get-url origin`: the hostname is
   matched as a substring, so `github.com`, `gitlab.com`, and self-hosted
   `gitlab.corp.internal` all resolve.
3. **Fail fast** — an undeterminable host ("could not determine code host …")
   or an unsupported one ("unsupported host …") raises `IssueHostError` with a
   clear message rather than silently targeting the wrong forge. An
   unauthenticated CLI fails the same way via `ensure_ready()`.

```python
from sdlc.issue_host import get_adapter, resolve_host

host = resolve_host(".", override=cli_flag)   # "github" | "gitlab"
adapter = get_adapter(host)
adapter.ensure_ready()                          # raises if the CLI is absent/unauthed
issue = adapter.issue_create("Story 22.2-001", body, labels=["story", "epic:22"])
```

## Issue rendering & the label/board taxonomy (Story 22.2-002)

The body and labels of a managed story issue are generated by a **pure**
renderer ([`controller/src/sdlc/story_render.py`](../controller/src/sdlc/story_render.py)):
a `StoryDoc` (the spec parsed from the MD) in, markdown + labels out. The body
markdown is **host-neutral** — only the status/board *surface* differs per host.

### The managed body block

Every story issue's spec lives inside a **managed region** that the sync
regenerates from the MD on every pass (**MD wins**):

```
<!-- managed: do not edit -->
<!-- sdlc-story: 22.2-002 -->

…the story's verbatim spec: user story, AC, DoD, points, risk…

<!-- /managed -->
```

- The hidden **`<!-- sdlc-story: <id> -->` marker** is the **source of identity**
  (the exact-id match `issue_find` uses). The `story` label is only the coarse
  human/list filter and never replaces the marker.
- Content **outside** the markers (human comments, discussion) is **never
  touched**: `replace_managed_block()` rewrites only the region between them, and
  a hand-edit *inside* the region is reverted on the next sync. A human-created
  issue with no region gets one appended, preserving its existing prose.
- The issue **title** is `"<id>: <human title>"`; the `##### Story` header line
  is excluded from the body since it becomes the title.

### Labels — the portable cross-host baseline

`story_labels(epic, feature, points, risk)` returns the same labels on either
host:

| Label | Meaning |
|-------|---------|
| `story` | framework-managed story issue (distinct from `bug`/own issues); enables a fast host-side filter (`gh issue list --label story`) |
| `epic:NN` | the epic, e.g. `epic:22` |
| `feature:NN.F` | the feature, e.g. `feature:22.2` |
| `points:N` | story points — the **only** points surface on GitLab Free (omitted when unknown) |
| `risk:*` | `risk:low` / `risk:medium` / `risk:high` (omitted when unknown) |

### Status surface — per host

`status_surface(host, epic, feature, points, risk)` returns the host-specific
board fields on top of those labels:

| Surface | GitHub | GitLab (Free/Core) |
|---------|--------|--------------------|
| Status | Projects v2 **`Status` field** | Issue Board column (via labels) — `status_field` is `None` |
| Points | custom number field **`Points`** (native velocity/roll-up) | none outside Premium "weight" — the `points:N` label only (`points_field` is `None`) |
| Epic grouping | the `epic:NN` label | the `epic:NN` label **+ an `epic-NN` milestone** on the Issue Board |

The `points:N` label is the additive-safe baseline; the GitHub `Points` number
field is a GitHub-only nicety. An unsupported host raises `IssueHostError`.

## Reconcile — field-directional sync (Story 22.4-001)

The field-directional sync engine keeps each story's issue and the local ledger
consistent without drift. It ships as a tested library, not a CLI verb — there
is no `sdlc issues sync` command today. At runtime, `sdlc issues init` backfills
the board, and the build loop keeps each story's issue current via Story
22.4-002: a `status:<slug>` label + comment as the story moves through the
coarse states `building → in-review → merging` (build and coverage share
`building`, and duplicate transitions are deduplicated), plus auto-close on
merge through the `Closes #N` link. The engine
([`controller/src/sdlc/story_sync.py`](../controller/src/sdlc/story_sync.py))
is **strictly field-directional** — every field has exactly one writer, which is
what makes a repeated sync a no-op (no echo loop):

| Field | Direction | Writer | How |
|-------|-----------|--------|-----|
| managed spec block | **push** (MD → host) | the MD spec | `replace_managed_block` rewrites only the managed region; human content outside it is preserved, a hand-edit inside it is reverted |
| taxonomy labels (`story`, `epic:NN`, `feature:NN.F`, `points:N`, `risk:*`) | **push** (inventory → host) | the inventory spec | only *missing* labels are added, so an unchanged set writes nothing |
| `status:<slug>` label | **push** (ledger → host) | the build/ledger execution status | rendered from the cached `status` (`DONE` → `status:done`); absent when no status is cached |
| `owner` | **pull** (host → ledger) | the host assignee | the issue's single assignee is cached as `owner` |
| `human_status` (`blocked` / `wontfix`) | **pull** (host → ledger) | a human's label | a `blocked`/`wontfix` label is cached; the build **skips** a `wontfix` story (`blocked` is surfaced but still worked) |

**Push** writes only managed fields; **pull** reads only human fields and is the
*only* write-back into the ledger from the host. So a second pass with no real
change touches nothing on the host (`NOOP`). A story not yet mapped on the host
is skipped (`UNMAPPED`) — the idempotent mirror (Story 22.2-003) must create the
issue first. This is the same reconcile engine `sdlc issues init` (Story
22.3-001) builds on.

## Adopt a repo — `sdlc issues init` (Story 22.3-001)

`sdlc issues init` is the one command to stand up the **full** board for a fresh
or taken-over repo. It backfills an issue for **every** story across **every**
epic — the complete picture, *done included* — not just open work.

```text
sdlc issues init [--host github|gitlab] [--root PATH] [--db PATH]
```

What it does, in order:

1. **Projects every story** from `docs/stories/epic-*.md` into the local
   inventory cache (the spec rows the mapping is recorded onto).
2. **Backfills** — mirrors every story via the idempotent engine: each gets one
   issue carrying the [taxonomy labels](#labels--the-portable-cross-host-baseline)
   and the hidden `<!-- sdlc-story: <id> -->` marker, with its `host` + `issue_ref`
   recorded. A re-run **updates** rather than duplicates.
3. **Closes the Done stories** — a story whose `**Status**:` is `Done` (or whose
   Definition-of-Done checklist is fully checked) is **created and immediately
   closed**, so the board shows full history while the open-issues list stays
   equal to the real remaining work.

It is **idempotent**: an interrupted or rate-limited run just gets re-run —
already-mapped stories are updated, never duplicated, so the resume is cheap.

| Flag | Default | Meaning |
|------|---------|---------|
| `--host` | auto-detect from `origin` | Force the host when the remote is ambiguous/absent. |
| `--root` | current directory | Repo root holding `docs/stories/`. |
| `--db` | `./.sdlc-state.db` | Ledger DB path (created if absent). |

**Exit codes:** `0` on success; `1` when the repo has no framework-format stories
(the message points at `generate-epics` first); `2` when the host can't be
determined/is unsupported, or the host CLI isn't authenticated.

### Walkthrough — GitHub

```bash
gh auth login                 # gh is the identity + transport (no shared token)
cd ~/code/my-repo
sdlc issues init
# init github: 42 story(ies) backfilled (42 created); 9 Done issue(s) closed.
```

GitHub also gets the Projects v2 `Status` field and the `Points` number field
described above.

### Walkthrough — GitLab (Free/Core)

```bash
glab auth login               # same model — your own glab auth
cd ~/code/my-repo
sdlc issues init              # a gitlab remote routes to glab automatically
# init gitlab: 42 story(ies) backfilled (42 created); 9 Done issue(s) closed.
```

On GitLab Free the epic surfaces as an `epic:NN` label + an `epic-NN` milestone
on the Issue Board; points stay the `points:N` label. For self-hosted GitLab the
host resolves by hostname substring — pass `--host gitlab` if detection can't
tell from the remote.

### Re-running after an interruption

```bash
sdlc issues init              # a 429 or Ctrl-C stopped the first pass? re-run it
# init github: 42 story(ies) backfilled (42 updated); 9 Done issue(s) closed.
```

The second pass reports `updated` instead of `created` — each story resolves to
its existing issue, so nothing is duplicated and the Done issues stay closed.

## Assigning a story or an epic (`sdlc issues assign`, Story 22.5-002)

Ownership is the **human-write-back lane** of the projection — the one place a CLI
writes *to* the host. The host (GitHub/GitLab) stays authoritative; the inventory
`owner` is a cached read.

```bash
sdlc issues assign 22.5-002 alice        # assign one story
sdlc issues assign epic-22 bob           # cascade: every story in epic-22 → bob
sdlc issues assign 22.5-002 alice --host gitlab   # force the host (else auto-detect)
```

- **Target** is a story id (`NN.F-NNN`) or an epic id (`epic-NN`). An `epic-NN`
  target **cascades** to every story in that epic (enumerated from the inventory,
  Story 22.1-002) in one idempotent pass — assigning the epic is how its DRI is set.
- **Fail fast (exit 2)** on an empty/unknown user (validated once up front via
  `user_exists`, so a typo never half-assigns a cascade), a malformed target, an
  unsupported host, or an epic with no stories — nothing is assigned.
- **Unmapped stories are reported, never silently skipped (exit 1).** A story with
  no issue on this host (never mirrored, or mirrored only to the *other* host) is
  listed with a "mirror it first" hint; the mapped stories in the same cascade are
  still assigned.
- **Idempotent**: re-assigning the same user is a no-op — the cached `owner` is
  checked first, so an already-owned story needs no host write.

The verb is a thin command over the adapter's `issue_assign`
([`controller/src/sdlc/story_assign.py`](../controller/src/sdlc/story_assign.py)),
so it works unchanged on GitHub and GitLab.

## Identity is free

There is **no shared bot token**: identity is each contributor's own `gh`/`glab`
auth, so attribution is real. A shared token is discouraged — it collapses
attribution into one actor.

## Auth & CI tokens (Story 23.6-001)

Auth has two lanes — a **local** lane for developer-driven runs and a **CI** lane
for jobs that run on a runner ([`controller/src/sdlc/host_auth.py`](../controller/src/sdlc/host_auth.py)).

**Local runs** act as the developer. `resolve_local_login(adapter)` verifies the
`gh`/`glab` CLI is installed and authenticated and returns the login — the same
`ensure_ready` the mirror uses, so a local GitLab run uses the developer's
`glab auth` identity (and `whoami` for attribution, per the section above). No
token is read from the environment or a file; an unauthenticated CLI fails fast
with the host's own `gh auth login` / `glab auth login` hint.

**CI-side actions** (release, pipeline status) have no interactive login, so
`resolve_ci_token(host, env)` reads a token from the process environment in
priority order — **never from a committed file**:

| Host | Env vars (priority order) |
|------|---------------------------|
| GitLab | `GITLAB_TOKEN` → `GL_TOKEN` → `CI_JOB_TOKEN` |
| GitHub | `GH_TOKEN` → `GITHUB_TOKEN` |

On GitLab, prefer a **project access token** or a masked CI/CD variable
(`GITLAB_TOKEN`); the runner-injected, job-scoped `CI_JOB_TOKEN` is the fallback
of last resort. A defined-but-blank variable is treated as absent, so an empty
CI/CD variable never shadows a real token further down the list.

**Minimal token scopes** (`token_scopes(host)`) — provision a least-privilege
project access token, not an owner PAT:

| Host | Scopes | Covers |
|------|--------|--------|
| GitLab | `api`, `write_repository` | release + MR pipeline status (`api`); the version tag push (`write_repository`) |
| GitHub | `repo` | the equivalent grant |

**Same protections, both hosts** (Epic-13). A `CiToken`'s `value` is excluded
from its `repr`/`str` so the secret never lands in a traceback, log, or ledger
record — only the `host` and the `source` env-var name are shown. `redact(text,
*secrets)` masks both known credential shapes (GitHub `ghp_…` / `github_pat_…`,
GitLab `glpat-…`) and any explicit literal a caller passes (for a shapeless
`CI_JOB_TOKEN`) before anything is logged. `find_committed_tokens(text)` is the
no-secret-committed tripwire: a hardcoded PAT in tracked source is flagged, while
an env reference (`$CI_JOB_TOKEN`, `${GITLAB_TOKEN}`) passes clean — so the
"read the token from a CI/CD variable" pattern is the only one that survives.

## Adopt a GitLab project — preflight (Story 23.6-002)

Before the first build against a company GitLab repo, run the **adoption
preflight** so a run never fails halfway on a missing prerequisite:

```bash
sdlc doctor --gitlab [--target PATH]   # add --exit-code to gate automation
```

It extends the Epic-15 `sdlc doctor` health-check with four GitLab-target checks
— glab installed/authenticated, the project + default branch resolve, CI/CD is
enabled, and the [`.gitlab-ci.yml` gate template](gitlab-ci-template.md) is
present — each reporting a `CLEAN`/`WARN`/`FAIL` and the remedy. The full
zero-to-green-MR walkthrough is in
[**gitlab-adoption.md**](gitlab-adoption.md)
([`controller/src/sdlc/gitlab_preflight.py`](../controller/src/sdlc/gitlab_preflight.py)).

## Testing

Every adapter takes an injectable **`runner`** (`Runner = Callable[[argv],
RunResult]`) — the single seam where the host CLI is called. Tests stub it to
assert the exact argv and feed canned stdout, so the suite never needs a live
`gh`/`glab`. See
[`controller/tests/test_issue_host.py`](../controller/tests/test_issue_host.py).
