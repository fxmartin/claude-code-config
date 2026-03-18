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
  echo "Usage: $0 [--uninstall] [--dry-run] [--skip-mcp] [--skip-tools]"
  echo ""
  echo "Options:"
  echo "  --uninstall    Remove symlinks created by this script"
  echo "  --dry-run      Show what would be done without making changes"
  echo "  --skip-mcp     Skip MCP config generation (useful on Nix-managed machines)"
  echo "  --skip-tools   Skip CLI tools installation (yazi, bat, fd, etc.)"
  echo "  --help         Show this help"
  exit 0
}

# Parse arguments
UNINSTALL=false
DRY_RUN=false
SKIP_MCP=false
SKIP_TOOLS=false
for arg in "$@"; do
  case "$arg" in
    --uninstall)   UNINSTALL=true ;;
    --dry-run)     DRY_RUN=true ;;
    --skip-mcp)    SKIP_MCP=true ;;
    --skip-tools)  SKIP_TOOLS=true ;;
    --help)        usage ;;
    *)             error "Unknown option: $arg"; usage ;;
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

# ─── CLI Tools (Yazi + utilities) ────────────────────────────────────
if $SKIP_TOOLS; then
  warn "Skipping CLI tools (--skip-tools)"
elif [ "$PLATFORM" != "macOS" ]; then
  warn "CLI tools install is macOS-only (brew). Skipping on $PLATFORM."
else
  echo ""
  echo "Installing CLI tools (yazi file manager & utilities)..."

  BREW_PACKAGES=(
    yazi        # Terminal file manager
    bat         # Syntax-highlighted file viewer
    fd          # Fast find alternative
    ripgrep     # Fast grep alternative
    fzf         # Fuzzy finder
    zoxide      # Smarter cd with frecency
    ffmpeg      # Media preview support
    imagemagick # Image preview/conversion
    poppler     # PDF preview
    sevenzip    # Archive preview
    jq          # JSON processing
  )
  BREW_CASKS=(
    font-symbols-only-nerd-font  # File icons in yazi
  )

  if command -v brew &>/dev/null; then
    run brew install "${BREW_PACKAGES[@]}"
    run brew install --cask "${BREW_CASKS[@]}"
    info "CLI tools installed"
  else
    warn "Homebrew not found — skipping CLI tools. Install from https://brew.sh"
  fi

  # Install yazi plugins
  if command -v ya &>/dev/null; then
    echo ""
    echo "Installing yazi plugins..."
    run ya pkg add yazi-rs/plugins:full-border
    run ya pkg add yazi-rs/plugins:git
    info "Yazi plugins installed"
  fi

  # Configure yazi
  YAZI_DIR="$HOME/.config/yazi"
  run mkdir -p "$YAZI_DIR"

  if [ ! -f "$YAZI_DIR/yazi.toml" ] || $DRY_RUN; then
    if ! $DRY_RUN; then
      cat > "$YAZI_DIR/yazi.toml" << 'TOML'
[mgr]
ratio = [1, 2, 5]
sort_by = "natural"
sort_sensitive = false
sort_reverse = false
sort_dir_first = true
show_hidden = true
show_symlink = true

[preview]
max_width = 1000
max_height = 1000

[opener]
edit = [
  { run = '$EDITOR %s', block = true, for = "unix" },
]

[plugin]
TOML
    fi
    info "Created yazi.toml"
  else
    info "yazi.toml already exists — skipping"
  fi

  if [ ! -f "$YAZI_DIR/init.lua" ] || $DRY_RUN; then
    if ! $DRY_RUN; then
      cat > "$YAZI_DIR/init.lua" << 'LUA'
-- full-border plugin
require("full-border"):setup()

-- git plugin
require("git"):setup()
LUA
    fi
    info "Created init.lua"
  else
    info "init.lua already exists — skipping"
  fi

  # Add y() shell function to .zshrc if not present
  if ! grep -q 'function y()' "$HOME/.zshrc" 2>/dev/null; then
    if ! $DRY_RUN; then
      cat >> "$HOME/.zshrc" << 'ZSH'

# Yazi file manager — cd to last browsed directory on exit
function y() {
  local tmp="$(mktemp -t "yazi-cwd.XXXXXX")" cwd
  yazi "$@" --cwd-file="$tmp"
  if cwd="$(command cat -- "$tmp")" && [ -n "$cwd" ] && [ "$cwd" != "$PWD" ]; then
    builtin cd -- "$cwd"
  fi
  rm -f -- "$tmp"
}
ZSH
    fi
    info "Added y() function to ~/.zshrc"
  else
    info "y() function already in ~/.zshrc — skipping"
  fi
fi

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
