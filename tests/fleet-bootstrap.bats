#!/usr/bin/env bats
# ABOUTME: Behavior tests for scripts/fleet-bootstrap.sh (Story 35.2-008): the Linux dev box
# ABOUTME: becomes a worker — and a second run changes nothing (idempotent).
#
# Everything the script would install or start is a stub earlier on PATH (or an
# INSTALL_SH / DEPLOY_SH seam), and HOME is a scratch directory, so no run touches
# the machine. Stubs append to one call log; tests assert on it and on the files
# the script writes, not on stdout wording. Marketplace/plugin state lives in
# marker files, so a second run sees what the first one "installed".
#
# A negated assertion is written `! cmd || false`: errexit ignores a command
# inverted with `!`, so on any line but a test's last a bare `! grep` asserts nothing.

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

    # gh records GH_PATH so a test can see the helper is written as a PATH lookup.
    stub gh 'echo "gh-env GH_PATH=${GH_PATH:-}" >>"${CALLS}"; exit 0'
    # glab is logged in to the local GitLab unless STUB_GLAB_AUTH_RC=1 says otherwise —
    # the stub, never a real glab on the test host, answers. As git's credential
    # helper it hands out a token a test can recognise.
    stub glab '
case "$1 $2" in
  "auth status") exit "${STUB_GLAB_AUTH_RC:-0}" ;;
  "auth git-credential") [ "$3" = get ] && printf "username=oauth2\npassword=glab-token\n" ;;
esac
exit 0'
    stub uv 'exit 0'
    # The scanners the coverage gate runs: present unless a test removes one.
    stub semgrep 'exit 0'
    stub osv-scanner 'exit 0'
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
    # sdlc records its working directory: doctor judges the repo it is run from.
    stub sdlc 'echo "sdlc-cwd ${PWD}" >>"${CALLS}"; exit "${STUB_DOCTOR_RC:-0}"'
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

@test "a fresh box starts the worker once: enable --now is not followed by a restart" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    grep -qx "systemctl --user enable --now sdlc-worker.service" "${CALLS}"
    # A restart here would cut the just-started worker's start-up self-check short.
    ! grep -q "restart" "${CALLS}" || false
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
    ! grep -q "^claude plugin marketplace add" "${CALLS}" || false
    ! grep -q "^claude plugin install" "${CALLS}" || false
    ! grep -q "daemon-reload" "${CALLS}" || false
    ! grep -q "restart" "${CALLS}" || false
    # …while the idempotent commands still run, converging a half-done box.
    grep -qx "install.sh --core" "${CALLS}"
    grep -qx "gh auth setup-git" "${CALLS}"
}

@test "a long plugin list still reads as installed: a match is never lost to SIGPIPE" {
    # `grep -q` stops reading at the first match; the lister's next write then dies of
    # SIGPIPE, which pipefail reads as "not found", so the step re-adds what is there.
    # A megabyte after the match outlasts any pipe buffer, so this needs no timing.
    stub claude '
case "$2 $3" in
  "marketplace list") echo "fx-claude-config"; printf "%01000000d\n" 0; exit 0 ;;
esac
case "$2" in
  list) echo "autonomous-sdlc@fx-claude-config"; printf "%01000000d\n" 0; exit 0 ;;
esac
exit 0'

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    ! grep -q "^claude plugin marketplace add" "${CALLS}" || false
    ! grep -q "^claude plugin install" "${CALLS}" || false
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

@test "the fleet config's URL is compared literally: a '.' in it matches only a '.'" {
    echo "queue_url: http://home-labXexample.ts.net:8790" >"${HOME}/.sdlc-fleet.yaml"

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    [[ "$output" == *"does not name ${QUEUE}"* ]]
}

@test "the same URL quoted or with a trailing slash is recognised, not flagged" {
    echo "queue_url: \"${QUEUE}/\"" >"${HOME}/.sdlc-fleet.yaml"

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    [[ "$output" != *"does not name"* ]]
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
    ! grep -q systemctl "${CALLS}" || false
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
    ! grep -q "install.sh" "${CALLS}" || false
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

@test "--help documents the nix develop wrapper, --github-only and how to reach the box" {
    run "${BOOTSTRAP}" --help

    [ "$status" -eq 0 ]
    [[ "$output" == *"nix develop ~/.config/nix-dev-env"* ]]
    [[ "$output" == *"--github-only"* ]]
    [[ "$output" == *"ssh -J home-lab"* ]]
    [[ "$output" == *"semgrep"* && "$output" == *"osv-scanner"* ]]
    # The whole header and nothing after it — no line range to fall out of step with it.
    [[ "$output" == *"at boot." ]]
    [[ "$output" != *"set -euo pipefail"* ]]
}

# --- CI wiring -------------------------------------------------------------------------

@test "ci.yml bats job runs the fleet-bootstrap suite" {
    grep -q 'tests/fleet-bootstrap\.bats' "${BATS_TEST_DIRNAME}/../.github/workflows/ci.yml"
}

# --- git credentials for both forges (review of #851) ----------------------------------

@test "gh's credential helper is written as a PATH lookup, not a /nix/store path" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    grep -qx "gh-env GH_PATH=gh" "${CALLS}"
}

