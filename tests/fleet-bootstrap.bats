#!/usr/bin/env bats
# ABOUTME: Behavior tests for scripts/fleet-bootstrap.sh (Story 35.2-008): the Linux dev box
# ABOUTME: becomes a worker — and a second run changes nothing (idempotent).
#
# Everything the script would install or start is a stub earlier on PATH (or an
# INSTALL_SH / DEPLOY_SH seam), and HOME is a scratch directory, so no run touches
# the machine. Stubs append to one call log; tests assert on it and on the files
# the script writes, not on stdout wording. Marketplace/plugin state lives in
# marker files, so a second run sees what the first one "installed".

BOOTSTRAP="${BATS_TEST_DIRNAME}/../scripts/fleet-bootstrap.sh"
TEMPLATE="${BATS_TEST_DIRNAME}/../templates/systemd/sdlc-worker.service"
QUEUE="http://home-lab.example.ts.net:8790"

setup() {
    TMP="$(mktemp -d)"
    export HOME="${TMP}/home"
    mkdir -p "${HOME}" "${TMP}/bin"
    unset XDG_CONFIG_HOME
    CALLS="${TMP}/calls.log"
    : >"${CALLS}"
    export CALLS TMP

    stub() {  # stub NAME BODY…: a command that logs its argv, then runs BODY
        local name="$1"; shift
        {
            echo '#!/usr/bin/env bash'
            echo "echo \"${name} \$*\" >>\"${CALLS}\""
            printf '%s\n' "$@"
        } >"${TMP}/bin/${name}"
        chmod +x "${TMP}/bin/${name}"
    }

    stub gh 'exit 0'
    stub uv 'exit 0'
    stub loginctl 'echo Linger=yes'
    # `claude plugin …`: list/marketplace list read marker files that add/install write.
    stub claude '
case "$2 $3" in
  "marketplace list") [ -e "$TMP/marketplace" ] && echo "fx-claude-config" ; exit 0 ;;
  "marketplace add") touch "$TMP/marketplace" ;;
  "install "*) touch "$TMP/plugin" ;;
esac
case "$2" in
  list) [ -e "$TMP/plugin" ] && echo "autonomous-sdlc@fx-claude-config" ; exit 0 ;;
esac
exit 0'
    stub sdlc 'exit "${STUB_DOCTOR_RC:-0}"'
    stub systemctl '
case "$2" in
  is-active) [ -e "$TMP/active" ] ;;
  enable) touch "$TMP/active" ;;
esac'

    export INSTALL_SH="${TMP}/install.sh" DEPLOY_SH="${TMP}/deploy.sh"
    printf '#!/usr/bin/env bash\necho "install.sh $*" >>"%s"\n' "${CALLS}" >"${INSTALL_SH}"
    printf '#!/usr/bin/env bash\necho "deploy.sh $*" >>"%s"\n' "${CALLS}" >"${DEPLOY_SH}"
    chmod +x "${INSTALL_SH}" "${DEPLOY_SH}"

    # The system PATH last so mktemp, install, cmp, grep and friends resolve; the
    # stubs first so `claude` and friends never do.
    export PATH="${TMP}/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH}"
}

teardown() { rm -rf "${TMP}"; }

UNIT() { echo "${HOME}/.config/systemd/user/sdlc-worker.service"; }

# --- the first run ---------------------------------------------------------------------

@test "a fresh box ends with sdlc, the plugin, the fleet config and a running unit" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    grep -qx "install.sh --core" "${CALLS}"
    grep -q "^claude plugin marketplace add " "${CALLS}"
    grep -qx "claude plugin install autonomous-sdlc@fx-claude-config" "${CALLS}"
    grep -qx "deploy.sh " "${CALLS}"
    grep -qx "gh auth setup-git" "${CALLS}"
    [ "$(cat "${HOME}/.sdlc-fleet.yaml")" = "queue_url: ${QUEUE}" ]
    cmp "${TEMPLATE}" "$(UNIT)"
    grep -qx "systemctl --user enable --now sdlc-worker.service" "${CALLS}"
}

@test "steps run in order: config, plugin, controller, git auth, doctor, then the unit" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    [ "$status" -eq 0 ]

    order="$(grep -nE '^(install.sh|claude plugin install|deploy.sh|gh auth setup-git|sdlc doctor|systemctl --user enable)' "${CALLS}" | cut -d: -f2 | cut -d' ' -f1-2 | tr '\n' '|')"
    [ "${order}" = "install.sh --core|claude plugin|deploy.sh |gh auth|sdlc doctor|systemctl --user|" ]
}

@test "doctor is asked for an exit status so automation can tell CLEAN from not" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    grep -qx "sdlc doctor --exit-code" "${CALLS}"
}

@test "the token file is created empty with mode 600 and the unit may read it" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    [ "$status" -eq 0 ]

    f="${HOME}/.config/sdlc/worker.env"
    [ -f "$f" ] && [ ! -s "$f" ]
    [ "$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f")" = "600" ]
    grep -qx 'EnvironmentFile=-%h/.config/sdlc/worker.env' "$(UNIT)"
}

@test "the unit lands where systemctl --user looks, and XDG_CONFIG_HOME moves it" {
    XDG_CONFIG_HOME="${TMP}/xdg" run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    [ "$status" -eq 0 ]

    [ -f "${TMP}/xdg/systemd/user/sdlc-worker.service" ]
}

