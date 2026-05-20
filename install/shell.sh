#!/usr/bin/env bash
# ABOUTME: --shell mode — append the dev() and y() helper functions to ~/.zshrc.
# ABOUTME: Idempotent: skips append if the function header is already present.
#
# Sourced by install.sh after common.sh. Expects HOME, DRY_RUN.
#
# Platform branching (zsh vs bash, WSL2 specifics) is Story 3.1-002 — this
# module only writes to ~/.zshrc today.

install_shell_run() {
  echo ""
  echo "[shell] Adding helper functions to ~/.zshrc..."

  local zshrc="$HOME/.zshrc"

  install_shell_append_dev "$zshrc"
  install_shell_append_y   "$zshrc"
}

# Append the dev() cmux workspace launcher if absent.
install_shell_append_dev() {
  local zshrc="$1"
  if grep -q 'function dev()' "$zshrc" 2>/dev/null; then
    info "dev() function already in ~/.zshrc — skipping"
    return
  fi
  if [ "${DRY_RUN:-false}" = "true" ]; then
    echo "  [dry-run] append dev() function to $zshrc"
    return
  fi
  cat >> "$zshrc" << 'ZSH'

# cmux dev workspace — opens 3 workspaces: claude/shell | terminal | yazi
function dev() {
  local dir="${1:-.}"
  dir="$(cd "$dir" 2>/dev/null && pwd)" || { echo "Invalid directory: $1"; return 1; }
  local is_repo=false
  git -C "$dir" rev-parse --is-inside-work-tree &>/dev/null && is_repo=true

  # Workspace 1 — claude code (if repo) or plain terminal
  local ws1_out
  ws1_out="$(cmux new-workspace --cwd "$dir" 2>&1)" || { echo "Failed to open cmux workspace"; return 1; }
  local ws1_ref
  ws1_ref="$(echo "$ws1_out" | grep -oE 'workspace:[0-9]+')"
  if $is_repo; then
    sleep 0.3
    cmux send --workspace "$ws1_ref" "claude\n"
  fi

  # Workspace 2 — pure terminal, renamed "terminal"
  sleep 0.3
  local ws2_out
  ws2_out="$(cmux new-workspace --cwd "$dir" 2>&1)"
  local ws2_ref
  ws2_ref="$(echo "$ws2_out" | grep -oE 'workspace:[0-9]+')"
  if [ -n "$ws2_ref" ]; then
    cmux rename-workspace "$ws2_ref" "terminal"
  fi

  # Workspace 3 — yazi file manager
  sleep 0.3
  local ws3_out
  ws3_out="$(cmux new-workspace --cwd "$dir" 2>&1)"
  local ws3_ref
  ws3_ref="$(echo "$ws3_out" | grep -oE 'workspace:[0-9]+')"
  if [ -n "$ws3_ref" ]; then
    cmux send --workspace "$ws3_ref" "yazi\n"
  fi

  # Focus back on workspace 1
  sleep 0.3
  [ -n "$ws1_ref" ] && cmux focus --workspace "$ws1_ref"
}
ZSH
  info "Added dev() function to ~/.zshrc"
}

# Append the y() yazi wrapper if absent.
install_shell_append_y() {
  local zshrc="$1"
  if grep -q 'function y()' "$zshrc" 2>/dev/null; then
    info "y() function already in ~/.zshrc — skipping"
    return
  fi
  if [ "${DRY_RUN:-false}" = "true" ]; then
    echo "  [dry-run] append y() function to $zshrc"
    return
  fi
  cat >> "$zshrc" << 'ZSH'

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
  info "Added y() function to ~/.zshrc"
}
