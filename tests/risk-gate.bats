#!/usr/bin/env bats
# Tests for scripts/risk-gate-detect.sh (Story 8.2-001).
#
# Strategy: pipe a fixture of changed file paths into the detector and assert
# which paths are flagged high-risk, which pattern they hit, and the exit code
# convention (0 = at least one match, 1 = clean). A second fixture covers the
# additive per-repo override via .sdlc-risk-config.yaml.

DETECTOR="${BATS_TEST_DIRNAME}/../scripts/risk-gate-detect.sh"
FIXTURES="${BATS_TEST_DIRNAME}/fixtures/risk-gate"

@test "flags a migrations file and names the matched pattern" {
    run bash -c "'${DETECTOR}' < '${FIXTURES}/changed-files.txt'"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"db/migrations/0001_init.sql"*"**/migrations/**"* ]]
}

@test "flags auth, billing, terraform, dockerfile, shell, iam, policies" {
    run bash -c "'${DETECTOR}' < '${FIXTURES}/changed-files.txt'"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"src/auth/login.py"* ]]
    [[ "${output}" == *"services/billing/invoice.py"* ]]
    [[ "${output}" == *"infra/network.tf"* ]]
    [[ "${output}" == *"infra/prod.tfvars"* ]]
    [[ "${output}" == *"Dockerfile"* ]]
    [[ "${output}" == *"config/secrets/keys.json"* ]]
    [[ "${output}" == *"scripts/deploy.sh"* ]]
    [[ "${output}" == *"platform/iam/role.json"* ]]
    [[ "${output}" == *"platform/policies/access.json"* ]]
    [[ "${output}" == *".github/workflows/ci.yml"* ]]
}

@test "does not flag low-risk documentation or application source" {
    run bash -c "'${DETECTOR}' < '${FIXTURES}/changed-files.txt'"
    [ "${status}" -eq 0 ]
    [[ "${output}" != *"README.md"* ]]
    [[ "${output}" != *"docs/guide.md"* ]]
    [[ "${output}" != *"src/app/main.py"* ]]
}

@test "exits 1 with no output when the change set is clean" {
    run bash -c "'${DETECTOR}' < '${FIXTURES}/clean-files.txt'"
    [ "${status}" -eq 1 ]
    [ -z "${output}" ]
}

@test "a per-repo override adds patterns additively" {
    tmp_root="$(mktemp -d)"
    mkdir -p "${tmp_root}/controller/src/sdlc/config"
    cp "${BATS_TEST_DIRNAME}/../controller/src/sdlc/config/high-risk-patterns.yaml" \
        "${tmp_root}/controller/src/sdlc/config/high-risk-patterns.yaml"
    printf 'high_risk_patterns:\n  - "**/special/**"\n' \
        > "${tmp_root}/.sdlc-risk-config.yaml"

    # A file matching only the override is flagged...
    run bash -c "printf 'app/special/thing.py\n' | '${DETECTOR}' '${tmp_root}'"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"app/special/thing.py"*"**/special/**"* ]]

    # ...and the baseline patterns still apply.
    run bash -c "printf 'db/migrations/0001_init.sql\n' | '${DETECTOR}' '${tmp_root}'"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"**/migrations/**"* ]]

    rm -rf "${tmp_root}"
}

@test "exits 2 when the config file is missing" {
    tmp_root="$(mktemp -d)"
    run bash -c "printf 'x\n' | '${DETECTOR}' '${tmp_root}'"
    [ "${status}" -eq 2 ]
    [[ "${output}" == *"error:"* ]]
    rm -rf "${tmp_root}"
}