# --- idempotence -----------------------------------------------------------------------

@test "a second run installs and restarts nothing, and leaves every file byte-identical" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    [ "$status" -eq 0 ]
    before="$(cd "${HOME}" && find . -type f -exec cksum {} + | sort)"
    : >"${CALLS}"

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    [ "$(cd "${HOME}" && find . -type f -exec cksum {} + | sort)" = "${before}" ]
    ! grep -q "^claude plugin marketplace add" "${CALLS}"
    ! grep -q "^claude plugin install" "${CALLS}"
    ! grep -q "daemon-reload" "${CALLS}"
    ! grep -q "restart" "${CALLS}"
    # …while the idempotent commands still run, converging a half-done box.
    grep -qx "install.sh --core" "${CALLS}"
    grep -qx "gh auth setup-git" "${CALLS}"
}

@test "a changed unit is reloaded and the running worker restarted" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    printf '\n# stale\n' >>"$(UNIT)"
    : >"${CALLS}"

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    cmp "${TEMPLATE}" "$(UNIT)"
    grep -qx "systemctl --user daemon-reload" "${CALLS}"
    grep -qx "systemctl --user restart sdlc-worker.service" "${CALLS}"
}

@test "an existing fleet config is the operator's: never overwritten, a different URL is flagged" {
    printf 'queue_url: http://elsewhere:1\n# mine\n' >"${HOME}/.sdlc-fleet.yaml"

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    [ "$(cat "${HOME}/.sdlc-fleet.yaml")" = "$(printf 'queue_url: http://elsewhere:1\n# mine')" ]
    [[ "$output" == *"does not name ${QUEUE}"* ]]
}

@test "a token the operator put in the env file survives a re-run, and its mode is restored" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    echo 'SDLC_QUEUE_TOKEN=abc' >"${HOME}/.config/sdlc/worker.env"
    chmod 644 "${HOME}/.config/sdlc/worker.env"

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$(cat "${HOME}/.config/sdlc/worker.env")" = "SDLC_QUEUE_TOKEN=abc" ]
    [ "$(stat -c %a "${HOME}/.config/sdlc/worker.env" 2>/dev/null || stat -f %Lp "${HOME}/.config/sdlc/worker.env")" = "600" ]
}

@test "--queue-url is optional once the fleet config names one" {
    echo "queue_url: ${QUEUE}" >"${HOME}/.sdlc-fleet.yaml"

    run "${BOOTSTRAP}"

    [ "$status" -eq 0 ]
}

# --- doctor, linger, flags -------------------------------------------------------------

@test "a doctor that is not clean is the script's exit status, after the unit is in place" {
    export STUB_DOCTOR_RC=2
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 2 ]
    grep -qx "systemctl --user enable --now sdlc-worker.service" "${CALLS}"
}

@test "linger off is a warning with the fix, not a failure" {
    stub loginctl 'echo Linger=no'

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    [[ "$output" == *"enable-linger"* ]]
}

@test "--skip-service bootstraps without systemd" {
    rm "${TMP}/bin/systemctl"

    run "${BOOTSTRAP}" --queue-url "${QUEUE}" --skip-service

    [ "$status" -eq 0 ]
    [ ! -e "$(UNIT)" ]
    ! grep -q systemctl "${CALLS}"
}

@test "--dry-run changes nothing" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}" --dry-run

    [ "$status" -eq 0 ]
    [ -z "$(find "${HOME}" -type f)" ]
    [ ! -s "${CALLS}" ] || ! grep -qE '^(install.sh|deploy.sh|gh auth setup-git|systemctl|claude plugin (install|marketplace add)|sdlc)' "${CALLS}"
}

# --- preflight: refuse before changing anything ----------------------------------------

@test "outside the dev shell it names the missing tool and the nix develop line" {
    rm "${TMP}/bin/uv"
    PATH="${TMP}/bin:/usr/bin:/bin" run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -ne 0 ]
    [[ "$output" == *"uv not found"* && "$output" == *"nix develop ~/.config/nix-dev-env"* ]]
    [ ! -s "${CALLS}" ]
}

@test "a gh that is not logged in stops the run with the box untouched" {
    stub gh 'exit 1'

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -ne 0 ]
    [[ "$output" == *"gh is not logged in"* ]]
    ! grep -q "install.sh" "${CALLS}"
    [ ! -e "${HOME}/.sdlc-fleet.yaml" ]
}

@test "no queue URL anywhere is refused" {
    run "${BOOTSTRAP}"

    [ "$status" -ne 0 ]
    [[ "$output" == *"--queue-url is required"* ]]
}

@test "a queue URL that is not http(s) is refused" {
    run "${BOOTSTRAP}" --queue-url "ftp://nope"

    [ "$status" -ne 0 ]
    [[ "$output" == *"http(s) URL"* ]]
}

@test "an unknown flag is refused" {
    run "${BOOTSTRAP}" --frobnicate

    [ "$status" -ne 0 ]
    [[ "$output" == *"unknown flag"* ]]
}

@test "--help documents the nix develop wrapper and how to reach the box" {
    run "${BOOTSTRAP}" --help

    [ "$status" -eq 0 ]
    [[ "$output" == *"nix develop ~/.config/nix-dev-env"* ]]
    [[ "$output" == *"ssh -J home-lab"* ]]
}
