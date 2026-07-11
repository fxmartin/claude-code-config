#!/usr/bin/env bats
# ABOUTME: Behavior tests for scripts/deploy.sh — the single-command deploy that
# ABOUTME: keeps the sdlc controller and the autonomous-sdlc plugin on one version.
#
# deploy.sh composes two steps that had drifted apart in practice:
#
#   1. scripts/install-controller.sh  → uv tool install --force controller/
#   2. claude plugin update           → move the plugin pointer to the new version
#
# Both steps are expensive and mutate the machine, so the script exposes two
# seams the suite drives instead of the real thing:
#
#   - INSTALL_CONTROLLER  overrides the path to step 1's script.
#   - `claude` is resolved from PATH, so a stub earlier on PATH intercepts step 2.
#
# Each stub touches a marker file; a test asserts on the presence or absence of
# that marker rather than on stdout, so the assertions survive log rewording.

DEPLOY="${BATS_TEST_DIRNAME}/../scripts/deploy.sh"

setup() {
    TMP="$(mktemp -d)"
    STUB_BIN="${TMP}/bin"
    mkdir -p "${STUB_BIN}"

    CONTROLLER_MARKER="${TMP}/controller-ran"
    PLUGIN_MARKER="${TMP}/plugin-ran"

    # Stub for step 1, injected via the INSTALL_CONTROLLER seam.
    FAKE_INSTALL_CONTROLLER="${TMP}/fake-install-controller.sh"
    cat >"${FAKE_INSTALL_CONTROLLER}" <<EOF
#!/usr/bin/env bash
touch "${CONTROLLER_MARKER}"
EOF
    chmod +x "${FAKE_INSTALL_CONTROLLER}"

    # Stub for step 2, injected by prepending STUB_BIN to PATH. Records the
    # argv so a test can assert the plugin id is passed through intact.
    cat >"${STUB_BIN}/claude" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >"${PLUGIN_MARKER}"
EOF
    chmod +x "${STUB_BIN}/claude"
}

teardown() {
    [ -n "${TMP:-}" ] && rm -rf "${TMP}"
}

# Run deploy.sh with both seams active and `claude` present on PATH.
_run_deploy() {
    run env \
        INSTALL_CONTROLLER="${FAKE_INSTALL_CONTROLLER}" \
        PATH="${STUB_BIN}:${PATH}" \
        bash "${DEPLOY}" "$@"
}

# Run deploy.sh with an empty PATH prefix so `claude` cannot be found.
# `command -v` still searches the real PATH, so we blank it to a minimal set
# that has coreutils but no `claude`.
_run_deploy_without_claude() {
    run env \
        INSTALL_CONTROLLER="${FAKE_INSTALL_CONTROLLER}" \
        PATH="/usr/bin:/bin" \
        bash "${DEPLOY}" "$@"
}

@test "deploy.sh is executable" {
    [ -x "${DEPLOY}" ]
}

@test "--help exits 0 and documents both steps" {
    run bash "${DEPLOY}" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"controller"* ]]
    [[ "$output" == *"plugin"* ]]
}

# usage() prints a hardcoded line range of this script's header. Editing the
# header silently truncates --help unless that range moves too; assert on the
# last paragraph so the drift is caught here rather than by a confused user.
@test "--help prints the whole header, through the verify hint" {
    run bash "${DEPLOY}" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"restart of Claude Code"* ]]
    [[ "$output" == *"sdlc --version"* ]]
}

@test "--help documents that a default run requires claude" {
    run bash "${DEPLOY}" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"--controller-only"* ]]
}

@test "--help runs neither step" {
    _run_deploy --help
    [ "$status" -eq 0 ]
    [ ! -e "${CONTROLLER_MARKER}" ]
    [ ! -e "${PLUGIN_MARKER}" ]
}

@test "default run performs both steps" {
    _run_deploy
    [ "$status" -eq 0 ]
    [ -e "${CONTROLLER_MARKER}" ]
    [ -e "${PLUGIN_MARKER}" ]
}

@test "default run passes the plugin@marketplace id to claude" {
    _run_deploy
    [ "$status" -eq 0 ]
    run cat "${PLUGIN_MARKER}"
    [[ "$output" == *"plugin update"* ]]
    [[ "$output" == *"autonomous-sdlc@fx-claude-config"* ]]
}

@test "--dry-run runs neither step" {
    _run_deploy --dry-run
    [ "$status" -eq 0 ]
    [ ! -e "${CONTROLLER_MARKER}" ]
    [ ! -e "${PLUGIN_MARKER}" ]
}

@test "--dry-run still reports both steps" {
    _run_deploy --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"install-controller"* ]]
    [[ "$output" == *"autonomous-sdlc@fx-claude-config"* ]]
}

@test "--controller-only skips the plugin update" {
    _run_deploy --controller-only
    [ "$status" -eq 0 ]
    [ -e "${CONTROLLER_MARKER}" ]
    [ ! -e "${PLUGIN_MARKER}" ]
}

@test "--plugin-only skips the controller install" {
    _run_deploy --plugin-only
    [ "$status" -eq 0 ]
    [ ! -e "${CONTROLLER_MARKER}" ]
    [ -e "${PLUGIN_MARKER}" ]
}

@test "--controller-only and --plugin-only together is rejected" {
    _run_deploy --controller-only --plugin-only
    [ "$status" -ne 0 ]
}

@test "unknown flag exits non-zero" {
    _run_deploy --no-such-flag
    [ "$status" -ne 0 ]
}

# A default run that moves only the controller pointer is the exact version drift
# this script exists to prevent. A non-zero exit is not enough: `claude`'s absence
# is knowable before any mutation, so the run must abort in preflight, leaving the
# machine untouched. `--controller-only` is the supported way to opt out.
@test "missing claude fails the default run" {
    _run_deploy_without_claude
    [ "$status" -ne 0 ]
    [[ "$output" == *"claude"* ]]
}

@test "missing claude aborts before installing the controller" {
    _run_deploy_without_claude
    [ "$status" -ne 0 ]
    [ ! -e "${CONTROLLER_MARKER}" ]
    [ ! -e "${PLUGIN_MARKER}" ]
}

@test "--dry-run without claude on PATH still exits 0 and mutates nothing" {
    _run_deploy_without_claude --dry-run
    [ "$status" -eq 0 ]
    [ ! -e "${CONTROLLER_MARKER}" ]
    [ ! -e "${PLUGIN_MARKER}" ]
}

@test "a missing controller installer aborts before the plugin update" {
    rm -f "${FAKE_INSTALL_CONTROLLER}"
    _run_deploy
    [ "$status" -ne 0 ]
    [ ! -e "${PLUGIN_MARKER}" ]
}

@test "missing claude points the user at --controller-only" {
    _run_deploy_without_claude
    [ "$status" -ne 0 ]
    [[ "$output" == *"--controller-only"* ]]
}

@test "missing claude fails when the plugin step was explicitly requested" {
    _run_deploy_without_claude --plugin-only
    [ "$status" -ne 0 ]
}

@test "--controller-only succeeds without claude on PATH" {
    _run_deploy_without_claude --controller-only
    [ "$status" -eq 0 ]
    [ -e "${CONTROLLER_MARKER}" ]
}

@test "a failing controller install aborts before the plugin update" {
    cat >"${FAKE_INSTALL_CONTROLLER}" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
    chmod +x "${FAKE_INSTALL_CONTROLLER}"
    _run_deploy
    [ "$status" -ne 0 ]
    [ ! -e "${PLUGIN_MARKER}" ]
}
