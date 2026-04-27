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

## Step 7: Initial Commit

```bash
git add .gitignore CLAUDE.md PROJECT-SEED.md
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
- Files created: `.gitignore`, `CLAUDE.md`, `PROJECT-SEED.md`
- **Next step**: "Run `/brainstorm` to define product requirements. It will pick up your PROJECT-SEED.md automatically."
