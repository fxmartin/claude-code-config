# Project Init — Generation Rules

## Step 1: Initialize Git Repository

```bash
git init
git branch -M main
```

## Step 2: Generate .gitignore

Generate a `.gitignore` tailored to the detected tech stack. Use standard patterns for:

| Stack | Include |
|-------|---------|
| Python | `__pycache__/`, `*.pyc`, `.venv/`, `dist/`, `*.egg-info/`, `.ruff_cache/` |
| TypeScript/Node | `node_modules/`, `dist/`, `.next/`, `.turbo/` |
| Bun | `node_modules/`, `dist/` |
| Go | Binary name, `vendor/` (if not vendoring) |
| Rust | `target/`, `Cargo.lock` (for libraries only) |
| Docker | `.env`, `*.log` |

Always include:
```
.DS_Store
.env
.env.*
*.log
.vscode/
.idea/
```

## Step 3: Create GitHub Remote

> GitLab master? Use the "GitLab-master variant" section at the end instead of Steps 3, 4 and 8.

```bash
gh repo create <project-name> --<visibility> --source=. --remote=origin
```

Where `<visibility>` is `public` or `private` based on Q&A answer.

If the repo already exists on GitHub, ask the user whether to link to it or abort.

## Step 4: Apply Standard Labels

Delete all GitHub default labels first, then create the standard set.

### Remove defaults
```bash
gh label list --json name --jq '.[].name' | while read -r label; do
  gh label delete "$label" --yes
done
```

### Create standard labels (26 base labels)

**Severity:**
| Label | Color | Description |
|-------|-------|-------------|
| `low` | `C2E0C6` | Low severity |
| `medium` | `FBCA04` | Medium severity |
| `high` | `FF6B35` | High severity |
| `critical` | `B60205` | Critical severity — blocks core functionality |

**Component:**
| Label | Color | Description |
|-------|-------|-------------|
| `frontend` | `61DAFB` | Frontend / UI components |
| `backend` | `5319E7` | Backend / server-side |
| `database` | `0E8A16` | Database / migrations / models |
| `infra` | `333333` | CI/CD, Docker, deployment |
| `api` | `006B75` | API endpoints / routers |

**Workflow:**
| Label | Color | Description |
|-------|-------|-------------|
| `bug` | `D73A4A` | Something isn't working |
| `enhancement` | `A2EEEF` | New feature or request |
| `refactor` | `E4E669` | Code refactoring / cleanup |
| `test` | `BFD4F2` | Test coverage / test infrastructure |
| `performance` | `FF6B35` | Performance improvement |
| `documentation` | `0075CA` | Improvements or additions to documentation |
| `blocked` | `B60205` | Blocked by dependency or decision |
| `security` | `B60205` | Security vulnerabilities or hardening |

**Meta:**
| Label | Color | Description |
|-------|-------|-------------|
| `breaking-change` | `B60205` | Introduces a breaking change |
| `tech-debt` | `FBCA04` | Technical debt reduction |
| `ux` | `61DAFB` | User experience / usability |
| `hotfix` | `B60205` | Urgent production fix |
| `in-progress` | `0E8A16` | Currently being worked on |
| `needs-triage` | `D876E3` | Needs severity/component classification |
| `duplicate` | `CFD3D7` | This issue or pull request already exists |
| `question` | `D876E3` | Further information is requested |
| `wontfix` | `FFFFFF` | This will not be worked on |

Additionally, create **2-5 project-specific labels** based on the Q&A answers. Use your judgment to add domain-specific labels that match the project's functional areas (e.g., `llm-pipeline`, `ingestion`, `cartography`, `visualization`).

## Step 5: Generate CLAUDE.md (Lightweight)

Generate a `CLAUDE.md` with foundational sections only. Deep sections (testing strategy, CI/CD pipeline, data model, etc.) will be filled after `/brainstorm` and `/generate-epics`.

