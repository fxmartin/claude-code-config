#!/usr/bin/env bash
# ABOUTME: --tools mode — install CLI utilities (yazi, bat, fd, rg, fzf, …).
# ABOUTME: macOS uses Homebrew; WSL2 prefers apt (override with --prefer-brew).
#
# Sourced by install.sh after common.sh. Expects PLATFORM, DRY_RUN, PREFER_BREW.

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

  # WSL2 uses Debian/Ubuntu package names — fd is fd-find, ripgrep is ripgrep,
  # bat is bat (Ubuntu 22.04 ships it; on older systems it is batcat).
  local apt_packages=(
    bat         # Syntax-highlighted file viewer
    fd-find     # Fast find alternative (binary is `fdfind` on apt)
    ripgrep     # Fast grep alternative
    fzf         # Fuzzy finder
    zoxide      # Smarter cd with frecency
    ffmpeg      # Media preview support
    imagemagick # Image preview/conversion
    poppler-utils # PDF preview (pdftotext, pdfinfo)
    p7zip-full  # Archive preview
    jq          # JSON processing
  )

  case "${PLATFORM:-}" in
    macOS)
      install_tools_macos
      ;;
    WSL2)
      install_tools_wsl2
      ;;
    *)
      # Plain Linux (non-WSL): same apt path as WSL2 for now, minus the
      # Windows-side warnings. Kept simple — production Linux support is not
      # an Epic-03 goal.
      if [ "${DRY_RUN:-false}" = "true" ]; then
        echo "  [dry-run] apt install ${apt_packages[*]}"
        warn "CLI tools install on ${PLATFORM:-unknown} is best-effort (WSL2 is the tested Linux target)"
      else
        warn "CLI tools install is macOS/WSL2-tested. ${PLATFORM:-unknown} is best-effort."
      fi
      ;;
  esac

  # Yazi plugins / config — only attempt if ya/yazi exist or we're previewing.
  if command -v ya &>/dev/null || [ "${DRY_RUN:-false}" = "true" ]; then
    if [ "${DRY_RUN:-false}" = "true" ] && ! command -v ya &>/dev/null; then
      echo "  [dry-run] ya pkg add yazi-rs/plugins:full-border"
      echo "  [dry-run] ya pkg add yazi-rs/plugins:git"
    elif command -v ya &>/dev/null; then
      run ya pkg add yazi-rs/plugins:full-border || run ya pkg upgrade yazi-rs/plugins:full-border
      run ya pkg add yazi-rs/plugins:git || run ya pkg upgrade yazi-rs/plugins:git
      info "Yazi plugins installed"
    fi
  fi

  install_tools_yazi_config
}

# macOS branch — Homebrew is the only supported package manager.
install_tools_macos() {
  local brew_packages=(
    yazi bat fd ripgrep fzf zoxide ffmpeg imagemagick poppler sevenzip jq
  )
  local brew_casks=(font-symbols-only-nerd-font)

  if command -v brew &>/dev/null; then
    run brew install "${brew_packages[@]}"
    run brew install --cask "${brew_casks[@]}"
    info "Tools installed via Homebrew"
  else
    # In dry-run mode we still want a visible "would use brew" line so the
    # test suite (and a human reading the preview) sees the intent.
    if [ "${DRY_RUN:-false}" = "true" ]; then
      echo "  [dry-run] brew install ${brew_packages[*]}"
      echo "  [dry-run] brew install --cask ${brew_casks[*]}"
      warn "Homebrew not found on this machine — install from https://brew.sh"
    else
      warn "Homebrew not found — skipping CLI tools. Install from https://brew.sh"
    fi
  fi
}

# WSL2 branch — apt is the default; brew only with --prefer-brew.
#
# yazi is not in the Ubuntu/Debian apt repos (as of 24.04). We always emit a
# one-line cargo hint so the user knows how to install it. Other tools have
# direct apt equivalents (fd-find / ripgrep / bat / jq / fzf / zoxide /
# poppler-utils / p7zip-full / ffmpeg / imagemagick).
install_tools_wsl2() {
  local brew_packages=(
    yazi bat fd ripgrep fzf zoxide ffmpeg imagemagick poppler sevenzip jq
  )
  local apt_packages=(
    bat fd-find ripgrep fzf zoxide ffmpeg imagemagick poppler-utils p7zip-full jq
  )

  if [ "${PREFER_BREW:-false}" = "true" ]; then
    if command -v brew &>/dev/null; then
      run brew install "${brew_packages[@]}"
      info "Tools installed via Homebrew (--prefer-brew on WSL2)"
    else
      if [ "${DRY_RUN:-false}" = "true" ]; then
        echo "  [dry-run] brew install ${brew_packages[*]}"
        warn "--prefer-brew was set but Homebrew is not on PATH — install from https://brew.sh"
      else
        warn "--prefer-brew set but Homebrew not found. Falling back to apt."
        install_tools_wsl2_apt "${apt_packages[@]}"
      fi
    fi
    return
  fi

  install_tools_wsl2_apt "${apt_packages[@]}"
}

# Apt install with sudo. yazi is handled separately via a cargo hint because
# it does not ship in apt as of Ubuntu 24.04.
install_tools_wsl2_apt() {
  local pkgs=("$@")
  if [ "${DRY_RUN:-false}" = "true" ]; then
    echo "  [dry-run] sudo apt update"
    echo "  [dry-run] sudo apt install -y ${pkgs[*]}"
  else
    if command -v apt-get &>/dev/null; then
      run sudo apt-get update
      run sudo apt-get install -y "${pkgs[@]}"
      info "Tools installed via apt"
    else
      warn "apt-get not found on this WSL2 box — install manually or pass --prefer-brew"
    fi
  fi
  # yazi has no apt package today. Surface a concrete install hint instead of
  # silently skipping it. The user can opt out if they already have it.
  warn "yazi is not in apt — run: cargo install --locked yazi-fm yazi-cli"
  if [ "${DRY_RUN:-false}" = "true" ]; then
    echo "  [dry-run] cargo install --locked yazi-fm yazi-cli  # (if cargo is available)"
  fi
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
