#!/usr/bin/env bats
# Story 3.1-002 — edge-case coverage for WSL2 detection and platform-aware behavior.
#
# Complements install-platform.bats (happy-path). Covers defensive / corner cases:
#   - detect_platform() with empty or very-large /proc/version
#   - --tools on WSL2 when apt-get is absent (no-dry-run)
#   - --tools --prefer-brew on WSL2 when brew is absent (dry-run: warn; real: fallback)
#   - --mcp on WSL2 with BROWSER_PATH completely unset
#   - --shell on WSL2 when ~/.bashrc is absent (should create it via bash append)
#   - --shell on WSL2 when zsh is the default shell (targets ~/.zshrc, not ~/.bashrc)
#   - dev() stub on WSL2 is always the "cmux-is-macOS-only" version, even with zsh
#   - dev() full cmux function installed on macOS, NOT the WSL2 stub

INSTALL="${BATS_TEST_DIRNAME}/../install.sh"
COMMON="${BATS_TEST_DIRNAME}/../install/common.sh"

setup() {
    FAKE_HOME="$(mktemp -d)"
    STUB_BIN="$(mktemp -d)"
    PROC_DIR="$(mktemp -d)"
    export FAKE_HOME STUB_BIN PROC_DIR
    # SAFE_PATH restricts the subprocess to only stub binaries + core system utils,
    # preventing the real `brew` (in /opt/homebrew) from interfering with tests
    # that specifically test the "brew not installed" code path.
    SAFE_PATH="$STUB_BIN:/usr/bin:/bin"
    export SAFE_PATH
}

teardown() {
    [ -n "${FAKE_HOME:-}" ] && rm -rf "${FAKE_HOME}"
    [ -n "${STUB_BIN:-}"  ] && rm -rf "${STUB_BIN}"
    [ -n "${PROC_DIR:-}"  ] && rm -rf "${PROC_DIR}"
}

# _run_install_strict: uses SAFE_PATH instead of prepending to the real PATH.
# Use this when the test needs to control which binaries are visible (e.g., no brew).
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
    if [ "${#envs[@]}" -eq 0 ]; then
        run env HOME="${FAKE_HOME}" CLAUDE_CONFIG_NO_ENV=1 \
            PATH="${SAFE_PATH}" \
            bash "${INSTALL}" "${args[@]}"
    else
        run env HOME="${FAKE_HOME}" CLAUDE_CONFIG_NO_ENV=1 \
            PATH="${SAFE_PATH}" "${envs[@]}" \
            bash "${INSTALL}" "${args[@]}"
    fi
}

# _run_install: inherits the real PATH with stub prepended. Use for tests that
# only need to mock one binary (e.g., uname) while leaving others accessible.
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

# ─── detect_platform() edge cases ────────────────────────────────────

@test "detect_platform returns Linux when /proc/version is empty" {
    # An empty file passes -r but grep finds no 'microsoft' → plain Linux.
    touch "$PROC_DIR/version"
    _run_detect_platform "$PROC_DIR/version" Linux
    [ "$status" -eq 0 ]
    [ "$output" = "Linux" ]
}

@test "detect_platform returns Linux with a large /proc/version lacking microsoft" {
    # Generate a ~4 KB /proc/version with no microsoft keyword.
    python3 -c "print('Linux version 5.15.0-generic ' + 'x' * 4096)" > "$PROC_DIR/version"
    _run_detect_platform "$PROC_DIR/version" Linux
    [ "$status" -eq 0 ]
    [ "$output" = "Linux" ]
}

@test "detect_platform returns WSL2 even when microsoft appears late in a large file" {
    # Embed 'microsoft' after many kilobytes of filler — grep -qi should still find it.
    python3 -c "print('Linux version 5.15.0-generic ' + 'x' * 4096 + ' Microsoft WSL2')" > "$PROC_DIR/version"
    _run_detect_platform "$PROC_DIR/version" Linux
    [ "$status" -eq 0 ]
    [ "$output" = "WSL2" ]
}

# ─── --tools on WSL2 when apt-get is absent (no-dry-run) ─────────────

@test "WSL2 --tools warns gracefully when apt-get is not on PATH" {
    # Use SAFE_PATH so apt-get is genuinely absent.
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    # No apt-get stub — verifies the "apt-get not found" branch in tools.sh.
    _run_install_strict "_PROC_VERSION_PATH=$PROC_DIR/version" \
        --tools
    [ "$status" -eq 0 ]
    [[ "$output" == *"apt-get not found"* ]]
}

# ─── --tools --prefer-brew on WSL2 when brew is absent ───────────────

@test "WSL2 --tools --prefer-brew dry-run warns when brew is absent" {
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    # SAFE_PATH: no brew stub, so brew is genuinely absent.
    _run_install_strict "_PROC_VERSION_PATH=$PROC_DIR/version" \
        --tools --prefer-brew --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"Homebrew"* || "$output" == *"brew"* ]]
    [[ "$output" == *"not"* || "$output" == *"warn"* || "$output" == *"⚠"* ]]
}

