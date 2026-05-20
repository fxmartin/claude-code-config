#!/usr/bin/env bats
# Story 3.1-002 — WSL2 detection and platform-aware behavior.
#
# The dispatcher sets PLATFORM via detect_platform() (in install/common.sh).
# Mode scripts read $PLATFORM and branch accordingly:
#   - --tools  prefers apt on WSL2, falls back to brew with --prefer-brew, and
#              hints `cargo install --locked yazi-fm` when apt cannot install yazi.
#   - --mcp    validates BROWSER_PATH (warns on Windows-side or unreachable paths).
#   - --shell  appends to ~/.bashrc when zsh is not the default; replaces dev()
#              with a stub that prints a "cmux is macOS-only" message.
#
# Tests isolate state via:
#   * FAKE_HOME            — per-test tempdir, used for HOME
#   * _PROC_VERSION_PATH   — override for /proc/version so detect_platform()
#                            can be unit-tested without mounting a fake proc.
#   * STUB_BIN on PATH     — fake `apt` / `apt-get` / `brew` / `cargo` / `uname`
#                            so the dispatcher reports intent without side effects.

INSTALL="${BATS_TEST_DIRNAME}/../install.sh"
COMMON="${BATS_TEST_DIRNAME}/../install/common.sh"

setup() {
    FAKE_HOME="$(mktemp -d)"
    STUB_BIN="$(mktemp -d)"
    PROC_DIR="$(mktemp -d)"
    export FAKE_HOME STUB_BIN PROC_DIR
}

teardown() {
    [ -n "${FAKE_HOME:-}" ] && rm -rf "${FAKE_HOME}"
    [ -n "${STUB_BIN:-}"  ] && rm -rf "${STUB_BIN}"
    [ -n "${PROC_DIR:-}"  ] && rm -rf "${PROC_DIR}"
}

# Run install.sh with optional env-var overrides (KEY=value) intermixed with
# script flags (anything starting with a dash). The helper sorts them out so
# the call shape is `env KEY=value... bash install.sh -- --flag1 --flag2`.
_run_install() {
    local envs=() args=()
    local arg
    for arg in "$@"; do
        if [[ "$arg" == -* ]]; then
            args+=("$arg")
        else
            envs+=("$arg")
        fi
    done
    if [ "${#envs[@]}" -eq 0 ]; then
        run env HOME="${FAKE_HOME}" CLAUDE_CONFIG_NO_ENV=1 \
            bash "${INSTALL}" "${args[@]}"
    else
        run env HOME="${FAKE_HOME}" CLAUDE_CONFIG_NO_ENV=1 "${envs[@]}" \
            bash "${INSTALL}" "${args[@]}"
    fi
}

# Source install/common.sh and call detect_platform with controlled inputs.
# Forks a subshell so the source-guard does not leak between tests.
_run_detect_platform() {
    local proc_path="$1" uname_out="$2"
    printf '#!/bin/sh\necho %s\n' "$uname_out" > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    run env PATH="$STUB_BIN:$PATH" \
        _PROC_VERSION_PATH="$proc_path" \
        SCRIPT_DIR="$(dirname "$COMMON")/.." \
        CLAUDE_DIR="$FAKE_HOME/.claude" \
        DRY_RUN=false \
        COMMON_SH="$COMMON" \
        bash -c '. "$COMMON_SH"; detect_platform'
}

# ─── detect_platform() unit tests ────────────────────────────────────

@test "detect_platform returns Darwin on macOS (no /proc/version)" {
    # _PROC_VERSION_PATH points at a non-existent file → macOS branch.
    _run_detect_platform "$PROC_DIR/nope" Darwin
    [ "$status" -eq 0 ]
    [ "$output" = "Darwin" ]
}

@test "detect_platform returns WSL2 when /proc/version contains microsoft" {
    printf 'Linux version 5.15.0 Microsoft@... WSL2\n' > "$PROC_DIR/version"
    _run_detect_platform "$PROC_DIR/version" Linux
    [ "$status" -eq 0 ]
    [ "$output" = "WSL2" ]
}

@test "detect_platform is case-insensitive on the microsoft marker" {
    printf 'Linux version 5.15.0 microsoft-standard-WSL2\n' > "$PROC_DIR/version"
    _run_detect_platform "$PROC_DIR/version" Linux
    [ "$status" -eq 0 ]
    [ "$output" = "WSL2" ]
}

