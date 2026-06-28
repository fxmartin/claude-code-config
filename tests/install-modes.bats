#!/usr/bin/env bats
# Story 3.1-001 — behavior tests for the modal installer.
#
# install.sh dispatches to install/core.sh, install/tools.sh, install/mcp.sh,
# install/shell.sh based on flags. Each mode must be idempotent and --dry-run
# must EXACTLY match the actions that the actual run would perform.
#
# Tests isolate state by pointing HOME at a per-test temp directory.

INSTALL="${BATS_TEST_DIRNAME}/../install.sh"

setup() {
    FAKE_HOME="$(mktemp -d)"
    # Stub PATH that strips brew / apt so --tools never tries to actually
    # install anything on the runner; the dispatcher should still report what
    # it *would* do.
    STUB_BIN="$(mktemp -d)"
    # Provide a stub "command" wrapper so brew / apt detection both return
    # false even on a Mac-with-brew runner.
    export FAKE_HOME STUB_BIN
}

teardown() {
    [ -n "${FAKE_HOME:-}" ] && rm -rf "${FAKE_HOME}"
    [ -n "${STUB_BIN:-}"  ] && rm -rf "${STUB_BIN}"
}

# Run installer with a pristine HOME and no .env loaded.
_run_install() {
    run env HOME="${FAKE_HOME}" CLAUDE_CONFIG_NO_ENV=1 bash "${INSTALL}" "$@"
}

@test "--help prints usage and exits 0" {
    _run_install --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"--core"* ]]
    [[ "$output" == *"--tools"* ]]
    [[ "$output" == *"--mcp"* ]]
    [[ "$output" == *"--shell"* ]]
    [[ "$output" == *"--all"* ]]
}

# ─── --core ──────────────────────────────────────────────────────────

@test "--core --dry-run exits 0 and makes no changes" {
    _run_install --core --dry-run
    [ "$status" -eq 0 ]
    [ ! -e "${FAKE_HOME}/.claude" ]
    [ ! -e "${FAKE_HOME}/.claude.json" ]
    # zero real symlinks anywhere under HOME
    run find "${FAKE_HOME}" -type l
    [ -z "$output" ]
}

@test "--core --dry-run lists exactly the symlink set" {
    _run_install --core --dry-run
    [ "$status" -eq 0 ]
    for target in \
        CLAUDE.md agents commands settings.json statusline-command.sh \
        keybindings.json reference-docs docs skills hooks fx-claude-config \
        codex-build-adapter.sh qwen-build-adapter.sh
    do
        [[ "$output" == *"[dry-run]"*"${target}"* ]]
    done
    # 13 ln -s lines expected (10 config items + 1 marketplace + 2 build
    # adapters onto PATH, Story 21.3-001). Shared skills are committed relative
    # symlinks inside commands/, so the installer no longer links them in
    # separately (they would dirty the repo).
    ln_lines="$(printf '%s\n' "$output" | grep -c '\[dry-run\] ln -s')"
    [ "$ln_lines" -eq 13 ]
}

@test "--core --dry-run previews git submodule init" {
    _run_install --core --dry-run
    [ "$status" -eq 0 ]
    # skills/model-shelf is a git submodule; a plain clone leaves it empty,
    # so --core must initialize submodules or the symlinked skill is dead.
    [[ "$output" == *"[dry-run] git -C"*"submodule update --init"* ]]
}

@test "--core creates the symlink set and exits 0" {
    _run_install --core
    [ "$status" -eq 0 ]
    [ -L "${FAKE_HOME}/.claude/CLAUDE.md" ]
    [ -L "${FAKE_HOME}/.claude/agents" ]
    [ -L "${FAKE_HOME}/.claude/commands" ]
    [ -L "${FAKE_HOME}/.claude/skills" ]
    [ -L "${FAKE_HOME}/.claude/hooks" ]
    [ -L "${FAKE_HOME}/.claude/settings.json" ]
    [ -L "${FAKE_HOME}/.claude/statusline-command.sh" ]
    [ -L "${FAKE_HOME}/.claude/keybindings.json" ]
    [ -L "${FAKE_HOME}/.claude/reference-docs" ]
    [ -L "${FAKE_HOME}/.claude/docs" ]
    [ -L "${FAKE_HOME}/.claude/plugins/marketplaces/fx-claude-config" ]
}

