#!/usr/bin/env bats
# ABOUTME: Behavior tests for scripts/fleet-bootstrap.sh (Stories 35.2-008, 35.2-009): the Linux dev
# ABOUTME: box and the Mac become workers — and a second run changes nothing (idempotent).
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
# The worker unit as the bootstrap installs it: the template with the stub's tailnet address.
RENDERED_UNIT() { sed "s|__TAILNET_IP__|${1:-100.64.0.9}|g" "${TEMPLATE}"; }
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
    # `queue list --json` answers from $TMP/queue.json (no jobs unless a test writes it).
    stub sdlc '
if [ "$1 $2" = "queue list" ]; then
  echo "queue-token ${SDLC_QUEUE_TOKEN:-}" >>"${CALLS}"
  [ "${STUB_QUEUE_RC:-0}" = 0 ] || exit "${STUB_QUEUE_RC}"
  if [ -f "$TMP/queue.json" ]; then cat "$TMP/queue.json"; else echo "{\"jobs\": []}"; fi
  exit 0
fi
echo "sdlc-cwd ${PWD}" >>"${CALLS}"
echo "doctor-token ${SDLC_QUEUE_TOKEN:-}" >>"${CALLS}"
exit "${STUB_DOCTOR_RC:-0}"'
    # One marker per unit: enable --now NAME starts NAME.
    stub systemctl '
case "$2" in
  is-active) [ -e "$TMP/active.$4" ] ;;
  enable) touch "$TMP/active.$4" ;;
esac'
    # The platform is the stub's to say, so these suites read the same on a Mac and in CI:
    # Linux unless a test sets STUB_UNAME=Darwin.
    printf '#!/bin/sh\necho "${STUB_UNAME:-Linux}"\n' >"${TMP}/bin/uname"
    chmod +x "${TMP}/bin/uname"
    # `tailscale ip -4` prints a tailnet address, or nothing when STUB_TAILSCALE_IP is set empty.
    stub tailscale 'echo "${STUB_TAILSCALE_IP-100.64.0.9}"'
    # launchctl keeps one marker per loaded label, as the real one keeps its domain.
    stub launchctl '
case "$1" in
  print) [ -e "$TMP/loaded.${2##*/}" ] ;;
  bootstrap) touch "$TMP/loaded.$(basename "$3" .plist)" ;;
  bootout) rm -f "$TMP/loaded.${2##*/}" ;;
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
    cmp <(RENDERED_UNIT) "$(UNIT)"
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
    [ "${order}" = "install.sh --core|claude plugin|deploy.sh |gh auth|sdlc doctor|systemctl --user|systemctl --user|" ]
}

@test "doctor is asked for an exit status so automation can tell CLEAN from not" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    grep -q "^sdlc doctor --exit-code" "${CALLS}"
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
    cmp <(RENDERED_UNIT) "$(UNIT)"
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

# --- the Linux dashboard unit (35.2-009) -------------------------------------------------

DASHBOARD_UNIT() { echo "${HOME}/.config/systemd/user/sdlc-dashboard.service"; }

@test "linux gains the dashboard as a second user unit, bound to the tailnet address" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    # Through the dev shell, as the worker is: the forge panel runs gh and glab, which live there.
    grep -qx "ExecStart=/nix/var/nix/profiles/default/bin/nix develop %h/.config/nix-dev-env -c sdlc dashboard --host 100.64.0.9 --port 8787" "$(DASHBOARD_UNIT)"
    ! grep -q "__" "$(DASHBOARD_UNIT)" || false
    grep -qx "systemctl --user enable --now sdlc-worker.service" "${CALLS}"
    grep -qx "systemctl --user enable --now sdlc-dashboard.service" "${CALLS}"
    cmp <(RENDERED_UNIT) "$(UNIT)"
}

@test "linux: a changed dashboard unit is restarted, and the worker is left running" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    printf '\n# stale\n' >>"$(DASHBOARD_UNIT)"
    : >"${CALLS}"

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    grep -qx "systemctl --user restart sdlc-dashboard.service" "${CALLS}"
    ! grep -q "restart sdlc-worker" "${CALLS}" || false
}

@test "linux without a tailnet address stops before writing any unit" {
    STUB_TAILSCALE_IP="" run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -ne 0 ]
    [[ "$output" == *"every"*"interface"* ]]
    [ ! -e "$(UNIT)" ] && [ ! -e "$(DASHBOARD_UNIT)" ]
    ! grep -q "install.sh" "${CALLS}" || false
}

@test "linux refuses the macOS-only options: the unit is worker dev on claude-shared" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}" --worker m3 --pool p

    [ "$status" -ne 0 ]
    [[ "$output" == *"macOS options"* ]]

    run "${BOOTSTRAP}" --queue-url "${QUEUE}" --host dev
    [ "$status" -ne 0 ]
    [[ "$output" == *"macOS options"* ]]
}

# --- the Linux worker advertises its dashboard (35.2-010) --------------------------------

# The worker unit 35.2-008 installed: its ExecStart carries no --dashboard-url.
OLD_UNIT() { RENDERED_UNIT | sed 's| --dashboard-url http://100.64.0.9:8787||'; }

