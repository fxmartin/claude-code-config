#!/usr/bin/env bats
# ABOUTME: Tests for scripts/validate-gitlab-ci.sh and the shipped GitLab CI gate template.
# ABOUTME: Story 23.3-001 — the .gitlab-ci.yml quality-gate template for adopted GitLab repos.
#
# Strategy: drive the validator against the real template (templates/gitlab-ci.yml,
# the "good" case) and against fixtures under tests/fixtures/gitlab-ci/ that each
# break one acceptance criterion (missing gate job, malformed YAML, a Premium-only
# keyword). Mirrors the tests/validate-agent-registry.bats pattern.

VALIDATOR="${BATS_TEST_DIRNAME}/../scripts/validate-gitlab-ci.sh"
TEMPLATE="${BATS_TEST_DIRNAME}/../templates/gitlab-ci.yml"
FIXTURES="${BATS_TEST_DIRNAME}/fixtures/gitlab-ci"

@test "validator is executable" {
    [ -x "${VALIDATOR}" ]
}

@test "shipped template exists at templates/gitlab-ci.yml" {
    [ -f "${TEMPLATE}" ]
}

@test "validator passes on the shipped template (default path)" {
    run "${VALIDATOR}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"OK:"* ]]
}

@test "validator passes when the template path is given explicitly" {
    run "${VALIDATOR}" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
}

@test "shipped template is valid YAML" {
    run "${VALIDATOR}" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
    [[ "${output}" != *"not valid YAML"* ]]
}

@test "template declares every required quality gate job" {
    # AC: lint (shellcheck/ruff), tests (pytest/bats), schema/contract checks,
    # secret scan, and commit-format — all present as GitLab CI jobs.
    for job in secrets-scan shellcheck ruff json-schema commit-format pytest bats; do
        run grep -E "^${job}:" "${TEMPLATE}"
        [ "${status}" -eq 0 ]
    done
}

@test "template declares pipeline stages" {
    run grep -E "^stages:" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
}

@test "commit-format job is scoped to merge-request pipelines" {
    # commitlint must only lint MR commits, not the protected default branch
    # history (mirrors the GitHub `if: pull_request` guard).
    run grep -n "CI_MERGE_REQUEST_DIFF_BASE_SHA" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
}

@test "template uses no Premium/Ultimate-only constructs" {
    # Free/Core only — no merge trains.
    run grep -iE "merge_train" "${TEMPLATE}"
    [ "${status}" -ne 0 ]
}

@test "validator fails and names the missing job when a gate is absent" {
    run "${VALIDATOR}" "${FIXTURES}/missing-gate.gitlab-ci.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"ruff"* ]]
}

@test "validator fails on malformed YAML" {
    run "${VALIDATOR}" "${FIXTURES}/malformed.gitlab-ci.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"not valid YAML"* ]]
}

@test "validator fails on a Premium-only keyword" {
    run "${VALIDATOR}" "${FIXTURES}/premium.gitlab-ci.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"merge_train"* ]]
}

@test "validator errors clearly when the target file does not exist" {
    run "${VALIDATOR}" "${FIXTURES}/does-not-exist.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"not found"* ]]
}
