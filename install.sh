#!/usr/bin/env bash
# ABOUTME: Portable install script for claude-code-config
# ABOUTME: Creates symlinks from ~/.claude/ to this repo. Works on macOS and Linux.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
CLAUDE_JSON="$HOME/.claude.json"
BACKUP_DIR="$CLAUDE_DIR/backups/install-$(date +%Y%m%d-%H%M%S)"

# Colors (if terminal supports them)
if [ -t 1 ]; then
  GREEN='\033[0;32m'
  YELLOW='\033[0;33m'
  RED='\033[0;31m'
  NC='\033[0m'
else
  GREEN='' YELLOW='' RED='' NC=''
fi

info()  { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
error() { echo -e "${RED}✗${NC} $*" >&2; }

usage() {
  echo "Usage: $0 [--uninstall] [--dry-run] [--skip-mcp]"
  echo ""
  echo "Options:"
  echo "  --uninstall   Remove symlinks created by this script"
  echo "  --dry-run     Show what would be done without making changes"
  echo "  --skip-mcp    Skip MCP config generation (useful on Nix-managed machines)"
  echo "  --help        Show this help"
  exit 0
}

# Parse arguments
UNINSTALL=false
DRY_RUN=false
SKIP_MCP=false
for arg in "$@"; do
  case "$arg" in
    --uninstall) UNINSTALL=true ;;
    --dry-run)   DRY_RUN=true ;;
    --skip-mcp)  SKIP_MCP=true ;;
    --help)      usage ;;
    *)           error "Unknown option: $arg"; usage ;;
  esac
done

run() {
  if $DRY_RUN; then
    echo "  [dry-run] $*"
  else
    "$@"
  fi
}

backup_if_exists() {
  local target="$1"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    run mkdir -p "$BACKUP_DIR"
    run mv "$target" "$BACKUP_DIR/$(basename "$target")"
    warn "Backed up $(basename "$target") to $BACKUP_DIR/"
  fi
}

create_symlink() {
  local src="$1"
  local dst="$2"
  local name
  name="$(basename "$dst")"

  # Skip if already pointing to the right place
  if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
    info "$name already linked"
    return
  fi

  backup_if_exists "$dst"

  # Remove existing symlink pointing elsewhere
  if [ -L "$dst" ]; then
    run rm "$dst"
  fi

  if [ -d "$src" ]; then
    run ln -sfn "$src" "$dst"
  else
    run ln -sf "$src" "$dst"
  fi
  info "Linked $name → $src"
}

remove_symlink() {
  local dst="$1"
  local src="$2"
  local name
  name="$(basename "$dst")"

  if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
    run rm "$dst"
    info "Removed symlink: $name"
  fi
}

# ─── Uninstall ────────────────────────────────────────────────────────
if $UNINSTALL; then
  echo "Removing claude-code-config symlinks..."
  remove_symlink "$CLAUDE_DIR/CLAUDE.md"               "$SCRIPT_DIR/CLAUDE.md"
  remove_symlink "$CLAUDE_DIR/agents"                   "$SCRIPT_DIR/agents"
  remove_symlink "$CLAUDE_DIR/commands"                 "$SCRIPT_DIR/commands"
  remove_symlink "$CLAUDE_DIR/settings.json"            "$SCRIPT_DIR/settings.json"
  remove_symlink "$CLAUDE_DIR/statusline-command.sh"    "$SCRIPT_DIR/statusline-command.sh"
  remove_symlink "$CLAUDE_DIR/keybindings.json"         "$SCRIPT_DIR/keybindings.json"
  remove_symlink "$CLAUDE_DIR/reference-docs"            "$SCRIPT_DIR/reference-docs"
  remove_symlink "$CLAUDE_DIR/docs"                     "$SCRIPT_DIR/docs"
  remove_symlink "$CLAUDE_DIR/skills"                   "$SCRIPT_DIR/skills"
  remove_symlink "$CLAUDE_DIR/hooks"                    "$SCRIPT_DIR/hooks"
  echo "Done. MCP config (~/.claude.json) was not modified."
  exit 0
