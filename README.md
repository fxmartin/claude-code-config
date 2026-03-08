# claude-code-config

Standalone Claude Code configuration — agents, commands, MCP servers, settings, docs, hooks, and keybindings.

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
| `commands/` | 20 slash commands organized into 6 categories |
| `commands/dev/` | Core development lifecycle (brainstorm, requirements, stories, build) |
| `commands/issues/` | Issue creation and fixing |
| `commands/quality/` | Coverage, code review, roasting |
| `commands/project/` | Progress tracking, stats, documentation |
| `commands/devops/` | Release management |
| `commands/research/` | Domain-specific analysis (crypto, client, profile) |
| `settings.json` | Settings (statusline, plugins) |
| `statusline-command.sh` | Statusline display script |
| `keybindings.json` | Keyboard shortcuts |
| `docs/` | Reference docs (python, source-control, containers) |
| `hooks/` | Hook configurations |
| `mcp/config.template.json` | MCP server template (env var substitution) |

## Install options

```bash
./install.sh              # Full install with MCP config
./install.sh --skip-mcp   # Skip MCP (Nix handles it)
./install.sh --dry-run    # Preview changes
./install.sh --uninstall  # Remove symlinks
```

## Environment variables

See `.env.example`. Currently:

- `BROWSER_PATH` — path to Chromium-based browser for Playwright MCP server

## MCP Servers

The portable install uses `npx` to run MCP servers:
- **context7** — Library documentation
- **sequential-thinking** — Step-by-step reasoning
- **playwright** — Browser automation

On Nix machines, MCP servers use Nix-installed binaries instead of npx.
