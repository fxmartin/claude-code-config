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

> **Scope:** this is the *issue* adapter only. It does **not** make the build
> *pipeline* run on GitLab (Merge Requests, GitLab CI, `glab mr diff`) — that is
> the separate future "Pipeline on GitLab" epic.

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
| `close_keyword(ref)` | the PR/MR close-link line | `Closes #N` | `Closes #N` |

### Normalised types

- **`issue_ref`** — a string that hides the GitHub *issue number* vs the GitLab
  per-project *iid*. The inventory stores `host` + `issue_ref` together (Story
  22.1-001) to identify the remote item. The adapter parses the ref out of the
  created-issue URL (`…/issues/123`, GitLab `…/-/issues/5`).
- **`Issue`** — a host-neutral record: `host`, `ref`, `url`, `title`, `state`
  (normalised to `open`/`closed` — GitHub `OPEN`/`CLOSED`, GitLab
  `opened`/`closed`), and `assignees` (a tuple of login/username strings).
- **`close_keyword`** — both GitHub PRs and GitLab MRs auto-close an issue in
  the same project on merge with `Closes #N`, so the form is shared. (On GitLab
  the build opens an MR, which belongs to the separate Pipeline-on-GitLab epic;
  until then the close-link rides the GitHub PR path.)

### GitLab tier note

The company GitLab is **Free/Core**, so the GitLab path avoids all
Premium/Ultimate constructs: a **single assignee** (multiple assignees are
Premium), and **labels** for taxonomy rather than native epics or issue weight.

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

## Identity is free

There is **no shared bot token**: identity is each contributor's own `gh`/`glab`
auth, so attribution is real. A shared token is discouraged — it collapses
attribution into one actor.

## Testing

Every adapter takes an injectable **`runner`** (`Runner = Callable[[argv],
RunResult]`) — the single seam where the host CLI is called. Tests stub it to
assert the exact argv and feed canned stdout, so the suite never needs a live
`gh`/`glab`. See
[`controller/tests/test_issue_host.py`](../controller/tests/test_issue_host.py).