@test "linux: the worker unit advertises the dashboard at the address the dashboard binds" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    grep -qx "ExecStart=/nix/var/nix/profiles/default/bin/nix develop %h/.config/nix-dev-env -c sdlc queue run --worker dev --pool claude-shared --pool codex-shared --dashboard-url http://100.64.0.9:8787 --follow" "$(UNIT)"
    ! grep -q "__TAILNET_IP__" "$(UNIT)" || false
    grep -q -- "--host 100.64.0.9 --port 8787" "$(DASHBOARD_UNIT)"
}

@test "linux: both units take the one address tailscale ip -4 prints" {
    STUB_TAILSCALE_IP="100.101.7.42" run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    cmp <(RENDERED_UNIT 100.101.7.42) "$(UNIT)"
    grep -q -- "--host 100.101.7.42 --port 8787" "$(DASHBOARD_UNIT)"
}

@test "linux: the dashboard unit is installed before the worker that advertises it" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    order="$(grep '^systemctl --user enable --now' "${CALLS}" | cut -d' ' -f5 | tr '\n' ' ')"
    [ "${order}" = "sdlc-dashboard.service sdlc-worker.service " ]
}

@test "linux: a box already installed keeps both units when the address goes blank" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    before="$(cksum <"$(UNIT)") $(cksum <"$(DASHBOARD_UNIT)")"
    : >"${CALLS}"

    STUB_TAILSCALE_IP="" run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -ne 0 ]
    [ "$(cksum <"$(UNIT)") $(cksum <"$(DASHBOARD_UNIT)")" = "${before}" ]
    ! grep -q "^systemctl" "${CALLS}" || false
}

@test "linux: an idle worker unit from before 35.2-010 is replaced and restarted" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    OLD_UNIT >"$(UNIT)"
    : >"${CALLS}"

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    cmp <(RENDERED_UNIT) "$(UNIT)"
    grep -qx "systemctl --user daemon-reload" "${CALLS}"
    grep -qx "systemctl --user restart sdlc-worker.service" "${CALLS}"
    ! grep -q "restart sdlc-dashboard" "${CALLS}" || false
}

@test "linux: a worker with a job in flight is not restarted: pending restart, left as installed" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    OLD_UNIT >"$(UNIT)"
    OLD_UNIT >"${TMP}/old.service"
    echo '{"jobs": [{"id": 44, "state": "running", "worker": "dev"}, {"id": 45, "state": "running", "worker": "m3max"}]}' >"${TMP}/queue.json"
    : >"${CALLS}"

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    # Not converged, so not 0: the box still runs the worker it had.
    [ "$status" -eq 1 ]
    [[ "$output" == *"pending restart (job 44 running)"* ]]
    [[ "$output" == *"re-run this script once the worker is idle"* ]]
    cmp "${TMP}/old.service" "$(UNIT)"
    ! grep -q "daemon-reload" "${CALLS}" || false
    ! grep -q "restart sdlc-worker" "${CALLS}" || false
}

@test "linux: a worker whose queue cannot be read is not restarted either" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    OLD_UNIT >"$(UNIT)"
    : >"${CALLS}"

    STUB_QUEUE_RC=1 run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 1 ]
    [[ "$output" == *"pending restart"* ]]
    cmp <(OLD_UNIT) "$(UNIT)"
    ! grep -q "restart sdlc-worker" "${CALLS}" || false
}

@test "linux: the in-flight check asks with the worker's own token from worker.env" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    echo 'SDLC_QUEUE_TOKEN=worker-secret' >>"${HOME}/.config/sdlc/worker.env"
    OLD_UNIT >"$(UNIT)"
    : >"${CALLS}"

    SDLC_QUEUE_URL="http://elsewhere:1" run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    grep -qx "queue-token worker-secret" "${CALLS}"
    # The token reaches the stub through the environment, never an argv `ps` would show.
    ! grep -q "^sdlc .*worker-secret" "${CALLS}" || false
}

@test "linux: a stopped worker unit is replaced without asking the queue about jobs" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    OLD_UNIT >"$(UNIT)"
    rm "${TMP}/active.sdlc-worker.service"
    : >"${CALLS}"

    STUB_QUEUE_RC=1 run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    cmp <(RENDERED_UNIT) "$(UNIT)"
    ! grep -q "^sdlc queue list" "${CALLS}" || false
}

