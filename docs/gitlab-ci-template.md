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
