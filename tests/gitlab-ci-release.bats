#!/usr/bin/env bats
# ABOUTME: Tests for scripts/validate-gitlab-release.sh and the GitLab CI release template.
# ABOUTME: Story 23.4-001 — the .gitlab-ci-release.yml semver-tag + GitLab Release flow.
#
# Strategy mirrors tests/gitlab-ci-template.bats: drive the validator against the
# real template (templates/gitlab-ci-release.yml, the "good" case) and against
# fixtures under tests/fixtures/gitlab-release/ that each break one acceptance
# criterion (no release job, no compute-release reuse, runs on MR instead of the
# default branch, missing loop guard, a Premium-only keyword, malformed YAML).

VALIDATOR="${BATS_TEST_DIRNAME}/../scripts/validate-gitlab-release.sh"
TEMPLATE="${BATS_TEST_DIRNAME}/../templates/gitlab-ci-release.yml"
FIXTURES="${BATS_TEST_DIRNAME}/fixtures/gitlab-release"

@test "validator is executable" {
    [ -x "${VALIDATOR}" ]
}

@test "shipped release template exists at templates/gitlab-ci-release.yml" {
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

@test "shipped release template is valid YAML" {
    run "${VALIDATOR}" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
    [[ "${output}" != *"not valid YAML"* ]]
}

@test "template declares a release stage" {
    run grep -E "^stages:" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
    run grep -E "release" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
}

@test "template declares the publish-release job" {
    run grep -E "^publish-release:" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
}

@test "template reuses Epic-05 compute-release.sh (port, don't fork)" {
    # AC + Technical Notes: the semver bump must come from the single source of
    # truth, scripts/compute-release.sh, not a re-implemented bumper.
    run grep -n "compute-release.sh" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
}

@test "template publishes a GitLab Release via release-cli (Free/Core)" {
    # AC: Free/Core uses GitLab Releases + release-cli.
    run grep -n "release-cli" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
}

@test "release job is scoped to the default branch, not merge requests" {
    # AC: the release job runs when commits land on the default branch — the
    # GitLab equivalent of GitHub's `on: push: branches: [main]`.
    run grep -n "CI_DEFAULT_BRANCH" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
    # It must not trigger on merge-request pipelines.
    run grep -n "merge_request_event" "${TEMPLATE}"
    [ "${status}" -ne 0 ]
}

@test "release job guards against its own chore(release) bump commit (no loop)" {
    # AC/Technical Notes: same semantics as Epic-05 — the release commit must not
    # retrigger a release. Mirrors release-guard.sh's subject-only chore(release) skip.
    run grep -nE "chore\\\\?\(release\\\\?\)" "${TEMPLATE}"
    [ "${status}" -eq 0 ]
}

@test "template uses no Premium/Ultimate-only constructs" {
    run grep -iE "merge_train" "${TEMPLATE}"
    [ "${status}" -ne 0 ]
}

@test "validator fails when the release job is absent" {
    run "${VALIDATOR}" "${FIXTURES}/missing-release-job.gitlab-ci.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"publish-release"* ]]
}

@test "validator fails when the template does not reuse compute-release.sh" {
    run "${VALIDATOR}" "${FIXTURES}/no-compute-release.gitlab-ci.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"compute-release.sh"* ]]
}

@test "validator fails when the release runs on merge requests" {
    run "${VALIDATOR}" "${FIXTURES}/runs-on-mr.gitlab-ci.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"default branch"* ]]
}

@test "validator fails when the loop guard is missing" {
    run "${VALIDATOR}" "${FIXTURES}/no-guard.gitlab-ci.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"chore(release)"* ]]
}

@test "validator fails on malformed YAML" {
    run "${VALIDATOR}" "${FIXTURES}/malformed.gitlab-ci.yml"
    [ "${status}" -ne 0 ]
    [[ "${output}" == *"not valid YAML"* ]]
}

@test "validator fails when the template is not a YAML mapping" {
    run "${VALIDATOR}" "${FIXTURES}/not-mapping.gitlab-ci.yml"
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