# jq ships in /usr/bin on macOS and on the CI images, where run_without cannot hide it:
# run the bootstrap on a PATH of the stubs and every /usr/bin and /bin command but TOOL.
run_without_system() {  # run_without_system TOOL BOOTSTRAP-ARG…
    local tool="$1"; shift
    mkdir "${TMP}/sys"
    ln -s /usr/bin/* /bin/* "${TMP}/sys/" 2>/dev/null || true  # a merged /usr names each twice
    rm -f "${TMP}/sys/${tool}" "${TMP}/bin/${tool}"
    stub git 'exit 0'  # git may live outside /usr/bin on a CI image
    PATH="${TMP}/bin:${TMP}/sys" run "${BOOTSTRAP}" "$@"
}

# Without jq the in-flight check cannot read the queue, so every re-run would leave a
# changed worker unit "pending restart" for a queue that works (review of #926).
@test "linux: without jq the run is refused before anything changes" {
    run_without_system jq --queue-url "${QUEUE}"

    [ "$status" -ne 0 ]
    [[ "${output}" == *"jq not found"* && "${output}" == *"--skip-service"* ]]
    ! grep -qE '^(install.sh|deploy.sh|git|systemctl)' "${CALLS}" || false
    [ ! -e "${HOME}/.sdlc-fleet.yaml" ]
    [ ! -e "$(UNIT)" ]
}

@test "linux: --skip-service without jq is not refused: it installs no unit" {
    run_without_system jq --queue-url "${QUEUE}" --skip-service

    [ "$status" -eq 0 ]
    grep -qx "install.sh --core" "${CALLS}"
    [ ! -e "$(UNIT)" ]
}

# --- macOS: LaunchAgents rendered from templates/launchd (35.2-009) ---------------------

LAUNCHD="${BATS_TEST_DIRNAME}/../templates/launchd"
AGENT() { echo "${HOME}/Library/LaunchAgents/com.fxmartin.sdlc-$1.plist"; }

# The string that follows <string>FLAG</string> in a rendered plist.
value_after() { awk -v flag="<string>$2</string>" 'index($0, flag) { getline; print }' "$1" | sed 's/.*<string>\(.*\)<\/string>.*/\1/'; }

darwin() { export STUB_UNAME=Darwin; }
mac() { "${BOOTSTRAP}" --worker m3max --pool claude-m3 --pool codex-shared --queue-url "${QUEUE}" "$@"; }

@test "a Mac gets a worker and a dashboard agent rendered for it, and both are loaded" {
    darwin
    run mac

    [ "$status" -eq 0 ]
    w="$(AGENT worker)"; d="$(AGENT dashboard)"
    ! grep -q "__[A-Z_]*__" <(sed '/^<!--/,/^-->/d' "$w") || false
    ! grep -q "__[A-Z_]*__" <(sed '/^<!--/,/^-->/d' "$d") || false
    [ "$(value_after "$w" --worker)" = "m3max" ]
    [ "$(value_after "$w" --pool | tr '\n' ' ')" = "claude-m3 codex-shared " ]
    [ "$(value_after "$w" --dashboard-url)" = "http://100.64.0.9:8787" ]
    [ "$(value_after "$d" --host)" = "100.64.0.9" ]
    grep -qF "<string>${HOME}/.local/bin/sdlc</string>" "$w"
    grep -qx "launchctl bootstrap gui/$(id -u) ${w}" "${CALLS}"
    grep -qx "launchctl bootstrap gui/$(id -u) ${d}" "${CALLS}"
    # No queue service unless asked for, and no systemd unit on a Mac.
    [ ! -e "$(AGENT queue)" ]
    [ ! -e "$(UNIT)" ]
}

@test "a Mac's worker plist differs from the template only in its name, its pools and its values" {
    darwin
    run mac
    [ "$status" -eq 0 ]

    expected="$(sed -e "s|__HOME__|${HOME}|g" -e "s|__USER__|$(id -un)|g" -e "s|__TAILNET_IP__|100.64.0.9|g" "${LAUNCHD}/com.fxmartin.sdlc-worker.plist")"
    # The template already names the M3 Max and its pools, so rendering it for them is the identity.
    [ "$expected" = "$(cat "$(AGENT worker)")" ]
}

@test "a Mac's own worker name and a single pool replace the template's" {
    darwin
    run "${BOOTSTRAP}" --worker home-lab --pool claude-shared --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    [ "$(value_after "$(AGENT worker)" --worker)" = "home-lab" ]
    [ "$(value_after "$(AGENT worker)" --pool)" = "claude-shared" ]
    ! grep -q "m3max\|claude-m3\|codex-shared" <(sed '/^<!--/,/^-->/d' "$(AGENT worker)") || false
}

@test "the dashboard is installed before the worker that advertises it" {
    darwin
    run mac
    [ "$status" -eq 0 ]

    order="$(grep -n '^launchctl bootstrap' "${CALLS}" | sed 's/.*sdlc-\([a-z]*\)\.plist/\1/' | tr '\n' ' ')"
    [ "${order}" = "dashboard worker " ]
}

@test "--queue-service on home-lab renders the queue agent too, with the login it is given" {
    darwin
    run mac --queue-service --queue-allow fx@example.com

    [ "$status" -eq 0 ]
    q="$(AGENT queue)"
    [ "$(value_after "$q" --bind)" = "100.64.0.9:8790" ]
    grep -qF "<string>fx@example.com</string>" "$q"
    grep -qx "launchctl bootstrap gui/$(id -u) ${q}" "${CALLS}"
}

@test "--queue-service keeps the login of the queue agent already installed" {
    darwin
    run mac --queue-service --queue-allow fx@example.com
    [ "$status" -eq 0 ]
    before="$(cat "$(AGENT queue)")"
    : >"${CALLS}"

    run mac --queue-service

    [ "$status" -eq 0 ]
    [ "$(cat "$(AGENT queue)")" = "${before}" ]
    ! grep -q "launchctl boot" "${CALLS}" || false
}

@test "--queue-service with no login anywhere is refused before anything changes" {
    darwin
    run mac --queue-service

    [ "$status" -ne 0 ]
    [[ "$output" == *"--queue-allow"* ]]
    ! grep -q "install.sh" "${CALLS}" || false
}

@test "re-running a Mac with nothing changed leaves every agent loaded and untouched" {
    darwin
    run mac
    [ "$status" -eq 0 ]
    before="$(cd "${HOME}" && find . -type f -exec cksum {} + | sort)"
    : >"${CALLS}"

    run mac

    [ "$status" -eq 0 ]
    [ "$(cd "${HOME}" && find . -type f -exec cksum {} + | sort)" = "${before}" ]
    ! grep -qE "^launchctl (bootstrap|bootout)" "${CALLS}" || false
    [[ "$output" == *"already installed and current"* ]]
}

@test "an agent that is current but not loaded is loaded, not rewritten" {
    darwin
    run mac
    rm "${TMP}/loaded.com.fxmartin.sdlc-dashboard"
    before="$(cksum <"$(AGENT dashboard)")"
    : >"${CALLS}"

    run mac

    [ "$status" -eq 0 ]
    [ "$(cksum <"$(AGENT dashboard)")" = "${before}" ]
    grep -qx "launchctl bootstrap gui/$(id -u) $(AGENT dashboard)" "${CALLS}"
}

@test "an agent that differs from its template is replaced: booted out, then bootstrapped" {
    darwin
    run mac
    printf '<!-- stale -->\n' >>"$(AGENT dashboard)"
    : >"${CALLS}"

    run mac

    [ "$status" -eq 0 ]
    ! grep -q "stale" "$(AGENT dashboard)" || false
    out="$(grep -n 'sdlc-dashboard' "${CALLS}" | grep launchctl | sed 's/^[0-9]*:launchctl \([a-z]*\).*/\1/' | tr '\n' ' ')"
    # The closing `print` is bootstrap_agent confirming the agent is loaded (35.2-011).
    [ "${out}" = "print bootout bootstrap print " ] || [ "${out}" = "bootout bootstrap print " ]
    [ -e "${TMP}/loaded.com.fxmartin.sdlc-dashboard" ]
}

@test "a worker plist from before 35.4-006 is replaced and gains --dashboard-url" {
    darwin
    run mac
    # What the M3 Max was left with: no --dashboard-url pair.
    grep -v -e '--dashboard-url' -e 'http://100.64.0.9:8787' "$(AGENT worker)" >"${TMP}/old.plist"
    cp "${TMP}/old.plist" "$(AGENT worker)"
    : >"${CALLS}"

    run mac

    [ "$status" -eq 0 ]
    [ "$(value_after "$(AGENT worker)" --dashboard-url)" = "http://100.64.0.9:8787" ]
    grep -qx "launchctl bootout gui/$(id -u)/com.fxmartin.sdlc-worker" "${CALLS}"
}

@test "a re-run keeps the --host the installed worker registers as, and its slot cap" {
    darwin
    run mac
    # The M3 as found: `--host m3max` (its hostname -s is m3-max) and no --dashboard-url yet.
    awk '{ print } /<string>--follow<\/string>/ {
        print "    <string>--host</string>"; print "    <string>m3max</string>"
        print "    <string>--slots</string>"; print "    <string>3</string>" }' "$(AGENT worker)" \
        | grep -v -e '--dashboard-url' -e 'http://100.64.0.9:8787' >"${TMP}/old.plist"
    cp "${TMP}/old.plist" "$(AGENT worker)"

    run mac

    [ "$status" -eq 0 ]
    [ "$(value_after "$(AGENT worker)" --dashboard-url)" = "http://100.64.0.9:8787" ]
    [ "$(value_after "$(AGENT worker)" --host)" = "m3max" ]
    [ "$(value_after "$(AGENT worker)" --slots)" = "3" ]
}