@test "--core is idempotent on second run" {
    _run_install --core
    [ "$status" -eq 0 ]
    # Snapshot inodes / link targets after the first run.
    before="$(find "${FAKE_HOME}/.claude" -maxdepth 3 | LC_ALL=C sort)"
    _run_install --core
    [ "$status" -eq 0 ]
    after="$(find "${FAKE_HOME}/.claude" -maxdepth 3 | LC_ALL=C sort)"
    [ "$before" = "$after" ]
}

# ─── --core build adapters on PATH (Story 21.3-001) ──────────────────

@test "--core symlinks the build adapters onto PATH" {
    _run_install --core
    [ "$status" -eq 0 ]
    # The harness registry dispatches the workers by bare name, so the adapters
    # must resolve on PATH — the installer mirrors them into ~/.local/bin (where
    # uv places sdlc), which under the test HOME is $FAKE_HOME/.local/bin.
    local repo codex qwen
    repo="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
    codex="${FAKE_HOME}/.local/bin/codex-build-adapter.sh"
    qwen="${FAKE_HOME}/.local/bin/qwen-build-adapter.sh"
    [ -L "$codex" ]
    [ -L "$qwen" ]
    [ "$(readlink "$codex")" = "${repo}/scripts/codex-build-adapter.sh" ]
    [ "$(readlink "$qwen")"  = "${repo}/scripts/qwen-build-adapter.sh" ]
    # Symlinks resolve to executable regular files.
    [ -x "$codex" ]
    [ -x "$qwen" ]
}

@test "--core adapter symlinks are idempotent" {
    _run_install --core
    [ "$status" -eq 0 ]
    local codex qwen before_codex before_qwen
    codex="${FAKE_HOME}/.local/bin/codex-build-adapter.sh"
    qwen="${FAKE_HOME}/.local/bin/qwen-build-adapter.sh"
    before_codex="$(readlink "$codex")"
    before_qwen="$(readlink "$qwen")"
    _run_install --core
    [ "$status" -eq 0 ]
    [ -L "$codex" ]
    [ -L "$qwen" ]
    [ "$(readlink "$codex")" = "$before_codex" ]
    [ "$(readlink "$qwen")"  = "$before_qwen" ]
}

@test "--core warns when the bin dir is not on PATH" {
    # The randomized $FAKE_HOME/.local/bin is never on the inherited PATH, so the
    # installer must emit the actionable "add it to PATH" warning with the exact
    # export line.
    _run_install --core
    [ "$status" -eq 0 ]
    [[ "$output" == *"is not on your PATH"* ]]
    [[ "$output" == *"export PATH=\"${FAKE_HOME}/.local/bin:"* ]]
}

@test "--core (no other flag) skips tools, mcp, shell" {
    _run_install --core
    [ "$status" -eq 0 ]
    # MCP file must not be created when --mcp is not selected.
    [ ! -e "${FAKE_HOME}/.claude.json" ]
    # shellrc files must not be touched.
    [ ! -e "${FAKE_HOME}/.zshrc" ]
    [ ! -e "${FAKE_HOME}/.bashrc" ]
}

# ─── --tools ─────────────────────────────────────────────────────────

@test "--tools --dry-run previews homebrew/apt actions without running them" {
    _run_install --tools --dry-run
    [ "$status" -eq 0 ]
    # Expect dry-run output to reference at least one package manager
    # (brew/apt) or a fallback warning when neither is present.
    [[ "$output" == *"brew"* || "$output" == *"apt"* || "$output" == *"Homebrew"* ]]
    # No real install should have happened; the marker file used by the
    # tools module to track yazi config must be absent.
    [ ! -e "${FAKE_HOME}/.config/yazi/yazi.toml" ]
    [ ! -e "${FAKE_HOME}/.config/yazi/init.lua" ]
}

