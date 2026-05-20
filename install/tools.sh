#!/usr/bin/env bash
# ABOUTME: --tools mode — install CLI utilities (yazi, bat, fd, rg, fzf, …).
# ABOUTME: macOS uses Homebrew; other platforms are a graceful no-op for now
# ABOUTME: (apt support lands in Story 3.1-002).
#
# Sourced by install.sh after common.sh. Expects PLATFORM, DRY_RUN.

install_tools_run() {
  echo ""
  echo "[tools] Installing CLI utilities..."

  local brew_packages=(
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
  local brew_casks=(
    font-symbols-only-nerd-font  # File icons in yazi
  )

  case "${PLATFORM:-}" in
    macOS)
      if command -v brew &>/dev/null; then
        run brew install "${brew_packages[@]}"
        run brew install --cask "${brew_casks[@]}"
        info "Tools installed via Homebrew"
      else
        # In dry-run mode we still want a visible "would use brew" line so
        # the test suite (and a human reading the preview) sees the intent.
        if [ "${DRY_RUN:-false}" = "true" ]; then
          echo "  [dry-run] brew install ${brew_packages[*]}"
          echo "  [dry-run] brew install --cask ${brew_casks[*]}"
          warn "Homebrew not found on this machine — install from https://brew.sh"
        else
          warn "Homebrew not found — skipping CLI tools. Install from https://brew.sh"
        fi
      fi
      ;;
    *)
      # Other platforms: emit a preview-only message. Real apt support is
      # Story 3.1-002 (WSL2 detection). For now we surface the intent so
      # --dry-run still has something to print.
      if [ "${DRY_RUN:-false}" = "true" ]; then
        echo "  [dry-run] apt install fd-find ripgrep bat jq fzf  # (3.1-002)"
        warn "CLI tools install on ${PLATFORM:-unknown} ships in Story 3.1-002 (WSL2)"
      else
        warn "CLI tools install is macOS-only for now. Skipping on ${PLATFORM:-unknown}."
      fi
      ;;
  esac

  # Yazi plugins / config — only attempt if ya/yazi exist or we're previewing.
  if command -v ya &>/dev/null || [ "${DRY_RUN:-false}" = "true" ]; then
    if [ "${DRY_RUN:-false}" = "true" ] && ! command -v ya &>/dev/null; then
      echo "  [dry-run] ya pkg add yazi-rs/plugins:full-border"
      echo "  [dry-run] ya pkg add yazi-rs/plugins:git"
    elif command -v ya &>/dev/null; then
      run ya pkg add yazi-rs/plugins:full-border
      run ya pkg add yazi-rs/plugins:git
      info "Yazi plugins installed"
    fi
  fi

  install_tools_yazi_config
}

# Write yazi config files only if they are absent (idempotent). In dry-run
# mode, emit the action without writing.
install_tools_yazi_config() {
  local yazi_dir="$HOME/.config/yazi"

  if [ "${DRY_RUN:-false}" = "true" ]; then
    echo "  [dry-run] mkdir -p $yazi_dir"
    if [ ! -f "$yazi_dir/yazi.toml" ]; then
      echo "  [dry-run] write $yazi_dir/yazi.toml"
    else
      info "yazi.toml already exists — skipping"
    fi
    if [ ! -f "$yazi_dir/init.lua" ]; then
      echo "  [dry-run] write $yazi_dir/init.lua"
    else
      info "init.lua already exists — skipping"
    fi
    return
  fi

  mkdir -p "$yazi_dir"

  if [ ! -f "$yazi_dir/yazi.toml" ]; then
    cat > "$yazi_dir/yazi.toml" << 'TOML'
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
    info "Created yazi.toml"
  else
    info "yazi.toml already exists — skipping"
  fi

  if [ ! -f "$yazi_dir/init.lua" ]; then
    cat > "$yazi_dir/init.lua" << 'LUA'
-- full-border plugin
require("full-border"):setup()

-- git plugin
require("git"):setup()
LUA
    info "Created init.lua"
  else
    info "init.lua already exists — skipping"
  fi
}