@test "--host and --slots land in the worker agent, and a re-run without them changes nothing" {
    darwin
    run mac --host m3max --slots 3
    [ "$status" -eq 0 ]
    [ "$(value_after "$(AGENT worker)" --host)" = "m3max" ]
    [ "$(value_after "$(AGENT worker)" --slots)" = "3" ]
    before="$(cksum <"$(AGENT worker)")"
    : >"${CALLS}"

    run mac

    [ "$status" -eq 0 ]
    [ "$(cksum <"$(AGENT worker)")" = "${before}" ]
    ! grep -qE "^launchctl (bootstrap|bootout)" "${CALLS}" || false
}

@test "a host name or slot cap that is not valid is refused" {
    darwin
    run mac --host 'a|b'
    [ "$status" -ne 0 ]
    [[ "$output" == *"--host must be"* ]]

    run mac --slots 0
    [ "$status" -ne 0 ]
    [[ "$output" == *"--slots must be"* ]]
    ! grep -q "install.sh" "${CALLS}" || false
}

@test "a renamed worker's job in flight under its installed name still holds it back" {
    darwin
    run mac
    printf '<!-- stale -->\n' >>"$(AGENT worker)"
    cp "$(AGENT worker)" "${TMP}/stale.plist"
    # Job 7 runs under the name the installed agent registered: booting it out would SIGTERM it.
    echo '{"jobs": [{"id": 7, "state": "running", "worker": "m3max"}]}' >"${TMP}/queue.json"

    run "${BOOTSTRAP}" --worker m3-max --pool claude-m3 --queue-url "${QUEUE}"

    [ "$status" -eq 1 ]
    [[ "$output" == *"pending restart (job 7 running)"* ]]
    cmp "${TMP}/stale.plist" "$(AGENT worker)"
}