fi

# ─── Install ──────────────────────────────────────────────────────────
echo "Installing claude-code-config from: $SCRIPT_DIR"
echo ""

# Detect platform
case "$(uname -s)" in
  Darwin) PLATFORM="macOS" ;;
  Linux)  PLATFORM="Linux" ;;
  *)      PLATFORM="$(uname -s)"; warn "Untested platform: $PLATFORM" ;;
esac
info "Platform: $PLATFORM"

# Create ~/.claude if missing
if [ ! -d "$CLAUDE_DIR" ]; then
  run mkdir -p "$CLAUDE_DIR"
  info "Created $CLAUDE_DIR"
fi

# Create symlinks for all config files/dirs
create_symlink "$SCRIPT_DIR/CLAUDE.md"               "$CLAUDE_DIR/CLAUDE.md"
create_symlink "$SCRIPT_DIR/agents"                   "$CLAUDE_DIR/agents"
create_symlink "$SCRIPT_DIR/commands"                 "$CLAUDE_DIR/commands"
create_symlink "$SCRIPT_DIR/settings.json"            "$CLAUDE_DIR/settings.json"
create_symlink "$SCRIPT_DIR/statusline-command.sh"    "$CLAUDE_DIR/statusline-command.sh"
create_symlink "$SCRIPT_DIR/keybindings.json"         "$CLAUDE_DIR/keybindings.json"
create_symlink "$SCRIPT_DIR/reference-docs"            "$CLAUDE_DIR/reference-docs"
create_symlink "$SCRIPT_DIR/docs"                     "$CLAUDE_DIR/docs"
create_symlink "$SCRIPT_DIR/skills"                    "$CLAUDE_DIR/skills"
create_symlink "$SCRIPT_DIR/hooks"                    "$CLAUDE_DIR/hooks"

# ─── MCP Configuration ───────────────────────────────────────────────
if $SKIP_MCP; then
  warn "Skipping MCP config (--skip-mcp)"
else
  echo ""
  echo "Configuring MCP servers..."

  # Load .env if it exists
  if [ -f "$SCRIPT_DIR/.env" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
    info "Loaded .env"
  fi

  # Check required env vars
  if [ -z "${BROWSER_PATH:-}" ]; then
    warn "BROWSER_PATH not set. Create .env from .env.example or export BROWSER_PATH"
    warn "MCP config will have empty browser path"
    BROWSER_PATH=""
  fi

  # Process template: substitute env vars
  TEMPLATE="$SCRIPT_DIR/mcp/config.template.json"
  if [ -f "$TEMPLATE" ]; then
    MCP_CONFIG=$(sed "s|\\\$BROWSER_PATH|$BROWSER_PATH|g" "$TEMPLATE")

    if [ -f "$CLAUDE_JSON" ]; then
      # Merge mcpServers into existing ~/.claude.json
      if command -v jq &>/dev/null; then
        MERGED=$(jq -s '
          .[0] as $existing |
          .[1].mcpServers as $newServers |
          $existing * {mcpServers: (($existing.mcpServers // {}) * $newServers)}
        ' "$CLAUDE_JSON" <(echo "$MCP_CONFIG"))
        if ! $DRY_RUN; then
          echo "$MERGED" > "$CLAUDE_JSON"
        fi
        info "Merged MCP servers into $CLAUDE_JSON"
      else
        warn "jq not found — cannot merge MCP config. Install jq or use --skip-mcp"
      fi
    else
      # Create new config
      if ! $DRY_RUN; then
        echo "$MCP_CONFIG" > "$CLAUDE_JSON"
      fi
      info "Created $CLAUDE_JSON with MCP servers"
    fi
  else
    warn "MCP template not found: $TEMPLATE"
  fi
fi

echo ""
info "Installation complete!"
echo ""
echo "Verify with:"
echo "  ls -la ~/.claude/CLAUDE.md"
echo "  claude mcp list"