```markdown
# <PROJECT-NAME> — <Tagline from objective>

## Project Context

<2-3 sentences from the objective answer. What it does, who it's for, why it matters.>

## Tech Stack

- **Language**: <from Q&A>
- **Framework**: <from Q&A>
- **Runtime**: <from Q&A>

## Architecture

<1-2 sentences describing the architecture style from Q&A>

## Repository Structure

```
<project-name>/
├── <predicted top-level structure based on stack and architecture>
├── CLAUDE.md
├── PROJECT-SEED.md
└── .gitignore
```

## Preferred CLI Tools

Use these instead of their traditional counterparts. They're installed and expected.

| Instead of | Use | Why |
|------------|-----|-----|
| `find` | `fd` | Faster, respects `.gitignore` |
| `grep` (via Bash) | `rg` | ripgrep — faster, better defaults |
| `cat` | `bat` | Syntax highlighting, line numbers |
| `cd` | `zoxide` (`z`) | Jump to frecent directories |
| `jq` for JSON | `jq` | Installed for JSON processing |

## GitHub Operations — Use `gh` CLI (NOT MCP)

Always use `gh` CLI for all GitHub operations (issues, PRs, releases, API calls).

## Key Docs

<!-- Populated after /brainstorm and /generate-epics -->
- `PROJECT-SEED.md` — Project seed data for downstream skills
```

### CLAUDE.md Quality Checklist
- [ ] Project context is clear and specific (not generic boilerplate)
- [ ] Tech stack matches Q&A answers exactly
- [ ] Repository structure is plausible for the chosen architecture
- [ ] CLI tools table is included
- [ ] No sections are included that require deep-dive answers not yet collected

## Step 6: Generate PROJECT-SEED.md

This is the **handoff file** that `/brainstorm` reads to skip already-answered questions and pre-fill context.

```markdown
# Project Seed — <project-name>

> Auto-generated by `/project-init` on <YYYY-MM-DD>. Consumed by `/brainstorm`.

## Objective

<Full objective text from Q&A question 1>

## Tech Stack

- **Language**: <from Q&A>
- **Framework**: <from Q&A>
- **Runtime**: <from Q&A>

## Architecture

<Architecture style from Q&A question 3>

## Repo

- **Name**: <project-name>
- **Visibility**: <public/private>
- **GitHub URL**: <https://github.com/...>
- **Created**: <YYYY-MM-DD>

## Notes

<Content from "Anything else?" question, or "None" if skipped>
```

## Step 6b: Generate .sdlc-harness.yaml (Harness Pin)

Pin the repo's agent harness so its routing is **declared, not inherited**.

Without this file a repo has no "default routing" — it silently follows the
`default:` inside the *installed* controller's registry
(`sdlc/config/harnesses.yaml`). That registry ships inside the wheel, so every
`install-controller.sh` / `uv tool install --force` resets it, and a colleague's
differently-configured install resolves differently again. The repo file is the
only declaration that survives a redeploy.

Resolve the value the machine is actually configured for, so a host already set up
for another harness bootstraps consistently instead of silently reverting new
repos to `claude`:

```bash
HARNESS="$(sdlc doctor --json 2>/dev/null \
  | jq -r '.findings[] | select(.check=="harness") | .detail' \
  | sed -n 's/.*default=\([A-Za-z0-9_-]*\).*/\1/p' | head -1)"
HARNESS="${HARNESS:-claude}"   # sdlc absent or unreadable → the built-in default
```

Read the `default=` value out of the detail string rather than matching a list of
known harness names: the registry grows (`opencode` arrived in Story 29.2-001),
and a stale enum here fails *silently* — the match whiffs, `${HARNESS:-claude}`
fires, and the new repo is pinned to `claude`, which is exactly the reverting
behaviour this lookup exists to prevent.

Write a **minimal** file — the pin plus one pointer. Do not reproduce the full
capability commentary here; a fresh repo's author has not met roles, adapters, or
capability flags yet, and `/project-init` is measured on time-to-first-PR.

```yaml
# Agent harness for this repo. Precedence: --harness flag > this file >
# installed registry default > built-in claude. See `sdlc doctor`.
harness:
  default: <HARNESS>
```

Keep it to `default:`. Per-role routing (`roles: {review: codex, ...}`) is a
deliberate later choice, not a bootstrap decision.

## Step 7: Initial Commit