@test "a worker with a job in flight is not restarted: pending restart, left for the next run" {
    darwin
    run mac
    printf '<!-- stale -->\n' >>"$(AGENT worker)"
    cp "$(AGENT worker)" "${TMP}/stale.plist"
    echo '{"jobs": [{"id": 7, "state": "running", "worker": "m3max"}, {"id": 8, "state": "queued", "worker": null}]}' >"${TMP}/queue.json"
    : >"${CALLS}"

    run mac

    # Not converged, so not 0: the Mac still runs the agent it had, and the run says to re-run.
    [ "$status" -eq 1 ]
    [[ "$output" == *"re-run this script once the worker is idle"* ]]
    [[ "$output" == *"pending restart (job 7 running)"* ]]
    cmp "${TMP}/stale.plist" "$(AGENT worker)"
    ! grep -q "bootout gui/$(id -u)/com.fxmartin.sdlc-worker" "${CALLS}" || false
    [ -e "${TMP}/loaded.com.fxmartin.sdlc-worker" ]
}

@test "the job in flight on another worker does not hold this worker back" {
    darwin
    run mac
    printf '<!-- stale -->\n' >>"$(AGENT worker)"
    echo '{"jobs": [{"id": 7, "state": "running", "worker": "dev"}]}' >"${TMP}/queue.json"

    run mac

    [ "$status" -eq 0 ]
    ! grep -q "stale" "$(AGENT worker)" || false
}

@test "a worker whose queue cannot be read is not restarted either" {
    darwin
    run mac
    printf '<!-- stale -->\n' >>"$(AGENT worker)"
    : >"${CALLS}"

    STUB_QUEUE_RC=1 run mac

    [ "$status" -eq 1 ]
    [[ "$output" == *"pending restart"* ]]
    grep -q "stale" "$(AGENT worker)"
    ! grep -q "launchctl bootout" "${CALLS}" || false
}

@test "an unloaded worker is replaced without asking the queue about jobs" {
    darwin
    run mac
    printf '<!-- stale -->\n' >>"$(AGENT worker)"
    rm "${TMP}/loaded.com.fxmartin.sdlc-worker"
    echo '{"jobs": [{"id": 7, "state": "running", "worker": "m3max"}]}' >"${TMP}/queue.json"

    run mac

    [ "$status" -eq 0 ]
    ! grep -q "stale" "$(AGENT worker)" || false
}

@test "without a tailnet address a Mac stops before writing any agent" {
    darwin
    STUB_TAILSCALE_IP="" run mac

    [ "$status" -ne 0 ]
    [[ "$output" == *"every"*"interface"* ]]
    [ ! -e "$(AGENT worker)" ] && [ ! -e "$(AGENT dashboard)" ]
    ! grep -qE "^(install.sh|launchctl)" "${CALLS}" || false
}

@test "--dry-run on a Mac prints the diff of an agent it would replace and changes nothing" {
    darwin
    run mac
    printf '<!-- stale -->\n' >>"$(AGENT dashboard)"
    before="$(cd "${HOME}" && find . -type f -exec cksum {} + | sort)"
    : >"${CALLS}"

    run mac --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"would replace"* ]]
    [[ "$output" == *"-<!-- stale -->"* ]]
    [ "$(cd "${HOME}" && find . -type f -exec cksum {} + | sort)" = "${before}" ]
    ! grep -qE "^launchctl (bootstrap|bootout)" "${CALLS}" || false
}

@test "--dry-run on a Mac with no agents installed names what it would install" {
    darwin
    run mac --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"would install"*"com.fxmartin.sdlc-worker.plist"* ]]
    [[ "$output" == *"+    <string>http://100.64.0.9:8787</string>"* ]]
    [ -z "$(find "${HOME}" -type f)" ]
}

@test "a Mac needs a worker name and at least one pool" {
    darwin
    run "${BOOTSTRAP}" --queue-url "${QUEUE}" --pool p
    [ "$status" -ne 0 ]
    [[ "$output" == *"--worker NAME is required"* ]]

    run "${BOOTSTRAP}" --queue-url "${QUEUE}" --worker w
    [ "$status" -ne 0 ]
    [[ "$output" == *"--pool"* ]]
}

@test "a worker name or pool that is not a plain word is refused" {
    darwin
    run "${BOOTSTRAP}" --queue-url "${QUEUE}" --worker 'a|b' --pool p
    [ "$status" -ne 0 ]
    [[ "$output" == *"--worker must be"* ]]

    run "${BOOTSTRAP}" --queue-url "${QUEUE}" --worker w --pool '<x>'
    [ "$status" -ne 0 ]
    [[ "$output" == *"--pool must be"* ]]
}

@test "--skip-service on a Mac installs no agent and needs no tailnet" {
    darwin
    STUB_TAILSCALE_IP="" run "${BOOTSTRAP}" --queue-url "${QUEUE}" --skip-service

    [ "$status" -eq 0 ]
    [ ! -e "$(AGENT worker)" ]
    ! grep -q launchctl "${CALLS}" || false
}

@test "--help documents the Mac options and the in-flight rule" {
    run "${BOOTSTRAP}" --help

    [ "$status" -eq 0 ]
    [[ "$output" == *"--worker NAME --pool P"* ]]
    [[ "$output" == *"--queue-service"* ]]
    [[ "$output" == *"pending restart (job N running)"* ]]
    [[ "$output" == *"at boot." ]]
}

# --- a home-manager Mac, the run that repairs drift, the queue token (review of #877) ----

