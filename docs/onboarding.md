# Onboarding — LTM Colleagues

**Story:** [6.1-001](stories/epic-06-public-release-readiness.md#story-61-001-onboarding-doc-for-ltm-colleagues)
**Audience:** the five LTM colleagues piloting the `autonomous-sdlc` plugin.
**Promise:** from "FX told me to read this" to "I just shipped a PR built by autonomous agents" in **under 15 minutes**.

---

## Welcome

This framework is an opinionated Claude Code configuration that runs the full SDLC — bootstrap, discovery, planning, build, review, merge — as a chain of autonomous Claude Code skills. You bring an idea; the pipeline produces a series of merged PRs against your repo. It is designed for solo engineers who want senior-level discipline (TDD, code review, conventional commits, parallel worktrees) without the headcount.

You are one of five LTM colleagues we are inviting to install it on your own machine and run a real project through `/brainstorm → /generate-epics → /build-stories`. This document is the single source of truth for that walkthrough. If something is unclear at the end of it, open an issue — the docs are wrong, not you.

---

## Prerequisites

Before you start, confirm the following on the machine you intend to install on:

| Requirement | Why |
|-------------|-----|
| macOS 13+ **or** Windows 11 with WSL2 (Ubuntu 22.04) | Tested platforms. Intel and Apple Silicon both supported on macOS. |
| [Claude Code](https://claude.com/claude-code) installed and signed in | The harness is a Claude Code configuration; without it nothing runs. |
| `gh` CLI authenticated (`gh auth status` returns "Logged in") | The pipeline creates PRs, files issues, and reads release tags through `gh`. |
| `git` configured with your name and email | Conventional Commits + signed-author CI rely on this. |
| ~5 GB free disk space | Worktrees, plugin caches, MCP server downloads. |
| Internet access | First-run downloads `node_modules`, MCP servers, Homebrew/apt packages. |

A working `node` is **not** strictly required up front — the installer's `--core` mode is symlinks only. You will need Node later (via `npx`) for the MCP servers (`--mcp` mode).

---

## Install — pick one path

There are **two install paths**. They produce the same set of plugin skills inside Claude Code. Pick the one that matches how you want to interact with the framework.

| You want to… | Use |
|--------------|-----|
| Just use the SDLC plugin and pull updates with one command | **Path A — Claude Code plugin marketplace** |
| Edit skills, contribute back, or iterate on the harness itself | **Path B — local clone + `install.sh`** |

### Install path A — Claude Code plugin marketplace

Inside any running Claude Code session, run:

```text
/plugin marketplace add fxmartin/claude-code-config
/plugin install autonomous-sdlc@fx-claude-config
```

Claude Code clones this repo into `~/.claude/plugins/marketplaces/fx-claude-config/` and installs the `autonomous-sdlc` plugin (8 SDLC skills) from `./plugins/autonomous-sdlc`. Pull updates later with:

```text
/plugin marketplace update fx-claude-config
```

You do **not** need to clone anything manually. The marketplace install is enough for the pilot.

Verify the install resolved:

```bash
jq '.plugins | keys' ~/.claude/plugins/installed_plugins.json
# expect output to include "autonomous-sdlc@fx-claude-config"
```

Restart Claude Code (or run `/plugin reload` if your build supports it). The slash menu should now offer `/brainstorm`, `/generate-epics`, `/create-epic`, `/create-story`, `/build-stories`, `/fix-issue`, `/project-init`, `/resume-build-agents` — each labelled `(autonomous-sdlc)` in the autocomplete.

### Install path B — local clone + `install.sh`

```bash
git clone git@github.com:fxmartin/claude-code-config.git ~/dev/claude-code-config
cd ~/dev/claude-code-config
cp .env.example .env          # machine-specific values (BROWSER_PATH, optional Telegram creds)
./install.sh --core           # symlinks only; conservative default
```

The installer is **modal** — pick one or more of these flags. Order does not matter; modes compose:

| Mode | What it does | Touches |
|------|--------------|---------|
| `--core` (default) | Symlinks `agents/`, `commands/`, `skills/`, `hooks/`, `CLAUDE.md`, `settings.json`, plus the marketplace symlink so the plugin resolves | `~/.claude/` |
| `--tools` | Installs `yazi`, `bat`, `fd`, `rg`, `fzf`, `zoxide`, `jq`, `ffmpeg`, `imagemagick`, `poppler`, `sevenzip` | Homebrew on macOS / apt on WSL2 (override with `--prefer-brew`) |
| `--mcp` | Merges `mcp/config.template.json` into `~/.claude.json` (Playwright + context7 MCP servers) | `~/.claude.json` |
| `--shell` | Appends the `dev()` and `y()` shell helpers | `~/.zshrc` (macOS) or `~/.bashrc` (WSL2 non-zsh) |
| `--all` | All four modes in one shot | everything above |
| `--dry-run` | Prints every action it WOULD take, mutates nothing | — |
| `--uninstall` | Removes the `--core` symlinks (other modes untouched) | `~/.claude/` |

Recommended for the pilot:

```bash
./install.sh --core --mcp     # symlinks + Playwright/context7 MCP servers
./install.sh --all --dry-run  # preview what --all would do before you commit
```

Path B also gives you the plugin (the `--core` mode symlinks the local marketplace into `~/.claude/plugins/marketplaces/fx-claude-config/`). After `--core` runs once, register the local marketplace inside Claude Code:

```text
/plugin marketplace add fx-claude-config
/plugin install autonomous-sdlc@fx-claude-config
```

Now edits you make to `plugins/autonomous-sdlc/skills/<name>/SKILL.md` land live in Claude Code without re-installing.

#### Windows / WSL2 specifics

If you are on Windows 11 + WSL2, the installer auto-detects WSL2 (via `/proc/version`) and switches package manager + shellrc accordingly. See [`docs/install-windows.md`](install-windows.md) for the full step-by-step from a fresh Windows box — including `wsl --install`, `gh auth login`, and the `BROWSER_PATH` `/mnt/c/` mount path for MCP.

---

## First-run smoke test

Before driving the pipeline against a real project, prove the install is healthy.

### Path A — verify the plugin loaded

In a fresh Claude Code session, type `/` and confirm the eight SDLC skills appear in the menu (labelled `(autonomous-sdlc)`):

- `/brainstorm`
- `/build-stories`
- `/create-epic`
- `/create-story`
- `/fix-issue`
- `/generate-epics`
- `/project-init`
- `/resume-build-agents`

Then invoke `/brainstorm` in a throwaway repo and confirm it reaches the first interview question.

### Path B — run the smoke-test script

If you cloned the repo, you have a fully automated smoke test:

```bash
cd ~/dev/claude-code-config
bash scripts/smoke-test.sh
```

The script creates an isolated `$HOME` under `mktemp -d`, runs `./install.sh --core --dry-run`, `--core`, an idempotent second `--core`, then `--uninstall`. It prints a per-phase report and the summary line `SMOKE_TEST: <pass>/<total> passed`. Anything other than `4/4 passed` is a failure to report.

Full smoke-test reference, including the manual checklist for `--tools`/`--mcp` and the path-B Claude Code session verification, is in [`docs/smoke-test.md`](smoke-test.md).

---

## Your first autonomous build

This is the moment of truth. We will take a new repo from blank to merged PRs end-to-end. Total time: 20–45 minutes of wall-clock, ~5 minutes of your active attention.

### Step 1 — Pick a project

Create or `cd` into a small repo you actually want to build. Anything with a clear single-sentence objective works ("a CLI that converts CSV to JSON"). Avoid mixing the pilot with a production repo on your first run.

### Step 2 — `/project-init` (only if the repo is empty)

If your repo is brand new and empty:

```text
/project-init "csv-to-json CLI"
```

It runs 5 quick discovery questions (objective, stack, architecture, repo visibility, catch-all), then `git init`s, creates a GitHub remote via `gh repo create`, applies 26 standard labels, writes a tailored `.gitignore`, a scaffold `CLAUDE.md`, and a `PROJECT-SEED.md` that the next step reads from.

If your repo already has code in it, **skip Phase 0** and go straight to `/brainstorm`.

### Step 3 — `/brainstorm`

```text
/brainstorm
```

A Senior PM persona conducts an 8-question structured interview (problem space, personas, success metrics, capabilities, scope boundaries, technical constraints, priority, acceptance criteria). If `PROJECT-SEED.md` exists from `/project-init`, the interview skips redundant questions.

Output: `REQUIREMENTS.md` at the repo root, plus a refreshed `CLAUDE.md` filled in with testing strategy, CI/CD, database, and deployment sections.

### Step 4 — `/generate-epics`

```text
/generate-epics
```

Transforms `REQUIREMENTS.md` into:

- `docs/stories/STORIES.md` — master overview
- `docs/stories/epic-NN-<name>.md` — INVEST-compliant user stories, one file per epic
- `docs/stories/non-functional-requirements.md`

If you want to add more epics later interactively, run `/create-epic`. To add stories to an existing epic, `/create-story <epic-NN> "<description>"`.

### Step 5 — `/build-stories`

The big one. Recommended for the first real run:

```text
/build-stories epic-01 --sequential
```

`--sequential` runs one story at a time. It is slower but easier to follow — start here. Once you trust the flow, drop `--sequential` and let the orchestrator run cohorts of up to 5 stories in parallel.

What you will see:

1. **Discovery agent** parses every epic file and emits a `QUEUE_JSON` build queue.
2. **Cohort scheduler** groups stories whose deps are complete into parallel cohorts.
3. Each cohort runs 4 stages: **build → coverage → review → merge** (the first three parallel, merge sequential).
4. Each build agent runs in its own git worktree (isolated checkout), writes tests first (TDD), then implements until green, pushes a branch, and opens a PR.
5. The senior-code-reviewer agent reviews every PR before merge.
6. On failure: a Bugfix Agent classifies (`CODE_BUG` / `TEST_BUG` / `ENV_ISSUE`), files a GitHub issue, fixes, retries (max 2 attempts).

### Step 6 — Watch what success looks like

For each story in the cohort, expect (in order):

- `BUILD_STARTED <story-id>` event
- `TESTS_GREEN <story-id>` event
- `BRANCH_PUSHED <story-id>` event
- `COVERAGE_MEASURED <story-id>` event with a percent ≥ 90
- `APPROVED <story-id>` event from the reviewer
- `MERGED <story-id>` event

The orchestrator prints a final summary with the merged-PR manifest. On GitHub, you will see N new PRs, each green, each merged into `main`.

### Step 7 — When it fails

The pipeline fails gracefully. The three most common failure modes:

1. **Preflight test red** — your local `main` is already broken. Fix the failing test before re-running. The orchestrator will refuse to start otherwise.
2. **Agent dispatch fail** — usually a Claude Code permission prompt waiting in the background. Check the cmux permission pill (red) or the Claude Code permission dialog and accept; the agent will resume.
3. **Merge conflict** — two stories in the same cohort touched overlapping files. The merge agent will report `MERGE_CONFLICT <story-id>` and skip it. Re-run with `/build-stories resume` after fixing the conflict.

If the pipeline gives up on a story (status `FAILED` after two retries), its dependents in later cohorts become `BLOCKED`. The Summary agent emits a clear failed-stories list at the end so you can decide whether to retry, refactor, or defer.

---

## Optional integrations

These are **opt-in**. Skip them on your first install and add them later if you want them.

### cmux (macOS only)

[cmux](https://www.cmux.dev/) is a native macOS terminal built on Ghostty for multi-agent AI development. With cmux running, the harness emits:

- **Status pills** showing the current phase and story
- **Progress bar** showing global run progress (`Cohort 2/4, Stage 3/4`)
- **Sidebar ledger** of structured per-agent events
- **Desktop notifications** on milestones (preflight failure, first story failure, finish)

Without cmux, every call to `cmux-bridge.sh` silently no-ops. Nothing in the pipeline depends on cmux being present. **WSL2 colleagues: cmux is macOS-only and the `dev()` shell helper is a no-op stub for you** — your pipeline runs identically, just without the sidebar UI. See [`docs/cmux-integration.md`](cmux-integration.md) for setup details.

### Telegram notifications

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in your `.env`, and the harness sends a message to your phone on:

- Run start
- First failure
- E2E gate failure
- Abort
- Run finish

Failures are gated to one-per-run so your phone does not buzz 47 times during a bad run. Telegram is platform-agnostic — works identically on macOS and WSL2.

### Secrets pre-commit hook (recommended)

CI already blocks any PR that introduces a credential, API key, or token — the
`secrets-scan` job runs [gitleaks](https://github.com/gitleaks/gitleaks) first,
before the build (Story 9.2-001). To catch a leak *before* you push, opt in to
the local pre-commit hook that runs the same scan against your staged changes:

```bash
pipx install pre-commit   # or: brew install pre-commit / uv tool install pre-commit
pre-commit install        # registers the hook from .pre-commit-config.yaml
```

Both the hook and CI share [`.gitleaks.toml`](../.gitleaks.toml), so the
allowlist stays consistent. If a scan ever fires, do **not** just delete the
line — rotate the secret and scrub it from history. The full runbook is in
[`docs/security-gates.md`](security-gates.md).

---

## Commit conventions

This repo and any repo you build with the framework enforces [Conventional Commits](https://www.conventionalcommits.org/). A `commit-format` CI job runs `commitlint --from origin/main --to HEAD` on every PR and **fails the PR** if any commit in the range violates the rules. You will see this most often when you raise a PR against this repo.

**Format:**

```
type(scope): subject

[optional body]

[optional footer(s)]
```

**Allowed types:** `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `ci`, `perf`, `build`, `revert`.

**Subject rules:**

- starts lower-case
- no trailing period
- whole header ≤ 72 chars

**Examples:**

```
feat(brainstorm): add 8-question discovery interview

The Senior PM persona now produces REQUIREMENTS.md from a structured
8-question interview covering personas, metrics, and acceptance criteria.
```

```
fix(installer): handle WSL2 paths with spaces correctly
```

```
docs(onboarding): add ltm colleague onboarding guide
```

```
chore(release): v1.13.2
```

Breaking changes are flagged with a `BREAKING CHANGE:` footer or a `!` after the type (e.g. `feat(api)!: drop /v1 endpoints`). This drives a MAJOR semver bump on release.

The autonomous build agents generate commit messages in this format automatically — you only need to know the rules when you write commits by hand (which is mostly bug-fix PRs against the harness itself).

---

## Contributing changes to the framework

### Branch protection on `main`

`main` is protected by a repository ruleset — **direct pushes are rejected for everyone, including the maintainer**. Day-to-day this means: branch, commit, `gh pr create`, wait for green, merge.

- Every change lands through a **pull request**. No approval count is enforced, so a maintainer can self-merge once CI is green.
- These CI checks are **required** before a PR can merge: `Static checks`, `Contract checks`, `Commit format (commitlint)`, `Behavior tests (bats)`, and both `Smoke test (clean-machine install)` matrix legs (macOS + Ubuntu).
- Force-pushes to and deletion of `main` are blocked.
- The one direct-push exception is the **release pipeline**: it pushes its `chore(release): vX.Y.Z` bump commit and tag using a deploy key, which the ruleset lists as a bypass actor. (Personal GitHub repos cannot grant the Actions app a ruleset bypass, hence the deploy key.)

### CHANGELOG maintenance

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and is **maintained by the release workflow, not by hand**. On every push to `main` that contains `feat`, `fix`, `perf`, or `refactor` commits, the workflow computes the next semver from the commit types, prepends a dated section under `## [Unreleased]` (feat → Added, fix → Fixed, perf/refactor → Changed), commits it as `chore(release): vX.Y.Z`, tags, and publishes a GitHub Release with the same notes.

Hand-edit only the `[Unreleased]` section, and only for context a commit subject cannot carry (e.g. multi-commit features that deserve one narrative entry). Never rewrite already-released sections — the git tag is the release authority.

### Adding a new agent

Subagent definitions are markdown files under `agents/` (personal helpers live in `agents/personal/`). To add one:

1. Create `agents/<agent-name>.md` with YAML frontmatter (`name`, `description`, optional `tools`, `model`, `color`) followed by the agent's system prompt. Copy an existing file such as `agents/qa-engineer.md` as a starting point.
2. The **file basename** (without `.md`) is the agent's identity: any skill or command that references `subagent_type=<agent-name>` resolves against the basenames of files in `agents/` (subdirectories included). Built-in Claude Code types (`general-purpose`, `Plan`, `Explore`, …) are allowlisted and need no file.
3. Before pushing, run `scripts/validate-agent-registry.sh` — it greps every `*.md` under `plugins/`, `skills/`, and `commands/` for `subagent_type=` references and fails listing any that do not resolve. The same check runs in CI as the required `Contract checks` job, so an unresolved reference blocks the merge.

---

## State and resume

Progress is persisted in a **SQLite state ledger** at `.sdlc-state.db` in the repo root. Status values are `DONE` / `IN_PROGRESS` / `FAILED` / `SKIPPED` / `PENDING`, recorded per story per stage. You do not need to operate the ledger yourself — the pipeline reads and writes it.

If a run is interrupted (Claude Code restart, machine reboot, ctrl-C), pick up where you left off with:

```text
/build-stories resume
```

The orchestrator queries the ledger for the latest incomplete run and resumes from the last completed stage. Stories already in `DONE` are skipped.

A human-readable markdown view of the ledger is generated on demand by `scripts/sdlc-state.sh render`. Deep details are in the [Epic-04 SQLite state ledger](stories/epic-04-sqlite-state-ledger.md) stories — you do not need them for the pilot.

---

## Getting help

If anything in this guide is wrong, unclear, or broken:

1. **File a GitHub issue** at <https://github.com/fxmartin/claude-code-config/issues/new>. Include:
   - your OS + version (e.g. `macOS 14.4 Apple Silicon`, `Windows 11 + WSL2 Ubuntu 22.04`)
   - the exact command you ran
   - the full error output (paste, don't summarize)
   - what you expected
2. **Tag the issue** with `pilot-feedback` so it surfaces in the Story 6.3-001 review.
3. **Ping FX** only if the issue is genuinely blocking the pilot — otherwise let the issue queue work.

For pilot feedback specifically, every friction point you hit during the install / first-build flow should become an issue. We expect the pilot to surface 5–15 of them; that is the point.

---

## Known limitations

- **cmux is macOS-only.** WSL2 colleagues get no sidebar UI; the pipeline runs identically without it. The `dev()` shell helper is a no-op stub on WSL2.
- **Package manager preference is platform-pinned.** `--tools` mode prefers Homebrew on macOS and apt on WSL2/Linux. Override with `--prefer-brew` if you have Homebrew installed on WSL2.
- **commitlint requires `node_modules` locally** for in-repo validation. CI does not need this — the CI job installs commitlint itself. If you want to run `npx commitlint` locally, `npm install` in the repo root once.
- **`build-stories --parallel` caps at 5 concurrent agents.** This is a RAM ceiling on a 48 GB MacBook Pro M3 Max — six agents start swapping, seven thrash. If you are on less than 48 GB, use `--sequential` or `--limit=N`.
- **The framework writes to your filesystem.** Every install action is idempotent and `--dry-run` previews exactly what changes, but install on a machine you control.
- **Some E2E tests require Playwright + a real browser via `BROWSER_PATH`.** If you skip `--mcp`, the E2E gate is reduced. The build pipeline still works.

---

## Tested with

| Component | Version | Date verified |
|-----------|---------|---------------|
| macOS | 13+ (Ventura, Sonoma, Sequoia) on Intel and Apple Silicon | 2026-05-20 |
| Windows / WSL2 | Windows 11 + WSL2 Ubuntu 22.04 LTS | 2026-05-20 (target — see [`docs/install-windows.md`](install-windows.md)) |
| Claude Code | v2.1.119 | 2026-05-20 |
| `gh` CLI | 2.x | 2026-05-20 |
| cmux | latest (optional, macOS only) | 2026-05-20 |

---

## Reviewed by

- [ ] Reviewed by one LTM colleague (pending pilot)

_Per the convention established by Story 3.2-001, colleague review against a live install is pending before the MVP pilot kicks off (Story 6.3-001)._