# What a run leaves as git's helper list for the local GitLab: a blank entry, which
# empties the list inherited from /etc/gitconfig or a catch-all credential.helper
# (as `gh auth setup-git` does for GitHub), then glab's.
GITLAB_HELPERS="$(printf '\n!glab auth git-credential')"

@test "a glab logged in to the local GitLab becomes git's only credential helper for it" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    grep -qx "glab auth status --hostname gitlab.test" "${CALLS}"
    [ "$(git config --global --get-all credential.http://gitlab.test.helper)" = "${GITLAB_HELPERS}" ]
}

@test "a second run leaves the GitLab helper list exactly as the first run wrote it" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    [ "$(git config --global --get-all credential.http://gitlab.test.helper)" = "${GITLAB_HELPERS}" ]
}

@test "a catch-all credential helper set before the bootstrap never answers for the GitLab" {
    # git asks every helper that applies, in config order, and takes the first
    # answer: without the blank entry this older helper's stale credential would be
    # the one sent to the GitLab, and glab would never be asked.
    git config --global credential.helper '!f() { echo username=generic; echo password=stale; }; f'

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    [ "$status" -eq 0 ]

    # No system config: the test host's own helper (a Mac's osxkeychain) stays out of it.
    cred="$(printf 'protocol=http\nhost=gitlab.test\n\n' \
        | GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 git credential fill)"
    [[ "${cred}" == *"password=glab-token"* ]]
}

@test "a dry run names the GitLab helper it would set and writes no git config" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}" --dry-run

    [ "$status" -eq 0 ]
    [[ "${output}" == *"credential.http://gitlab.test.helper"* ]]
    [ ! -e "${HOME}/.gitconfig" ]
}

# --- no worker that cannot sync GitLab (review of #851, round 4) -----------------------
#
# A worker without glab's helper fails every GitLab clone and backs off in its own
# memory, while the free slots it keeps reporting make its peers defer GitLab jobs to
# it. So the unit is never started on such a box unless the operator opts in.

@test "without a logged-in glab the run is refused before anything changes, naming the way out" {
    export STUB_GLAB_AUTH_RC=1

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -ne 0 ]
    [[ "${output}" == *"glab auth login --hostname gitlab.test"* ]]
    [[ "${output}" == *"--github-only"* ]]
    ! grep -qE '^(install.sh|deploy.sh|systemctl)' "${CALLS}" || false
    [ ! -e "${HOME}/.sdlc-fleet.yaml" ]
    [ ! -e "$(UNIT)" ]
    [ ! -e "${HOME}/.gitconfig" ]
}

@test "a box with no glab at all — the box as found — is refused the same way" {
    # Only directories the test controls or the OS owns: a glab installed for the
    # host's user (a nix profile, ~/.local/bin) must not stand in for the absent one.
    if (PATH="/usr/bin:/bin"; command -v glab) >/dev/null 2>&1; then
        skip "a system glab in /usr/bin or /bin cannot be hidden from this test"
    fi
    rm "${TMP}/bin/glab"
    stub git 'exit 0'  # git may live outside /usr/bin on a CI image; it is never run here

    PATH="${TMP}/bin:/usr/bin:/bin" run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -ne 0 ]
    [[ "${output}" == *"glab auth login --hostname gitlab.test"* ]]
    ! grep -q "install.sh" "${CALLS}" || false
    [ ! -e "$(UNIT)" ]
}

@test "--github-only starts the worker anyway, and says to pin GitLab jobs elsewhere" {
    export STUB_GLAB_AUTH_RC=1

    run "${BOOTSTRAP}" --queue-url "${QUEUE}" --github-only

    [ "$status" -eq 0 ]
    [[ "${output}" == *"only sync GitHub repos"* ]]
    [[ "${output}" == *"--host home-lab"* ]]
    grep -qx "systemctl --user enable --now sdlc-worker.service" "${CALLS}"
    [ -z "$(git config --global --get-all credential.http://gitlab.test.helper || true)" ]
}