# git's global config is a read-only /nix/store link there: gh and git fail every write.
# A stub fails it, not a read-only file — CI runs as root, which a chmod does not stop.
unwritable_git_config() {
    stub gh 'case "$1 $2" in "auth setup-git") echo "could not lock config file" >&2; exit 1 ;; esac; exit 0'
    stub git 'case "$1 $2" in "config --global") echo "error: could not lock config file" >&2; exit 255 ;; esac; exit 0'
}

@test "a Mac whose global git config cannot be written warns at step 4 and still renders its agents" {
    darwin
    unwritable_git_config

    run mac

    [ "$status" -eq 0 ]
    [[ "$output" == *"could not write gh's credential helper"* ]]
    [[ "$output" == *"could not write credential.http://gitlab.test.helper"* ]]
    [ -e "$(AGENT worker)" ] && [ -e "$(AGENT dashboard)" ]
    grep -q "^sdlc doctor" "${CALLS}"
}

@test "linux still stops at an unwritable git config, before the unit is installed" {
    unwritable_git_config

    run "${BOOTSTRAP}" --queue-url "${QUEUE}"

    [ "$status" -eq 1 ]
    [ ! -e "$(UNIT)" ]
    ! grep -q "^sdlc doctor" "${CALLS}" || false
}

@test "doctor skips the two checks step 7 settles, on a Mac and on linux; --skip-service skips none" {
    darwin
    run mac
    grep -qx "sdlc doctor --exit-code --skip fleet-agent-drift --skip worker-dashboard --skip worker-keychain" "${CALLS}"

    : >"${CALLS}"
    run mac --skip-service
    [ "$status" -eq 0 ]
    grep -qx "sdlc doctor --exit-code" "${CALLS}"

    # Linux judges its units the same way since 35.2-010; worker-keychain is a Mac's alone.
    : >"${CALLS}"
    unset STUB_UNAME
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    grep -qx "sdlc doctor --exit-code --skip fleet-agent-drift --skip worker-dashboard" "${CALLS}"

    : >"${CALLS}"
    run "${BOOTSTRAP}" --queue-url "${QUEUE}" --skip-service
    grep -qx "sdlc doctor --exit-code" "${CALLS}"
}

# Story 35.2-014: doctor's worker-keychain check is n/a until the worker agent exists, so a
# first run's step 6 cannot show it. The summary does, once step 7 has installed the agent.
@test "a Mac's summary shows the Keychain check, after the worker agent is loaded" {
    darwin
    run mac

    [ "$status" -eq 0 ]
    grep -qx "sdlc doctor --exit-code --only worker-keychain" "${CALLS}"
    loaded="$(grep -n "^launchctl bootstrap .*com.fxmartin.sdlc-worker.plist" "${CALLS}" | tail -n1 | cut -d: -f1)"
    shown="$(grep -n "^sdlc doctor --exit-code --only worker-keychain" "${CALLS}" | cut -d: -f1)"
    [ -n "${loaded}" ] && [ "${shown}" -gt "${loaded}" ]
}

@test "a Keychain that can lock is the Mac run's exit status, with its agents in place" {
    darwin
    stub sdlc '
if [ "$1 $2" = "queue list" ]; then echo "{\"jobs\": []}"; exit 0; fi
case " $* " in *" --only worker-keychain "*) echo "[WARN] Worker Keychain — timeout=300s"; exit 1 ;; esac
exit 0'

    run mac

    [ "$status" -eq 1 ]
    [[ "$output" == *"[WARN] Worker Keychain"* ]]
    [[ "$output" == *"login Keychain"* ]]
    [ -e "$(AGENT worker)" ] && [ -e "$(AGENT dashboard)" ]
}

@test "linux, --skip-service and --dry-run never run the summary's Keychain check" {
    run "${BOOTSTRAP}" --queue-url "${QUEUE}"
    [ "$status" -eq 0 ]
    ! grep -q -- "--only worker-keychain" "${CALLS}" || false

    darwin
    : >"${CALLS}"
    run mac --skip-service
    [ "$status" -eq 0 ]
    ! grep -q -- "--only worker-keychain" "${CALLS}" || false

    : >"${CALLS}"
    run mac --dry-run
    [ "$status" -eq 0 ]
    ! grep -q -- "--only worker-keychain" "${CALLS}" || false
    [[ "$output" == *"would run: sdlc doctor --exit-code --only worker-keychain"* ]]
}

@test "the run that repairs a drifted worker exits 0, though doctor warned about that drift" {
    darwin
    run mac
    # A worker from before 35.4-006: doctor's drift and dashboard checks WARN on it — unless skipped.
    sed -i.bak '/<string>--dashboard-url<\/string>/,/<string>http:/d' "$(AGENT worker)"
    stub sdlc '
if [ "$1 $2" = "queue list" ]; then echo "{\"jobs\": []}"; exit 0; fi
case " $* " in *" --skip fleet-agent-drift --skip worker-dashboard "*|*" --only worker-keychain "*) exit 0 ;; esac
exit 1'

    run mac

    [ "$status" -eq 0 ]
    [ "$(value_after "$(AGENT worker)" --dashboard-url)" = "http://100.64.0.9:8787" ]
}