@test "--tools --dry-run does not create yazi config files" {
    _run_install --tools --dry-run
    [ "$status" -eq 0 ]
    [ ! -e "${FAKE_HOME}/.config" ]
}

# ─── --mcp ───────────────────────────────────────────────────────────

@test "--mcp --dry-run previews the jq merge" {
    _run_install --mcp --dry-run
    [ "$status" -eq 0 ]
    # Output references MCP / jq / config.template merge intent
    [[ "$output" == *"MCP"* || "$output" == *"mcp"* ]]
    # No claude.json should have been created.
    [ ! -e "${FAKE_HOME}/.claude.json" ]
}

@test "--mcp creates ~/.claude.json from template" {
    _run_install --mcp
    [ "$status" -eq 0 ]
    [ -f "${FAKE_HOME}/.claude.json" ]
    # File must be valid JSON
    run jq -e '.mcpServers' "${FAKE_HOME}/.claude.json"
    [ "$status" -eq 0 ]
}

@test "--mcp is idempotent" {
    _run_install --mcp
    [ "$status" -eq 0 ]
    before="$(cat "${FAKE_HOME}/.claude.json")"
    _run_install --mcp
    [ "$status" -eq 0 ]
    after="$(cat "${FAKE_HOME}/.claude.json")"
    [ "$before" = "$after" ]
}

# ─── --shell ─────────────────────────────────────────────────────────

@test "--shell --dry-run previews zshrc/bashrc appends" {
    _run_install --shell --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"dev()"* || "$output" == *"y()"* || "$output" == *"zshrc"* || "$output" == *"bashrc"* ]]
    # No shellrc must have been created.
    [ ! -e "${FAKE_HOME}/.zshrc" ]
    [ ! -e "${FAKE_HOME}/.bashrc" ]
}

@test "--shell appends dev() and y() to ~/.zshrc" {
    touch "${FAKE_HOME}/.zshrc"
    _run_install --shell
    [ "$status" -eq 0 ]
    run grep -q 'function dev()' "${FAKE_HOME}/.zshrc"
    [ "$status" -eq 0 ]
    run grep -q 'function y()' "${FAKE_HOME}/.zshrc"
    [ "$status" -eq 0 ]
}

@test "--shell is idempotent" {
    touch "${FAKE_HOME}/.zshrc"
    _run_install --shell
    [ "$status" -eq 0 ]
    before="$(cat "${FAKE_HOME}/.zshrc")"
    _run_install --shell
    [ "$status" -eq 0 ]
    after="$(cat "${FAKE_HOME}/.zshrc")"
    [ "$before" = "$after" ]
}

# ─── --all ───────────────────────────────────────────────────────────

@test "--all --dry-run exercises every mode and exits 0" {
    _run_install --all --dry-run
    [ "$status" -eq 0 ]
    # Every mode's marker phrase appears at least once.
    [[ "$output" == *"core"* ]]
    [[ "$output" == *"tools"* || "$output" == *"Tools"* ]]
    [[ "$output" == *"MCP"* || "$output" == *"mcp"* ]]
    [[ "$output" == *"shell"* || "$output" == *"Shell"* ]]
    # No real changes
    [ ! -e "${FAKE_HOME}/.claude" ]
    [ ! -e "${FAKE_HOME}/.claude.json" ]
}

@test "--all dry-run output equals union of per-mode dry-runs for ln + jq" {
    _run_install --all --dry-run
    [ "$status" -eq 0 ]
    all_ln="$(printf '%s\n' "$output" | grep -c '\[dry-run\] ln -s')"
    # 13 = the --core symlink set (tools/shell modes create no symlinks).
    [ "$all_ln" -eq 13 ]
}

# ─── Backward-compat flags ───────────────────────────────────────────

