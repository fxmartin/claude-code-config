#!/usr/bin/env bash
# ABOUTME: Modal installer for claude-code-config (--core / --tools / --mcp / --shell / --all).
# ABOUTME: Dispatcher only; per-mode logic lives in install/<mode>.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
CLAUDE_JSON="$HOME/.claude.json"
BACKUP_DIR="$CLAUDE_DIR/backups/install-$(date +%Y%m%d-%H%M%S)"

# Default flags. The mode flags are off-by-default; if the caller passes none,
# the dispatcher selects --core (conservative additive default).
DRY_RUN=false
UNINSTALL=false
MODE_CORE=false
MODE_TOOLS=false
MODE_MCP=false
MODE_SHELL=false

# Legacy compat: --skip-mcp / --skip-tools (default-on opt-out). These will
# be removed in the next MAJOR release.
LEGACY_SKIP_MCP=false
LEGACY_SKIP_TOOLS=false

usage() {
  cat <<'USAGE'
Usage: install.sh [MODE...] [--dry-run] [--uninstall] [--help]

Modes (additive, combine freely):
  --core       Symlink config into ~/.claude (default if no mode flag is given).
  --tools      Install CLI utilities (yazi, bat, fd, rg, fzf, zoxide, jq, …).
  --mcp        Merge mcp/config.template.json into ~/.claude.json.
  --shell      Append dev() and y() helper functions to ~/.zshrc.
  --all        Shortcut for --core --tools --mcp --shell.

Options:
  --dry-run    Preview every action without making changes.
  --uninstall  Remove symlinks created by --core. Does not touch tools, MCP, or shellrc.
  --help       Show this help.

Backward-compatible (deprecated, removed in next MAJOR):
  --skip-mcp    Equivalent to --core --tools --shell.
  --skip-tools  Equivalent to --core --mcp --shell.
USAGE
  exit 0
}

# ─── Argument parsing ────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --core)        MODE_CORE=true ;;
    --tools)       MODE_TOOLS=true ;;
    --mcp)         MODE_MCP=true ;;
    --shell)       MODE_SHELL=true ;;
    --all)         MODE_CORE=true; MODE_TOOLS=true; MODE_MCP=true; MODE_SHELL=true ;;
    --dry-run)     DRY_RUN=true ;;
    --uninstall)   UNINSTALL=true ;;
    --skip-mcp)    LEGACY_SKIP_MCP=true ;;
    --skip-tools)  LEGACY_SKIP_TOOLS=true ;;
    --help|-h)     usage ;;
    *)
      echo "✗ Unknown option: $arg" >&2
      usage
      ;;
  esac
done

# ─── Mode resolution ─────────────────────────────────────────────────
# Translate legacy flags first (so they compose with explicit modes).
if $LEGACY_SKIP_MCP; then
  echo "⚠ --skip-mcp is DEPRECATED — equivalent to --core --tools --shell. Will be removed in the next MAJOR release." >&2
  MODE_CORE=true; MODE_TOOLS=true; MODE_SHELL=true
fi
if $LEGACY_SKIP_TOOLS; then
  echo "⚠ --skip-tools is DEPRECATED — equivalent to --core --mcp --shell. Will be removed in the next MAJOR release." >&2
  MODE_CORE=true; MODE_MCP=true; MODE_SHELL=true
fi

# If no mode was selected, default to --core (conservative).
if ! $MODE_CORE && ! $MODE_TOOLS && ! $MODE_MCP && ! $MODE_SHELL && ! $UNINSTALL; then
  MODE_CORE=true
fi

# Export everything the modules need before sourcing.
export SCRIPT_DIR CLAUDE_DIR CLAUDE_JSON BACKUP_DIR DRY_RUN

# ─── Load modules ────────────────────────────────────────────────────
# shellcheck source=install/common.sh
source "$SCRIPT_DIR/install/common.sh"
# shellcheck source=install/core.sh
source "$SCRIPT_DIR/install/core.sh"
# shellcheck source=install/tools.sh
source "$SCRIPT_DIR/install/tools.sh"
# shellcheck source=install/mcp.sh
source "$SCRIPT_DIR/install/mcp.sh"
# shellcheck source=install/shell.sh
source "$SCRIPT_DIR/install/shell.sh"

# ─── Platform detection (used by --tools today; expanded in 3.1-002) ─
case "$(uname -s)" in
  Darwin) PLATFORM="macOS" ;;
  Linux)  PLATFORM="Linux" ;;
  *)      PLATFORM="$(uname -s)" ;;
esac
export PLATFORM

# ─── Uninstall short-circuits everything else ────────────────────────
if $UNINSTALL; then
  echo "Removing claude-code-config symlinks..."
  install_core_uninstall
  echo "Done. MCP config (~/.claude.json) was not modified."
  exit 0
fi

# ─── Banner ──────────────────────────────────────────────────────────
echo "Installing claude-code-config from: $SCRIPT_DIR"
info "Platform: $PLATFORM"
echo "Modes selected:$( $MODE_CORE  && echo ' core'  )$( $MODE_TOOLS && echo ' tools' )$( $MODE_MCP   && echo ' mcp'   )$( $MODE_SHELL && echo ' shell' )"
$DRY_RUN && echo "(dry-run — no changes will be made)"

# ─── Dispatch ────────────────────────────────────────────────────────
$MODE_CORE  && install_core_run
$MODE_TOOLS && install_tools_run
$MODE_MCP   && install_mcp_run
$MODE_SHELL && install_shell_run

echo ""
info "Installation complete!"
echo ""
echo "Verify with:"
echo "  ls -la ~/.claude/CLAUDE.md"
echo "  claude mcp list"
