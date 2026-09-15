#!/usr/bin/env bats
# Arch Linux / Omarchy support — platform-aware behaviour on plain Linux.
#
# Complements install-platform.bats (WSL2) and install-platform-edge.bats.
# On a non-WSL Linux host detect_platform() returns "Linux"; these tests pin
# what each mode does there:
#   - --tools  uses pacman when it is on PATH (Arch); keeps the apt preview
#              when it is not (other distros stay best-effort).
#   - --shell  honours $SHELL (bash → ~/.bashrc, zsh → ~/.zshrc) and installs
#              a tmux-based dev() instead of the macOS cmux launcher.
#   - --mcp    auto-detects a Chromium-based browser when BROWSER_PATH is unset
#              and warns when it is set but not executable.
#
# Tests isolate state via FAKE_HOME, STUB_BIN on a strict PATH, and a fake
# /proc/version (via _PROC_VERSION_PATH) so no test depends on the host OS.
# No test mutates permissions (chmod-based fault injection no-ops as root in
# CI containers); the "not executable" case uses a nonexistent path instead.

INSTALL="${BATS_TEST_DIRNAME}/../install.sh"

setup() {
    FAKE_HOME="$(mktemp -d)"
    STUB_BIN="$(mktemp -d)"
    PROC_DIR="$(mktemp -d)"
    export FAKE_HOME STUB_BIN PROC_DIR
    # Strict PATH: only stubs plus core utils, so the host's real pacman/brew/
    # tmux/browsers never leak into a test.
    SAFE_PATH="$STUB_BIN:/usr/bin:/bin"
    export SAFE_PATH
    _linux
}

teardown() {
    [ -n "${FAKE_HOME:-}" ] && rm -rf "${FAKE_HOME}"
    [ -n "${STUB_BIN:-}"  ] && rm -rf "${STUB_BIN}"
    [ -n "${PROC_DIR:-}"  ] && rm -rf "${PROC_DIR}"
}

# Make detect_platform() report plain Linux regardless of the host.
_linux() {
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    echo "Linux version 6.12.0-arch1-1 (linux@archlinux) #1 SMP PREEMPT_DYNAMIC" > "$PROC_DIR/version"
}

# _stub NAME [BODY]: create an executable stub that logs its argv to
# $STUB_BIN/NAME.log and exits 0 (or runs BODY when given).
_stub() {
    local name="$1" body="${2:-exit 0}"
    printf '#!/bin/sh\necho "$*" >> "%s/%s.log"\n%s\n' "$STUB_BIN" "$name" "$body" > "$STUB_BIN/$name"
    chmod +x "$STUB_BIN/$name"
}

# Run install.sh on the strict PATH with optional KEY=value env overrides
# intermixed with flags (anything starting with a dash is a flag).
_run_install_strict() {
    local envs=() args=()
    local arg
    for arg in "$@"; do
        if [[ "$arg" == -* ]]; then
            args+=("$arg")
        else
            envs+=("$arg")
        fi
    done
    run env -i HOME="${FAKE_HOME}" CLAUDE_CONFIG_NO_ENV=1 \
        PATH="${SAFE_PATH}" _PROC_VERSION_PATH="${PROC_DIR}/version" \
        "${envs[@]}" bash "${INSTALL}" "${args[@]}"
}

PACMAN_LINE='[dry-run] sudo pacman -S --needed --noconfirm yazi bat fd ripgrep fzf zoxide ffmpeg imagemagick poppler 7zip jq ttf-nerd-fonts-symbols tmux'

# ─── --tools ─────────────────────────────────────────────────────────

@test "Linux --tools --dry-run uses pacman when pacman is on PATH" {
    _stub pacman
    _run_install_strict --tools --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"Platform: Linux"* ]]
    [[ "$output" == *"$PACMAN_LINE"* ]]
    [[ "$output" != *"cargo install"* ]]
    [[ "$output" != *"[dry-run] apt"* ]]
    [[ "$output" != *"best-effort"* ]]
}

@test "Linux --tools --dry-run prefers pacman when both pacman and apt-get are present" {
    _stub pacman
    _stub apt-get
    _run_install_strict --tools --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"$PACMAN_LINE"* ]]
    [[ "$output" != *"apt install"* ]]
}