@test "detect_platform returns Linux on non-WSL Linux" {
    # /proc/version exists but does NOT contain microsoft.
    printf 'Linux version 5.15.0 generic Ubuntu\n' > "$PROC_DIR/version"
    _run_detect_platform "$PROC_DIR/version" Linux
    [ "$status" -eq 0 ]
    [ "$output" = "Linux" ]
}

# ─── --tools on WSL2 ─────────────────────────────────────────────────

@test "WSL2 --tools --dry-run prefers apt over brew" {
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    # Pretend brew IS installed (would otherwise be a no-op).
    printf '#!/bin/sh\necho "BREW $*"\n' > "$STUB_BIN/brew"
    chmod +x "$STUB_BIN/brew"
    _run_install "PATH=$STUB_BIN:$PATH" "_PROC_VERSION_PATH=$PROC_DIR/version" \
        --tools --dry-run
    [ "$status" -eq 0 ]
    # Output must mention apt (preferred) and report the WSL2 platform banner.
    [[ "$output" == *"apt"* ]]
    [[ "$output" == *"WSL2"* ]]
}

@test "WSL2 --tools --prefer-brew uses brew when present" {
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    printf '#!/bin/sh\necho "BREW $*"\n' > "$STUB_BIN/brew"
    chmod +x "$STUB_BIN/brew"
    _run_install "PATH=$STUB_BIN:$PATH" "_PROC_VERSION_PATH=$PROC_DIR/version" \
        --tools --prefer-brew --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"brew"* ]]
}

@test "WSL2 --tools hints at cargo for yazi when apt cannot install it" {
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    _run_install "PATH=$STUB_BIN:$PATH" "_PROC_VERSION_PATH=$PROC_DIR/version" \
        --tools --dry-run
    [ "$status" -eq 0 ]
    # The cargo install hint for yazi must be present somewhere in the output.
    [[ "$output" == *"cargo install --locked yazi-fm"* ]]
}

# ─── --mcp on WSL2 ───────────────────────────────────────────────────

@test "WSL2 --mcp accepts a /mnt/c/ Windows-side BROWSER_PATH with a warning" {
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    _run_install "PATH=$STUB_BIN:$PATH" "_PROC_VERSION_PATH=$PROC_DIR/version" \
        "BROWSER_PATH=/mnt/c/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe" \
        --mcp --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"/mnt/c/"* ]]
    [[ "$output" == *"Windows"* || "$output" == *"warn"* || "$output" == *"⚠"* ]]
}

@test "WSL2 --mcp warns on an unreachable C:\\\\ BROWSER_PATH" {
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    _run_install "PATH=$STUB_BIN:$PATH" "_PROC_VERSION_PATH=$PROC_DIR/version" \
        'BROWSER_PATH=C:\Program Files\Brave\brave.exe' \
        --mcp --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"unreachable"* || "$output" == *"not reachable"* || "$output" == *"warn"* || "$output" == *"⚠"* ]]
    [[ "$output" == *"C:"* || "$output" == *"BROWSER_PATH"* ]]
}

# ─── --shell on WSL2 ─────────────────────────────────────────────────

@test "WSL2 --shell appends to ~/.bashrc when zsh is not the default" {
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    # Set SHELL to bash so the module picks bashrc.
    touch "${FAKE_HOME}/.bashrc"
    _run_install "SHELL=/bin/bash" "PATH=$STUB_BIN:$PATH" \
        "_PROC_VERSION_PATH=$PROC_DIR/version" --shell
    [ "$status" -eq 0 ]
    # bashrc must contain the y() function; zshrc must remain absent.
    run grep -q 'function y()' "${FAKE_HOME}/.bashrc"
    [ "$status" -eq 0 ]
    [ ! -e "${FAKE_HOME}/.zshrc" ]
}

@test "WSL2 --shell installs a dev() stub that says cmux is macOS-only" {
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    touch "${FAKE_HOME}/.bashrc"
    _run_install "SHELL=/bin/bash" "PATH=$STUB_BIN:$PATH" \
        "_PROC_VERSION_PATH=$PROC_DIR/version" --shell
    [ "$status" -eq 0 ]
    # The dev() stub message must appear in bashrc.
    run grep -F 'cmux is macOS-only' "${FAKE_HOME}/.bashrc"
    [ "$status" -eq 0 ]
    # And the function declaration must still be present.
    run grep -E 'function dev\(\)|^dev\(\)' "${FAKE_HOME}/.bashrc"
    [ "$status" -eq 0 ]
}
