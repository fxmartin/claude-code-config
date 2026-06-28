#!/usr/bin/env bash
# ABOUTME: --core mode — symlink config files/dirs from the repo into ~/.claude.
# ABOUTME: Idempotent: re-running with everything in place is a no-op.
#
# Sourced by install.sh after common.sh. Expects SCRIPT_DIR, CLAUDE_DIR, DRY_RUN.

install_core_run() {
  # Guard (#179): refuse to install from an ephemeral build worktree. --core
  # symlinks every managed ~/.claude entry to $SCRIPT_DIR; if SCRIPT_DIR is a
  # throwaway agent worktree (.claude/worktrees/agent-*), those links dangle the
  # moment the worktree is torn down, silently breaking the live install. Only
  # the stable main checkout may own ~/.claude.
  case "$SCRIPT_DIR" in
    */.claude/worktrees/*)
      error "Refusing --core install: SCRIPT_DIR is inside an agent worktree (${SCRIPT_DIR})."
      error "Run install.sh from the main checkout so ~/.claude links to a stable path."
      return 1
      ;;
  esac

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

  # Shared skills (ADR-002) are the single source of truth under shared-skills/.
  # They are exposed as bare top-level slash commands (e.g. /coverage, /roast,
  # /create-issue) via committed RELATIVE symlinks inside commands/ (e.g.
  # commands/coverage.md -> ../shared-skills/coverage.md), which the commands
  # directory symlink above already carries into ~/.claude. We must NOT symlink
  # them in here: $CLAUDE_DIR/commands resolves back to $SCRIPT_DIR/commands, so
  # writing into it would replace the committed relative links with absolute
  # ones and dirty the repo on every run.

  # Local marketplace — exposes the autonomous-sdlc plugin to Claude Code.
  ensure_dir "$CLAUDE_DIR/plugins/marketplaces"
  create_symlink "$SCRIPT_DIR" "$CLAUDE_DIR/plugins/marketplaces/fx-claude-config"

  # Build-harness adapters on PATH (Story 21.3-001). The harness registry invokes
  # the Codex/Qwen workers by BARE NAME (e.g. `codex-build-adapter.sh`), resolved
  # on PATH at dispatch — so the adapters must live in a PATH dir or a cross-harness
  # build fails with "command not found". uv installs the `sdlc` controller into
  # ~/.local/bin, so we mirror the adapters into that same dir: an installed `sdlc`
  # then runs a Codex/Qwen build with no manual `ln -sf`. Override with
  # SDLC_ADAPTER_BIN_DIR (used by the bats suite to isolate state).
  local bin_dir="${SDLC_ADAPTER_BIN_DIR:-$HOME/.local/bin}"
  ensure_dir "$bin_dir"
  create_symlink "$SCRIPT_DIR/scripts/codex-build-adapter.sh" "$bin_dir/codex-build-adapter.sh"
  create_symlink "$SCRIPT_DIR/scripts/qwen-build-adapter.sh"  "$bin_dir/qwen-build-adapter.sh"

  # The bare-name dispatch only resolves if $bin_dir is on PATH. Warn (never fail)
  # with the exact line to fix it. The check is deterministic, so it is safe to
  # emit in dry-run too.
  case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) warn "$bin_dir is not on your PATH — add it so the build adapters resolve: export PATH=\"$bin_dir:\$PATH\"" ;;
  esac
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
  # Shared-skill commands are committed relative symlinks inside commands/, so
  # removing the commands symlink above already unlinks them; nothing to do here.
  remove_symlink "$CLAUDE_DIR/plugins/marketplaces/fx-claude-config" "$SCRIPT_DIR"

  # Build-harness adapters (Story 21.3-001). remove_symlink only unlinks a link
  # that points at our src, so an unrelated file of the same name is left alone.
  local bin_dir="${SDLC_ADAPTER_BIN_DIR:-$HOME/.local/bin}"
  remove_symlink "$bin_dir/codex-build-adapter.sh" "$SCRIPT_DIR/scripts/codex-build-adapter.sh"
  remove_symlink "$bin_dir/qwen-build-adapter.sh"  "$SCRIPT_DIR/scripts/qwen-build-adapter.sh"
}
