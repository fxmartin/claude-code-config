#!/usr/bin/env bash
# ABOUTME: Shared helpers for install/*.sh modules (logging, run-guard, symlinks).
# ABOUTME: Sourced by install.sh and every mode script; never executed directly.
#
# Contract:
#   Callers MUST set the following before sourcing:
#     SCRIPT_DIR     — absolute path to the repo root (where install.sh lives)
#     CLAUDE_DIR     — target directory under HOME (usually $HOME/.claude)
#     DRY_RUN        — "true" or "false"
#
#   This file defines: info, warn, error, run, backup_if_exists,
#                       create_symlink, remove_symlink.

# Idempotent source-guard so multiple modules can include common.sh safely.
if [ "${_CLAUDE_INSTALL_COMMON_LOADED:-0}" = "1" ]; then
  return 0
fi
_CLAUDE_INSTALL_COMMON_LOADED=1

# Colours when stdout is a TTY.
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

# run(): in dry-run mode, prints the command prefixed with [dry-run]; otherwise
# executes it. Every mutating step in every mode MUST go through this so dry-run
# output exactly matches actual-run actions.
run() {
  if [ "${DRY_RUN:-false}" = "true" ]; then
    echo "  [dry-run] $*"
  else
    "$@"
  fi
}

# ensure_dir(): mkdir -p with run-guard and a friendly log line. The legacy
# install.sh printed "Created $CLAUDE_DIR" unconditionally — which lied in
# dry-run mode. We now log the action and let run() decide whether to perform
# it, so dry-run output matches the actual mkdir invocation.
ensure_dir() {
  local dir="$1"
  if [ ! -d "$dir" ]; then
    run mkdir -p "$dir"
  fi
}

# Backup non-symlink targets to a timestamped folder so an --uninstall can
# restore them if needed. Idempotent: a missing target is a no-op.
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

  # Idempotent: already pointing at the right place → no-op.
  if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
    info "$name already linked"
    return
  fi

  backup_if_exists "$dst"

  # Remove existing symlink pointing elsewhere so ln -s can replace it cleanly.
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

# detect_platform(): prints one of Darwin | WSL2 | Linux | <uname-fallback>.
#
# Strategy:
#   - macOS (Darwin) is decided by `uname -s` alone — /proc/version does not
#     exist there and any attempt to read it would be wasted IO.
#   - On Linux, we look at /proc/version (or the path in _PROC_VERSION_PATH
#     for tests) for a case-insensitive "microsoft" substring. WSL1 and WSL2
#     both match — for the framework's purposes they are treated as WSL2,
#     since WSL1 is end-of-life on Windows 11 and the modal installer only
#     officially supports WSL2.
#   - Anything else falls through to the raw `uname -s` value so unfamiliar
#     platforms surface with a readable label in the banner.
detect_platform() {
  local proc_version="${_PROC_VERSION_PATH:-/proc/version}"
  local uname_s
  uname_s="$(uname -s)"
  case "$uname_s" in
    Darwin)
      echo "Darwin"
      ;;
    Linux)
      if [ -r "$proc_version" ] && grep -qi 'microsoft' "$proc_version"; then
        echo "WSL2"
      else
        echo "Linux"
      fi
      ;;
    *)
      echo "$uname_s"
      ;;
  esac
}