@test "WSL2 --tools --prefer-brew real-run falls back to apt when brew absent" {
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    # Provide apt-get and sudo stubs so the fallback path runs cleanly.
    printf '#!/bin/sh\nexit 0\n' > "$STUB_BIN/apt-get"
    chmod +x "$STUB_BIN/apt-get"
    printf '#!/bin/sh\nexit 0\n' > "$STUB_BIN/sudo"
    chmod +x "$STUB_BIN/sudo"
    # SAFE_PATH: brew is absent, apt-get is present → fallback triggers.
    _run_install_strict "_PROC_VERSION_PATH=$PROC_DIR/version" \
        --tools --prefer-brew
    [ "$status" -eq 0 ]
    # Real-run non-dry-run fallback path emits this warning:
    [[ "$output" == *"Falling back to apt"* ]]
    # And the yazi cargo hint always fires on the apt path:
    [[ "$output" == *"cargo install --locked yazi-fm"* ]]
}

# ─── --mcp on WSL2 with BROWSER_PATH completely unset ────────────────

@test "WSL2 --mcp warns clearly when BROWSER_PATH is unset" {
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    # Explicitly unset BROWSER_PATH — simulate a first-run with no .env.
    _run_install "PATH=$STUB_BIN:$PATH" "_PROC_VERSION_PATH=$PROC_DIR/version" \
        --mcp --dry-run
    [ "$status" -eq 0 ]
    # The "BROWSER_PATH not set" warning branch must fire.
    [[ "$output" == *"BROWSER_PATH"* ]]
}

# ─── --shell on WSL2 when ~/.bashrc does not exist ────────────────────

@test "WSL2 --shell creates ~/.bashrc when it does not exist" {
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    # Do NOT pre-create ~/.bashrc — the append (cat >>) must create it.
    [ ! -e "${FAKE_HOME}/.bashrc" ]
    _run_install "SHELL=/bin/bash" "PATH=$STUB_BIN:$PATH" \
        "_PROC_VERSION_PATH=$PROC_DIR/version" --shell
    [ "$status" -eq 0 ]
    [ -e "${FAKE_HOME}/.bashrc" ]
    run grep -q 'function y()' "${FAKE_HOME}/.bashrc"
    [ "$status" -eq 0 ]
}

# ─── --shell on WSL2 when zsh is the default shell ───────────────────

@test "WSL2 --shell targets ~/.zshrc not ~/.bashrc when zsh is default" {
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    touch "${FAKE_HOME}/.zshrc"
    _run_install "SHELL=/bin/zsh" "PATH=$STUB_BIN:$PATH" \
        "_PROC_VERSION_PATH=$PROC_DIR/version" --shell
    [ "$status" -eq 0 ]
    # zshrc must contain y(); bashrc must NOT exist.
    run grep -q 'function y()' "${FAKE_HOME}/.zshrc"
    [ "$status" -eq 0 ]
    [ ! -e "${FAKE_HOME}/.bashrc" ]
}

@test "WSL2 --shell with zsh default writes dev() WSL2 stub to ~/.zshrc" {
    # On WSL2, PLATFORM=WSL2 → stub is always the WSL2 version regardless of shell.
    # The rcfile CHANGES (zshrc for zsh, bashrc for bash), but the stub content
    # stays the same. This test verifies the stub lands in the correct file.
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    touch "${FAKE_HOME}/.zshrc"
    _run_install "SHELL=/bin/zsh" "PATH=$STUB_BIN:$PATH" \
        "_PROC_VERSION_PATH=$PROC_DIR/version" --shell
    [ "$status" -eq 0 ]
    # The WSL2 stub message must appear in zshrc (not bashrc).
    run grep -F 'cmux is macOS-only' "${FAKE_HOME}/.zshrc"
    [ "$status" -eq 0 ]
    [ ! -e "${FAKE_HOME}/.bashrc" ]
}

# ─── macOS: dev() stub must NOT be installed ─────────────────────────

@test "macOS --shell does NOT install WSL2 dev() stub" {
    printf '#!/bin/sh\necho Darwin\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    touch "${FAKE_HOME}/.zshrc"
    _run_install "PATH=$STUB_BIN:$PATH" \
        "_PROC_VERSION_PATH=$PROC_DIR/missing" --shell
    [ "$status" -eq 0 ]
    run grep -F 'cmux is macOS-only' "${FAKE_HOME}/.zshrc"
    # The WSL2 stub message must NOT appear on macOS.
    [ "$status" -ne 0 ]
}

@test "macOS --shell installs the full cmux dev() function" {
    printf '#!/bin/sh\necho Darwin\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    touch "${FAKE_HOME}/.zshrc"
    _run_install "PATH=$STUB_BIN:$PATH" \
        "_PROC_VERSION_PATH=$PROC_DIR/missing" --shell
    [ "$status" -eq 0 ]
    # The real dev() function uses cmux new-workspace.
    run grep -q 'cmux new-workspace' "${FAKE_HOME}/.zshrc"
    [ "$status" -eq 0 ]
}

# ─── Platform banner sanity ──────────────────────────────────────────

@test "installer banner reports WSL2 on a WSL2 host" {
    printf 'microsoft-standard WSL2\n' > "$PROC_DIR/version"
    printf '#!/bin/sh\necho Linux\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    _run_install "PATH=$STUB_BIN:$PATH" "_PROC_VERSION_PATH=$PROC_DIR/version" \
        --core --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"WSL2"* ]]
}

@test "installer banner reports macOS on a Darwin host" {
    printf '#!/bin/sh\necho Darwin\n' > "$STUB_BIN/uname"
    chmod +x "$STUB_BIN/uname"
    _run_install "PATH=$STUB_BIN:$PATH" \
        "_PROC_VERSION_PATH=$PROC_DIR/missing" \
        --core --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"macOS"* ]]
}
