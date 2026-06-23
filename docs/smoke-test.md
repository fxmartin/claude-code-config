# Smoke Test — Clean-Machine Install Verification

**Stories:** [3.2-002](stories/epic-03-cross-platform-installer.md#story-32-002-clean-machine-install-verification-macos-and-wsl2),
[6.4-001](stories/epic-06-public-release-readiness.md#story-64-001-verify-both-plugin-install-paths-end-to-end)
**Audience:** FX (release captain) and any LTM colleague verifying a fresh box.

This document describes the two layers of smoke testing for `install.sh`:

1. **Automated** — `scripts/smoke-test.sh` (path A) and
   `scripts/verify-plugin-install.sh` (path B), both run in GitHub Actions on
   every PR and on every push to `main`.
2. **Manual** — the parts CI cannot cover (real Homebrew, real `BROWSER_PATH`,
   real `/build-stories` end-to-end, real `/plugin marketplace add` inside a
   Claude Code session). Run before every release tag (Epic-05).

---

## Two install paths (Story 6.4-001)

The README advertises **two ways** to install the framework. Both must work on
a fresh box, both are exercised by CI, and the manual checklist below catches
the parts CI cannot reach.

| Path | What the user runs | What it produces |
|------|--------------------|------------------|
| **A. local clone + `install.sh`** | `git clone …` then `./install.sh --core` | Symlinks under `~/.claude/`, including `~/.claude/plugins/marketplaces/fx-claude-config → <repo>`. The local marketplace exposes the `autonomous-sdlc` plugin to Claude Code. |
| **B. GitHub-direct marketplace** | `/plugin marketplace add fxmartin/claude-code-config` then `/plugin install autonomous-sdlc@fx-claude-config` inside a running Claude Code session | Claude Code clones the repo, reads `.claude-plugin/marketplace.json`, then resolves the `autonomous-sdlc` plugin at `plugins/autonomous-sdlc/`. The eight SDLC skills become available. |

### Automated coverage for each path

| Path | Script | What it asserts |
|------|--------|-----------------|
| A | `scripts/smoke-test.sh` | `install.sh --core` dry-run, install, idempotent re-run, uninstall — all inside an isolated `$HOME`. Verifies the marketplace symlink is created and removed. |
| B | `scripts/verify-plugin-install.sh` | `.claude-plugin/marketplace.json` is valid JSON, declares the `autonomous-sdlc` plugin, the plugin manifest is valid JSON with `name`/`version`/`description`, every `skills/<name>/SKILL.md` exists and its frontmatter `name:` matches the directory. Does NOT invoke Claude Code — structural validation only. |

Both run in CI (`.github/workflows/ci.yml`). Both can be invoked locally:

```bash
bash scripts/smoke-test.sh             # path A end-to-end
bash scripts/verify-plugin-install.sh  # path B structural check
```

The path-B script prints a `VERIFY_PLUGIN: <pass>/<total> passed` summary that
CI greps for and `tests/plugin-install-paths.bats` asserts on.

### Manual path-B verification (pre-release checklist)

CI cannot drive a real Claude Code session. Before each release tag, FX runs
the path-B install on a clean machine and confirms the skills resolve:

1. **macOS (clean user)**:
   - Open Claude Code in any throwaway repo.
   - Run `/plugin marketplace add fxmartin/claude-code-config`.
   - Run `/plugin install autonomous-sdlc@fx-claude-config`.
   - Restart Claude Code (or `/plugin reload` if available).
   - In the slash menu, confirm `/brainstorm`, `/generate-epics`,
     `/build-stories`, `/create-epic`, `/create-story`, `/fix-issue`,
     `/project-init`, `/resume-build-agents` all show up.
   - Invoke `/brainstorm` and confirm it runs the interview flow.
2. **WSL2 (Ubuntu 22.04, clean user)**:
   - Same sequence. The skills must load and run; `notify-telegram.sh` is a
     silent no-op when Telegram is unconfigured (no fatal errors).

**Pass criteria:** all eight skills surface in the slash menu on both
platforms; `/brainstorm` reaches at least the first interview question
without erroring.

### What verify-plugin-install.sh does NOT cover

- It does not run Claude Code. There is no headless `/plugin install`.
- It does not check that GitHub serves the repo over HTTPS (handled by GitHub).
- It does not verify the plugin works in a Codex session — that path is the
  autonomous-sdlc Codex plugin under a separate repo and is out of scope.

---

## Automated Smoke Test

### What it covers

`scripts/smoke-test.sh` exercises the `--core` happy path inside an isolated
temp `$HOME` (`mktemp -d`). It runs four phases and asserts each one:

| Phase | Command | Assertions |
|-------|---------|------------|
| 1. Dry-run | `./install.sh --core --dry-run` | exit 0, non-empty output, no filesystem mutations |
| 2. Install | `./install.sh --core` | exit 0, expected symlinks exist |
| 3. Idempotent | `./install.sh --core` (again) | exit 0, filesystem snapshot unchanged |
| 4. Uninstall | `./install.sh --uninstall` | exit 0, symlinks removed |

The summary line `SMOKE_TEST: <pass>/<total> passed` is the contract CI greps
for. Non-zero exit on any failure.

### Where it runs

`.github/workflows/ci.yml` defines a `smoke-test` job with a matrix that runs
on both `macos-latest` and `ubuntu-latest`. Total wall-clock: under one minute
on either runner.

### How to run it locally

```bash
bash scripts/smoke-test.sh
```

It will print a per-phase report and the summary line. No flags required. The
script creates and tears down its own temp `$HOME` so it never touches your
real `~/.claude`.

### Constraints (deliberately not covered by CI)

- **No real `brew install` or `apt install`.** `--tools` mode is not exercised
  because the CI runners are not throwaway hardware and we will not pay for
  package-manager network round-trips on every PR. Manual runs cover this.
- **No real `~/.claude.json` merge against a populated file.** `--mcp` mode
  is covered by `tests/install-modes.bats`; the smoke test scope is `--core`
  only (the symlink dispatch, which is the highest-blast-radius path).
- **No `/build-stories` end-to-end.** That requires a live Claude Code
  session and a sample project. Manual only.

---

## Manual Smoke Tests (pre-release checklist)

Run before tagging a release. Document the run in the PR notes or the release
draft (date + platform + result).

### M1 — macOS with Homebrew (`--tools`)

**Goal:** verify the `--tools` mode installs the eleven CLI utilities and
configures `yazi` on a Mac that has Homebrew installed.

**Steps:**

1. On a clean Mac (or a fresh test user), confirm `brew --version` works and
   the eleven target tools are NOT yet installed:
   ```bash
   for tool in yazi bat fd rg fzf zoxide ffmpeg magick pdftotext 7zz jq; do
     command -v "$tool" || echo "missing: $tool"
   done
   ```
2. From the repo root, run:
   ```bash
   ./install.sh --tools
   ```
3. Verify every tool is now installed:
   ```bash
   for tool in yazi bat fd rg fzf zoxide ffmpeg magick pdftotext 7zz jq; do
     command -v "$tool" >/dev/null && echo "ok: $tool" || echo "FAIL: $tool"
   done
   ```
4. Verify yazi config:
   ```bash
   test -f ~/.config/yazi/yazi.toml && echo "ok: yazi.toml"
   test -f ~/.config/yazi/init.lua && echo "ok: init.lua"
   ```
5. Re-run `./install.sh --tools` and confirm it reports "already installed"
   for every tool (idempotency).

**Pass criteria:** every tool reports `ok`, both yazi files exist, second run
performs no installs.

### M2 — `--mcp` with a real `BROWSER_PATH`

**Goal:** verify the MCP config merge writes a working `~/.claude.json` that
Claude Code can actually read.

**Steps:**

1. Back up your existing `~/.claude.json` (the installer does this for you,
   but belt-and-braces):
   ```bash
   cp ~/.claude.json ~/.claude.json.smoke-backup.$(date +%s)
   ```
2. Edit `.env` (or export inline) to set a real browser path:
   - macOS: `BROWSER_PATH=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
   - WSL2: `BROWSER_PATH=/mnt/c/Program Files/Google/Chrome/Application/chrome.exe`
3. Run:
   ```bash
   ./install.sh --mcp
   ```
4. Verify the merge:
   ```bash
   jq '.mcpServers | keys' ~/.claude.json
   ```
5. Open Claude Code and run `/mcp` (or `claude mcp list` from a terminal) and
   confirm the playwright server appears.

**Pass criteria:** `jq` returns the expected server keys, `claude mcp list`
shows the playwright server, no error in the Claude Code MCP panel.

### M3 — End-to-end `/build-stories` on a sample project

**Goal:** confirm the full SDLC pipeline (orchestrator → worktrees → review →
merge) actually runs after a clean install.

**Steps:**

1. From a clean install of the framework (`./install.sh --all`), clone a
   small test repo (e.g. `gh repo clone fxmartin/claude-code-config-fixtures`
   if it exists, or any small repo).
2. Open Claude Code in that repo.
3. Invoke `/brainstorm` then `/generate-epics` to seed `REQUIREMENTS.md` and
   `stories/`.
4. Invoke `/build-stories` and let it run a single P0 story.
5. Verify the orchestrator created a worktree, the build agent pushed a
   branch, the PR landed, and the ledger (`.sdlc-state.db`) recorded the run.

**Pass criteria:** PR created, CI green on the PR, ledger has a `runs` row
and at least one `stages` row with `status=passed`.

### M4 — WSL2 dry-run + install

**Goal:** confirm WSL2 detection and apt-preferred behavior work on a fresh
Ubuntu 22.04 WSL2 image.

**Steps:**

1. From a clean WSL2 Ubuntu 22.04 (`wsl --install -d Ubuntu-22.04`):
   ```bash
   sudo apt update && sudo apt install -y git curl jq
   gh auth login
   git clone <repo>
   cd claude-code-config
   ./install.sh --core --dry-run
   ./install.sh --core
   ./install.sh --tools   # uses apt
   ./install.sh --mcp     # validates BROWSER_PATH points at /mnt/c/...
   ./install.sh --shell   # appends to ~/.bashrc, not ~/.zshrc
   ```
2. Verify symlinks under `~/.claude`, verify yazi/bat/fd/rg/fzf/jq via apt,
   verify `~/.bashrc` contains the `dev()` no-op stub.

**Pass criteria:** all four modes complete cleanly; the WSL2 banner is
correct; the `dev()` function in `~/.bashrc` is the WSL2 stub, not the macOS
cmux version.

---

## Release checklist

For every release tag (Epic-05 release workflow):

- [ ] CI smoke-test job green on both `macos-latest` and `ubuntu-latest`.
- [ ] M1 run on a Mac, dated.
- [ ] M2 run with a real `BROWSER_PATH`, dated.
- [ ] M3 run on a sample project, PR link recorded.
- [ ] M4 run on WSL2 Ubuntu 22.04, dated.

Record results in the release draft or the PR that bumps the version.

---

## Reference

- Path A script: [`scripts/smoke-test.sh`](../scripts/smoke-test.sh)
- Path A tests: [`tests/smoke-test.bats`](../tests/smoke-test.bats)
- Path B script: [`scripts/verify-plugin-install.sh`](../scripts/verify-plugin-install.sh)
- Path B tests: [`tests/plugin-install-paths.bats`](../tests/plugin-install-paths.bats)
- CI jobs: `.github/workflows/ci.yml` → `smoke-test`, `static-checks`
- Stories: [`epic-03`](stories/epic-03-cross-platform-installer.md), [`epic-06`](stories/epic-06-public-release-readiness.md)