# The token an operator put in the installed worker by hand, as the M3's was (#833).
TOKEN="dummy-token-0123"
hand_add_token() {  # AGENT-FILE
    awk -v t="${TOKEN}" '/<key>EnvironmentVariables<\/key>/ { e = 1 }
        e && /<\/dict>/ { print "\t\t<key>SDLC_QUEUE_TOKEN</key>"; print "\t\t<string>" t "</string>"; e = 0 }
        { print }' "$1" >"$1.new" && mv "$1.new" "$1"
    printf '<!-- stale -->\n' >>"$1"
}

@test "a re-render keeps the installed worker's queue token, installs it 0600, and asks the queue with it" {
    darwin
    run mac
    hand_add_token "$(AGENT worker)"
    : >"${CALLS}"

    run mac

    [ "$status" -eq 0 ]
    ! grep -q "stale" "$(AGENT worker)" || false
    [ "$(grep -A1 '<key>SDLC_QUEUE_TOKEN</key>' "$(AGENT worker)" | tail -n1)" = "    <string>${TOKEN}</string>" ]
    [ "$(ls -l "$(AGENT worker)" | cut -c1-10)" = "-rw-------" ]
    [ "$(ls -l "$(AGENT dashboard)" | cut -c1-10)" = "-rw-r--r--" ]
    grep -qx "queue-token ${TOKEN}" "${CALLS}"
    # The token never reaches a command line, where `ps` would show it.
    ! grep -Ev "^(queue|doctor)-token " "${CALLS}" | grep -q "${TOKEN}" || false

    # And a second run, token and all, changes nothing.
    before="$(cksum "$(AGENT worker)")"; : >"${CALLS}"
    run mac
    [ "$status" -eq 0 ]
    [ "$(cksum "$(AGENT worker)")" = "${before}" ]
    ! grep -q "launchctl bootout" "${CALLS}" || false
}

@test "a dry-run diff masks the queue token" {
    darwin
    run mac
    hand_add_token "$(AGENT worker)"

    run mac --dry-run

    [ "$status" -eq 0 ]
    [[ "$output" == *"would replace"*"-<!-- stale -->"* ]]
    [[ "$output" == *"<string>(redacted)</string>"* ]]
    [[ "$output" != *"${TOKEN}"* ]]
}

# launchd may still hold a label just booted out: the first bootstrap after a bootout fails.
slow_launchd() {
    stub launchctl '
case "$1" in
  print) [ -e "$TMP/loaded.${2##*/}" ] ;;
  bootstrap)
    if [ -e "$TMP/booting-out" ] && [ "${STUB_BOOTSTRAP_FAILS:-1}" != 0 ]; then
      [ "${STUB_BOOTSTRAP_FAILS:-1}" = always ] || rm -f "$TMP/booting-out"
      # A real launchctl refusal is two lines, as on the M3 Max.
      printf "Bootstrap failed: 5: Input/output error\nTry re-running the command as root for richer errors.\n" >&2; exit 5
    fi
    touch "$TMP/loaded.$(basename "$3" .plist)" ;;
  bootout) rm -f "$TMP/loaded.${2##*/}"; touch "$TMP/booting-out" ;;
esac'
    export BOOTSTRAP_RETRY_DELAY=0
}

@test "a bootstrap refused right after a bootout is retried, and the agent ends loaded" {
    darwin
    run mac
    slow_launchd
    printf '<!-- stale -->\n' >>"$(AGENT dashboard)"
    : >"${CALLS}"

    run mac

    [ "$status" -eq 0 ]
    [ "$(grep -c "^launchctl bootstrap gui/$(id -u) $(AGENT dashboard)$" "${CALLS}")" -eq 2 ]
    [ -e "${TMP}/loaded.com.fxmartin.sdlc-dashboard" ]
}

# Story 35.2-011: launchctl's stderr from a try the retry loop recovers from is not a failure.
@test "a bootstrap refused once is captured and logged as a retry, not printed as a failure" {
    darwin
    run mac
    slow_launchd
    printf '<!-- stale -->\n' >>"$(AGENT dashboard)"

    run mac

    [ "$status" -eq 0 ]
    [[ "$output" != *"Bootstrap failed"* ]]
    [[ "$output" == *"retrying"*"sdlc-dashboard"* || "$output" == *"sdlc-dashboard"*"retrying"* ]]
    # One log line per retry: the refusal's first line, not launchctl's run-as-root hint.
    [[ "$output" == *"(5: Input/output error); retrying (1/5)"* ]]
    [[ "$output" != *"richer errors"* ]]
    [[ "$output" == *"done."* ]]
}

@test "a bootstrap that succeeds but leaves the agent unloaded fails the run" {
    darwin
    run mac
    stub launchctl '
case "$1" in
  print) exit 1 ;;
  bootstrap) exit 0 ;;
  bootout) rm -f "$TMP/loaded.${2##*/}" ;;
esac'
    printf '<!-- stale -->\n' >>"$(AGENT dashboard)"

    run mac

    [ "$status" -ne 0 ]
    [[ "$output" == *"sdlc-dashboard"*"not loaded"* ]]
}

@test "a bootstrap that keeps failing stops the run, naming the agent left unloaded" {
    darwin
    run mac
    slow_launchd
    export STUB_BOOTSTRAP_FAILS=always
    printf '<!-- stale -->\n' >>"$(AGENT dashboard)"
    : >"${CALLS}"

    run mac

    [ "$status" -ne 0 ]
    [[ "$output" == *"launchctl bootstrap com.fxmartin.sdlc-dashboard kept failing"* ]]
    # The last refusal is reported once the tries run out, not hidden.
    [[ "$output" == *"5: Input/output error"* ]]
    [ "$(grep -c "^launchctl bootstrap gui/$(id -u) $(AGENT dashboard)$" "${CALLS}")" -eq 5 ]
}