@test "--skip-mcp emits deprecation warning" {
    _run_install --skip-mcp --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"deprecat"* || "$output" == *"DEPRECAT"* ]]
}

@test "--skip-mcp is equivalent to --core --tools --shell (dry-run)" {
    _run_install --skip-mcp --dry-run
    [ "$status" -eq 0 ]
    out_legacy="$output"
    _run_install --core --tools --shell --dry-run
    [ "$status" -eq 0 ]
    out_new="$output"
    # Both should perform the same number of ln operations (13 core)
    # and neither should attempt the MCP jq merge.
    legacy_ln="$(printf '%s\n' "$out_legacy" | grep -c '\[dry-run\] ln -s')"
    new_ln="$(printf '%s\n'    "$out_new"    | grep -c '\[dry-run\] ln -s')"
    [ "$legacy_ln" -eq 13 ]
    [ "$new_ln" -eq 13 ]
    # Neither should mention writing to ~/.claude.json
    [[ "$out_legacy" != *"Merged MCP"* ]]
    [[ "$out_new" != *"Merged MCP"* ]]
}

@test "--skip-tools emits deprecation warning" {
    _run_install --skip-tools --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"deprecat"* || "$output" == *"DEPRECAT"* ]]
}

@test "--skip-tools is equivalent to --core --mcp --shell (dry-run)" {
    _run_install --skip-tools --dry-run
    [ "$status" -eq 0 ]
    out_legacy="$output"
    _run_install --core --mcp --shell --dry-run
    [ "$status" -eq 0 ]
    out_new="$output"
    legacy_ln="$(printf '%s\n' "$out_legacy" | grep -c '\[dry-run\] ln -s')"
    new_ln="$(printf '%s\n'    "$out_new"    | grep -c '\[dry-run\] ln -s')"
    [ "$legacy_ln" -eq 13 ]
    [ "$new_ln" -eq 13 ]
}

# ─── --uninstall ─────────────────────────────────────────────────────

@test "--uninstall removes symlinks created by --core" {
    _run_install --core
    [ "$status" -eq 0 ]
    [ -L "${FAKE_HOME}/.claude/CLAUDE.md" ]
    _run_install --uninstall
    [ "$status" -eq 0 ]
    [ ! -L "${FAKE_HOME}/.claude/CLAUDE.md" ]
    [ ! -L "${FAKE_HOME}/.claude/agents" ]
    [ ! -L "${FAKE_HOME}/.claude/skills" ]
}

@test "--uninstall removes the adapter symlinks" {
    _run_install --core
    [ "$status" -eq 0 ]
    [ -L "${FAKE_HOME}/.local/bin/codex-build-adapter.sh" ]
    [ -L "${FAKE_HOME}/.local/bin/qwen-build-adapter.sh" ]
    _run_install --uninstall
    [ "$status" -eq 0 ]
    [ ! -L "${FAKE_HOME}/.local/bin/codex-build-adapter.sh" ]
    [ ! -L "${FAKE_HOME}/.local/bin/qwen-build-adapter.sh" ]
}

# ─── Dry-run drift fix (the Codex regression) ────────────────────────

@test "--core --dry-run does NOT print 'Created ~/.claude' when dir is absent" {
    [ ! -e "${FAKE_HOME}/.claude" ]
    _run_install --core --dry-run
    [ "$status" -eq 0 ]
    # The legacy bug printed "Created ~/.claude" during dry-run even though no
    # directory was actually created. The fix routes mkdir through the same
    # dry-run guard as everything else, so the user-facing message must be
    # absent (or clearly tagged [dry-run]) when nothing actually happened.
    if [[ "$output" == *"Created ${FAKE_HOME}/.claude"* ]]; then
        # If the line appears it MUST be tagged as a preview.
        [[ "$output" == *"[dry-run]"*"${FAKE_HOME}/.claude"* ]]
    fi
    # Belt-and-braces: the directory itself was not created.
    [ ! -e "${FAKE_HOME}/.claude" ]
}
