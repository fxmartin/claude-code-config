#!/usr/bin/env bats
# Tests for scripts/release-guard.sh (Story 5.3-001).
#
# The guard decides whether the release workflow proceeds. The critical
# property: the decision keys ONLY off the commit SUBJECT (first line). A
# commit whose BODY merely mentions the skip-release token — e.g. a PR body
# documenting the escape hatch — must still release. This regression already
# bit the 5.2-001 merge once, hence the explicit body-vs-subject coverage.

GUARD="${BATS_TEST_DIRNAME}/../scripts/release-guard.sh"

# The literal skip token, assembled at runtime so this very test file can
# never itself trip a substring guard.
skip_token() { printf '[%s]' 'skip release'; }

@test "plain feat subject proceeds" {
    run "${GUARD}" <<<'feat: add a thing'
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"proceed=true"* ]]
}

@test "chore(release) subject is skipped (recursion guard)" {
    run "${GUARD}" <<<'chore(release): v1.5.0'
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"proceed=false"* ]]
}

@test "skip token in the subject is skipped" {
    run "${GUARD}" <<<"docs: emergency fix $(skip_token)"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"proceed=false"* ]]
}

@test "skip token only in the BODY still releases" {
    # A feat commit whose body documents the escape hatch — must NOT skip.
    msg="$(printf 'feat: document the release escape hatch\n\nThe %s token in a commit subject skips the release workflow.' "$(skip_token)")"
    run "${GUARD}" <<<"${msg}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"proceed=true"* ]]
}

@test "chore(release) only in the BODY still releases" {
    # A feature commit whose body quotes the bump-commit prefix — must NOT skip.
    msg="$(printf 'feat: add release pipeline\n\nThe workflow pushes a chore(release): commit after tagging.')"
    run "${GUARD}" <<<"${msg}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"proceed=true"* ]]
}

@test "multi-line feat body with both tokens in the body still releases" {
    msg="$(printf 'feat(release): changelog bootstrap\n\nDocuments chore(release): commits and the %s token.' "$(skip_token)")"
    run "${GUARD}" <<<"${msg}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"proceed=true"* ]]
}

@test "plain fix subject proceeds" {
    # fix: is the other common release-triggering type — must not be guarded
    run "${GUARD}" <<<'fix: correct off-by-one in version parser'
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"proceed=true"* ]]
}

@test "empty commit message defaults to proceed=true" {
    # An empty message has no subject to match — safe default is to release
    run "${GUARD}" <<<''
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"proceed=true"* ]]
}

@test "CRLF line endings do not defeat subject-only guards" {
    # GitHub event payloads may use CRLF; the guard must still correctly skip
    # a chore(release): subject even when lines end with \r\n.
    run bash -c "printf 'chore(release): v9.9.9\r\nbody line' | ${GUARD}"
    [ "${status}" -eq 0 ]
    [[ "${output}" == *"proceed=false"* ]]
}