@test "Linux --tools --dry-run keeps the apt preview when pacman is absent" {
    _run_install_strict --tools --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"[dry-run] apt install"* ]]
    [[ "$output" != *"pacman"* ]]
}

@test "Linux --tools real-run invokes sudo pacman --needed --noconfirm" {
    _stub sudo
    _stub pacman
    _run_install_strict --tools
    [ "$status" -eq 0 ]
    [ -f "$STUB_BIN/sudo.log" ]
    grep -q '^pacman -S --needed --noconfirm yazi bat fd ripgrep fzf zoxide ffmpeg imagemagick poppler 7zip jq ttf-nerd-fonts-symbols tmux$' "$STUB_BIN/sudo.log"
    [[ "$output" == *"Tools installed via pacman"* ]]
}

@test "Linux --tools --dry-run does not create yazi config" {
    _stub pacman
    _run_install_strict --tools --dry-run
    [ "$status" -eq 0 ]
    [ ! -e "${FAKE_HOME}/.config" ]
}

# ─── --shell: shellrc selection ──────────────────────────────────────

@test "Linux --shell with bash default appends to ~/.bashrc" {
    _run_install_strict SHELL=/bin/bash --shell
    [ "$status" -eq 0 ]
    [ -e "${FAKE_HOME}/.bashrc" ]
    grep -q 'function y()' "${FAKE_HOME}/.bashrc"
    [ ! -e "${FAKE_HOME}/.zshrc" ]
}

@test "Linux --shell with zsh default appends to ~/.zshrc" {
    _run_install_strict SHELL=/usr/bin/zsh --shell
    [ "$status" -eq 0 ]
    [ -e "${FAKE_HOME}/.zshrc" ]
    grep -q 'function y()' "${FAKE_HOME}/.zshrc"
    [ ! -e "${FAKE_HOME}/.bashrc" ]
}

# ─── --shell: tmux dev() ─────────────────────────────────────────────

@test "Linux --shell installs the tmux dev() (not cmux, not the WSL2 stub)" {
    _run_install_strict SHELL=/bin/bash --shell
    [ "$status" -eq 0 ]
    grep -q 'function dev()' "${FAKE_HOME}/.bashrc"
    grep -q 'tmux new-session' "${FAKE_HOME}/.bashrc"
    ! grep -q 'cmux new-workspace' "${FAKE_HOME}/.bashrc"
    ! grep -q 'cmux is macOS-only' "${FAKE_HOME}/.bashrc"
}

@test "Linux --shell --dry-run previews the tmux dev() append" {
    _run_install_strict SHELL=/bin/bash --shell --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"[dry-run] append dev() tmux function to ${FAKE_HOME}/.bashrc"* ]]
    [ ! -e "${FAKE_HOME}/.bashrc" ]
}

@test "Linux --shell is idempotent for the tmux dev()" {
    _run_install_strict SHELL=/bin/bash --shell
    [ "$status" -eq 0 ]
    before="$(cat "${FAKE_HOME}/.bashrc")"
    _run_install_strict SHELL=/bin/bash --shell
    [ "$status" -eq 0 ]
    after="$(cat "${FAKE_HOME}/.bashrc")"
    [ "$before" = "$after" ]
    [ "$(grep -c 'function dev()' "${FAKE_HOME}/.bashrc")" -eq 1 ]
}

# Install --shell, then invoke the installed dev() against DIR with a logging
# tmux stub. HAS_SESSION_RC controls what `tmux has-session` returns.
_run_dev() {
    local dir="$1" has_session_rc="${2:-1}" tmux_env="${3:-}"
    _run_install_strict SHELL=/bin/bash --shell
    [ "$status" -eq 0 ]
    _stub tmux "[ \"\$1\" = has-session ] && exit $has_session_rc; exit 0"
    run env -i HOME="${FAKE_HOME}" PATH="${SAFE_PATH}" TMUX="$tmux_env" \
        bash -c 'eval "$(sed -n "/^function dev()/,/^}/p" "$HOME/.bashrc")"; dev "$1"' _ "$dir"
}