# Story 35.2-011: an SSH shell has no SDLC_QUEUE_TOKEN; the installed worker agent does.
@test "the doctor step asks with the installed worker agent's queue token" {
    darwin
    run mac
    hand_add_token "$(AGENT worker)"
    : >"${CALLS}"

    run env -u SDLC_QUEUE_TOKEN "${BOOTSTRAP}" --worker m3max --pool claude-m3 --pool codex-shared --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    grep -qx "doctor-token ${TOKEN}" "${CALLS}"
}

@test "the doctor step keeps a token the shell already has" {
    darwin
    run mac
    hand_add_token "$(AGENT worker)"
    : >"${CALLS}"

    SDLC_QUEUE_TOKEN=shell-token run mac

    grep -qx "doctor-token shell-token" "${CALLS}"
}

@test "the doctor step sends no token when no worker agent carries one" {
    darwin
    : >"${CALLS}"

    run env -u SDLC_QUEUE_TOKEN "${BOOTSTRAP}" --worker m3max --pool claude-m3 --queue-url "${QUEUE}"

    [ "$status" -eq 0 ]
    grep -qx "doctor-token " "${CALLS}"
}

# The file on disk is the convergence proof: a re-run and doctor both read it. Written
# before a bootout that fails, it would say "current" while launchd runs the old agent.
@test "a bootout that fails leaves the old plist installed, so the next run still replaces it" {
    darwin
    run mac
    # What the M3 Max was left with: no --dashboard-url pair.
    grep -v -e '--dashboard-url' -e 'http://100.64.0.9:8787' "$(AGENT worker)" >"${TMP}/old.plist"
    cp "${TMP}/old.plist" "$(AGENT worker)"
    stub launchctl '
case "$1" in
  print) [ -e "$TMP/loaded.${2##*/}" ] ;;
  bootstrap) touch "$TMP/loaded.$(basename "$3" .plist)" ;;
  bootout)
    if [ -e "$TMP/bootout-fails" ]; then echo "Boot-out failed: 5: Input/output error" >&2; exit 5; fi
    rm -f "$TMP/loaded.${2##*/}" ;;
esac'
    touch "${TMP}/bootout-fails"

    run mac

    [ "$status" -ne 0 ]
    cmp -s "${TMP}/old.plist" "$(AGENT worker)"
    [ -e "${TMP}/loaded.com.fxmartin.sdlc-worker" ]
    [[ "$output" == *"launchctl bootout com.fxmartin.sdlc-worker failed"* ]]

    rm "${TMP}/bootout-fails"
    : >"${CALLS}"
    run mac

    [ "$status" -eq 0 ]
    [ "$(value_after "$(AGENT worker)" --dashboard-url)" = "http://100.64.0.9:8787" ]
    grep -qx "launchctl bootout gui/$(id -u)/com.fxmartin.sdlc-worker" "${CALLS}"
}

# launchd hands the worker its plist's EnvironmentVariables, never this shell's.
@test "the in-flight check asks the queue the installed worker drains, not the shell's" {
    darwin
    run mac
    awk '/<key>EnvironmentVariables<\/key>/ { e = 1 }
        e && /<\/dict>/ { print "    <key>SDLC_QUEUE_URL</key>"; print "    <string>http://worker-queue.example:8790</string>"; e = 0 }
        { print }' "$(AGENT worker)" >"${TMP}/w.plist"
    mv "${TMP}/w.plist" "$(AGENT worker)"
    printf '<!-- stale -->\n' >>"$(AGENT worker)"
    printf '{"jobs": [{"id": 7, "state": "running", "worker": "m3max"}]}' >"${TMP}/queue.json"
    stub sdlc '
if [ "$1 $2" = "queue list" ]; then
  echo "queue-url ${SDLC_QUEUE_URL:-} fleet-disabled ${SDLC_FLEET_DISABLED:-}" >>"${CALLS}"
  cat "$TMP/queue.json"; exit 0
fi
exit 0'
    export SDLC_QUEUE_URL=http://shell-queue.example:8790 SDLC_FLEET_DISABLED=1
    : >"${CALLS}"

    run mac

    [ "$status" -eq 1 ]
    grep -qx "queue-url http://worker-queue.example:8790 fleet-disabled " "${CALLS}"
    [[ "$output" == *"pending restart (job 7 running)"* ]]

    # Without one in its plist, the worker reads ~/.sdlc-fleet.yaml: so does the check.
    sed '/<key>SDLC_QUEUE_URL<\/key>/,+1d' "$(AGENT worker)" >"${TMP}/w.plist"
    mv "${TMP}/w.plist" "$(AGENT worker)"
    : >"${CALLS}"

    run mac

    [ "$status" -eq 1 ]
    grep -qx "queue-url  fleet-disabled " "${CALLS}"
}

@test "--help says --skip-service skips a Mac's LaunchAgents too, and that the token is kept" {
    run "${BOOTSTRAP}" --help

    [ "$status" -eq 0 ]
    [[ "$output" == *"the LaunchAgents on a Mac"* ]]
    [[ "$output" == *"SDLC_QUEUE_TOKEN"*"0600"* ]]
}
