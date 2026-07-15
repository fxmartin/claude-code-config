#!/usr/bin/env bats
# Story 2.1-002 — behavior tests for `install.sh --dry-run`.
#
# install.sh creates symlinks under ~/.claude. The `--dry-run` flag must make
# every mutating step a no-op while still reporting it. To keep the real
# ~/.claude untouched we point HOME at an isolated temp directory and snapshot
# it before and after the run.
#
# `--skip-tools` skips the macOS-only Homebrew path and `--skip-mcp` skips the
# ~/.claude.json merge, so the suite is portable across macOS, Linux and the
# Ubuntu runner that stands in for WSL2 in CI.

INSTALL="${BATS_TEST_DIRNAME}/../install.sh"

setup() {
    FAKE_HOME="$(mktemp -d)"
}

teardown() {
    [ -n "${FAKE_HOME:-}" ] && rm -rf "${FAKE_HOME}"
}

# Snapshot every path under a directory (sorted). Empty string when missing.
_snapshot() {
    local dir="$1"
    [ -d "$dir" ] || { printf ''; return 0; }
    find "$dir" | LC_ALL=C sort
}

@test "install.sh --dry-run --skip-tools --skip-mcp exits 0" {
    run env HOME="${FAKE_HOME}" bash "${INSTALL}" --dry-run --skip-tools --skip-mcp
    [ "$status" -eq 0 ]
}

@test "dry-run creates no symlinks under HOME" {
    before="$(_snapshot "${FAKE_HOME}")"
    run env HOME="${FAKE_HOME}" bash "${INSTALL}" --dry-run --skip-tools --skip-mcp
    [ "$status" -eq 0 ]
    after="$(_snapshot "${FAKE_HOME}")"
    # The directory tree must be byte-identical before and after.
    [ "$before" = "$after" ]
    # And there must be zero symlinks anywhere under HOME.
    run find "${FAKE_HOME}" -type l
    [ -z "$output" ]
}

@test "dry-run does not create ~/.claude or ~/.claude.json" {
    run env HOME="${FAKE_HOME}" bash "${INSTALL}" --dry-run --skip-tools --skip-mcp
    [ "$status" -eq 0 ]
    [ ! -e "${FAKE_HOME}/.claude" ]
    [ ! -e "${FAKE_HOME}/.claude.json" ]
}

@test "dry-run output mentions every target file" {
    run env HOME="${FAKE_HOME}" bash "${INSTALL}" --dry-run --skip-tools --skip-mcp
    [ "$status" -eq 0 ]
    # Every config target install.sh links must show up in the dry-run report.
    for target in \
        CLAUDE.md agents commands settings.json statusline-command.sh \
        keybindings.json reference-docs docs skills hooks fx-claude-config \
        codex-build-adapter.sh qwen-build-adapter.sh overengineering-lens.sh
    do
        [[ "$output" == *"[dry-run]"*"${target}"* ]]
    done
}

@test "dry-run emits a [dry-run] line for every symlink it would create" {
    run env HOME="${FAKE_HOME}" bash "${INSTALL}" --dry-run --skip-tools --skip-mcp
    [ "$status" -eq 0 ]
    # install.sh links 10 config items + the local marketplace + 2 build-harness
    # adapters (codex/qwen, Story 21.3-001) + the over-engineering lens wrapper
    # (issue #445) = 14 symlinks. Shared skills (ADR-002)
    # are committed relative symlinks inside commands/, carried in by the commands
    # directory symlink, so they are not linked separately (doing so would rewrite
    # them as absolute and dirty the repo).
    ln_lines="$(printf '%s\n' "$output" | grep -c '\[dry-run\] ln -s')"
    [ "$ln_lines" -eq 14 ]
}