@test "dev() creates claude/terminal/yazi windows and attaches when outside tmux" {
    local proj="${FAKE_HOME}/myproj"
    mkdir -p "$proj" && git -C "$proj" init -q
    _run_dev "$proj"
    [ "$status" -eq 0 ]
    local log="$STUB_BIN/tmux.log"
    grep -q "^has-session -t =myproj$" "$log"
    grep -q "^new-session -d -s myproj -c $proj -n claude$" "$log"
    grep -q "^send-keys -t =myproj:claude claude C-m$" "$log"
    grep -q "^new-window -t =myproj -c $proj -n terminal$" "$log"
    grep -q "^new-window -t =myproj -c $proj -n yazi$" "$log"
    grep -q "^send-keys -t =myproj:yazi yazi C-m$" "$log"
    grep -q "^select-window -t =myproj:claude$" "$log"
    grep -q "^attach-session -t =myproj$" "$log"
    ! grep -q "switch-client" "$log"
}

@test "dev() switches client when already inside tmux" {
    local proj="${FAKE_HOME}/myproj"
    mkdir -p "$proj"
    _run_dev "$proj" 1 "/tmp/tmux-1000/default,1234,0"
    [ "$status" -eq 0 ]
    grep -q "^switch-client -t =myproj$" "$STUB_BIN/tmux.log"
    ! grep -q "attach-session" "$STUB_BIN/tmux.log"
}

@test "dev() skips launching claude when the directory is not a git repo" {
    local proj="${FAKE_HOME}/plain"
    mkdir -p "$proj"
    _run_dev "$proj"
    [ "$status" -eq 0 ]
    grep -q "^new-session -d -s plain" "$STUB_BIN/tmux.log"
    ! grep -q "claude C-m" "$STUB_BIN/tmux.log"
    grep -q "^send-keys -t =plain:yazi yazi C-m$" "$STUB_BIN/tmux.log"
}

@test "dev() is idempotent when the session already exists" {
    local proj="${FAKE_HOME}/myproj"
    mkdir -p "$proj"
    _run_dev "$proj" 0
    [ "$status" -eq 0 ]
    ! grep -q "new-session" "$STUB_BIN/tmux.log"
    ! grep -q "new-window" "$STUB_BIN/tmux.log"
    grep -q "^attach-session -t =myproj$" "$STUB_BIN/tmux.log"
}

@test "dev() sanitises the session name" {
    local proj="${FAKE_HOME}/my.proj:x"
    mkdir -p "$proj"
    _run_dev "$proj"
    [ "$status" -eq 0 ]
    grep -q "^new-session -d -s my_proj_x -c " "$STUB_BIN/tmux.log"
}

@test "dev() rejects an invalid directory" {
    _run_dev "${FAKE_HOME}/does-not-exist"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Invalid directory"* ]]
    [ ! -e "$STUB_BIN/tmux.log" ]
}

@test "dev() reports a clear error when tmux is missing" {
    local proj="${FAKE_HOME}/myproj"
    mkdir -p "$proj"
    _run_install_strict SHELL=/bin/bash --shell
    [ "$status" -eq 0 ]
    # No tmux stub on the strict PATH.
    run env -i HOME="${FAKE_HOME}" PATH="${SAFE_PATH}" \
        bash -c 'eval "$(sed -n "/^function dev()/,/^}/p" "$HOME/.bashrc")"; dev "$1"' _ "$proj"
    [ "$status" -eq 1 ]
    [[ "$output" == *"tmux not found"* ]]
}

# ─── --mcp: BROWSER_PATH on Linux ────────────────────────────────────

@test "Linux --mcp auto-detects chromium when BROWSER_PATH is unset" {
    _stub chromium
    _run_install_strict --mcp --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"BROWSER_PATH auto-detected: ${STUB_BIN}/chromium"* ]]
    [[ "$output" != *"BROWSER_PATH not set"* ]]
}

@test "Linux --mcp prefers brave over chromium" {
    _stub chromium
    _stub brave
    _run_install_strict --mcp --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"BROWSER_PATH auto-detected: ${STUB_BIN}/brave"* ]]
}

@test "Linux --mcp still warns when no browser is on PATH" {
    _run_install_strict --mcp --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"BROWSER_PATH not set"* ]]
    [[ "$output" != *"auto-detected"* ]]
}

@test "Linux --mcp warns when BROWSER_PATH is set but not executable" {
    _run_install_strict "BROWSER_PATH=${FAKE_HOME}/nope" --mcp --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"BROWSER_PATH=${FAKE_HOME}/nope is not an executable file"* ]]
}