```bash
git add .gitignore CLAUDE.md PROJECT-SEED.md .sdlc-harness.yaml
git commit -m "chore: initialize repository with CLAUDE.md and PROJECT-SEED.md

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

## Step 8: Push to Remote

```bash
git push -u origin main
```

## Step 9: Display Summary

Show the user:
- GitHub repo URL
- Number of labels created (base + project-specific)
- Files created: `.gitignore`, `CLAUDE.md`, `PROJECT-SEED.md`, `.sdlc-harness.yaml`
- **Next step**: "Run `/brainstorm` to define product requirements. It will pick up your PROJECT-SEED.md automatically."

## GitLab-master variant (Master repo = GitLab on home-lab)

The GitHub answer keeps Steps 1-9 exactly as written above. On the GitLab path the
forge is `http://gitlab.test` (`export GITLAB_HOST=gitlab.test`; `glab` runs as
`root`). GitHub is only the mirror target: nothing is pushed or merged there.

### Step 3 (GitLab): Create remotes

```bash
glab api -X POST projects -f name=<project-name> -f visibility=<visibility> \
  -f initialize_with_readme=false -f default_branch=main
git remote add origin http://gitlab.test/root/<project-name>.git
gh repo create <project-name> --<visibility> --source=. --remote=github
```

`--source=. --remote=github` adds the mirror target as remote `github`; do not push to it.

#### Credential helper (GitLab)

Set repo-locally so the first push does not fail on "could not read Username":

```bash
git config --local credential.http://gitlab.test.helper '!glab auth git-credential'
```

### Step 4 (GitLab): Labels

Issues and labels live on GitLab. Apply the same 26 base labels (and the 2-5
project-specific ones) from Step 4 with one call per label — `glab` has no bulk
apply. GitHub labels are not applied.

```bash
glab label create --name "<label>" --color "#<Color>" --description "<Description>"
```

### Step 6c (GitLab): Forge declaration and CI

The sdlc controller cannot detect the forge from a `gitlab.test` remote, so without
this file the first PR open fails and parks the story.

#### .sdlc-forge.yaml (GitLab)

```yaml
# Declares the code host for the sdlc controller: origin is the local-ci-cd
# GitLab on home-lab, whose hostname carries no "gitlab" tell for auto-detection
# to key on.
forge: gitlab
gitlab_url: http://gitlab.test
```

Install `templates/gitlab-ci.yml` as `.gitlab-ci.yml` (Story 23.3-001 prerequisites
apply). Keep `.github/workflows/ci.yml` where the stack generates one: it is the
hosted fallback for the single-appliance risk, not a redundant gate.

Add `.sdlc-forge.yaml` and `.gitlab-ci.yml` to the Step 7 `git add`.

### CLAUDE.md additions (GitLab)

Replace the "GitHub Operations" section with the following, and append the CI notes:

```markdown
## Source Control — local GitLab is master

- `origin` is `http://gitlab.test/root/<project-name>.git`; the `github` remote is a
  push-mirror target only. Never push to `github`; never merge on GitHub.
- Change flow: branch → push `origin` → merge request on GitLab → appliance
  pipeline green → merge on GitLab.
- Issues and MRs live on GitLab — use `glab` (`GITLAB_HOST=gitlab.test`), not `gh`.

## CI (offline)

- Jobs use the platform's `GOMODCACHE` volume: never commit `vendor/`.
- CI has no network: warm images and modules on home-lab (`local-ci-cd cache warm` /
  `local-ci-cd cache warm-deps`) before the first pipeline.
```

### Step 8 (GitLab): Push

```bash
git push -u origin main
```

### Step 9 (GitLab): Summary additions

In addition to the standard summary:
- Master repo: GitLab `http://gitlab.test/root/<project-name>`; GitHub is the mirror target.
- Mirror: when the `local-ci-cd` CLI and `LOCAL_CI_CD_MIRROR_GITHUB_TOKEN` are available
  on this machine, run the command below; otherwise print it for the operator to run
  on the appliance — never skip silently:
  `local-ci-cd mirror add root/<project-name> --username x-access-token --token-env LOCAL_CI_CD_MIRROR_GITHUB_TOKEN`
- Warn: mirrors run with `keep_divergent_refs`, so a GitHub-side merge silently strands GitLab.
- Warn: CI is offline — run `local-ci-cd cache warm` / `cache warm-deps` on home-lab before the first pipeline.
- Files created also include `.sdlc-forge.yaml` and `.gitlab-ci.yml`.
