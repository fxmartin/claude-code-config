# GitLab CI quality-gate template

> Story 23.3-001 (Epic-23 — *Pipeline on GitLab*). Brings the framework's GitHub
> Actions quality gates to a **GitLab Free/Core** target repo, so a Merge Request
> is gated to the same standard a Pull Request is. This is the gate that the
> autonomous build loop polls to block a merge on a red pipeline (Story 23.2-002).

## What it is

`templates/gitlab-ci.yml` is an **installable** `.gitlab-ci.yml` for a company
GitLab repo that adopts the autonomous-SDLC build loop. It is **not** the
framework's own CI (the framework stays on GitHub — see the Epic-23 non-goals).

## Install

Copy the template to the root of the target repo and commit it:

```bash
cp templates/gitlab-ci.yml /path/to/target-repo/.gitlab-ci.yml
```

The adoption preflight (Story 23.6-002) verifies the template is present along
with the rest of the prerequisites. Validate the template locally with:

```bash
scripts/validate-gitlab-ci.sh                 # checks the shipped template
scripts/validate-gitlab-ci.sh path/to/.gitlab-ci.yml
```

The validator parses the YAML, requires every gate job and the `stages` block,
and rejects any Premium/Ultimate-only construct (e.g. merge trains). It runs in
the `contract-checks` and `bats` CI jobs.

## Prerequisites in the target repo

| File / tool        | Used by gate     | Notes                                            |
| ------------------ | ---------------- | ------------------------------------------------ |
| `.gitleaks.toml`   | `secrets-scan`   | Same allowlist/config as the local pre-commit.   |
| `.commitlintrc.json` | `commit-format` | Conventional-commit rules.                       |
| `uv` + `pyproject.toml` | `pytest`, `ruff` | Python project managed with `uv`.           |
| `tests/*.bats`     | `bats`           | Behaviour/shell tests.                           |

Jobs whose tooling a given repo does not use can be removed from the copied
file; the validator only enforces the full gate set on the shipped template.

## Gate parity (GitHub Actions ↔ GitLab CI)

Every GitLab CI job maps 1:1 to a GitHub Actions gate. "Green" means the same
set of checks passed on both hosts.

| Quality gate            | GitHub Actions (`.github/workflows/ci.yml`) | GitLab CI (`templates/gitlab-ci.yml`) | Tool                         |
| ----------------------- | ------------------------------------------- | ------------------------------------- | ---------------------------- |
| Secret scan             | `secrets-scan` job                          | `secrets-scan` (stage `secret-scan`)  | gitleaks + `.gitleaks.toml`  |
| Lint — shell            | `static-checks` (Shellcheck step)           | `shellcheck` (stage `lint`)           | shellcheck `--severity=warning` |
| Lint — Python           | (target-repo gate)                          | `ruff` (stage `lint`)                 | ruff                         |
| JSON / schema / contract | `static-checks` (Validate JSON) + `contract-checks` | `json-schema` (stage `lint`)  | jq                           |
| Commit format           | `commit-format` (PR-only)                   | `commit-format` (MR-only)             | commitlint                   |
| Tests — Python          | `controller-smoke` (pytest)                 | `pytest` (stage `test`)               | uv + pytest                  |
| Tests — behaviour/shell | `behavior-tests` (bats)                     | `bats` (stage `test`)                 | bats                         |

### Mapping notes

- **MR vs PR scope for commit-format.** GitHub guards the job with
  `if: github.event_name == 'pull_request'` and diffs `origin/main..HEAD`. GitLab
  guards it with `rules: $CI_PIPELINE_SOURCE == "merge_request_event"` and diffs
  `$CI_MERGE_REQUEST_DIFF_BASE_SHA..HEAD`, so protected default-branch history
  stays exempt on both hosts.
- **Secret scan ordering.** On both hosts the secret scan runs first and the test
  jobs depend on it (`needs: secrets-scan`) so a leaked credential fails the build
  before any test job can echo it (Story 9.2-001).