@test "--github-only only lifts the refusal: a logged-in glab still becomes the helper" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}" --github-only

    [ "$status" -eq 0 ]
    [ "$(git config --global --get-all credential.http://gitlab.test.helper)" = "${GITLAB_HELPERS}" ]
}

@test "--skip-service without glab is not refused: it starts no worker, and warns" {
    export STUB_GLAB_AUTH_RC=1
    rm "${TMP}/bin/systemctl"

    run "${BOOTSTRAP}" --queue-url "${QUEUE}" --skip-service

    [ "$status" -eq 0 ]
    [[ "${output}" == *"only sync GitHub repos"* ]]
    ! grep -q systemctl "${CALLS}" || false
    [ ! -e "$(UNIT)" ]
}

# --- no worker that would merge unscanned work (review of #851, round 5) ---------------
#
# The coverage gate reports a missing scanner SKIPPED, and SKIPPED passes: a story built
# on a box without semgrep or osv-scanner would merge without the SAST and dependency
# scans a Mac blocks it on. So the unit is never started there, and no flag lifts that.

# Run the bootstrap without TOOL, on a PATH of only directories the test controls or the
# OS owns: a scanner installed for the host's user (a nix profile, ~/.local/bin,
# Homebrew) must not stand in for the one the test removed.
run_without() {  # run_without TOOL BOOTSTRAP-ARG…
    local tool="$1"; shift
    if (PATH="/usr/bin:/bin"; command -v "${tool}") >/dev/null 2>&1; then
        skip "a system ${tool} in /usr/bin or /bin cannot be hidden from this test"
    fi
    rm "${TMP}/bin/${tool}"
    stub git 'exit 0'  # git may live outside /usr/bin on a CI image
    PATH="${TMP}/bin:/usr/bin:/bin" run "${BOOTSTRAP}" "$@"
}

@test "without semgrep the run is refused before anything changes, naming the way out" {
    run_without semgrep --queue-url "${QUEUE}"

    [ "$status" -ne 0 ]
    [[ "${output}" == *"semgrep not found on PATH"* ]]
    [[ "${output}" == *"SKIPPED"* ]]  # why: the gate would pass the story unscanned
    [[ "${output}" == *"uv tool install semgrep"* && "${output}" == *"--skip-service"* ]]
    ! grep -qE '^(install.sh|deploy.sh|git|systemctl)' "${CALLS}" || false
    [ ! -e "${HOME}/.sdlc-fleet.yaml" ]
    [ ! -e "$(UNIT)" ]
}

@test "without osv-scanner the run is refused the same way" {
    run_without osv-scanner --queue-url "${QUEUE}"

    [ "$status" -ne 0 ]
    [[ "${output}" == *"osv-scanner not found on PATH"* ]]
    ! grep -qE '^(install.sh|deploy.sh|git|systemctl)' "${CALLS}" || false
    [ ! -e "$(UNIT)" ]
}

@test "with neither scanner the refusal names both" {
    rm "${TMP}/bin/osv-scanner"
    run_without semgrep --queue-url "${QUEUE}"

    [ "$status" -ne 0 ]
    [[ "${output}" == *"semgrep and osv-scanner not found on PATH"* ]]
}

@test "--github-only does not lift the scanner refusal" {
    run_without semgrep --queue-url "${QUEUE}" --github-only

    [ "$status" -ne 0 ]
    [[ "${output}" == *"semgrep not found on PATH"* ]]
    ! grep -qE '^(install.sh|systemctl)' "${CALLS}" || false
    [ ! -e "$(UNIT)" ]
}

@test "--skip-service without the scanners is not refused: it starts no worker" {
    rm "${TMP}/bin/systemctl"
    run_without semgrep --queue-url "${QUEUE}" --skip-service

    [ "$status" -eq 0 ]
    grep -qx "install.sh --core" "${CALLS}"
    ! grep -q systemctl "${CALLS}" || false
    [ ! -e "$(UNIT)" ]
}

@test "doctor judges the repo even when the bootstrap is run from another directory" {
    cd "${HOME}"
    run "${BOOTSTRAP}" --queue-url "${QUEUE}" --github-only

    [ "$status" -eq 0 ]
    grep -qx "sdlc-cwd $(cd "${BATS_TEST_DIRNAME}/.." && pwd)" "${CALLS}"
}
