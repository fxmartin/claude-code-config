#!/usr/bin/env bats
# ABOUTME: Behavior tests for scripts/deploy.sh — the single-command deploy that
# ABOUTME: keeps the sdlc controller and the autonomous-sdlc plugin on one version.
#
# deploy.sh composes two steps that had drifted apart in practice, run in this
# order (the remote, fallible step first — see the ordering tests below):
#
#   1. claude plugin update           → move the plugin pointer to the new version
#   2. scripts/install-controller.sh  → uv tool install --force controller/
#
# Both steps are expensive and mutate the machine, so the script exposes two
# seams the suite drives instead of the real thing:
#
#   - INSTALL_CONTROLLER  overrides the path to the controller step's script.
#   - `claude` is resolved from PATH, so a stub earlier on PATH intercepts the
#     plugin step.
#
# Each stub touches a marker file and appends to a shared order log; tests
# assert on markers and order rather than on stdout, so the assertions survive
# log rewording.

DEPLOY="${BATS_TEST_DIRNAME}/../scripts/deploy.sh"

setup() {
    TMP="$(mktemp -d)"
    STUB_BIN="${TMP}/bin"
    mkdir -p "${STUB_BIN}"

    CONTROLLER_MARKER="${TMP}/controller-ran"
    PLUGIN_MARKER="${TMP}/plugin-ran"
    ORDER_LOG="${TMP}/order.log"

    # Stub for the controller step, injected via the INSTALL_CONTROLLER seam.
    FAKE_INSTALL_CONTROLLER="${TMP}/fake-install-controller.sh"
    cat >"${FAKE_INSTALL_CONTROLLER}" <<EOF
#!/usr/bin/env bash
touch "${CONTROLLER_MARKER}"
echo controller >>"${ORDER_LOG}"
EOF
    chmod +x "${FAKE_INSTALL_CONTROLLER}"

    # Stub for the plugin step, injected by prepending STUB_BIN to PATH.
    # Records the argv so a test can assert the plugin id is passed through.
    cat >"${STUB_BIN}/claude" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >"${PLUGIN_MARKER}"
echo plugin >>"${ORDER_LOG}"
EOF
    chmod +x "${STUB_BIN}/claude"

    # Stub container runtime for the sandbox-image step (issue #614), injected
    # via the SANDBOX_RUNTIME seam so no suite run ever builds a real image.
    # `build` records its argv; `image inspect` prints a podman-style bare id.
    SANDBOX_MARKER="${TMP}/sandbox-built"
    FAKE_ID="$(printf 'd%.0s' $(seq 64))"
    cat >"${STUB_BIN}/fake-runtime" <<EOF
#!/usr/bin/env bash
case "\$1" in
  build) printf '%s\n' "\$*" >"${SANDBOX_MARKER}"; echo sandbox >>"${ORDER_LOG}" ;;
  image) echo "\${FAKE_IMAGE_ID:-${FAKE_ID}}" ;;
esac
EOF
    chmod +x "${STUB_BIN}/fake-runtime"

    # A throwaway copy of the pin file, so the repo's real pin is never touched.
    PIN_FILE="${TMP}/sandbox-image.yaml"
    printf '# pins\namd64:\narm64:\n' >"${PIN_FILE}"
    case "$(uname -m)" in
        x86_64|amd64) ARCH=amd64 ;;
        aarch64|arm64) ARCH=arm64 ;;
    esac
}

teardown() {
    [ -n "${TMP:-}" ] && rm -rf "${TMP}"
}

# Run deploy.sh with both seams active and `claude` present on PATH.
_run_deploy() {
    run env \
        INSTALL_CONTROLLER="${FAKE_INSTALL_CONTROLLER}" \
        SANDBOX_RUNTIME="${STUB_BIN}/fake-runtime" \
        SANDBOX_PIN_FILE="${PIN_FILE}" \
        PATH="${STUB_BIN}:${PATH}" \
        bash "${DEPLOY}" "$@"
}

