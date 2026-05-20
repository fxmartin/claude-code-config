#!/usr/bin/env bash
# ABOUTME: --shell mode — append dev() and y() helper functions to the shellrc.
# ABOUTME: Idempotent: skips append if the function header is already present.
#
# Sourced by install.sh after common.sh. Expects HOME, DRY_RUN, PLATFORM.
#
# Target file selection:
#   - macOS or Linux with $SHELL ending in /zsh → ~/.zshrc.
#   - WSL2 with non-zsh shell → ~/.bashrc.
#   - Anything else falls back to ~/.zshrc (existing behaviour).
#
# dev() on WSL2 is a stub that prints "cmux is macOS-only; this command is a
# no-op on WSL2" so the user can keep the same muscle memory across machines
# without launching a broken cmux invocation.

install_shell_run() {
  echo ""

  local rcfile
  rcfile="$(install_shell_target_rc)"
  echo "[shell] Adding helper functions to $rcfile..."

  install_shell_append_dev "$rcfile"
  install_shell_append_y   "$rcfile"
}

# Pick the shellrc to append to based on PLATFORM and $SHELL. Returns an
# absolute path. On WSL2 with a non-zsh default shell, this is ~/.bashrc;
# everywhere else (including WSL2 with zsh) it remains ~/.zshrc to match the
# pre-3.1-002 default.
install_shell_target_rc() {
  if [ "${PLATFORM:-}" = "WSL2" ] && [[ "${SHELL:-}" != */zsh ]]; then
    echo "$HOME/.bashrc"
  else
    echo "$HOME/.zshrc"
  fi
}

# Append the dev() cmux workspace launcher (or its WSL2 stub) if absent.
install_shell_append_dev() {
  local rcfile="$1"
  if grep -q 'function dev()' "$rcfile" 2>/dev/null; then
    info "dev() function already in $(basename "$rcfile") — skipping"
    return
  fi
  if [ "${PLATFORM:-}" = "WSL2" ]; then
    install_shell_append_dev_stub "$rcfile"
    return
  fi
  if [ "${DRY_RUN:-false}" = "true" ]; then
    echo "  [dry-run] append dev() function to $rcfile"
    return
  fi
  cat >> "$rcfile" << 'ZSH'

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
  info "Added dev() function to $(basename "$rcfile")"
}

# WSL2 stub for dev() — cmux is macOS-only, so the helper here just prints a
# clear message instead of trying (and failing) to spawn cmux workspaces.
install_shell_append_dev_stub() {
  local rcfile="$1"
  if [ "${DRY_RUN:-false}" = "true" ]; then
    echo "  [dry-run] append dev() WSL2 stub to $rcfile"
    return
  fi
  cat >> "$rcfile" << 'BASH'

# WSL2 stub — cmux is macOS-only, so this command is a clear no-op rather
# than a confusing "cmux: command not found" the first time a user types it.
function dev() {
  echo "cmux is macOS-only; this command is a no-op on WSL2"
  return 0
}
BASH
  info "Added dev() WSL2 stub to $(basename "$rcfile")"
}

# Append the y() yazi wrapper if absent.
install_shell_append_y() {
  local rcfile="$1"
  if grep -q 'function y()' "$rcfile" 2>/dev/null; then
    info "y() function already in $(basename "$rcfile") — skipping"
    return
  fi
  if [ "${DRY_RUN:-false}" = "true" ]; then
    echo "  [dry-run] append y() function to $rcfile"
    return
  fi
  cat >> "$rcfile" << 'ZSH'

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
  info "Added y() function to $(basename "$rcfile")"
}
