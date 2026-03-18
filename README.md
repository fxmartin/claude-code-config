# claude-code-config

Standalone Claude Code configuration — agents, commands, skills, MCP servers, settings, hooks, and keybindings.

## Quick Start

### Portable install (any macOS/Linux machine)

```bash
git clone git@github.com:fxmartin/claude-code-config.git
cd claude-code-config
cp .env.example .env   # Edit with your machine-specific values
./install.sh
```

### As a git submodule in nix-install

On Nix-managed machines, this repo is consumed as a submodule at `config/claude-code-config/`. The Nix activation script handles symlinks and MCP config generation — use `--skip-mcp` if running the install script manually:

```bash
./install.sh --skip-mcp
```

## What's included

| Path | Description |
|------|-------------|
| `CLAUDE.md` | Global instructions for Claude Code |
| `agents/` | 12 custom agent definitions (flat) |
| `skills/` | 15 skills (generators, approve-requirements, generate-epics, create-epic, build-stories, fix-issue, resume-build-agents, claude-docs, and more) |
| `commands/` | 17 slash commands organized into 6 categories |
| `templates/` | Shared reference templates used by generator skills |
| `reference-docs/` | Claude context references (python, source-control, containers) |
| `docs/` | User-facing documentation |
| `settings.json` | Settings (statusline, plugins, cmux lifecycle hooks) |
| `statusline-command.sh` | Statusline display script |
| `keybindings.json` | Keyboard shortcuts |
| `hooks/` | cmux integration: bridge utility, lifecycle hooks (agent tracking, notifications, workspace rename) |
| `mcp/config.template.json` | MCP server template (env var substitution) |

### Commands by category

| Category | Commands |
|----------|----------|
| `commands/dev/` | brainstorm, create-todo |
| `skills/` | approve-requirements, generate-epics, create-epic, build-stories, fix-issue, resume-build-agents, and more |
| `commands/issues/` | create-issue, fix-github-issue |
| `commands/quality/` | coverage, project-review, roast |
| `commands/project/` | create-project-summary-stats, create-user-documentation, sync-progress, update-estimated-time-spent, update-progress |
| `commands/devops/` | check-releases, plan-release-update |
| `commands/research/` | client-analysis, crypto-analysis, profile-analysis |

### Agents

| Agent | Domain |
|-------|--------|
| `backend-typescript-architect` | Bun + TypeScript backend systems |
| `bash-zsh-macos-engineer` | macOS shell scripting and automation |
| `crypto-coin-analyzer` | Single crypto ticker analysis |
| `crypto-market-agent` | Crypto market data retrieval |
| `executive-summary-generator` | Client company executive summaries |
| `meta-agent` | Generates new agent definitions |
| `podman-container-architect` | OCI containers, Podman, Containerfiles |
| `professional-profile-researcher` | LinkedIn and professional profile research |
| `python-backend-engineer` | FastAPI + uv + modern Python |
| `qa-engineer` | Testing strategy and quality assurance |
| `senior-code-reviewer` | Architecture, security, and code review |
| `ui-engineer` | Frontend components and UI design |

## Generator Skills

Three skills for scaffolding new Claude Code components from within Claude Code. See [`docs/generators.md`](docs/generators.md) for full documentation.

```bash
# Generate a command from a description
/create-command "a command that generates changelog entries"

# Generate an agent interactively (asks questions one at a time)
/create-agent

# Scaffold a skill with TODO placeholders
/create-skill --scaffold "lint fixer"
```

Each generator supports three modes:

| Mode | Invocation | Behavior |
|------|------------|----------|
| **Interactive** | `/create-agent` | Asks questions one at a time |
| **Direct** | `/create-agent "query optimizer"` | Generates from description |
| **Scaffold** | `/create-agent --scaffold` | Minimal template with TODOs |

All generators ask whether to install **globally** (this config repo, shared via symlink) or **locally** (current project's `.claude/`), and include a review cycle (approve, edit, or cancel) before writing files.

## Install options

```bash
./install.sh              # Full install with MCP config
./install.sh --skip-mcp   # Skip MCP (Nix handles it)
./install.sh --dry-run    # Preview changes
./install.sh --uninstall  # Remove symlinks
```

The installer creates symlinks from `~/.claude/` to this repo for: `CLAUDE.md`, `agents/`, `commands/`, `skills/`, `reference-docs/`, `docs/`, `settings.json`, `statusline-command.sh`, `keybindings.json`, and `hooks/`.

## Environment variables

See `.env.example`. Currently:

- `BROWSER_PATH` — path to Chromium-based browser for Playwright MCP server

## cmux Integration

Running on [cmux](https://www.cmux.dev/) — native macOS terminal built on Ghostty for multi-agent AI development. The `hooks/` directory provides real-time sidebar integration:

| Hook Script | Event | What it does |
|------------|-------|-------------|
| `cmux-bridge.sh` | — | Central utility: wraps all cmux CLI calls with graceful degradation + Telegram fallback |
| `cmux-session-start.sh` | `SessionStart` | Renames workspace to repo/folder name, logs session start |
| `cmux-agent-start.sh` | `SubagentStart` | Shows "Running: {agent}" status pill |
| `cmux-agent-stop.sh` | `SubagentStop` | Clears pill, desktop notification with result excerpt |
| `cmux-stop.sh` | `Stop` | Clears progress bar |
| `cmux-permission.sh` | `Notification` | Red "Permission Needed" alert + desktop notification |

Skills with sidebar integration: `/brainstorm`, `/create-epic`, `/generate-epics`, `/fix-issue` (11-phase progress bar), `/build-stories` (per-story progress + parallel pane management).

See [`docs/cmux-integration.md`](docs/cmux-integration.md) for full architecture and [`WORKFLOW-v2.md`](WORKFLOW-v2.md) for the enriched workflow.

## MCP Servers

The portable install uses `npx` to run MCP servers:
- **context7** — Library documentation
- **sequential-thinking** — Step-by-step reasoning
- **playwright** — Browser automation

On Nix machines, MCP servers use Nix-installed binaries instead of npx.
