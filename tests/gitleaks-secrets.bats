#!/usr/bin/env bats
# ABOUTME: Verifies the gitleaks secrets gate (Story 9.2-001).
# ABOUTME: Proves the scanner catches a planted credential and honours the allowlist.
#
# Strategy:
#   1. Copy the deliberate test fixture into an isolated temp dir and scan it
#      with the STOCK default ruleset (no repo config). gitleaks must flag the
#      planted token and exit non-zero — this is the core "catches a real leak"
#      contract from the story's Acceptance Criteria.
#   2. Scan the same fixture at its real repo path with the repo `.gitleaks.toml`
#      and assert it is allowlisted (clean) — proving the planted secret never
#      blocks a commit or the CI gate while detection still works in (1).
#   3. Assert the secret value is redacted out of gitleaks output (`--redact`).

REPO_ROOT="${BATS_TEST_DIRNAME}/.."
FIXTURE="${BATS_TEST_DIRNAME}/fixtures/leaked-key.txt"
CONFIG="${REPO_ROOT}/.gitleaks.toml"

setup() {
    TMP_SCAN="$(mktemp -d)"
    cp "${FIXTURE}" "${TMP_SCAN}/leaked-key.txt"
}

teardown() {
    [ -n "${TMP_SCAN:-}" ] && rm -rf "${TMP_SCAN}"
}

# Skips the scan tests when gitleaks is unavailable (e.g. the CI bats runner,
# which does not install it). The dedicated `secrets-scan` CI job installs and
# exercises gitleaks end-to-end, so detection is still gated in CI.
require_gitleaks() {
    command -v gitleaks > /dev/null 2>&1 || skip "gitleaks not installed"
}

@test "gitleaks catches the planted credential with default rules" {
    require_gitleaks
    run gitleaks detect --no-banner --redact --no-git --source "${TMP_SCAN}"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"leaks found"* ]]
}

@test "gitleaks redacts the secret value out of its output" {
    require_gitleaks
    run gitleaks detect --no-banner --redact --no-git --source "${TMP_SCAN}"
    [ "${status}" -ne 0 ]
    # The fabricated token must never appear verbatim in scan output.
    [[ "${output}" != *"ghp_012345678901234567890123456789abcdef"* ]]
}

@test "the repo .gitleaks.toml allowlists the fixture path" {
    require_gitleaks
    [ -f "${CONFIG}" ]
    run gitleaks detect --no-banner --redact --no-git \
        --config "${CONFIG}" --source "${REPO_ROOT}/tests/fixtures"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"no leaks found"* ]]
}

@test ".gitleaks.toml extends the default ruleset and declares an allowlist" {
    [ -f "${CONFIG}" ]
    grep -q "useDefault" "${CONFIG}"
    grep -q "allowlist" "${CONFIG}"
    grep -q "leaked-key" "${CONFIG}"
}
