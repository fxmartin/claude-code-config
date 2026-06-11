#!/usr/bin/env bash
# ABOUTME: --core mode — symlink config files/dirs from the repo into ~/.claude.
# ABOUTME: Idempotent: re-running with everything in place is a no-op.
#
# Sourced by install.sh after common.sh. Expects SCRIPT_DIR, CLAUDE_DIR, DRY_RUN.

install_core_run() {
  echo ""
  echo "[core] Symlinking config into ${CLAUDE_DIR}..."

  # Git submodules (skills/model-shelf) ship empty on a plain clone, which
  # would leave the symlinked skill as a dead directory. Idempotent: a
  # no-op when the submodule is already initialized. Skipped with a warning
  # for tarball downloads (no .git) or when git is unavailable.
  if [ -f "$SCRIPT_DIR/.gitmodules" ]; then
    if [ -e "$SCRIPT_DIR/.git" ] && command -v git >/dev/null 2>&1; then
      run git -C "$SCRIPT_DIR" submodule update --init
    else
      warn "Cannot initialize git submodules (no .git or git missing) — submodule-backed skills will be empty"
    fi
  fi

  ensure_dir "$CLAUDE_DIR"

  create_symlink "$SCRIPT_DIR/CLAUDE.md"               "$CLAUDE_DIR/CLAUDE.md"
  create_symlink "$SCRIPT_DIR/agents"                  "$CLAUDE_DIR/agents"
  create_symlink "$SCRIPT_DIR/commands"                "$CLAUDE_DIR/commands"
  create_symlink "$SCRIPT_DIR/settings.json"           "$CLAUDE_DIR/settings.json"
  create_symlink "$SCRIPT_DIR/statusline-command.sh"   "$CLAUDE_DIR/statusline-command.sh"
  create_symlink "$SCRIPT_DIR/keybindings.json"        "$CLAUDE_DIR/keybindings.json"
  create_symlink "$SCRIPT_DIR/reference-docs"          "$CLAUDE_DIR/reference-docs"
  create_symlink "$SCRIPT_DIR/docs"                    "$CLAUDE_DIR/docs"
  create_symlink "$SCRIPT_DIR/skills"                  "$CLAUDE_DIR/skills"
  create_symlink "$SCRIPT_DIR/hooks"                   "$CLAUDE_DIR/hooks"

  # Local marketplace — exposes the autonomous-sdlc plugin to Claude Code.
  ensure_dir "$CLAUDE_DIR/plugins/marketplaces"
  create_symlink "$SCRIPT_DIR" "$CLAUDE_DIR/plugins/marketplaces/fx-claude-config"
}

install_core_uninstall() {
  echo "[core] Removing symlinks from ${CLAUDE_DIR}..."
  remove_symlink "$CLAUDE_DIR/CLAUDE.md"               "$SCRIPT_DIR/CLAUDE.md"
  remove_symlink "$CLAUDE_DIR/agents"                  "$SCRIPT_DIR/agents"
  remove_symlink "$CLAUDE_DIR/commands"                "$SCRIPT_DIR/commands"
  remove_symlink "$CLAUDE_DIR/settings.json"           "$SCRIPT_DIR/settings.json"
  remove_symlink "$CLAUDE_DIR/statusline-command.sh"   "$SCRIPT_DIR/statusline-command.sh"
  remove_symlink "$CLAUDE_DIR/keybindings.json"        "$SCRIPT_DIR/keybindings.json"
  remove_symlink "$CLAUDE_DIR/reference-docs"          "$SCRIPT_DIR/reference-docs"
  remove_symlink "$CLAUDE_DIR/docs"                    "$SCRIPT_DIR/docs"
  remove_symlink "$CLAUDE_DIR/skills"                  "$SCRIPT_DIR/skills"
  remove_symlink "$CLAUDE_DIR/hooks"                   "$SCRIPT_DIR/hooks"
  remove_symlink "$CLAUDE_DIR/plugins/marketplaces/fx-claude-config" "$SCRIPT_DIR"
}