- **Pipeline triggering.** The template's `workflow.rules` run the pipeline on MR
  events and pushes to the default branch — the GitLab equivalent of
  `on: [pull_request, push: branches: [main]]` — and avoid duplicate
  branch + MR pipelines on the same commit.

## Free/Core constraint

The template uses only GitLab Free/Core CI keywords. It deliberately avoids
Premium/Ultimate-only constructs — **merge trains**, code-quality widgets,
multiple-approver rules. The validator fails if a Premium keyword (e.g.
`merge_train`) appears, keeping the template usable on the company standard.

## Release flow (`templates/gitlab-ci-release.yml`)

> Story 23.4-001. The GitLab port of Epic-05's GitHub-Actions release
> (`.github/workflows/release.yml`). On every push to the **default branch** it
> computes the Conventional-Commit semver bump, creates a `vX.Y.Z` **tag**, and
> publishes a **GitLab Release** with generated notes — the GitLab equivalent of
> the GitHub flow. Free/Core only: it publishes through `release-cli`.

### What it is

A second installable template, separate from the gate template, so the release
pipeline is opt-in. Install it into a target GitLab repo together with the
shared bumper:

```bash
cp templates/gitlab-ci-release.yml /path/to/target-repo/   # merge into .gitlab-ci.yml or include it
cp scripts/compute-release.sh       /path/to/target-repo/scripts/
```

Validate it locally:

```bash
scripts/validate-gitlab-release.sh                 # checks the shipped template
scripts/validate-gitlab-release.sh path/to/.gitlab-ci-release.yml
```

### Bump truth is shared, not forked

The version is computed by the **same** `scripts/compute-release.sh` that drives
the GitHub release (Epic-05, Story 5.2-001) — `feat` → MINOR, `fix`/`perf`/
`refactor` → PATCH, `BREAKING CHANGE:`/`!` → MAJOR, everything else → no
release. Vendoring that one script into the target repo keeps both hosts on a
single source of truth; the release pipeline does not re-implement the bumper.

### Release parity (GitHub Actions ↔ GitLab CI)

| Step                | GitHub Actions (`release.yml`)            | GitLab CI (`gitlab-ci-release.yml`)                 |
| ------------------- | ----------------------------------------- | --------------------------------------------------- |
| Trigger             | `on: push: branches: [main]`              | `rules: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH`    |
| Loop guard          | `release-guard.sh` (subject-only skip)    | `rules` skip on a `chore(release):` / `[skip release]` `$CI_COMMIT_TITLE` |
| Compute bump        | `scripts/compute-release.sh`              | `scripts/compute-release.sh` (same script)          |
| No release-worthy commits | guard/`BUMP=none` no-op             | `BUMP=none` → `exit 0` no-op                         |
| Idempotency         | skip if tag exists                        | skip if `refs/tags/vX.Y.Z` exists                   |
| Tag + publish       | `git tag` + `gh release create`           | `release-cli create --tag-name vX.Y.Z`              |
| Notes               | grouped CHANGELOG section                 | same grouping (`Added`/`Changed`/`Fixed`)           |

### Mapping notes

- **No infinite loop.** The release's own `chore(release):` bump commit is
  skipped by the `$CI_COMMIT_TITLE` rule (the GitLab equivalent of
  `release-guard.sh`'s subject-only match), and even if it ran,
  `compute-release.sh` would classify the lone bump commit as `BUMP=none`.
- **Repo-specific manifest bumps are out of scope.** Unlike the framework's own
  `release.yml` (which also bumps `plugin.json`/`marketplace.json`/the controller
  version), the installable template ships only the generic bump-tag-release
  flow; a target repo adds its own manifest steps if it needs them.
- **Free/Core publish surface.** `release-cli` and GitLab Releases are Free-tier;
  the validator rejects any Premium/Ultimate keyword, as with the gate template.