# Run deploy.sh with an empty PATH prefix so `claude` cannot be found.
# `command -v` still searches the real PATH, so we blank it to a minimal set
# that has coreutils but no `claude`.
_run_deploy_without_claude() {
    run env \
        INSTALL_CONTROLLER="${FAKE_INSTALL_CONTROLLER}" \
        SANDBOX_RUNTIME="${STUB_BIN}/fake-runtime" \
        SANDBOX_PIN_FILE="${PIN_FILE}" \
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

# Ordering: the plugin update is the remote, fallible step (marketplace,
# network) and its effect is deferred until Claude Code restarts; the controller
# install is local and idempotent. Running the fallible step FIRST means its
# failure leaves the machine untouched — preflight cannot predict a runtime
# marketplace failure, but ordering can contain it.
@test "default run updates the plugin before installing the controller" {
    _run_deploy
    [ "$status" -eq 0 ]
    run cat "${ORDER_LOG}"
    [ "${lines[0]}" = "sandbox" ]
    [ "${lines[1]}" = "plugin" ]
    [ "${lines[2]}" = "controller" ]
}

@test "a failing plugin update aborts before the controller install" {
    cat >"${STUB_BIN}/claude" <<'EOF'
#!/usr/bin/env bash
echo "marketplace unreachable" >&2
exit 1
EOF
    chmod +x "${STUB_BIN}/claude"
    _run_deploy
    [ "$status" -ne 0 ]
    [ ! -e "${CONTROLLER_MARKER}" ]
}

# The residual window: plugin updated, then the local controller install fails.
# The RUNNING system is still consistent (the new plugin only loads on restart),
# and re-running deploy.sh converges — but the exit must be non-zero and must
# say exactly that, so nobody restarts Claude Code onto a mismatched pair.
@test "a failing controller install after the plugin update exits non-zero with a converge remedy" {
    cat >"${FAKE_INSTALL_CONTROLLER}" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
    chmod +x "${FAKE_INSTALL_CONTROLLER}"
    _run_deploy
    [ "$status" -ne 0 ]
    [ -e "${PLUGIN_MARKER}" ]
    [[ "$output" == *"re-run"* ]]
    [[ "$output" == *"restart"* ]]
}

# --- Sandbox image (issue #614) ---------------------------------------------
# deploy.sh builds controller/sandbox/Containerfile and pins the resulting image
# id for this host's arch. The pin is controller package data, so the build runs
# before the controller install; and before the plugin, so a failed build (a
# base-image pull, npm) leaves the machine untouched.

@test "default run builds the sandbox image from the repo Containerfile" {
    _run_deploy
    [ "$status" -eq 0 ]
    run cat "${SANDBOX_MARKER}"
    [[ "$output" == *"build"* ]]
    [[ "$output" == *"controller/sandbox/Containerfile"* ]]
}

@test "the pinned id is recorded for this host's arch, sha256-prefixed" {
    _run_deploy
    [ "$status" -eq 0 ]
    run grep "^${ARCH}:" "${PIN_FILE}"
    [ "$output" = "${ARCH}: sha256:${FAKE_ID}" ]
}

@test "the other arch's pin is left untouched" {
    other=arm64; [ "${ARCH}" = arm64 ] && other=amd64
    other_pin="sha256:$(printf 'c%.0s' $(seq 64))"
    printf '%s: %s\n' "${other}" "${other_pin}" >>"${PIN_FILE}"
    _run_deploy
    [ "$status" -eq 0 ]
    run grep "^${other}: sha256" "${PIN_FILE}"
    [ "$output" = "${other}: ${other_pin}" ]
    run grep -c "^${ARCH}:" "${PIN_FILE}"
    [ "$output" = "1" ]
}

@test "a docker-style sha256-prefixed id is not double-prefixed" {
    FAKE_IMAGE_ID="sha256:${FAKE_ID}" _run_deploy
    [ "$status" -eq 0 ]
    run grep "^${ARCH}:" "${PIN_FILE}"
    [ "$output" = "${ARCH}: sha256:${FAKE_ID}" ]
}

@test "a malformed image id fails before the plugin or controller move" {
    FAKE_IMAGE_ID="not-an-id" _run_deploy
    [ "$status" -ne 0 ]
    [ ! -e "${PLUGIN_MARKER}" ]
    [ ! -e "${CONTROLLER_MARKER}" ]
    run grep -c "sha256" "${PIN_FILE}"
    [ "$output" = "0" ]
}

@test "a failing image build aborts with the machine untouched" {
    printf '#!/usr/bin/env bash\nexit 1\n' >"${STUB_BIN}/fake-runtime"
    _run_deploy
    [ "$status" -ne 0 ]
    [ ! -e "${PLUGIN_MARKER}" ]
    [ ! -e "${CONTROLLER_MARKER}" ]
}

@test "--skip-sandbox-image skips the build and leaves the pin alone" {
    _run_deploy --skip-sandbox-image
    [ "$status" -eq 0 ]
    [ ! -e "${SANDBOX_MARKER}" ]
    [ -e "${CONTROLLER_MARKER}" ]
    run grep -c "sha256" "${PIN_FILE}"
    [ "$output" = "0" ]
}

@test "--plugin-only never builds the sandbox image" {
    _run_deploy --plugin-only
    [ "$status" -eq 0 ]
    [ ! -e "${SANDBOX_MARKER}" ]
}

@test "--dry-run reports the sandbox build but builds nothing" {
    _run_deploy --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"would run"*"build"* ]]
    [ ! -e "${SANDBOX_MARKER}" ]
    run grep -c "sha256" "${PIN_FILE}"
    [ "$output" = "0" ]
}

@test "a forced runtime that is missing fails preflight" {
    run env \
        INSTALL_CONTROLLER="${FAKE_INSTALL_CONTROLLER}" \
        SANDBOX_RUNTIME="${TMP}/no-such-runtime" \
        SANDBOX_PIN_FILE="${PIN_FILE}" \
        PATH="${STUB_BIN}:${PATH}" \
        bash "${DEPLOY}"
    [ "$status" -ne 0 ]
    [ ! -e "${PLUGIN_MARKER}" ]
    [ ! -e "${CONTROLLER_MARKER}" ]
}

@test "no container runtime on PATH skips the image and still deploys" {
    # A PATH holding only the stubs and the few tools deploy.sh needs, so a real
    # podman/docker on the host cannot be auto-detected.
    MINBIN="${TMP}/minbin"
    mkdir -p "${MINBIN}"
    for tool in bash env dirname sed awk uname mktemp mv cat touch; do
        ln -s "$(command -v "${tool}")" "${MINBIN}/${tool}"
    done
    ln -s "${STUB_BIN}/claude" "${MINBIN}/claude"
    run env -u SANDBOX_RUNTIME \
        INSTALL_CONTROLLER="${FAKE_INSTALL_CONTROLLER}" \
        SANDBOX_PIN_FILE="${PIN_FILE}" \
        PATH="${MINBIN}" \
        "${MINBIN}/bash" "${DEPLOY}"
    [ "$status" -eq 0 ]
    [[ "$output" == *"sandbox image not built"* ]]
    [ -e "${CONTROLLER_MARKER}" ]
    [ -e "${PLUGIN_MARKER}" ]
}
